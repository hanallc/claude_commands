"""Check the shared review records, signed actions and table behavior."""
from __future__ import annotations
from scripts.console_text.test_support import accepting_console_text
from scripts.lint.memory_test_support import write_config_fixture

from scripts.production.change_review_test_support import pictured_changes

import shutil
import io
from contextlib import redirect_stderr, redirect_stdout
import html
import json
import re
import os
import selectors
import socket
import subprocess
import tempfile
import time
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, tzinfo
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Self, TextIO, cast
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import atomic_file, conf, conf_reads, urgency
from scripts.lib import user_zone as user_zone_config
from scripts.lib.process_group import run_process_group, stop_process_group
from scripts.lib.user_zone import user_zone
import unittest
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from scripts.agents.remote import local_machine
from scripts.for_you.action import ActionStore
from scripts.nightly_review import candidates
from scripts.production.production_doc import read_production
from scripts.production.issues import Issue
from scripts.production import issues as issue_store
from scripts.production import list_exchange, the_list
from scripts.production.staff_test_support import fixture_member
from scripts.production import change_review, change_routing, footer_counts, investigation, next_item, proposal_issue, record_store, staff, update_registration
from scripts.review_links import change_browser, change_page, console, for_you, page_frame, record_table, review_items, review_page
from scripts.review_links import question_path, question_record, review_link
from scripts.review_links import list_page, staff_names, test_change_page
from scripts.review_links import efficiency_reviews, new_item_dialog, reply_tokens, review_actions, review_events, review_later, review_submissions
from scripts.review_links.browser_test_support import UNDO_ACTION_SCRIPT, assert_status_states, firefox_probe_results
from scripts.review_links.test_record_table import save_notice_failures
from scripts.statusline import counts_row


def unpack_index(payload: str) -> list[review_page.ReviewIndexEntry]:
    """Decode the compact wire fields for exact index assertions."""
    packed = cast(dict[str,list[list[str | int | float | list[str] | None]]],json.loads(payload))
    names = cast(list[list[str]],packed['n'])
    entries: list[review_page.ReviewIndexEntry] = []
    for row in packed['r']:
        fields = cast(list[str | int | float], row[:11])
        key, item_id, kind, owner_ref, source_ref, title, state, filed, updated, hidden_until, urgency = fields
        owner, source = names[int(owner_ref)], names[int(source_ref)]
        entry = review_page.ReviewIndexEntry(key=str(key), id=str(item_id or key),
            type=review_page.WIRE_TYPES[int(kind)], owner=owner[0], sourceOwner=source[0],
            ownerName=owner[1], sourceName=source[1], title=str(title), state=review_page.INDEX_STATES[int(state)],
            filed=int(filed), updated=str(updated), hiddenUntil=float(hidden_until), urgency=str(urgency or 'normal'),
            ownerHtml=owner[2], sourceHtml=source[2])
        if len(row) > 11 and isinstance(handoff := row[11], list):
            entry['handoff'] = (handoff[0], handoff[1])
        if len(row) > 12 and isinstance(why := row[12], str):
            entry['waitingWhy'] = why
        if len(row) > 13 and row[13] is True:
            entry['type'] = review_items.ReviewType.RESPONSE
        if len(row) > 14 and isinstance(action_status := row[14], str):
            entry['actionStatus'] = action_status
        if len(row) > 15 and row[15] is True:
            entry['unreadAnswer'] = True
        entries.append(entry)
    return entries



BODY_READY_SCRIPT = r"""async function readyReviewCard(row){
 if(!row) throw new Error('The summary row is missing.');
 const end=performance.now()+8000;
 while(!recordDetails(row).querySelector('[data-review-form]')){
  if(performance.now()>end) throw new Error('The card body did not load: '+row.dataset.key);
  await new Promise(resolve=>requestAnimationFrame(resolve));
 }
 return recordDetails(row).querySelector('[data-review-form]');
}
"""

WAIT_FOR_BROWSER_STATE = r"""const until=(predicate,label,extraRoot,timeoutMs=HANG_GUARD_MS)=>new Promise((resolve,reject)=>{
 const cleanup=()=>{
  clearTimeout(timer);
  observer.disconnect();
  document.removeEventListener('fixture-progress',ready);
 };
 const ready=()=>{
  try{
   if(!predicate()) return;
  }catch(error){
   cleanup();reject(error);return;
  }
  cleanup();resolve();
 };
 const observer=new MutationObserver(ready);
 const options={childList:true,subtree:true,attributes:true,characterData:true};
 observer.observe(document,options);
 if(extraRoot) observer.observe(extraRoot,options);
 document.addEventListener('fixture-progress',ready);
 const timer=setTimeout(()=>{
  cleanup();reject(new Error('Timed out waiting for '+label+'.'));
 },timeoutMs);
 ready();
});""".replace('HANG_GUARD_MS', str(TEST_HANG_GUARD_SECS * 1000))


SNAPSHOT_FETCH_SCRIPT = r"""<script>
const snapshots=SNAPSHOTS;
let shown='initial';
const realFetch=fetch;
window.fetch=async(url,options={})=>{
 if(options.method==='POST') return new Promise(()=>{});
 const params=new URL(url,location.href).searchParams,current=snapshots[shown];
 if(params.has('body'))
  return new Response(current.bodies[params.get('body')],{headers:{'X-Review-Version':current.version}});
 if(params.has('index')) return new Response(current.index);
 if(params.get('version')==='1') return new Response(JSON.stringify({version:current.version,facts:current.facts}));
 return realFetch(url,options);
};
</script>"""

SNAPSHOT_PROBE = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const until=async (predicate,label)=>{
  const end=performance.now()+8000;
  while(!predicate()){if(performance.now()>end) throw new Error('Timed out waiting for '+label+'.');await frame();}
 };
 const row=number=>document.querySelector('[data-list-item][data-key="change-'+number+'"]');
 const summary=number=>row(number)?.querySelector('.list-row');
 const refresh=()=>{
  shown='fresh';
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 };
 try{
STEPS
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""


def snapshot_probe_page(page: str, snapshots: dict[str, dict[str, object]], steps: str) -> bytes:
    """Serve the snapshots to the page, then run the probe steps against change-N rows."""
    setup = SNAPSHOT_FETCH_SCRIPT.replace('SNAPSHOTS', json.dumps(snapshots).replace('</', '<\\/'))
    return (page.replace('<head>', '<head>'+setup, 1)
            .replace('<script>', '<script>'+BODY_READY_SCRIPT+'</script><script>', 1)
            .replace('</body>', SNAPSHOT_PROBE.replace('STEPS', steps)+'</body>')).encode()


def browser_markup(root: Path, *, show: str = 'open') -> str:
    """Adapt the existing fake refresh responses to the snapshot protocol."""
    snapshot = review_page.build_snapshot(root)
    bodies = {key:snapshot.body(key).decode() for key in snapshot.records}
    bridge = r"""
const fixtureFetch=fetch,fixtureBodies=FIXTURE_BODIES;
let fixtureVersion=0,fixtureIndexResponse;
window.fetch=async(url,options={})=>{
 if(options.method==='POST') return fixtureFetch(url,options);
 const params=new URL(url,location.href).searchParams;
 if(params.has('body')){
  const key=params.get('body'),text=fixtureBodies[key];
  const headers=new Headers({'X-Review-Version':fixtureIndexResponse?.version || document.querySelector('[data-review]').dataset.version});
  return {ok:true,headers,text:async()=>fixtureVersion && window.transformFixtureBody ? window.transformFixtureBody(text,key) : text};
 }
 if(params.has('index') && fixtureIndexResponse) return {ok:true,json:async()=>fixtureIndexResponse};
 const response=await fixtureFetch(url,options);
 if(!response.ok) return response;
 const text=await response.text(),page=new DOMParser().parseFromString(text,'text/html');
 const data=page.querySelector('[data-review-index]');
 const facts=pageRefreshFacts(page),version='fixture-'+(++fixtureVersion);
 fixtureIndexResponse={version,index:JSON.parse(data.textContent),facts};
 return {ok:true,json:async()=>fixtureIndexResponse,text:async()=>text};
};
""".replace('FIXTURE_BODIES',json.dumps(bodies).replace('</','<\\/'))
    return snapshot.render(show=show).decode().replace('<script>','<script>'+BODY_READY_SCRIPT+bridge+'</script><script>',1)


class ReviewPageTests(unittest.TestCase):
    fixture: test_change_page.ChangePageTests = test_change_page.ChangePageTests("runTest")
    root: Path = Path()
    status: change_page.DeliveryStatus = change_page.DeliveryStatus(Path())

    def test_review_ids_use_the_shared_no_wrap_cell(self) -> None:
        _ = self.question()
        body = review_page.render(self.root).decode()
        self.assertEqual(body.count(page_frame.BUTTON_STATES), 1)
        self.assertIn('class="record-id"', body)
        self.assertIn('.list-table .record-id{white-space:nowrap;overflow-wrap:normal;width:1%}', body)
        self.assertIn('.list-toggle{white-space:nowrap}', body)
        self.assertIn('<col style="width:17%">', body)

    def test_staff_owners_sources_and_log_names_have_role_colors(self) -> None:
        _ = self.question()
        _ = self.investigation(False)
        snapshot = review_page.build_snapshot(self.root)
        owner_menu = snapshot.render().decode().partition('id="list-owner"')[2].partition('</div>')[0]
        entry = next(entry for entry in snapshot.entries if entry['type'] == review_items.ReviewType.INVESTIGATION)
        for name, markup in ((entry['ownerName'], entry['ownerHtml']), (entry['sourceName'], entry['sourceHtml'])):
            self.assertIn('class="staff-name"', markup)
            escaped_name = re.escape(html.escape(name, quote=True))
            self.assertRegex(owner_menu, rf'data-owner-name="{escaped_name}"[^>]* aria-pressed="false"'
                                         + rf'[^>]*>{re.escape(markup)} <b>')
        body = ''.join(snapshot.body(item.key).decode() for item in snapshot.records.values())
        self.assertRegex(body, r'data-color-text="role.showrunner"[^>]*>page-owner</span>')
        self.assertRegex(body, r'data-color-text="role.unit-director"[^>]*>builder-unit</span>')
        self.assertNotRegex(body, r'class="staff-name"[^>]*>nate</span>')
        item = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.QUESTION)
        unknown = replace(item, owner=staff.StaffId('nate'), source_unit=staff.StaffId('unknown-source'))
        plain = self.card_details(unknown)
        self.assertIn('Owner: nate · Source: unknown-source</p>', plain)
        entry, = review_page.filter_index([unknown], staff.snapshot())
        self.assertEqual((entry['ownerHtml'], entry['sourceHtml']), ('nate', 'unknown-source'))

    def deliver_review_message(self, delivery: review_page.ReviewActionDelivery,
                               status: change_page.DeliveryStatus) -> None:
        assert isinstance(delivery, (change_page.MessageDelivery, change_page.NoMessageDelivery))
        change_page.deliver(delivery, status)

    def card_details(self, item: review_items.ReviewItem) -> str:
        snapshot = review_page.build_snapshot(self.root)
        assets: dict[str, str] = {}
        review_page.retain_asset(item, assets)
        inputs = review_page.ReviewBodyInputs(snapshot.browser, snapshot.members, snapshot.words_rows,
            assets, snapshot.zone, snapshot.names)
        return review_page.details(item, inputs)

    @override
    def setUp(self) -> None:
        self.enterContext(accepting_console_text())
        self.fixture = test_change_page.ChangePageTests('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.set_up_review(changes=3)
        self.fixture.page = change_page.ReviewPage(
            self.fixture.review_list, self.fixture.production, ('C1', 'C2', 'C3'), 'selected-fixture')
        self.configure_review()

    def configure_review(self) -> None:
        config = self.fixture.temporary / 'config'
        _ = write_config_fixture(config / "production.conf", "question_title_max_chars=100\n")
        _ = write_config_fixture(config / "console.conf",
            "page.review.url=python:scripts.review_links.review_page:page_url\n"
            + "page.review.items=python:scripts.review_links.review_page:waiting_items\n"
            + "page.review.icon=inbox\npage.review.title=Review\n"
            + "status.round_trip_limit_seconds=1\nstatus.render_limit_seconds=1\n"
            + "status.reading_delay_seconds=1\nstatus.review.age_limit_seconds=45\n")
        _ = write_config_fixture(config / "review_links.conf",
            "review_words_rows=4\nlater_soon_minutes=60\nlater_morning=09:00\n"
            + "later_week_day=Monday\nlater_week_time=09:00\npark_time_step_minutes=15\n")
        self.root = self.fixture.root
        _ = change_page.console_folder(self.root)
        self.status = change_page.DeliveryStatus(self.root)
        document = self.fixture.production.read_text()
        _ = self.fixture.production.write_text(document+'- **Merge branch:** main\n- **Log:** progress.log\n')

    def question(self, title: str = 'Which layout should the page use?') -> str:
        return self.fixture.file_user_question(title)

    def test_legacy_question_names_the_answer_relay_in_both_views(self) -> None:
        self.fixture.start_review_server()
        asker = fixture_member('producer', name='question-asker')
        key = self.fixture.file_user_question(asker=asker)
        snapshot = review_page.build_snapshot(self.root)
        review = snapshot.body(key).decode()
        browser = self.fixture.browser('questions')[2]
        for page in (review, browser):
            answer = re.search(r'<button [^>]*data-send-receiver="([^"]+)"[^>]*value="answer"', page)
            assert answer is not None
            self.assertEqual(html.unescape(answer[1]), asker.name)
        delivery, _ = review_page.submit(self.root, self.card_body(snapshot, key, 'answer', 'Use wide cards.'),
                                         self.status, snapshot=snapshot)
        assert isinstance(delivery, change_page.MessageDelivery)
        self.assertEqual(delivery.recipients, (asker.id,))

        renamed_key = self.fixture.file_user_question('Check feedback after an owner rename', asker=asker)
        initial = review_page.build_snapshot(self.root)
        original = self.card_body(initial, renamed_key, 'feedback', 'Keep this draft.')
        members = staff.snapshot()
        retained_question = initial.records[renamed_key]
        owner = members.by_id(retained_question.owner)
        assert owner is not None
        old_revision = review_page.record_revision(retained_question, initial.assets, members)
        renamed = staff.rename(owner.id, 'renamed-page-owner')
        new_revision = review_page.record_revision(retained_question, initial.assets, staff.snapshot())
        self.assertNotEqual(old_revision, new_revision)
        refreshed = review_page.build_snapshot(self.root)
        with self.assertRaisesRegex(review_items.ReviewRecordChanged, 'This item changed'):
            _ = review_page.submit(self.root, original, self.status, snapshot=refreshed)
        current = self.card_body(refreshed, renamed_key, 'feedback', 'Keep this draft.')
        delivery, _ = review_page.submit(self.root, current+'&'+urlencode({'receiver': renamed.name}),
                                         self.status, snapshot=refreshed)
        assert isinstance(delivery, change_page.MessageDelivery)
        self.assertEqual(delivery.recipients, (renamed.id,))
        self.assertIn('Keep this draft.', delivery.message)
        _ = staff.rename(owner.id, owner.name)

        moved_key = self.fixture.file_user_question('Check the relay after a move')
        queue_key = self.fixture.file_user_question('Check the Changes queue after a move')
        path, forms, _ = self.fixture.browser('questions')
        browser = change_browser.page_at(self.root, path+'?type=questions')
        old_queue = self.fixture.browser_fields(forms, queue_key, 'answer', 'Keep this queue draft.')
        initial = review_page.build_snapshot(self.root)
        original = self.card_body(initial, moved_key, 'answer', 'Keep these words.')
        source = initial.records[moved_key].source
        assert isinstance(source, review_items.QuestionSource)
        retained = source.record.path.read_bytes()
        destination = self.fixture.temporary/'destination.md'
        _ = destination.write_text(self.fixture.production.read_text())
        runner = fixture_member('showrunner', name='destination', doc=staff.LiveDoc(local_machine(), destination))
        _ = staff.move(self.fixture.unit.id, staff.ReportsTo(runner.id))
        refreshed = review_page.build_snapshot(self.root)
        self.assertEqual(initial.records[moved_key], refreshed.records[moved_key])
        with self.assertRaisesRegex(review_items.ReviewRecordChanged, 'This item changed'):
            _ = review_page.submit(self.root, original, self.status, snapshot=refreshed)
        self.assertEqual(source.record.path.read_bytes(), retained)
        markup = refreshed.body(moved_key).decode()
        answer = re.search(r'<button [^>]*data-send-receiver="([^"]+)"[^>]*value="answer"', markup)
        assert answer is not None
        shown = html.unescape(answer[1])
        self.assertEqual(shown, runner.name)
        current = self.card_body(refreshed, moved_key, 'answer', 'Keep these words.')
        identities = update_registration.QuestionStaff.read()
        scope = update_registration.question_scope(source.record.owner, identities.record)
        original_relay = change_browser.question_relay(source.record, scope, identities)
        next_relay = update_registration.AnswerRelay('The relay moved again.', asker.id)
        with patch.object(change_browser, 'question_relay', side_effect=(original_relay, next_relay)) as route:
            delivery, _ = review_page.submit(self.root, current+'&'+urlencode({'receiver': shown}),
                                             self.status, snapshot=refreshed)
        route.assert_called_once()
        assert isinstance(delivery, change_page.MessageDelivery)
        self.assertEqual(delivery.recipients, (runner.id,))
        self.assertIn('Keep these words.', delivery.message)

        without_revision = {key: value for key, value in old_queue.items() if key != 'question_revision'}
        for name, fields in (('stale revision', old_queue), ('missing revision', without_revision)):
            with self.subTest(form=name):
                refusal = self.fixture.http_request(path, fields)
                self.assertEqual(refusal.status, 409, refusal.body)
                self.assertIn('This item changed', refusal.body)
        _, forms, _ = self.fixture.browser('questions')
        queue = self.fixture.browser_fields(forms, queue_key, 'answer', 'Keep this queue draft.')
        with patch.object(update_registration, 'answer_relay', side_effect=(original_relay, next_relay)) as route:
            delivery, _ = change_browser.submit(browser, urlencode(queue), self.status)
        route.assert_called_once()
        assert isinstance(delivery, change_page.MessageDelivery)
        self.assertEqual(delivery.recipients, (runner.id,))
        self.assertIn('Keep this queue draft.', delivery.message)

    def ready_submission_recipient(self) -> staff.StaffRow:
        doc = self.fixture.production
        issues = doc.parent / 'native-proposals'
        _ = doc.write_text(doc.read_text() + f'> **Status: PRODUCTION — running.**\n- **Proposals:** {issues}\n')
        owner = staff.snapshot().by_doc(local_machine(), doc)
        assert owner is not None
        return owner

    def submission_request(self, kind: new_item_dialog.SubmissionKind, recipient: staff.StaffId
                           ) -> new_item_dialog.NewItemRequest:
        return new_item_dialog.NewItemRequest(f'r1-{uuid4().hex}-{kind}-{recipient}', kind, recipient,
                                              f'Check the {kind} record.', new_item_dialog.NoEvidence())

    def test_sent_rows_keep_native_types_and_reopen_only_questions_and_proposals(self) -> None:
        owner = self.ready_submission_recipient()
        _ = fixture_member('producer', name='morty')
        doc = self.fixture.temporary / 'producer.md'
        proposals = doc.parent / 'producer-proposals'
        _ = doc.write_text(f'# Producer\n- **Proposals:** {proposals}\n')
        producer = fixture_member('producer', name='producer', doc=staff.LiveDoc(local_machine(), doc))
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        initial = cache.get()
        cases = ((new_item_dialog.SubmissionKind.QUESTION, owner, review_items.ReviewType.QUESTION),
                 (new_item_dialog.SubmissionKind.PROPOSAL, producer, review_items.ReviewType.PROPOSAL),
                 (new_item_dialog.SubmissionKind.INVESTIGATION, owner, review_items.ReviewType.INVESTIGATION),
                 (new_item_dialog.SubmissionKind.BUG, owner, review_items.ReviewType.BUG))
        published: list[review_page.PublishedSubmission] = []
        for kind, recipient, review_type in cases:
            with self.subTest(kind=kind):
                request = self.submission_request(kind, recipient.id)
                created = cache.create_submission(request)
                published.append(created)
                snapshot = cache.get()
                row = snapshot.records[created.key]
                self.assertEqual((row.state, row.type, row.owner), (review_items.ReviewState.WAITING, review_type, recipient.id))
                self.assertEqual(review_items.source_path(row), created.saved.path)
                self.assertIn(created.saved.path.parent, snapshot.source_stamps)
                entries = {entry['key']: entry for entry in unpack_index(review_page.index_json(snapshot.entries))}
                if review_type in review_items.INVESTIGATION_TYPES:
                    self.assertNotIn(created.key, entries)
                else:
                    self.assertEqual(entries[created.key].get('waitingWhy'), f'{recipient.name} answers this')
                self.assertNotIn(created.saved.reply_token.encode(), snapshot.body(created.key))
                self.assertNotIn(created.saved.key.encode(), snapshot.index_response())
                self.assertNotIn(created.key, initial.records)
        snapshot = review_page.build_snapshot(self.root)
        self.assertEqual([item.key for item in review_items.read(
            self.root, kinds=(review_items.ReviewType.BUG,))], [published[-1].key])
        self.assertIn(proposals, snapshot.source_stamps)
        for created in published:
            with self.subTest(key=created.key):
                self.assertEqual(snapshot.records[created.key], cache.get().records[created.key])
                self.assertNotIn(created.key, {item.key for item in review_items.read_open(self.root)})
                with patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed.")):
                    self.assertTrue(change_page.answer_item(created.saved.key, 'The check passed.', created.saved.reply_token, self.status))
        open_keys = {item.key for item in review_items.read_open(self.root)}
        new_keys = {item.key for item in published}
        self.assertEqual(open_keys & new_keys, {item.key for item in published[:2]})
        self.assertEqual(review_items.investigations(self.fixture.production, staff.snapshot()), 0)
        reopened = review_page.build_snapshot(self.root)
        self.assertTrue({item.key for item in published[2:]}.isdisjoint(entry['key'] for entry in reopened.entries))
        self.assertEqual([reopened.records[created.key].type for created in published],
                         [review_items.ReviewType.RESPONSE, *[case[2] for case in cases[1:]]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_old_review_tab_refreshes_a_response_as_a_question(self) -> None:
        owner = self.ready_submission_recipient()
        request = self.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id)
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        created = cache.create_submission(request)
        before = cache.get().render(show='all').decode()
        with patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed.")):
            self.assertTrue(review_submissions.answer(created.saved.key, 'Use the wide layout.', created.saved.reply_token))
        fresh = review_page.build_snapshot(self.root)
        self.assertEqual(fresh.records[created.key].type, review_items.ReviewType.RESPONSE)

        source = subprocess.check_output(
            ['git', 'show', 'eb6e3239d:scripts/review_links/review_page.py'],
            cwd=Path(__file__).resolve().parents[2], text=True)
        start = '(reviewPlace => {'
        shared_script = review_page.SCRIPT.split(start, 1)[0]
        old_script = start + source.split("r'''"+start, 1)[1].rsplit("''')", 1)[0]
        old_table_source = subprocess.check_output(
            ['git', 'show', 'eb6e3239d:scripts/review_links/record_table.py'],
            cwd=Path(__file__).resolve().parents[2], text=True)
        old_table = old_table_source.split("SCRIPT = r'''", 1)[1].split("'''\n", 1)[0]
        # Use the controls and table code that an open tab retains.
        before = re.sub(r'<button [^>]*id="filter-type-response"[^>]*>.*?</button>', '', before, count=1)

        def old_chip(match: re.Match[str]) -> str:
            tag, group, value = match[0], match[1], match[2]
            saved_value = '' if value == 'all' and group != 'show' else value.removeprefix('staff:')
            return tag.replace(f'data-filter-value="{value}"', f'data-{group}-chip="{saved_value}"')

        before = re.sub(r'<button [^>]*id="filter-(type|show|owner)-[^"]*" data-filter-value="([^"]+)"',
                        old_chip, before)
        before = re.sub(r' data-filter-row="(?:type|show|owner)"', '', before)
        markup = before.replace(review_page.SCRIPT, shared_script+'{\n'+old_table+old_script+'\n}', 1)
        setup = r'''<script>
const nextReview=SNAPSHOT, responseKey=KEY, nativeFetch=fetch;
window.fetch=(url,options)=>{
 const params=new URL(url,location.href).searchParams;
 if(params.has('index') || (params.has('version') && !params.has('body')))
  return Promise.resolve(Response.json(nextReview));
 return nativeFetch(url,options);
};
</script>'''.replace('SNAPSHOT', fresh.index_response().decode()).replace('KEY', json.dumps(created.key))
        probe = r'''<script>(async()=>{
 const review=document.querySelector('[data-review]'), failures=[];
 const row=()=>review.querySelector('[data-key="'+CSS.escape(responseKey)+'"]');
 const questions=review.querySelector('#filter-type-question');
 questions.click();
 review.querySelector('#filter-show-all').click();
 if(row()?.dataset.state!=='Waiting') failures.push('The old tab shows the Waiting Question.');
 const refreshed=new Promise(resolve=>review.addEventListener('review-refreshed',resolve,{once:true}));
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await refreshed;
 if(review.dataset.version!==nextReview.version || row()?.dataset.state!=='Open' || row()?.dataset.type!=='question')
  failures.push('The old tab refreshes the Response as an Open Question.');
 if(questions.getAttribute('aria-pressed')!=='true') failures.push('The old tab keeps the Questions filter.');
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        markup = markup.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>', 1)
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_response_card_opens_a_legacy_link_and_keeps_the_filters_on_submit(self) -> None:
        owner = self.ready_submission_recipient()
        request = replace(self.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id),
                          words='Which layout should I use?')
        saved = review_submissions.submit(request)
        with patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed.")):
            self.assertTrue(review_submissions.answer(saved.key, 'Use the wide layout.', saved.reply_token))
        source = browser_markup(self.root)
        for action, state in (('read', 'Done'), ('question', 'Waiting')):
            with self.subTest(action=action):
                self.check_card_motion(source, 'const action='+json.dumps(action)+',state='+json.dumps(state)+r""";
 const chip=(name,value)=>document.querySelector('[data-filter-row="'+name+'"] [data-filter-value="'+
  (name==='owner' && value ? CSS.escape(staffChipValue(value)) : value || 'all')+'"]');
 const selected=name=>document.querySelector('[data-filter-row="'+name+'"] [data-filter-value][aria-pressed="true"]');
 const responses=chip('type','response');
 check(responses.hidden && chip('type','question').hidden,
  'Review hides the legacy types until a link selects one.');
 const row=rows().find(item=>item.dataset.type==='response');
 history.replaceState(null,'','#'+new URLSearchParams({type:'response',show:'Validating',item:row.dataset.key}));
 dispatchEvent(new HashChangeEvent('hashchange'));
 check(selected('type')===responses && !responses.hidden && chip('type','question').hidden &&
  responses.textContent.trim()==='Responses 1',
  'The legacy link shows the selected Responses chip with a count of one.');
 check(!chip('show','Validating') && chip('show','Done')?.querySelector('b').textContent==='0' &&
  selected('show')===chip('show','open'),
  'Responses starts in Open with a Done count.');
 check(selected('show')===chip('show','open') && !chip('show','Waiting') &&
  recordIsOpen(row),
  'The saved Response link opens the card under Open.');
 row.querySelector('.list-toggle').click();
 chip('owner',row.dataset.owner).click();
 chip('show','all').click();
 const words=document.querySelector('#list-search');
 words.value='which layout';words.dispatchEvent(new Event('input',{bubbles:true}));
 document.querySelector('[data-sort="3"]').click();
 const place=()=>{
  const params=new URLSearchParams(location.hash.slice(1));
  for(const field of ['item','linked','reveal']) params.delete(field);
  return params.toString();
 };
 const form=await openCard(rows().find(item=>item.dataset.key===row.dataset.key));
 const before=place(),card=form.closest('.review-panel');
 check(card.querySelector('[data-review-body]').textContent.includes('Which layout should I use?') &&
  card.querySelector('.response-answer h3').textContent==='Answer from page-owner' &&
  card.querySelector('.response-answer time')?.dateTime &&
  card.querySelector('.response-answer').textContent.includes('Use the wide layout.'),
  'The Response shows the question, answer, owner and answer time.');
 check(card.querySelector('.list-log').textContent.includes('Answered — page-owner') && !card.querySelector('.review-source'),
  'The Response log names the owner.');
 check([...form.querySelectorAll('.review-action-group')].map(group=>group.textContent.trim()).join('|')===
  'Closes the itemGot it|Sends to page-ownerAsk a follow-up' && form.elements.feedback.labels[0].textContent==='Your words',
  'The Response shows both labeled actions and Your words.');
 check(form.querySelector('.review-action-group:last-child [data-color-text="role.showrunner"]')?.textContent==='page-owner',
  'The Response action heading shows the showrunner color.');
 check(form.elements.feedback.placeholder==='Type your words for Ask a follow-up.',
  'The placeholder says which action sends the words.');
 const id=recordFromDetail(form).dataset.key;
 if(action==='question') form.elements.feedback.value='Can I use it on a phone?';
 form.requestSubmit(form.querySelector('[value="'+action+'"]'));
 const motion=copy(id)?.getAnimations()[0];
 if(!motion || !posts.has(row.dataset.key)) throw new Error('The Response save must start the fade.');
 posts.get(row.dataset.key)({headers:new Headers(),ok:true,json:async()=>({answer:'saved',state,type:action==='read'?'response':'question',later:0})});
 motion.finish();await frame();
 check(postCount===1 && !rows().some(item=>item.dataset.type==='response'),
  'The Response leaves the view after one submit.');
 check(place()===before && filterControl.ownerId(selected('owner'))===row.dataset.owner &&
  selected('type')===responses && selected('show')===chip('show','all') && words.value==='which layout',
  'The Response submit keeps each filter and the sort.');
""")

    def test_page_feedback_uses_the_native_review_reply_and_keeps_its_data(self) -> None:
        owner = self.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        request = self.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, owner.id)
        evidence = new_item_dialog.PageDataEvidence("The List", (("Item", "State"), ("The console", "Open <1>")))
        created = cache.create_submission(replace(request, evidence=evidence))
        self.assertEqual(created.saved.details.evidence, evidence)
        card = cache.get().body(created.key)
        self.assertIn(b'Page data', card)
        self.assertIn(b'Open &lt;1&gt;', card)
        answered = change_page.answer_item(created.saved.key, 'The display is correct.', created.saved.reply_token, self.status)
        self.assertTrue(answered)
        self.assertNotIn(created.key, {item.key for item in review_items.read_open(self.root)})
        record = investigation.load(created.saved.owner, created.saved.id)
        destination = record.list_destination
        assert isinstance(destination, investigation.ListDestination)
        link = page_frame.STUDIO_PAGE_PATH + page_frame.list_item_fragment(destination.keeper, destination.number)
        self.assertIn(f'<a href="{link}">Open List item {destination.number}</a>'.encode(), card)
        evidence_link = "http://console.test" + review_page.PAGE_PATH + "#" + urlencode({"item": created.key})
        prefix = change_page.list_report_delivery_prefix(record, f"Feedback\n\nEvidence in Review\n{evidence_link}")
        self.assertEqual(prefix, "Feedback\n\nEvidence on the List\nhttp://console.test" + link)
        row = the_list.load(destination.keeper, f"L{destination.number}")
        assert isinstance(row.report, the_list.ListReport)
        self.assertEqual((row.report.kind, row.report.evidence_key, row.report.words),
                         (the_list.ReportKind.INVESTIGATION, created.key, request.words))
        self.assertEqual(record.answer, 'The display is correct.')

    def test_bug_card_shows_captured_page_state_and_screenshot(self) -> None:
        import base64

        from scripts.review_links.test_review_submissions import PNG

        owner = self.ready_submission_recipient()
        _ = fixture_member("producer", name="morty")
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        evidence = new_item_dialog.PageDataEvidence("Review", (("Page", "Console"),))
        state = new_item_dialog.BugPageState(
            "http://127.0.0.1/console/review.html#owner=builds", new_item_dialog.PageViewport(1280, 800, True),
            2.4, ("Could not load brain.",), (), "Type All · State Open · Owner <builds>", True)
        request = self.submission_request(new_item_dialog.SubmissionKind.BUG, owner.id)
        viewport = new_item_dialog.PageViewport(1280, 800, True)
        created = cache.create_submission(
            replace(request, page="review", evidence=evidence, page_state=state),
            screenshot=new_item_dialog.PageScreenshot(base64.b64decode(PNG), viewport))
        self.assertIn(b"Screenshot of Review", cache.get().body(created.key))
        self.assertEqual(created.saved.details.evidence, evidence)
        snapshot = review_page.build_snapshot(self.root)
        self.assertEqual(snapshot.records[created.key].state, review_items.ReviewState.WAITING)
        card = snapshot.body(created.key).decode()
        self.assertIn('<details><summary>Page details</summary><dl class="page-state">', card)
        self.assertIn('<dt>Page</dt><dd>Review</dd>', card)
        self.assertIn('<dt>Filters</dt><dd>Type All · State Open · Owner &lt;builds&gt;</dd>', card)
        self.assertIn('<dt>Render time</dt><dd>2.40 s</dd><dt>Errors</dt><dd>1 error line</dd>', card)
        self.assertIn('</dl></details><figure><a data-image-zoom href="', card)
        self.assertIn(f'id={review_submissions.PAGE_SCREENSHOT.id}" loading="lazy" alt="Screenshot of Review"', card)
        self.assertIn('<figcaption>Screenshot of Review</figcaption>', card)

    def test_moved_reports_display_the_original_source_unit(self) -> None:
        owner = self.ready_submission_recipient()
        unit = self.fixture.unit
        reports: list[investigation.Investigation] = []
        for kind in investigation.InvestigationKind:
            request = self.submission_request(new_item_dialog.SubmissionKind(kind.value), unit.id)
            origin = new_item_dialog.ReviewSubmission(
                request.request_id, 'a'*64, request.evidence, (), 'b'*64)
            reports.append(investigation.file(owner.id, request.words, request.words, kind=kind,
                                               waiting_why='The owner checks the report.', submission=origin))
        snapshot = review_page.build_snapshot(self.root)
        entries = {entry['key']:entry for entry in unpack_index(review_page.index_json(snapshot.entries))}
        owner_html = snapshot.names.html(owner.id, owner.name)
        unit_html = snapshot.names.html(unit.id, unit.name)
        for report in reports:
            with self.subTest(kind=report.kind):
                key = f'investigation/{owner.id}/{report.id}'
                entry = entries[key]
                self.assertEqual((entry['owner'], entry['ownerName']), (owner.id, owner.name))
                self.assertEqual((entry['sourceOwner'], entry['sourceName']), (unit.id, unit.name))
                details = snapshot.body(key).decode()
                self.assertIn(f'Owner: {owner_html} · Source: {unit_html}</p>', details)

    def test_send_rejects_a_snapshot_built_before_publication(self) -> None:
        owner = self.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(self.root)
        self.addCleanup(cache.close)
        previous = cache.get()
        entered, release = threading.Event(), threading.Event()
        original = review_page.build_snapshot

        def rebuild(root: Path) -> review_page.ReviewSnapshot:
            snapshot = original(root)
            if not entered.is_set():
                entered.set()
                if not release.wait(TEST_HANG_GUARD_SECS):
                    raise TimeoutError('The test did not release the snapshot.')
            return snapshot

        with patch.object(review_page, 'build_snapshot', side_effect=rebuild):
            cache.invalidate()
            self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
            try:
                created = cache.create_submission(self.submission_request(new_item_dialog.SubmissionKind.BUG, owner.id))
                self.assertNotIn(created.key, previous.records)
                self.assertIn(created.key, cache.get().records)
            finally:
                release.set()
            cache.synchronize()
            self.assertIn(created.key, cache.get().records)

    def test_sent_proposal_uses_its_own_prerequisite(self) -> None:
        owner = self.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        created = cache.create_submission(self.submission_request(new_item_dialog.SubmissionKind.PROPOSAL, owner.id))
        source = created.saved.path
        related = source.parent / 'prerequisite.md'
        _ = source.write_text(source.read_text().replace('---\n', '---\ndepends_on: [prerequisite]\n', 1))
        for proposer, expected in ((self.fixture.unit.id, str(related)), ('another-owner', '')):
            with self.subTest(proposer=proposer):
                _ = related.write_text('---\nstatus: open\ntags: [proposal]\nstage: backlog\n'
                                       + f'proposed_by: {proposer}\n---\n')
                fresh = review_page.build_snapshot(self.root)
                retained = fresh.records[created.key]
                self.assertEqual(retained.queue_after, expected)
                self.assertEqual(review_items.read_current(self.root, retained, fresh.members), retained)

    def test_current_proposal_prerequisite_precedes_an_archive_with_the_same_name(self) -> None:
        owner = self.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        request = self.submission_request(new_item_dialog.SubmissionKind.PROPOSAL, owner.id)
        created = cache.create_submission(request)
        source = created.saved.path
        _ = source.write_text(source.read_text().replace('---\n', '---\ndepends_on: [aardvark]\n', 1))
        current = source.with_name('aardvark.md')
        archived = source.parent / 'archive' / current.name
        archived.parent.mkdir()
        header = ('---\nstatus: open\ntags: [proposal]\nstage: backlog\n'
                  f'proposed_by: {self.fixture.unit.id}\n---\n')
        _ = archived.write_text(header)
        archive_cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        archive_snapshot = archive_cache.get()
        record = archive_snapshot.records[created.key]
        self.assertEqual(record.queue_after, str(archived))
        with (patch('scripts.production.record_store.known_folders',
                    side_effect=AssertionError('The exact read scanned folders.')),
              patch.object(review_items, 'store_paths', side_effect=AssertionError('The exact read listed records.'))):
            self.assertEqual(review_items.read_current(self.root, record, archive_snapshot.members), record)
            retry = archive_cache.create_submission(request)
            self.assertEqual(retry.version, archive_snapshot.version)
            outcome = archive_cache.save(
                self.card_body(archive_snapshot, created.key, 'question', 'Which archive supplied the prerequisite?'),
                self.status)
            self.assertEqual(outcome.answer, 'question sent')
            assert isinstance(outcome.item, review_items.ReviewItem)
            self.assertEqual(outcome.item.queue_after, str(archived))

        _ = current.write_text(header)
        rebuilt = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        snapshot = rebuilt.get()
        record = snapshot.records[created.key]
        self.assertEqual(record.queue_after, str(current))
        self.assertEqual(review_items.read_current(self.root, record, snapshot.members), record)
        outcome = rebuilt.save(self.card_body(snapshot, created.key, 'feedback', 'Check the current prerequisite.'),
                               self.status)
        self.assertEqual(outcome.answer, 'feedback sent')

    def test_save_sent_proposal_with_local_urgency_times(self) -> None:
        owner = self.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        local_time = datetime(2026, 11, 30, 15, 30)
        for level in (urgency.Urgency(urgency.SOON, due=local_time), urgency.mark(local_time)):
            with self.subTest(level=level.level):
                request = self.submission_request(new_item_dialog.SubmissionKind.PROPOSAL, owner.id)
                created = cache.create_submission(replace(request, words=f'Check the {level.level} Proposal.'))
                _ = proposal_issue.set_urgency(created.saved.path, level, local_time.date())
                rebuilt = review_page.ReviewSnapshotCache(self.root, start_worker=False)
                snapshot = rebuilt.get()
                time = local_time.replace(tzinfo=snapshot.zone)
                expected = replace(level, due=time if level.due else None, marked=time if level.marked else None)
                self.assertEqual(snapshot.records[created.key].urgency, expected)
                outcome = rebuilt.save(self.card_body(snapshot, created.key, 'feedback', 'Check the urgency.'),
                                       self.status)
                self.assertEqual(outcome.answer, 'feedback sent')
                self.assertIsInstance(outcome.detail, review_page.ConfirmedReviewDetail)
                assert isinstance(outcome.detail, review_page.ConfirmedReviewDetail)
                self.assertIn('Check the urgency.', outcome.detail.body)
                self.assertNotIn('data-review-form', outcome.detail.body)
                assert isinstance(outcome.item, review_items.ReviewItem)
                self.assertEqual(outcome.item.urgency, expected)

    def test_asking_a_sent_proposal_keeps_its_card_after_the_reply_token_changes(self) -> None:
        owner = self.ready_submission_recipient()
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        created = cache.create_submission(self.submission_request(new_item_dialog.SubmissionKind.PROPOSAL, owner.id))
        self.assertTrue(change_page.answer_item(created.saved.key, 'The proposal is ready.',
                                                created.saved.reply_token, self.status))
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        words = 'Which checks should the proposal run?'
        before = cache.get()
        self.assertEqual(before.records[created.key].state, review_items.ReviewState.OPEN)
        reply = cache.save(self.card_body(before, created.key, 'question', words), self.status)
        self.assertEqual(reply.answer, 'question sent')
        self.assertIsInstance(reply.detail, review_page.ConfirmedReviewDetail)
        assert isinstance(reply.detail, review_page.ConfirmedReviewDetail)
        self.assertIn(words, reply.detail.body)
        snapshot = cache.get()
        record = snapshot.records[created.key]
        self.assertEqual((record.state, record.waiting_why), (review_items.ReviewState.WAITING, words))
        origin = review_items.submission_origin(record)
        assert isinstance(origin, new_item_dialog.ReviewSubmission)
        self.assertEqual(origin.request_id, created.saved.request_id)
        self.assertNotEqual(origin.reply_token, created.saved.reply_token)
        self.assertEqual(snapshot.submission_keys[origin.request_id], created.key)
        self.assertIn(words.encode(), snapshot.body(created.key))

    def assert_index_title(self, item: review_items.ReviewItem, title: str) -> None:
        entry, = review_page.filter_index([item], staff.snapshot())
        self.assertEqual(entry['title'], title)
        self.assertEqual(unpack_index(review_page.index_json([entry]))[0]['title'], title)

    def test_legacy_question_titles_are_short_but_search_and_details_keep_the_full_text(self) -> None:
        key = self.question()
        source = next(item.source for item in review_items.read(self.root) if item.key == key)
        assert isinstance(source, review_items.QuestionSource)
        path = source.record.path
        original = path.read_text()
        for title in ('Long <title> '+ 'x'*100, 'Short <title>\nThe full question follows.'):
            with self.subTest(title=title):
                item = source.record.item.copy()
                item['text'] = title
                item['parts'] = update_registration.QuestionParts(ask=title, context='Context remains visible.',
                    items=[], choices=[], recommend='')
                registration_line = next(line for line in original.splitlines() if line.startswith('registration: '))
                _ = path.write_text(original.replace(registration_line, 'registration: '+json.dumps(item)))
                snapshot = review_page.build_snapshot(self.root)
                self.assert_index_title(snapshot.records[key], title)
                self.assertEqual(snapshot.title_limit, 100)
                detail = snapshot.body(key).decode()
                for line in title.splitlines():
                    self.assertIn(html.escape(line), detail)
                self.assertIn('Context remains visible.', detail)

    def test_question_at_the_title_limit_shows_its_full_text_once_in_details(self) -> None:
        key = self.question()
        source = next(item.source for item in review_items.read(self.root) if item.key == key)
        assert isinstance(source, review_items.QuestionSource)
        title = 'W'*100
        for structured in (False, True):
            with self.subTest(structured=structured):
                source.record.item['text'] = title
                source.record.item['question'] = title
                if structured:
                    source.record.item['parts'] = update_registration.QuestionParts(
                        ask=title, context='', items=[], choices=[], recommend='')
                else:
                    _ = source.record.item.pop('parts', None)
                update_registration.write_question_record(source.record)
                snapshot = review_page.build_snapshot(self.root)
                self.assert_index_title(snapshot.records[key], title)
                self.assertEqual(snapshot.title_limit, len(title))
                detail = snapshot.render_body(snapshot.records[key], snapshot.assets).decode().split('data-review-body>', 1)[1]
                self.assertEqual(detail.count(title), 1)
                self.assertNotIn('<h3>Item</h3>', detail)

    def test_short_change_title_appears_in_the_item_section(self) -> None:
        item = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        title = 'Check the spacing'
        detail = self.card_details(replace(item, title=title))
        self.assertIn('<h3>Item</h3><p>'+title+'</p>', detail)
        self.assertEqual(detail.split('<div data-review-body>', 1)[0].count(title), 1)

    def test_question_with_a_distinct_ask_shows_title_and_ask_once_in_details(self) -> None:
        key = self.question()
        source = next(item.source for item in review_items.read(self.root) if item.key == key)
        assert isinstance(source, review_items.QuestionSource)
        title, ask = 'Layout choice', 'Which width should the page use?'
        source.record.item['text'] = title
        source.record.item['parts'] = update_registration.QuestionParts(
            ask=ask, context='Choose the width.', items=[], choices=[], recommend='')
        update_registration.write_question_record(source.record)
        snapshot = review_page.build_snapshot(self.root)
        detail = snapshot.render_body(snapshot.records[key], snapshot.assets).decode()
        self.assertIn('<h3>Item</h3><p>'+title+'</p>', detail)
        self.assertEqual(detail.count(title), 1)
        self.assertEqual(detail.count(ask), 1)
        self.assertIn('Choose the width.', detail)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_card_shows_the_title_ask_and_feedback_once(self) -> None:
        long_title = 'W'*100
        equal = self.fixture.file_user_question(long_title, question=long_title)
        title, ask = 'Layout choice', 'Which width should the page use?'
        distinct = self.question(title)
        source = next(item.source for item in review_items.read(self.root) if item.key == distinct)
        assert isinstance(source, review_items.QuestionSource)
        source.record.item['parts'] = update_registration.QuestionParts(
            ask=ask, context='Choose the width.', items=[], choices=[], recommend='')
        update_registration.write_question_record(source.record)
        change, words = 'page-owner/C1', 'Adjust the spacing.'
        _ = review_page.submit(self.root, self.body(change, 'feedback', words), self.status)

        page = browser_markup(self.root)
        cases = json.dumps([(equal, [long_title]), (distinct, [title, ask]), (change, [words])])
        probe = r'''<script>
(async()=>{
 const failures=[];
 try{
  frameElement.style.width='1280px';frameElement.style.height='820px';
  document.querySelector('[data-filter-row="show"] [data-filter-value="all"]').click();
  for(const [key,phrases] of CASES){
   const row=document.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');
   if(!row) throw new Error('The row is missing: '+key);
   row.querySelector('.list-toggle').click();
   const end=performance.now()+8000;
   while(!recordDetails(row)?.querySelector('.review-panel')){
    if(performance.now()>end) throw new Error('The card did not load: '+key);
    await new Promise(resolve=>requestAnimationFrame(resolve));
   }
   const detail=recordDetails(row);
   if(!fixtureBodies[key].includes('data-review-summary') || detail.querySelector('[data-review-summary]'))
    failures.push(key+': the response summary stays outside the card.');
   const shown=detail.innerText;
   for(const phrase of phrases){
    if(shown.split(phrase).length!==2) failures.push(key+': the card shows '+phrase+' once.');
   }
  }
 }catch(error){failures.push(String(error));}
 await fixtureFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''.replace('CASES', cases)
        markup = page.replace('</body>', probe+'</body>').encode()
        self.assertEqual(firefox_probe_results(markup), [[]])

    def test_investigation_shows_its_question_title_once_in_details(self) -> None:
        title = 'Why did the gate stop?'
        filed = investigation.file(self.fixture.unit.id, title, title, waiting_why='The owner must complete the investigation.')
        with patch.object(the_list, 'file_exchanges', side_effect=OSError('The List save failed.')):
            investigation_record = investigation.answer(filed.owner, filed.id, 'The test failed. The repair is ready.')
        item = next(item for item in review_items.read(self.root)
                    if item.key == f'investigation/{investigation_record.owner}/{investigation_record.id}')
        self.assertEqual(item.title, investigation_record.question)
        detail = self.card_details(item).split('data-review-body>', 1)[1]
        self.assertIn('<h3>You asked</h3>', detail)
        self.assertEqual(detail.count(html.escape(item.title)), 1)
        self.assertNotIn('<h3>Item</h3>', detail)
        self.assertIn(html.escape(investigation_record.answer), detail)

    def test_change_shows_its_title_once_when_the_proof_contains_it(self) -> None:
        item = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        assert isinstance(item.source, review_items.ChangeSource)
        title = 'The spacing agrees'
        source = replace(item.source, row={**item.source.row, 'What you see': title})
        detail = self.card_details(replace(item, title=title, source=source)).split('data-review-body>', 1)[1]
        self.assertIn('<h3>Proof</h3><p>'+title+'</p>', detail)
        self.assertEqual(detail.split('<img ', 1)[0].count(title), 1)
        self.assertNotIn('<h3>Item</h3>', detail)

    def test_each_review_type_limits_the_title_and_keeps_it_in_details(self) -> None:
        _ = self.question()
        title = 'A full <title> '+ 'x'*100+'\nMore facts.'
        _ = self.investigation()
        _ = self.decisions()
        for item in review_items.read(self.root):
            with self.subTest(kind=item.type):
                item = replace(item, title=title)
                self.assert_index_title(item, title)
                detail = self.card_details(item)
                for line in title.splitlines():
                    self.assertIn(html.escape(line), detail)

    def test_render_reads_the_title_limit_once_for_all_rows_and_details(self) -> None:
        title = 'A title '+ 'x'*70
        _ = self.question(title)
        _ = self.investigation()
        reads: list[str] = []
        read_config = conf.read_or_repository

        class TitleLimit:
            def whole(self, key: str) -> int:
                return {'question_title_max_chars':40}[key]

        def config(stem: str) -> conf.ConfigRead | TitleLimit:
            if stem == 'production':
                reads.append(stem)
                return TitleLimit()
            return read_config(stem)

        with patch.object(conf, 'read_or_repository', side_effect=config):
            rendered = review_page.render(self.root).decode()
        self.assertEqual(reads, ['production'])
        self.assertIn('data-title-limit="40"', rendered)
        self.assertIn(title, rendered.split('data-review-index>', 1)[1].split('</script>', 1)[0])

    def investigation(self, ready: bool = True) -> investigation.Investigation:
        item = investigation.file(self.fixture.unit.id, 'Why did the gate stop?', 'Read the gate log.', waiting_why='The owner must complete the investigation.')
        return investigation.answer(item.owner,item.id,'The test failed. The repair is ready.') if ready else item

    def legacy_investigation(self) -> investigation.Investigation:
        """Keep the answer on Review when List filing fails."""
        with patch.object(the_list, 'file_exchanges', side_effect=OSError('The List save failed.')):
            return self.investigation()

    def decisions(self) -> tuple[str, str]:
        """File one proposal and one efficiency for the page owner; return their Review keys."""
        folder = self.fixture.temporary / 'decision-proposals'
        folder.mkdir()
        _ = (folder / 'P1.md').write_text('---\nstatus: open\ntags: [proposal]\nstage: backlog\n'
            +f'proposed_by: {self.fixture.unit.id}\nproposal_id: P1\ndate_created: 2026-11-01\n---\n'
            +'# Smaller checks\n\nRun fewer checks.\n')
        ledger = self.fixture.temporary / 'decision-efficiency.md'
        _ = ledger.write_text('- 2026-11-01 tests: Faster checks — proposed\n')
        doc = self.fixture.production
        _ = doc.write_text(doc.read_text()+f'- **Proposals:** {folder}\n- **Efficiency ledger:** {ledger}\n')
        items = review_items.read(self.root)
        return (next(item.key for item in items if item.type == review_items.ReviewType.PROPOSAL),
                next(item.key for item in items if item.type == review_items.ReviewType.EFFICIENCY))

    def body(self, key: str, outcome: str, words: str = '') -> str:
        token, initial = change_browser.browser_manifest(change_page.console_folder(self.root))
        browser = change_page.ReviewBrowser(token,initial)
        return urlencode({'row':key,'token':change_browser.row_token(browser,key),'outcome':outcome,'feedback':words})

    def card_body(self, snapshot: review_page.ReviewSnapshot, key: str, outcome: str, words: str = '') -> str:
        fields = self.body(key, outcome, words)
        revision = re.search(rb'name="revision" value="([^"]+)"', snapshot.body(key))
        return fields+'&'+urlencode({'revision':revision[1].decode()}) if revision else fields

    def test_save_projection_matches_the_current_snapshot_for_every_source(self) -> None:
        issue, efficiency = self.decisions()
        question = self.question()
        with patch("scripts.review_links.answer_sources.file_saved_investigation"):
            report = self.investigation()
        investigation_key = f'investigation/{report.owner}/{report.id}'
        ledger = self.fixture.temporary/'backlog.md'
        linked = ledger.with_name('check-notes.md')
        _ = linked.write_text('Proposed 2026-11-01 by builder-unit\n')
        _ = ledger.write_text('- 2026-11-01 Run the checks (check-notes.md) — proposed\n'
                              +'- 2026-11-02 Review other checks — proposed\n')
        doc = self.fixture.production
        _ = doc.write_text(doc.read_text()+f'- **Improvement queue:** {ledger}\n')
        backlog = f'proposal/backlog: {ledger}: Run the checks'
        change = 'page-owner/C1'
        before = review_page.build_snapshot(self.root)

        def check(key: str, snapshot: review_page.ReviewSnapshot, retained: review_items.ReviewItem) -> None:
            item = review_items.read_current(self.root, retained, snapshot.members)
            normalized = review_items.normalize_times(item, snapshot.zone)
            self.assertEqual(normalized, snapshot.records[key])
            self.assertEqual(review_page.filter_index([normalized], snapshot.members)[0],
                             next(entry for entry in snapshot.entries if entry['key'] == key))

        for key in (change, question, issue, backlog, investigation_key, efficiency):
            with self.subTest(source=key):
                check(key, before, before.records[key])
        until = datetime.now(user_zone())+timedelta(days=1)
        review_later.save(question, until)
        check(question, review_page.build_snapshot(self.root), before.records[question])

        destination = self.fixture.temporary/'destination.md'
        _ = destination.write_text(doc.read_text())
        runner = fixture_member('showrunner', name='destination', doc=staff.LiveDoc(local_machine(), destination))
        _ = staff.move(self.fixture.unit.id, staff.ReportsTo(runner.id))
        moved = review_page.build_snapshot(self.root)
        for key in (issue, change, investigation_key, question, efficiency, backlog):
            with self.subTest(transfer=key):
                check(key, moved, before.records[key])
        self.assertEqual(moved.records[issue].owner, runner.id)

        members = replace(moved.members, rows=(runner, *(
            member for member in moved.members.rows if member.id != runner.id)))
        with self.subTest(transfer='shared Efficiency ledger'), patch.object(staff, 'snapshot', return_value=members):
            fresh = review_page.build_snapshot(self.root)
            self.assertEqual(fresh.records[efficiency].owner, runner.id)
            check(efficiency, fresh, fresh.records[efficiency])
            with self.assertRaises(review_items.ReviewRecordChanged):
                _ = review_page.submit(self.root, self.card_body(before, efficiency, 'feedback', 'Check it.'),
                                       self.status, snapshot=fresh)

        _ = ledger.write_text(ledger.read_text().replace('Run the checks (check-notes.md) — proposed',
                                                        'Run the checks (check-notes.md) — deferred'))
        with self.subTest(state='deferred'):
            self.assertNotIn(backlog, review_page.build_snapshot(self.root).records)
            with self.assertRaises(review_items.ReviewRecordMissing):
                _ = review_items.read_current(self.root, moved.records[backlog], staff.snapshot())
        _ = ledger.write_text(ledger.read_text().replace(' — deferred', ' — proposed'))

        for label in ('unrouted-a', 'unrouted-b'):
            target = change_review.list_file(label)
            _ = change_review.append_rows(target, pictured_changes([change_review.Visible(label, 'Check it', 'Proof')]),
                datetime(2026, 11, 1).date(), '', doc,
                producers=[staff.StaffId('missing-'+label)], record=staff.snapshot())
        snapshot = review_page.build_snapshot(self.root)
        for label in ('unrouted-a', 'unrouted-b'):
            key = f'{label}/C1'
            with self.subTest(unrouted=label):
                source = snapshot.records[key]
                assert isinstance(source.source, review_items.ChangeSource)
                self.assertIsInstance(source.source.assignment.owner, change_routing.UnroutedChange)
                check(key, snapshot, source)

        opening = cast(Callable[..., TextIO], Path.open)
        proposal_path = Path(issue.removeprefix('proposal/'))

        def unreadable(path: Path, *args: str, **kwargs: str) -> TextIO:
            if path == proposal_path:
                raise PermissionError('The proposal is unreadable.')
            return opening(path, *args, **kwargs)

        def unreadable_link(path: Path, *args: str, **kwargs: str) -> TextIO:
            if path == linked:
                raise PermissionError('The backlog notes are unreadable.')
            return opening(path, *args, **kwargs)

        with self.subTest(source='unused backlog notes'), patch.object(Path, 'open', unreadable_link):
            fresh = review_page.build_snapshot(self.root)
            check(backlog, fresh, before.records[backlog])
            self.assertIn(f'proposal/backlog: {ledger}: Review other checks', fresh.records)

        def unreadable_ledger(path: Path, *args: str, **kwargs: str) -> TextIO:
            if path == ledger:
                raise PermissionError('The backlog ledger is unreadable.')
            return opening(path, *args, **kwargs)

        with self.subTest(source='unreadable backlog ledger'), patch.object(Path, 'open', unreadable_ledger):
            fresh = review_page.build_snapshot(self.root)
            self.assertNotIn(backlog, fresh.records)
            self.assertIn(issue, fresh.records)
            self.assertTrue(any(str(ledger) in entry for entry in fresh.notices))
            with self.assertRaises(PermissionError):
                _ = review_items.read_current(self.root, before.records[backlog], staff.snapshot())

        unrelated = self.fixture.temporary/'unrelated.md'
        _ = fixture_member('showrunner', name='unrelated', doc=staff.LiveDoc(local_machine(), unrelated))
        for condition in ('unreadable', 'missing'):
            with self.subTest(document=condition):
                if condition == 'unreadable':
                    _ = unrelated.write_text('# Unrelated production\n')
                else:
                    unrelated.unlink()
                reads: list[Path] = []

                def read_document(path: Path, *args: str, **kwargs: str) -> TextIO:
                    if path == unrelated:
                        reads.append(path)
                        if condition == 'unreadable':
                            raise PermissionError('The unrelated document is unreadable.')
                    return opening(path, *args, **kwargs)

                with patch.object(Path, 'open', read_document):
                    fresh = review_page.build_snapshot(self.root)
                    reads.clear()
                    for key in (issue, change, question, backlog, investigation_key, efficiency):
                        with self.subTest(key=key):
                            check(key, fresh, moved.records[key])
                    self.assertEqual(reads, [], 'Exact projection reads only the selected production.')

        dependency = proposal_path.with_name('P2.md')
        issue_text = proposal_path.read_text()
        _ = dependency.write_text(issue_text.replace('proposal_id: P1', 'proposal_id: P2'))
        _ = proposal_path.write_text(issue_text.replace('proposal_id: P1', 'proposal_id: P1\ndepends_on: [P2]'))
        prerequisite = review_page.build_snapshot(self.root)
        self.assertEqual(prerequisite.records[issue].after, 'proposal/'+str(dependency))

        def read_dependency(path: Path, *args: str, **kwargs: str) -> TextIO:
            if path == dependency:
                raise PermissionError('The prerequisite is unreadable.')
            return opening(path, *args, **kwargs)

        with patch.object(Path, 'open', read_dependency):
            fresh = review_page.build_snapshot(self.root)
            self.assertEqual(fresh.records[issue].after, '')
            self.assertTrue(any(str(dependency) in notice for notice in fresh.notices))
            check(issue, fresh, prerequisite.records[issue])

        with patch.object(Path, 'open', unreadable):
            with self.subTest(source='unreadable proposal'):
                broken = review_page.build_snapshot(self.root)
                self.assertNotIn(issue, broken.records)
                self.assertIn(question, broken.records)
                self.assertTrue(any(str(proposal_path) in notice for notice in broken.notices))
                with self.assertRaises(PermissionError):
                    _ = review_items.read_current(self.root, moved.records[issue], staff.snapshot())

        before_route = before.body(change).decode()
        match = re.search(r'<button data-send-receiver="([^"]+)"[^>]+value="question"', before_route)
        assert match is not None
        words = 'Keep my question.'
        original = self.card_body(before, change, 'question', words)+'&'+urlencode({'receiver': match[1]})
        change_before = self.fixture.review_list.read_bytes()
        with self.assertRaises(review_items.ReviewRecordChanged):
            _ = review_page.submit(self.root, original, self.status, snapshot=moved)
        legacy = self.body(change, 'question', words)+'&'+urlencode({'receiver': match[1]})
        with self.assertRaisesRegex(change_page.QuestionAnswerRefused, 'This item changed.'):
            _ = change_browser.submit(moved.browser, legacy, self.status)
        self.assertEqual(self.fixture.review_list.read_bytes(), change_before)
        self.assertEqual(self.fixture.messages, [])
        fresh = moved.body(change).decode()
        displayed = re.search(r'<button data-send-receiver="([^"]+)"[^>]+value="question"', fresh)
        assert displayed is not None
        self.assertNotEqual(displayed[1], match[1])
        resent = self.card_body(moved, change, 'question', words)+'&'+urlencode({'receiver': displayed[1]})
        delivery, _ = review_page.submit(self.root, resent, self.status, snapshot=moved)
        self.deliver_review_message(delivery, self.status)
        self.assertEqual([recipient for recipient, _, message in self.fixture.messages if words in message],
                         [displayed[1]])

    def test_snapshot_shows_and_counts_proposals_from_a_producer_document(self) -> None:
        doc = self.fixture.temporary / 'producer.md'
        folder = self.fixture.temporary / 'producer-proposals'
        folder.mkdir()
        _ = doc.write_text(f'# Producer\n- **Proposals:** {folder}\n')
        producer = fixture_member('producer', name='producer', doc=staff.LiveDoc(local_machine(), doc))
        path = folder / 'P1.md'
        _ = path.write_text('---\nstatus: open\ntags: [proposal]\nstage: backlog\n'
            +f'proposed_by: {producer.id}\nproposal_id: P1\ndate_created: 2026-11-01\n---\n'
            +'# Reduce the build time\n')

        snapshot = review_page.build_snapshot(self.root)
        proposals = [item for item in snapshot.records.values() if item.type == review_items.ReviewType.PROPOSAL]
        self.assertEqual(review_items.count_items(proposals, datetime.now(snapshot.zone)), 1)
        self.assertIn('proposal/'+str(path), {entry['key'] for entry in snapshot.entries})
        self.assertEqual(proposals[0].owner, producer.id)
        self.assertIn(doc, snapshot.source_stamps)
        self.assertEqual(snapshot.notices, ())

    def test_snapshot_ignores_producer_documents_and_reports_missing_production(self) -> None:
        directory = self.fixture.temporary / 'brain'
        directory.mkdir()
        missing_producer = self.fixture.temporary / 'current.md'
        missing_production = self.fixture.temporary / 'production-missing.md'
        _ = fixture_member('producer', name='brain', doc=staff.LiveDoc(local_machine(), directory))
        _ = fixture_member('producer', name='morty', doc=staff.LiveDoc(local_machine(), missing_producer))
        _ = fixture_member('showrunner', name='missing', doc=staff.LiveDoc(local_machine(), missing_production))

        snapshot = review_page.build_snapshot(self.root)
        self.assertIn(f'Could not load {missing_production}.', snapshot.notices)
        for path in (directory, missing_producer):
            self.assertNotIn(f'Could not load {path}.', snapshot.notices)
            self.assertNotIn(path, snapshot.source_stamps)

    def test_change_projection_isolates_an_unreadable_production_path(self) -> None:
        doc = self.fixture.temporary / 'unreadable.md'
        listing = change_review.list_file('unreadable')
        _ = doc.write_text(f'# Unreadable\n- **Change review:** `{listing}`\n')
        runner = fixture_member('showrunner', name='unreadable', doc=staff.LiveDoc(local_machine(), doc))
        _ = change_review.append_rows(listing, pictured_changes([change_review.Visible('Another Change', 'Check it', 'Proof')]),
            datetime(2026, 11, 1).date(), '', doc, producers=[runner.id], record=staff.snapshot())
        before = review_page.build_snapshot(self.root)
        key = 'page-owner/C1'
        is_file = Path.is_file

        def check(path: Path, *, follow_symlinks: bool = True) -> bool:
            if path == doc:
                raise PermissionError('The fixture production path is unreadable.')
            return is_file(path, follow_symlinks=follow_symlinks)

        with patch.object(Path, 'is_file', check):
            fresh = review_page.build_snapshot(self.root)
            self.assertNotIn('unreadable/C1', fresh.records)
            self.assertTrue(any(str(doc) in entry for entry in fresh.notices), fresh.notices)
            current = review_items.read_current(self.root, before.records[key], fresh.members)
            self.assertEqual(review_items.normalize_times(current, fresh.zone), fresh.records[key])
            _ = review_page.submit(self.root, self.card_body(fresh, key, 'works'), self.status, snapshot=fresh)

    def test_projection_skips_records_with_unreadable_dependencies(self) -> None:
        proposal, efficiency = self.decisions()
        change = 'page-owner/C1'
        question = self.question()
        _ = review_page.submit(self.root, self.body(change, 'question', 'What did you find?'), self.status)
        item = next(item for item in review_items.read(self.root) if item.key == efficiency)
        assert isinstance(item.source, review_items.EfficiencySource)
        _ = efficiency_reviews.path(item.source.path).write_text('{"E1":{"log":[],"mark":"waiting"}}')
        review_later.save(question, datetime.now(user_zone())+timedelta(days=1))
        snapshot = review_page.build_snapshot(self.root)
        queue = snapshot.records[change].queue
        urgency.pin(queue, snapshot.records[change].queue_key)
        opening = cast(Callable[..., TextIO], Path.open)

        def unreadable(path: Path, *args: str, **kwargs: str) -> TextIO:
            if path == target:
                raise PermissionError('The review dependency is unreadable.')
            return opening(path, *args, **kwargs)

        for target, key, unaffected in (
            (change_routing.delivery_path(self.root, self.fixture.review_list, 'C1'), change, question),
            (efficiency_reviews.path(item.source.path), efficiency, question),
            (review_later.path(), question, ''),
            (urgency.state_dir()/(queue+'.json'), change, question),
            (Path(proposal.removeprefix('proposal/')), proposal, question),
        ):
            with self.subTest(path=target), patch.object(Path, 'open', unreadable):
                if target == review_later.path():
                    with self.assertRaises(PermissionError):
                        _ = review_page.build_snapshot(self.root)
                else:
                    fresh = review_page.build_snapshot(self.root)
                    self.assertNotIn(key, fresh.records)
                    self.assertIn(unaffected, fresh.records)
                    self.assertTrue(any(str(target) in notice for notice in fresh.notices), fresh.notices)
                with self.assertRaises(PermissionError):
                    _ = review_items.read_current(self.root, snapshot.records[key], staff.snapshot())

    def test_question_note_does_not_block_native_id_allocation(self) -> None:
        key = self.question()
        first = self.question_path(key)
        folder = first.parent
        record, = update_registration.selected_question_records([first], (), None)
        update_registration.write_question_record(record, assign_id=True)
        _ = (folder/'question-notes.md').write_text('# Notes about questions\n')
        _ = (folder/'question-broken.md').write_text('---\nrecord_type: question\n')
        _ = (folder/'question-other.md').write_text('---\nrecord_type: note\n---\n')
        filed = self.question('Does the ID follow the earlier question?')
        path = folder / filed.rsplit('/', 1)[-1]
        record, = update_registration.selected_question_records([path], (), None)
        update_registration.write_question_record(record, assign_id=True)
        self.assertEqual(review_page.build_snapshot(self.root).records[filed].id, 'page-owner/Q2')

    def test_snapshot_isolates_bad_sources_and_records_until_they_are_repaired(self) -> None:
        proposal, efficiency = self.decisions()
        good = self.question()
        source = Path(proposal.removeprefix('proposal/'))
        template = source.read_text()
        header = source.with_name('P2.md')
        log = source.with_name('P3.md')
        date = source.with_name('P4.md')
        list_path = self.fixture.review_list.with_name(change_review.PREFIX+'broken.md')
        listing = self.fixture.review_list.read_text()
        row = next(line for line in listing.splitlines() if line.startswith('| C1 |'))
        repairs = {
            header: template.replace('P1', 'P2'),
            log: template.replace('P1', 'P3'),
            date: template.replace('P1', 'P4'),
            list_path: listing,
        }
        broken = {
            header:'---\nstatus: open\n',
            log:repairs[log].replace('date_created: 2026-11-01',
                'date_created: 2026-11-01\nreview_log:\n  - {bad'),
            date:repairs[date].replace('2026-11-01', '2026-22-01'),
            list_path:listing.replace(row, '| C1 | too few cells |'),
        }
        keys = {header:'proposal/'+str(header), log:'proposal/'+str(log),
                date:'proposal/'+str(date), list_path:'broken/C1'}
        for path, text in broken.items():
            try:
                with self.subTest(bad_source=path.name):
                    _ = path.write_text(text)
                    isolated = review_page.build_snapshot(self.root)
                    self.assertNotIn(keys[path], isolated.records)
            finally:
                path.unlink()
        for path, text in broken.items():
            _ = path.write_text(text)
        snapshot = review_page.build_snapshot(self.root)
        self.assertTrue({proposal, efficiency, good, 'page-owner/C1'} <= snapshot.records.keys())
        self.assertTrue(keys.keys() <= snapshot.source_stamps.keys())
        self.assertFalse(set(keys.values()) & snapshot.records.keys())
        cache = review_page.ReviewSnapshotCache(self.root, interval=0.02)
        self.addCleanup(cache.close)
        for path, repaired in repairs.items():
            with self.subTest(path=path.name):
                published = threading.Event()
                original = review_page.build_snapshot
                def observe(root: Path) -> review_page.ReviewSnapshot:
                    fresh = original(root)
                    if keys[path] in fresh.records:
                        published.set()
                    return fresh
                with patch.object(review_page, 'build_snapshot', side_effect=observe):
                    _ = path.write_text(repaired)
                    self.assertTrue(published.wait(TEST_HANG_GUARD_SECS))
                    cache.synchronize()
                self.assertIn(keys[path], cache.get().records)

    def test_snapshot_worker_rebuilds_when_git_history_adds_a_proposals_folder(self) -> None:
        current, _ = self.decisions()
        doc = self.fixture.production
        former = doc.parent / 'former-proposals'
        former.mkdir()
        issue = former / 'P2.md'
        _ = issue.write_text(Path(current.removeprefix('proposal/')).read_text().replace('P1', 'P2'))
        key = 'proposal/'+str(issue)

        def git(*arguments: str) -> str:
            return subprocess.run(['git', '-C', str(doc.parent), '-c', 'user.name=Fixture',
                                   '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false', *arguments],
                                  check=True, capture_output=True, text=True).stdout.strip()

        _ = git('init', '-b', 'main')
        _ = git('add', doc.name)
        _ = git('commit', '-m', 'Set the current proposals folder.')
        _ = git('switch', '--detach')
        old_folder = Path(current.removeprefix('proposal/')).parent
        _ = doc.write_text(doc.read_text().replace(f'**Proposals:** {old_folder}', f'**Proposals:** {former}'))
        _ = git('commit', '-am', 'Set a former proposals folder.')
        commit = git('rev-parse', 'HEAD')
        _ = git('switch', 'main')

        cache = review_page.ReviewSnapshotCache(self.root, interval=0.02)
        self.addCleanup(cache.close)
        before = cache.get()
        self.assertNotIn(key, before.records)
        self.assertIn(doc.parent / '.git/refs/heads', before.source_stamps)
        doc_stamp = review_page.source_stamp(doc)
        published = threading.Event()
        build_snapshot = review_page.build_snapshot

        def observe(root: Path) -> review_page.ReviewSnapshot:
            fresh = build_snapshot(root)
            if key in fresh.records:
                published.set()
            return fresh

        with patch.object(review_page, 'build_snapshot', side_effect=observe):
            _ = git('update-ref', 'refs/heads/former-proposals', commit)
            self.assertTrue(published.wait(TEST_HANG_GUARD_SECS))
            cache.synchronize()
        self.assertEqual(review_page.source_stamp(doc), doc_stamp)
        self.assertIn(current, cache.get().records)
        self.assertIn(key, cache.get().records)

    def test_snapshot_worker_keeps_the_last_snapshot_after_an_unexpected_error(self) -> None:
        cache = review_page.ReviewSnapshotCache(self.root, interval=0.02)
        self.addCleanup(cache.close)
        original = cache.get()
        called = threading.Event()
        def fail(_root: Path) -> review_page.ReviewSnapshot:
            called.set()
            raise RuntimeError('The source read failed.')
        with patch.object(review_page, 'build_snapshot', side_effect=fail):
            cache.invalidate()
            self.assertTrue(called.wait(TEST_HANG_GUARD_SECS))
            cache.synchronize()
            self.assertIs(cache.get(), original)
        key = self.question('Can the worker still publish?')
        cache.invalidate()
        cache.synchronize()
        self.assertIn(key, cache.get().records)

    def test_snapshot_worker_keeps_rows_when_a_shared_source_is_unreadable(self) -> None:
        cache = review_page.ReviewSnapshotCache(self.root)
        self.addCleanup(cache.close)
        original = cache.get()
        read_text = Path.read_text
        iterdir = Path.iterdir

        def read(path: Path, *args: str, **kwargs: str) -> str:
            if path == review_later.path():
                raise PermissionError('The shared Later file is unreadable.')
            return read_text(path, *args, **kwargs)

        def list_folder(path: Path) -> Iterator[Path]:
            if path == change_review.lists_directory():
                raise PermissionError('The shared Change folder is unreadable.')
            return iterdir(path)

        for method, failure, message in (
            ('read_text', read, 'The shared Later file is unreadable.'),
            ('iterdir', list_folder, 'The shared Change folder is unreadable.'),
        ):
            with self.subTest(method=method), patch.object(Path, method, failure), self.assertLogs(level='WARNING') as notices:
                for _ in range(2):
                    cache.invalidate()
                    cache.synchronize()
                    self.assertTrue(cache.get() is original, 'The snapshot changed.')
            self.assertEqual(len(notices.output), 1)
            self.assertIn(message, notices.output[0])

    def test_exact_efficiency_read_and_save_use_the_snapshot_owner(self) -> None:
        _, key = self.decisions()
        unrelated = self.fixture.temporary / 'unrelated-efficiency.md'
        _ = unrelated.write_text('- **Efficiency ledger:** /unused-ledger.md\n')
        other = fixture_member('showrunner', name='unrelated', doc=staff.LiveDoc(local_machine(), unrelated))
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        snapshot = cache.get()
        members = replace(snapshot.members, rows=(other, *(
            member for member in snapshot.members.rows if member.id != other.id)))
        body = self.card_body(snapshot, key, 'feedback', 'Keep this check.')
        read_text = Path.read_text
        documents: list[Path] = []

        def read(path: Path, *args: str, **kwargs: str) -> str:
            if path in (unrelated, self.fixture.production):
                documents.append(path)
            return read_text(path, *args, **kwargs)

        with patch.object(Path, 'read_text', read), patch.object(staff, 'snapshot', return_value=members):
            # Read the production document once to identify the fixture ledger.
            self.assertEqual(review_items.efficiency_ledger(self.fixture.production),
                             review_items.source_path(snapshot.records[key]))
            current = review_items.read_current(self.root, snapshot.records[key], members)
            saved = cache.save(body, self.status)
        self.assertEqual(current, snapshot.records[key])
        self.assertEqual(documents, [self.fixture.production])
        self.assertIsInstance(saved.delivery, change_page.MessageDelivery)
        assert isinstance(saved.delivery, change_page.MessageDelivery)
        self.assertEqual(saved.delivery.recipients, (snapshot.records[key].owner,))

    def test_snapshot_retains_the_time_zone_and_versions_zone_edits(self) -> None:
        proposal, _ = self.decisions()
        report = self.investigation()
        key = f'investigation/{report.owner}/{report.id}'
        proposal_path = Path(proposal.removeprefix('proposal/'))
        proposed_at = datetime(2026, 11, 1, tzinfo=ZoneInfo('UTC'))
        event, = review_events.rows((review_events.ReviewLog(proposed_at, 'nate', 'Proposal log event'),))
        _ = proposal_path.write_text(proposal_path.read_text().replace(
            'date_created: 2026-11-01', 'date_created: 2026-11-01\nreview_log:\n  - ' + json.dumps(event)))
        with patch.object(review_page, 'user_zone', return_value=ZoneInfo('UTC')):
            original = review_page.build_snapshot(self.root)
        filed = original.records[proposal].times.filed
        report_filed = original.records[key].times.filed
        with patch.object(review_page, 'user_zone', side_effect=ValueError('Invalid live zone')):
            self.assertIn(filed.astimezone(ZoneInfo('UTC')).strftime('%b %d %H:%M').encode(), original.body(proposal))
            self.assertIn(report_filed.astimezone(ZoneInfo('UTC')).strftime('%b %d %H:%M').encode(), original.body(key))
        with patch.object(review_page, 'user_zone', return_value=ZoneInfo('Asia/Tokyo')):
            updated = review_page.build_snapshot(self.root)
        self.assertNotEqual(updated.version, original.version)
        self.assertIn(filed.astimezone(ZoneInfo('Asia/Tokyo')).strftime('%b %d %H:%M').encode(), updated.body(proposal))
        self.assertIn(report_filed.astimezone(ZoneInfo('Asia/Tokyo')).strftime('%b %d %H:%M').encode(), updated.body(key))
        self.assertEqual(updated.records[proposal].times.filed.replace(tzinfo=None), filed.replace(tzinfo=None))

    def test_snapshot_keeps_good_cards_and_opens_bad_cards_with_an_error(self) -> None:
        proposal, _ = self.decisions()
        good = self.question('Does the good card open?')
        bad = self.question('Does the bad card open?')
        path = Path(proposal.removeprefix('proposal/'))
        contents = path.read_text()
        good_path, bad_path = self.question_path(good), self.question_path(bad)
        good_text, bad_text = good_path.read_text(), bad_path.read_text()
        ranked = next(item for item in review_items.read(self.root) if item.key == good)
        for finding in ('F034', 'F036', 'F041', 'combined'):
            with self.subTest(finding=finding):
                words = json.dumps({'time':'2026-11-01T12:00:00', 'who':'nate', 'words':42})
                _ = path.write_text(contents.replace('date_created: 2026-11-01',
                    'date_created: 2026-11-01\nreview_log:\n  - '+words)
                    if finding in ('F036', 'combined') else contents)
                moved = finding in ('F034', 'combined')
                _ = good_path.write_text(good_text.replace('registration: {',
                    'registration: {"urgency": "user", "marked": "2026-11-01T12:00:00", ', 1)
                    if moved else good_text)
                if moved:
                    urgency.move_to_end(ranked.queue, ranked.queue_key, datetime.fromisoformat('2026-11-01T12:10:00+01:00'))
                _ = bad_path.write_text(bad_text.replace('"text": "Does the bad card open?"',
                    r'"text": "Invalid \ud800 title"') if finding in ('F041', 'combined') else bad_text)
                snapshot = review_page.build_snapshot(self.root)
                self.assertIn(b'Which layout gives each review card enough space', snapshot.body(good))
                if finding in ('F036', 'combined'):
                    self.assertIn(b'This card is not available', snapshot.body(proposal))
                if finding in ('F041', 'combined'):
                    self.assertIn(b'This card is not available', snapshot.body(bad))

    def test_snapshot_keeps_valid_efficiency_marks_and_later_times(self) -> None:
        _, efficiency = self.decisions()
        key = self.question()
        item = next(candidate for candidate in review_items.read(self.root) if candidate.key == efficiency)
        self.assertIsInstance(item.source, review_items.EfficiencySource)
        assert isinstance(item.source, review_items.EfficiencySource)
        ledger = item.source.path
        _ = ledger.write_text(ledger.read_text()+'- 2026-11-02 tests: Faster builds — proposed\n')
        marks = efficiency_reviews.path(ledger)
        _ = marks.write_text(json.dumps({'E1':{'log':[], 'mark':'done'}, 'E2':{'log':[], 'mark':'bad'}}))
        later = review_later.path()
        later.parent.mkdir(parents=True, exist_ok=True)
        bad_key = 'page-owner/C3'
        _ = later.write_text(json.dumps({key:'2030-12-01T12:00:00', bad_key:'bad'}))
        snapshot = review_page.build_snapshot(self.root)
        with self.subTest(finding='F035'):
            self.assertNotIn(efficiency, snapshot.records)
            self.assertIn(str(marks).encode(), snapshot.page)
        with self.subTest(finding='F040 Later times'):
            until = snapshot.records[key].times.later
            assert isinstance(until, datetime)
            self.assertIsNotNone(until.tzinfo)
            self.assertEqual(until.replace(tzinfo=None), datetime(2030, 12, 1, 12))
            self.assertIn(str(later).encode(), snapshot.page)
            self.assertNotIn(bad_key, snapshot.records)

    def test_snapshot_names_a_skipped_change_list(self) -> None:
        path = self.fixture.review_list
        listing = path.read_text()
        row = next(line for line in listing.splitlines() if line.startswith('| C1 |'))
        _ = path.write_text(listing.replace(row, '| C1 | too few cells |'))
        good = self.question()
        snapshot = review_page.build_snapshot(self.root)
        self.assertIn(good, snapshot.records)
        self.assertNotIn('page-owner/C2', snapshot.records)
        self.assertEqual(snapshot.page.count(str(path).encode()), 1)

    def test_snapshot_serves_the_shell_index_and_one_body_without_store_reads(self) -> None:
        key = self.question('A question with a private body?')
        _ = review_page.submit(self.root, self.body('page-owner/C1', 'works'), self.status)
        snapshot = review_page.build_snapshot(self.root)
        with patch.object(review_items, 'read', side_effect=AssertionError('A request read a store.')), patch.object(
                staff, 'snapshot', side_effect=AssertionError('A request read staff.')):
            shell = snapshot.render(show='all').decode()
            card = snapshot.body(key).decode()
            with self.assertRaises(LookupError):
                _ = snapshot.body('page-owner/C1')
        shell_markup = re.sub(r'<script\b[^>]*>.*?</script>', '', shell, flags=re.DOTALL)
        self.assertNotRegex(shell_markup, r'<form[^>]*\bdata-review-form\b')
        self.assertNotIn('<dialog id="review-new-item"', shell_markup)
        self.assertNotRegex(shell_markup, r'<form\b')
        self.assertNotIn('<tbody data-list-item', shell_markup, 'The browser mounts summaries from the index.')
        self.assertIn('data-review-index', shell)
        self.assertIn('Which layout gives each review card enough space for its controls?', card)
        self.assertIn('data-review-form', card)
        revision = review_page.record_revision(snapshot.records[key], snapshot.assets)
        self.assertRegex(revision, r'^[0-9a-f]{16}$')
        self.assertIn(f'name="revision" value="{revision}"', card)
        with self.assertRaises(LookupError):
            _ = snapshot.body('missing/key')

    def test_snapshot_changes_outside_requests_and_retains_the_current_version(self) -> None:
        members = staff.snapshot()
        cache = review_page.ReviewSnapshotCache(self.root, staff_scope=partial(staff.supplied_snapshot, members))
        self.addCleanup(cache.close)
        previous = cache.get()
        self.assertIs(previous.members, members)
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        original = review_page.build_snapshot

        def rebuild(root: Path) -> review_page.ReviewSnapshot:
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise TimeoutError('The test did not release the snapshot build.')
            fresh = original(root)
            completed.set()
            return fresh

        with patch.object(review_page, 'build_snapshot', side_effect=rebuild):
            cache.invalidate()
            self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
            try:
                with patch.object(review_items, 'read', side_effect=AssertionError('A request read a store.')):
                    self.assertIs(cache.get(), previous, 'A request keeps the current snapshot during an update.')
                _ = self.question('A newly filed question?')
            finally:
                release.set()
            self.assertTrue(completed.wait(TEST_HANG_GUARD_SECS))
        cache.synchronize()
        fresh = cache.get()
        self.assertIs(fresh.members, members)
        self.assertNotEqual(fresh.version, previous.version)
        self.assertIn('A newly filed question?', fresh.render().decode())

    def test_snapshot_detects_a_store_change_without_a_request(self) -> None:
        cache=review_page.ReviewSnapshotCache(self.root,interval=0.02)
        self.addCleanup(cache.close)
        previous=cache.get()
        built=threading.Event()
        title = 'A question filed after the snapshot?'
        original=review_page.build_snapshot

        def rebuild(root: Path) -> review_page.ReviewSnapshot:
            fresh=original(root)
            if any(item.title == title for item in fresh.records.values()):
                built.set()
            return fresh

        with patch.object(review_page,'build_snapshot',side_effect=rebuild):
            key=self.question(title)
            self.assertTrue(built.wait(TEST_HANG_GUARD_SECS),'The background worker detects the changed store.')
            cache.synchronize()
        current=cache.get()
        self.assertNotEqual(current.version,previous.version)
        self.assertIn(key,current.records)

    def test_a_rebuild_cannot_restore_a_record_from_before_a_save(self) -> None:
        key = self.question()
        cache = review_page.ReviewSnapshotCache(self.root, interval=TEST_HANG_GUARD_SECS, start_worker=False)
        self.addCleanup(cache.close)
        previous = cache.get()
        read, release, retry = threading.Event(), threading.Event(), threading.Event()
        original = review_page.build_snapshot
        during_retry: list[review_page.ReviewSnapshot] = []

        def rebuild(root: Path) -> review_page.ReviewSnapshot:
            if read.is_set():
                during_retry.append(cache.get())
                retry.set()
                return original(root)
            read.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise TimeoutError('The test did not release the old read.')
            return previous

        destination: investigation.ListDestination
        with patch.object(review_page, 'build_snapshot', side_effect=rebuild):
            cache.start()
            cache.invalidate()
            try:
                self.assertTrue(read.wait(TEST_HANG_GUARD_SECS))
                saved = cache.save(self.card_body(previous, key, 'answer', 'Use the first layout.'), self.status)
                self.assertEqual(cache.get().version, saved.version)
                self.assertNotIn(key, {entry['key'] for entry in cache.get().entries})
                handoff = cache.get().records[key].exchange_handoff
                assert isinstance(handoff, review_items.FiledReviewExchange)
                self.assertTrue(handoff.hides_card)
                destination = handoff.destination
            finally:
                release.set()
            self.assertTrue(retry.wait(TEST_HANG_GUARD_SECS))
            cache.synchronize()
        retry_snapshot, = during_retry
        self.assertNotIn(key, {entry['key'] for entry in retry_snapshot.entries})
        self.assertEqual(retry_snapshot.records[key].exchange_handoff, handoff)
        current = cache.get()
        self.assertNotIn(key, {entry['key'] for entry in current.entries})
        self.assertNotIn(key, current.records)
        item = the_list.load(destination.keeper, f'L{destination.number}')
        self.assertIs(item.state, the_list.ItemState.WORKING)

    def test_snapshot_skips_a_malformed_question_at_startup_and_during_updates(self) -> None:
        key = self.question()
        other = self.question('A second question?')
        path, second = self.question_path(key), self.question_path(other)
        original, second_original = path.read_text(), second.read_text()
        registration = next(line for line in original.splitlines() if line.startswith('registration: '))
        _ = path.write_text(original.replace(registration, 'registration: {bad'))
        cache = review_page.ReviewSnapshotCache(self.root)
        self.addCleanup(cache.close)
        self.assertNotIn(key, cache.get().records)
        self.assertIn(other, cache.get().records)
        registration = next(line for line in second_original.splitlines() if line.startswith('registration: '))
        _ = second.write_text(second_original.replace(registration, 'registration: {bad'))
        cache.invalidate()
        cache.synchronize()
        self.assertNotIn(other, cache.get().records)
        _ = path.write_text(original)
        _ = second.write_text(second_original)
        cache.invalidate()
        cache.synchronize()
        self.assertTrue({key, other} <= cache.get().records.keys())

    def question_path(self, key: str) -> Path:
        source = next(item.source for item in review_items.read(self.root) if item.key == key)
        assert isinstance(source, review_items.QuestionSource)
        return source.record.path

    def test_snapshot_skips_an_unclosed_header_in_the_shared_proposals_folder(self) -> None:
        proposal, _ = self.decisions()
        folder = Path(proposal.removeprefix('proposal/')).parent
        bad = folder / 'question-broken.md'
        _ = bad.write_text('---\nrecord_type: question\n')
        snapshot = review_page.build_snapshot(self.root)
        self.assertIn(proposal, snapshot.records, 'F016: a malformed neighbor does not hide a proposal.')
        self.assertIn(bad, snapshot.source_stamps)

    def test_snapshot_captures_the_change_list_stamp_before_the_read(self) -> None:
        target = self.fixture.review_list
        read = change_review.review_snapshot
        before = review_page.source_stamp(target)

        def read_then_edit(path: Path, *, include_reviewed: bool = False,
                           collect_owners: bool = False) -> change_review.ReviewListSnapshot:
            listing = read(path, include_reviewed=include_reviewed, collect_owners=collect_owners)
            if path == target and review_page.source_stamp(target) == before:
                with target.open('a') as stream:
                    _ = stream.write('\n')
            return listing

        with patch.object(change_review, 'review_snapshot', side_effect=read_then_edit):
            snapshot = review_page.build_snapshot(self.root)
        self.assertNotEqual(review_page.source_stamp(target), before)
        self.assertEqual(snapshot.source_stamps[target], before, 'F018: the read keeps the old stamp.')

    def test_snapshot_version_includes_the_words_rows_setting(self) -> None:
        key = self.question()
        old = review_page.build_snapshot(self.root)
        settings = self.fixture.temporary/'config/review_links.conf'
        _ = settings.write_text(settings.read_text().replace(
            f'review_words_rows={old.words_rows}', f'review_words_rows={old.words_rows+1}'))
        new = review_page.build_snapshot(self.root)
        self.assertIn(f'rows="{old.words_rows+1}"'.encode(), new.body(key))
        self.assertNotEqual(new.version, old.version, 'F023: body settings change the version.')

    def test_snapshot_detects_a_change_during_startup(self) -> None:
        key = self.question()
        path = self.question_path(key)
        original = review_page.build_snapshot
        updated = threading.Event()

        def build(root: Path) -> review_page.ReviewSnapshot:
            snapshot = original(root)
            if snapshot.records[key].title == 'Which layout should the page use?':
                _ = path.write_text(path.read_text().replace('Which layout should the page use?', 'Which layout fits?'))
            else:
                updated.set()
            return snapshot

        with patch.object(review_page, 'build_snapshot', side_effect=build):
            cache = review_page.ReviewSnapshotCache(self.root, interval=0.02)
            try:
                self.assertTrue(updated.wait(2), 'The worker notices the write during startup.')
                cache.synchronize()
                self.assertEqual(cache.get().records[key].title, 'Which layout fits?')
            finally:
                cache.close()

    def test_review_reads_a_legacy_question_and_the_native_writer_allocates_its_id(self) -> None:
        key = self.question()
        path = self.question_path(key)
        legacy = path.read_bytes()
        modified = path.stat().st_mtime_ns
        sequence = record_store.state_root() / 'record-ids' / 'question.water'
        self.assertNotIn(b'"review_id"', legacy)
        opened = next(item for item in review_items.read_open(self.root) if item.key == key)
        self.assertEqual(opened.id, 'page-owner/'+path.stem)
        self.assertEqual(path.read_bytes(), legacy)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertFalse(sequence.exists())
        page = review_page.render(self.root).decode()
        index = page.split('<script type="application/json" data-review-index>', 1)[1].split('</script>', 1)[0]
        self.assertEqual(next(entry['id'] for entry in unpack_index(index) if entry['key']==key), 'page-owner/'+path.stem)
        self.assertEqual(path.read_bytes(), legacy)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        record, = update_registration.selected_question_records([path], (), None)
        update_registration.write_question_record(record, assign_id=True)
        page = review_page.render(self.root).decode()
        index = page.split('<script type="application/json" data-review-index>', 1)[1].split('</script>', 1)[0]
        self.assertEqual(next(entry['id'] for entry in unpack_index(index) if entry['key']==key), 'page-owner/Q1')
        self.assertIn('"review_id": "Q1"', path.read_text())

        after = '2030-02-11T08:00'
        for zone, utc_hour in (('America/New_York', 13), ('Europe/Paris', 7)):
            with self.subTest(after=after, zone=zone), patch.dict(os.environ, {'USER_ZONE': zone}):
                record.item['after'] = after
                update_registration.write_question_record(record)
                with patch.object(question_path, 'read_later', wraps=question_path.read_later) as decode:
                    item = review_page.build_snapshot(self.root).records[key]
                self.assertEqual(item.times.later, datetime(2030, 2, 11, utc_hour, tzinfo=ZoneInfo('UTC')))
                self.assertEqual([call.args[0] for call in decode.call_args_list], [after])

        with patch.dict(os.environ, {'USER_ZONE': 'America/New_York'}):
            utc = ZoneInfo('UTC')
            now = datetime.now(utc)
            local_after = (now - timedelta(hours=2)).replace(tzinfo=None, second=0, microsecond=0)
            record.item['after'] = local_after.strftime('%Y-%m-%dT%H:%M')
            update_registration.write_question_record(record)
            snapshot = review_page.build_snapshot(self.root)
            item = snapshot.records[key]
            owner = next(member for member in snapshot.members.rows if member.id == record.owner)
            identities = update_registration.QuestionStaff.for_record(snapshot.members)
            self.assertIs(item.queue_at(now), question_path.Queue.LATER)
            self.assertEqual(counts_row.question_counts(owner, utc, identities, None),
                             (0, str(int(local_after.replace(tzinfo=ZoneInfo('America/New_York')).timestamp()))))
            _, entries = review_items.next_queue('question', self.fixture.production, False, snapshot.members)
            self.assertNotIn(item.queue_key, {entry.key for entry in entries})

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_ask_keeps_later_edits_and_ignores_a_response_after_reopen(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        validating = replace(change, state=review_items.ReviewState.VALIDATING, actions=('question',))
        with patch.object(review_items, 'read', return_value=[validating]):
            markup = browser_markup(self.root, show='all')
        rows = [replace(change, key=f'page-owner/C{number}', id=f'C{number}') for number in range(1, 61)]
        with patch.object(review_items, 'read', return_value=rows):
            open_markup = browser_markup(self.root)
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const fetchPage=window.fetch,content=document.querySelector('[data-review]');
 const row=()=>content.querySelector('[data-list-item][data-key="page-owner/C1"]');
 const toggle=()=>row().querySelector('.list-toggle');
 const close=()=>toggle().click(),error=()=>row()?.querySelector('[data-record-error]');
 let finishPost;
 window.fetch=(url,options)=>{
  if(options?.method==='POST') return new Promise(resolve=>{finishPost=resolve;});
  const query=new URL(url,location.href).searchParams;
  if(query.get('version')==='1') return Promise.resolve(Response.json({version:content.dataset.version,facts:pageRefreshFacts()}));
  return fetchPage(url,options);
 };
 const edit=(field,words)=>{field.value=words;field.dispatchEvent(new Event('input'));};
 const ask=form=>form.requestSubmit(form.querySelector('[value="question"]'));
 const frame=()=>new Promise(resolve=>requestAnimationFrame(resolve));
 const until=async(test,message)=>{
  const end=performance.now()+8000;
  while(!test()){
   if(performance.now()>end) throw new Error(message);
   await frame();
  }
 };
 const settled=form=>until(()=>!form.dataset.sending,'The save did not settle.');
 try{
  if(VALIDATING) content.querySelector('#filter-show-all').click();
  toggle().click();const first=await readyReviewCard(row()),words=first.elements.feedback;
  edit(words,'Which check passed?');
  if(VALIDATING){
   ask(first);
   edit(words,'Which check ran?');edit(words,'Which check passed?');
   words.focus();words.setSelectionRange(3,7);
   finishPost(Response.json({answer:'question saved'}));await settled(first);
   check(words.value==='Which check passed?' && document.activeElement===words &&
    words.selectionStart===3 && words.selectionEnd===7,
    'A saved Ask preserves words edited during the save and keeps their selection.');
   ask(first);close();toggle().click();
   const reopened=await readyReviewCard(row());
   finishPost(Response.json({reason:'An earlier refusal.'},{status:400}));await settled(first);
   check(recordIsOpen(row()) && !reopened.querySelector('[data-send-status]').textContent.includes('Not saved:'),
    'A refusal from before Close stays out of the reopened item.');
  }
  const sent=await readyReviewCard(row());
  ask(sent);close();
  if(!VALIDATING){
   scrollTo({top:document.documentElement.scrollHeight,behavior:'instant'});
   const departure=content.querySelector('[data-record-motion-key="page-owner/C1"]');
   await frame();
   check(scrollY>=innerHeight,'The page scrolls one viewport during the save.');
   await until(()=>!departure?.isConnected,'The row did not finish leaving.');
   scrollTo({top:0,behavior:'instant'});
   await until(row,'The pending row did not return.');
  }
  toggle().click();
  const reopened=await readyReviewCard(row());
  edit(reopened.elements.feedback,'Keep these later words.');
  finishPost(Response.json({answer:'question saved'}));await settled(sent);
  check(recordIsOpen(row()) && reopened.elements.feedback.value==='Keep these later words.' &&
   !reopened.querySelector('[data-send-status]').textContent.includes('Saved:'),
   'A response from before Close leaves the reopened item and its draft in place.');
  if(VALIDATING){
   ask(reopened);edit(reopened.elements.feedback,'Keep the words after Ask.');
   finishPost(new Response('Unreadable result'));await settled(reopened);
   check(!error().hidden && error().textContent==='Saved; the item could not refresh.' &&
    reopened.elements.feedback.value==='Keep the words after Ask.',
    'An unreadable successful response reports a display error and keeps the draft.');
   ask(reopened);finishPost(Response.json({reason:'The save failed.'},{status:400}));await settled(reopened);
   check(error().hidden && reopened.querySelector('[data-send-status]').textContent==='Not saved: The save failed.' &&
    reopened.elements.feedback.value==='Keep the words after Ask.',
    'A failed save reports a save error and keeps the draft.');
  }
 }catch(error){failures.push(String(error));}
 await fetchPage('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        for is_validating, source in ((True, markup), (False, open_markup)):
            with self.subTest(validating=is_validating):
                script = probe.replace('VALIDATING', json.dumps(is_validating))
                self.assertEqual(firefox_probe_results(source.replace('</body>', script + '</body>').encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_retries_an_unavailable_body_with_the_same_version(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        validating = replace(change, state=review_items.ReviewState.VALIDATING, actions=('question',))
        with patch.object(review_items, 'read', return_value=[validating]):
            markup = browser_markup(self.root, show='all')
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const fetchPage=window.fetch,content=document.querySelector('[data-review]');
 const open=()=>content.querySelector('[data-record-inline-view]');
 const detail=()=>open()?.querySelector('[data-record-inline-detail]');
 const error=()=>open()?.querySelector('[data-record-error]');
 const index=JSON.parse(content.querySelector('[data-review-index]').textContent);
 const firstLoad=FIRST_LOAD;
 let version=content.dataset.version,unavailable=firstLoad,recovered=false,bodyReads=0;
 window.fetch=(url,options)=>{
  const query=new URL(url,location.href).searchParams;
  if(options?.method==='POST') return Promise.resolve(Response.json({
   answer:'question saved',version,detail_unavailable:true,delivery_pending:false}));
  if(query.has('body')){
   const saved=query.has('saved');
   if(!saved) bodyReads++;
   const body=unavailable || saved ? CARD_ERROR : fixtureBodies[query.get('body')]+
    (recovered ? '<p>The saved question is now available.</p>' : '');
   return Promise.resolve(new Response(body,{headers:{'X-Review-Version':version}}));
  }
  return Promise.resolve(Response.json({version,index,facts:pageRefreshFacts()}));
 };
 const frame=()=>new Promise(resolve=>requestAnimationFrame(resolve));
 const until=async(test,message)=>{
  const end=performance.now()+8000;
  while(!test()){
   if(performance.now()>end) throw new Error(message);
   await frame();
  }
 };
 const refresh=()=>{
  const refreshed=new Promise(resolve=>content.addEventListener('review-refreshed',resolve,{once:true}));
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  return refreshed;
 };
 try{
  content.querySelector('#filter-show-all').click();
  const row=content.querySelector('[data-list-item]');
  row.querySelector('.list-toggle').click();
  if(firstLoad){
   await until(()=>!error()?.hidden,'The first load did not report the unavailable body.');
   check(error().textContent.includes('The item body is unavailable.') &&
    !detail()?.querySelector('[data-review-card-unavailable]') && !detail()?.querySelector('form'),
    'The first load reports the error without installing the HTTP 200 error body.');
  }else{
   const form=await readyReviewCard(row);
   form.elements.feedback.value='Which check passed?';
   form.elements.feedback.dispatchEvent(new Event('input'));
   form.requestSubmit(form.querySelector('[value="question"]'));
   await until(()=>!form.dataset.sending && !error()?.hidden,'The confirmed save did not report the display error.');
   unavailable=true;version='unavailable';
   await refresh();
   await until(()=>bodyReads>=2,'The refresh did not request the unavailable body.');
   await frame();
   check(detail()?.querySelector('form')===form && !detail()?.querySelector('[data-review-card-unavailable]') &&
    error().textContent==='Saved; the item could not refresh.',
    'The HTTP 200 error body leaves the shown form and saved message in place.');
  }
  const reads=bodyReads;
  unavailable=false;recovered=true;
  await refresh();
  await until(()=>detail()?.textContent.includes('The saved question is now available.'),'The body did not recover.');
  check(bodyReads===reads+1 && error()?.hidden && detail().textContent.includes('The saved question is now available.'),
   'A refresh of the same version requests and displays the recovered body.');
 }catch(error){failures.push(String(error));}
 await fetchPage('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        probe = probe.replace('CARD_ERROR', json.dumps(review_page.CARD_ERROR.decode()).replace('</', '<\\/'))
        for first_load in (False, True):
            with self.subTest(first_load=first_load):
                script = probe.replace('FIRST_LOAD', json.dumps(first_load))
                self.assertEqual(firefox_probe_results(markup.replace('</body>', script + '</body>').encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_shows_saved_actions_and_refreshes_until_delivery_ends(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        validating = replace(change, state=review_items.ReviewState.VALIDATING, actions=('question',))
        proposal, _ = self.decisions()
        proposal_item = next(item for item in review_items.read(self.root) if item.key == proposal)
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const fetchPage=window.fetch,content=document.querySelector('[data-review]');
 const row=()=>content.querySelector('[data-list-item]');
 const detail=()=>recordDetails(row());
 const [key]=Object.keys(fixtureBodies);
 const body=(revision,words)=>{
  const copy=document.createElement('template');copy.innerHTML=fixtureBodies[key];
  copy.content.querySelector('[name="revision"]').value=revision;
  copy.content.querySelector('[data-review-body]').insertAdjacentHTML('afterbegin','<p>'+words+'</p>');
  return copy.innerHTML;
 };
 const action=ACTION,filter=FILTER,completed=COMPLETED;
 let bodyReads=0,pageVersion=content.dataset.version,pendingDelivery=true;
 window.fetch=async(url,options)=>{
  const query=new URL(url,location.href).searchParams;
  if(options?.method==='POST'){
   pageVersion='own';
   const reply={answer:'saved',version:'own',delivery_pending:true,delivery:'fixture-delivery',detail_revision:'own-revision'};
   reply.state=completed ? 'Done' : action==='feedback' ? 'Waiting' : undefined;
   if(action==='feedback'){
    const saved=document.createElement('template');saved.innerHTML=body('own-revision','The saved words.');
    if(completed) saved.content.querySelector('form').remove();
    reply.detail_html=saved.innerHTML;
   }
   return Response.json(reply);
  }
  if(query.has('body') && ['own','during','settled','foreign'].includes(pageVersion)){
   const version=pageVersion;
   const words={own:'The saved words.',during:'Delivery in progress.',settled:'Delivery ended.',
    foreign:'A new owner update.'}[version];
   bodyReads++;
   return new Response(body(version+'-revision',words),{headers:{'X-Review-Version':version}});
  }
  if(!query.has('body') && (query.has('version') || query.has('index'))){
   const index=JSON.parse(content.querySelector('[data-review-index]').textContent);
   if(completed) index.r=[];
   else if(action==='feedback') index.r[0][6]=1;
   return Response.json({version:pageVersion,index,delivery_pending:pendingDelivery,facts:pageRefreshFacts()});
  }
  return fetchPage(url,options);
 };
 const until=async(test,message)=>{
  const end=performance.now()+8000;
  while(!test()){
   if(performance.now()>end) throw new Error(message);
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 try{
  content.querySelector(filter).click();
  row().querySelector('.list-toggle').click();
  const form=await readyReviewCard(row());
  form.elements.feedback.value='Which check passed?';
  form.elements.feedback.dispatchEvent(new Event('input'));
  form.requestSubmit(form.querySelector('[value="'+action+'"]'));
  await until(()=>detail().textContent.includes('The saved words.'),'The saved body did not appear.');
  check(action!=='question' || bodyReads>0,'Ask requests the saved body.');
  const refresh=version=>{
   pageVersion=version;
   dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  };
  await until(()=>content.dataset.version==='own','The saved index did not refresh.');
  if(completed){
   check(recordIsOpen(row()) && detail().textContent.includes('The saved words.') && !detail().querySelector('form'),
    'Proposal Tell keeps the completed result in the row.');
   await fetchPage('/results',{method:'POST',body:JSON.stringify(failures)});
   return;
  }
  refresh('during');
  await until(()=>detail().textContent.includes('Delivery in progress.'),'The pending delivery did not refresh.');
  pendingDelivery=false;
  refresh('settled');
  await until(()=>detail().textContent.includes('Delivery ended.'),'The settled delivery did not refresh.');
  refresh('foreign');
  await until(()=>detail().textContent.includes('A new owner update.'),'The owner update did not refresh.');
 }catch(error){failures.push(String(error));}
 await fetchPage('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        for item, action, filter_id, completed in ((validating, 'question', '#filter-show-all', False),
                                                   (change, 'feedback', '#filter-show-open', False),
                                                   (proposal_item, 'question', '#filter-show-open', False),
                                                   (proposal_item, 'feedback', '#filter-show-open', True)):
            with self.subTest(type=item.type, action=action):
                with patch.object(review_items, 'read', return_value=[item]):
                    markup = browser_markup(self.root)
                script = (probe.replace('ACTION', json.dumps(action)).replace('FILTER', json.dumps(filter_id))
                          .replace('COMPLETED', json.dumps(completed)))
                self.assertEqual(firefox_probe_results(markup.replace('</body>', script + '</body>').encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_closing_action_closes_when_the_saved_detail_is_unavailable(self) -> None:
        markup = browser_markup(self.root)
        probe = r'''<script>
(async()=>{
 const failures=[],fetchPage=window.fetch,content=document.querySelector('[data-review]');
 const opened=()=>content.querySelector('[data-record-inline-view]');
 window.fetch=(url,options)=>{
  if(options?.method==='POST')
   return Promise.resolve(Response.json({answer:'parked',detail_unavailable:true,delivery_pending:false}));
  const query=new URL(url,location.href).searchParams;
  if(query.get('version')==='1')
   return Promise.resolve(Response.json({version:content.dataset.version,facts:pageRefreshFacts()}));
  return fetchPage(url,options);
 };
 try{
  const row=content.querySelector('[data-list-item]');
  row.querySelector('.list-toggle').click();
  const form=await readyReviewCard(row);
  if(ACTION==='later'){
   form.querySelector('[data-later-toggle]').click();
   form.querySelector('[data-later-preset]').click();
  }else form.requestSubmit(form.querySelector('[value="'+ACTION+'"]'));
  const end=performance.now()+8000;
  while(opened() && performance.now()<end) await new Promise(resolve=>requestAnimationFrame(resolve));
  const error=content.querySelector('[data-hidden-action-status]');
  if(opened() || error.hidden || !error.textContent.includes('Saved; the item could not refresh.'))
   failures.push('The confirmed closing action collapses the row and reports the display failure.');
 }catch(error){failures.push(String(error));}
 await fetchPage('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        for action in ('later', 'works'):
            with self.subTest(action=action):
                script = probe.replace('ACTION', json.dumps(action))
                self.assertEqual(firefox_probe_results(markup.replace('</body>', script + '</body>').encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_history_opens_a_filtered_review_and_submits_only_its_own_form(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        records = [replace(change, key=f'page-owner/C{number}', id=f'page-owner/C{number}', title=title,
                           state=review_items.ReviewState.OPEN)
                   for number, title in ((1, 'Source task'), (2, 'Target task'))]
        with patch.object(review_items, 'read', return_value=records):
            markup = browser_markup(self.root)
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const realFetch=window.fetch,source='page-owner/C1',target='page-owner/C2';
 const findForm=()=>document.querySelector('[data-record-inline-detail] [data-review-form]');
 const row=key=>document.querySelector('[data-list-item][data-key="'+key+'"]');
 const shown=()=>new URLSearchParams(location.hash.slice(1));
 const changed=()=>new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
 const requests=[];
 let releaseBody,bodyRequested;
 const delayedBody=new Promise(resolve=>{releaseBody=resolve;});
 const requested=new Promise(resolve=>{bodyRequested=resolve;});
 window.fetch=async(url,options)=>{
  if(options?.method==='POST'){
   requests.push(new URLSearchParams(options.body));
   return new Promise(()=>{});
  }
  if(new URL(url,location.href).searchParams.get('body')===target){
   bodyRequested();await delayedBody;
  }
  return realFetch(url,options);
 };
 const until=async condition=>{
  const end=performance.now()+8000;
  while(!condition()){
   if(performance.now()>end) throw new Error('The expected form did not load.');
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 const draft=(form,value)=>{
  form.elements.feedback.value=value;
  form.elements.feedback.dispatchEvent(new Event('input',{bubbles:true}));
 };
 const send=form=>form.dispatchEvent(new SubmitEvent('submit',{cancelable:true,submitter:form.querySelector('[value="works"]')}));
 try{
  row(source).querySelector('.list-toggle').click();
  const first=await readyReviewCard(row(source));
  draft(first,'Source draft.');
  const sourceURL=location.href,sourceState=history.state;
  history.replaceState({recordTable:{opened:[target],linkedItem:'',revealedLinkedItem:'',top:scrollY}},'',
   '#'+new URLSearchParams({show:'open',q:'Source task',item:target}));
  history.pushState(sourceState,'',sourceURL);
  const back=changed();history.back();await back;
  check(recordIsOpen(row(target)) && shown().get('q')==='Source task' &&
   document.querySelector('#list-search').value==='Source task' && !findForm(),
   'Back opens the filtered target with its filters and waits for its form.');
  send(first);
  check(requests.length===0,'The previous form cannot submit while the target body loads.');
  await until(()=>!findForm() && recordIsOpen(row(target)));
  await requested;releaseBody();
  await until(()=>findForm()?.elements.row.value===target);
  const second=findForm(),token=second.elements.token.value,revision=second.elements.revision.value;
  draft(second,'Target draft.');
  const forward=changed();history.forward();await forward;
  await until(()=>findForm()?.elements.row.value===source);
  check(findForm().elements.feedback.value==='Source draft.','Forward restores the source draft.');
  const backAgain=changed();history.back();await backAgain;
  await until(()=>findForm()?.elements.row.value===target);
  const form=findForm();
  check(form.elements.feedback.value==='Target draft.','Back restores the target draft.');
  form.elements.row.value=source;send(form);form.elements.row.value=target;
  check(requests.length===0,'The form must identify the active record before it can submit.');
  send(form);
  const post=requests[0];
  check(requests.length===1 && post.get('row')===target && post.get('token')===token &&
   post.get('revision')===revision && post.get('feedback')==='Target draft.',
   'The POST contains the signed target identity and its draft.');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(markup.replace('</body>', probe + '</body>').encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_windowed_rows_keep_open_cards_and_drafts_across_scroll_and_refresh(self) -> None:
        _ = write_config_fixture(self.fixture.temporary / 'config/review_links.conf', 'page_request_timeout_seconds=3\n')
        key = self.question()
        records = review_items.read(self.root)
        question = next(item for item in records if item.key == key)
        assert isinstance(question.source,review_items.QuestionSource)
        question.source.record.item['parts']=update_registration.QuestionParts(
            ask='Which layout?',context='Keep the draft.',items=[],recommend='',choices=['Compact','Wide'])
        change = next(item for item in records if item.type == review_items.ReviewType.CHANGE)
        records = [question]+[replace(change,key=f'page-owner/C{number}',id=f'page-owner/C{number}',
            title=f'Review change {number}', state=review_items.ReviewState.OPEN)
            for number in range(1,401)]
        with patch.object(review_items,'read',return_value=records):
            initial = review_page.build_snapshot(self.root)
        changed = [replace(item,times=replace(item.times,updated=item.times.updated+timedelta(seconds=1))) for item in records]
        with patch.object(review_items,'read',return_value=changed):
            fresh = review_page.build_snapshot(self.root)
        fresh = replace(fresh,browser=change_page.ReviewBrowser('fresh-browser-token',''))
        expected_token = change_browser.row_token(fresh.browser,key)
        expected_revision = review_page.record_revision(fresh.records[key], fresh.assets)
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 WAIT_FOR_BROWSER_STATE
 const refresh=()=>{
  const review=document.querySelector('[data-review]');
  const refreshed=new Promise(resolve=>review.addEventListener('review-refreshed',resolve,{once:true}));
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  return refreshed;
 };
 const row=key=>document.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');
 const inline=()=>document.querySelector('[data-record-inline-view]');
 const close=()=>inline().closest('[data-list-item]').querySelector('.list-toggle').click();
 const form=key=>recordDetails(row(key))?.querySelector('[data-review-form]');
 const open=async key=>{
  if(!recordIsOpen(row(key))){
   if(row(key)) row(key).querySelector('.list-toggle').click();
   else location.hash=new URLSearchParams({item:key});
  }
  await until(()=>recordIsOpen(row(key)) && form(key),'the open card '+key);await frame();
 };
 try{
  await until(()=>window.releaseColdBody,'the pending linked card');
  await frame();await frame();
  check(document.querySelectorAll('[data-list-item]').length>1,
    'F077: the table mounts while the linked body request is pending.');
  window.releaseColdBody();
  await until(()=>form('page-owner/C399'),'the cold Open card');
  check(window.coldBodyMounts===0,'The cold card request starts before the first summary mounts.');
  check(row('page-owner/C399').dataset.state==='Open' &&
   row('page-owner/C399').querySelector('.list-toggle').getAttribute('aria-expanded')==='true',
   'An Open item opens directly from its key.');
  check(document.querySelectorAll('[data-list-item]').length<=100,'The first window mounts at most 100 summaries.');
  const firstToggle=row('page-owner/C399').querySelector('.list-toggle');
  const secondToggle=row('page-owner/C381').querySelector('.list-toggle');
  secondToggle.click();
  check(firstToggle.getAttribute('aria-expanded')==='false' && secondToggle.getAttribute('aria-expanded')==='true',
   'Opening the second Review row closes the first row.');
  await open(DRAFT_KEY);
  const draft=form(DRAFT_KEY),choice=recordDetails(row(DRAFT_KEY)).querySelector('input[name=choice]');
  choice.checked=true;choice.dispatchEvent(new Event('change'));
  draft.elements.feedback.value='Keep these draft words.';draft.elements.feedback.dispatchEvent(new Event('input'));
  draft.elements.later.value='2030-12-01T18:30';draft.elements.later.dispatchEvent(new Event('input'));
  const choiceValue=choice.value,first=[...document.querySelectorAll('[data-list-item]')].map(node=>node.dataset.key);
  close();
  document.querySelector('#list-search').focus({preventScroll:true});
  scrollTo(0,document.documentElement.scrollHeight);await until(()=>!row(DRAFT_KEY),'the closed draft to leave the window');
  check(document.querySelectorAll('[data-list-item]').length<=100 &&
    [...document.querySelectorAll('[data-list-item]')].some(node=>!first.includes(node.dataset.key)),
    'Scrolling loads distant summaries and removes old summaries.');
  check(!row(DRAFT_KEY) && !inline(),'The closed card leaves the window during distant scrolling.');
  await open(DRAFT_KEY);
  check(form(DRAFT_KEY).elements.feedback.value==='Keep these draft words.' &&
   form(DRAFT_KEY).elements.later.value==='2030-12-01T18:30' &&
   recordDetails(row(DRAFT_KEY)).querySelector('input[name=choice]:checked')?.value===choiceValue,
   'Feedback, choice and Later survive removal and mounting by key.');
  const feedback=form(DRAFT_KEY).elements.feedback;
  feedback.focus();
  scrollTo(0,document.documentElement.scrollHeight);await frame();await frame();
  check(document.activeElement===feedback && feedback.isConnected,'F013: scrolling keeps the focused row mounted.');
  await open(DRAFT_KEY);
  form(DRAFT_KEY).elements.feedback.focus();
  await fetch('/advance');
  await refresh();
  await until(()=>form(DRAFT_KEY)?.elements.token.value===EXPECTED_TOKEN,'the refreshed form');
  check(!inline().querySelector('[data-show-record-updates]'),'The inline row has no update control.');
  check(document.activeElement===form(DRAFT_KEY).elements.feedback,'F007: refresh retains focus in the row.');
  check(form(DRAFT_KEY).elements.feedback.value==='Keep these draft words.' &&
   form(DRAFT_KEY).elements.later.value==='2030-12-01T18:30' &&
   recordDetails(row(DRAFT_KEY)).querySelector('input[name=choice]:checked')?.value===choiceValue,
   'The open card and all editable values survive a new snapshot.');
  check(form(DRAFT_KEY).elements.token.value===EXPECTED_TOKEN,
   'The refreshed body supplies the current row token.');
  check(form(DRAFT_KEY).elements.revision.value===EXPECTED_REVISION,
   'F024: the refreshed body supplies the current record revision.');
  await fetch('/reset-counts');
  await refresh();
  const counts=await (await fetch('/counts')).json();
  check(counts.version===1 && counts.index===0,'An unchanged version does not fetch the index.');
  check(document.documentElement.scrollWidth<=innerWidth,'The row window fits the viewport.');
  document.activeElement.blur();
  const originalFetch=window.fetch;
  let releaseBody,releaseSave,posts=0,conflict='',holdVersion=false,timeoutVersion=false,stalled=false;
  let releaseVersion;
  const delayedBody=new Promise(resolve=>{releaseBody=resolve;});
  const delayedSave=new Promise(resolve=>{releaseSave=resolve;});
  window.fetch=async(url,options)=>{
   if(options?.method==='POST' && url===location.pathname){
    if(conflict){
     await originalFetch(conflict);
     const reason=conflict==='/remove-conflict' ? 'This item is no longer on the list.' :
      conflict.startsWith('/revise-conflict') ? 'This item changed.' : 'This item is already Done.';
     return new Response(JSON.stringify({reason}),{status:409});
    }
    posts++;document.dispatchEvent(new Event('fixture-progress'));
    await delayedSave;return new Response(JSON.stringify({reason:'Retry the save.'}),{status:400});
   }
   if(new URL(url,location.href).searchParams.get('version')==='1'){
    if(holdVersion){
     holdVersion=false;
     await new Promise(resolve=>{
      releaseVersion=resolve;
      document.dispatchEvent(new Event('fixture-progress'));
     });
    }
    if(timeoutVersion){
     timeoutVersion=false;stalled=true;
     document.dispatchEvent(new Event('fixture-progress'));
     if(!options?.signal) throw new Error('The reload has no deadline.');
     return new Promise((_,reject)=>{
      options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true});
     });
    }
   }
   const response=await originalFetch(url,options);
   if(new URL(url,location.href).searchParams.get('body')==='page-owner/C2') await delayedBody;
   return response;
  };
  location.hash=new URLSearchParams({item:'page-owner/C2'});
  await until(()=>row('page-owner/C2')?.querySelector('.list-toggle').getAttribute('aria-expanded')==='true','the delayed card');
  const top=scrollY;
  scrollTo(0,document.documentElement.scrollHeight);await frame();
  check(recordIsOpen(row('page-owner/C2')),'The delayed row stays open after background scroll.');
  scrollTo(0,top);releaseBody();
  const ready=async key=>{
   const end=performance.now()+1500;
   while(!form(key) && performance.now()<end) await frame();
   return form(key);
  };
  check(await ready('page-owner/C2'),'F003: the delayed response fills the open row.');
  const pending=form('page-owner/C2');
  pending.elements.later.value='2030-12-02T18:30';
  pending.requestSubmit(pending.querySelector('button[value=later]'));
  await until(()=>posts===1,'the save request');
  pending.elements.feedback.value='Edit during save.';
  pending.elements.feedback.dispatchEvent(new Event('input'));
  pending.elements.later.value='2030-12-03T18:30';
  pending.elements.later.dispatchEvent(new Event('input'));
  pending.elements.later.blur();
  scrollTo(0,document.documentElement.scrollHeight);await frame();
  check(new URLSearchParams(location.hash.slice(1)).get('item')==='page-owner/C2','The open row keeps its address during the save.');
  scrollTo(0,top);await until(()=>form('page-owner/C2'),'the pending form');
  const replacement=form('page-owner/C2');
  const laterButton=replacement.querySelector('button[value=later]');
  check(replacement.elements.later.value==='2030-12-03T18:30',
   'F019: the form mounts with the Later time edited during the save.');
  replacement.elements.later.value='2030-12-02T18:30';
  replacement.requestSubmit(laterButton);
  check(laterButton.disabled && posts===1,'F002: a remounted form cannot submit a second save.');
  releaseSave();
  await until(()=>!replacement.dataset.sending,'the failed save to finish',replacement);
  check(replacement.elements.feedback.value==='Edit during save.',
   'F019: a failed save keeps text edited before the form remounted.');
  const previousVersion=document.querySelector('[data-review]').dataset.version;
  await fetch('/advance-again');
  location.hash=new URLSearchParams({item:'page-owner/C4'});
  check(await ready('page-owner/C4'),'The newer card body loads.');
  await until(()=>document.querySelector('[data-review]').dataset.version!==previousVersion,
   'the index refresh after the body loads');
  check(row('page-owner/C4').dataset.title==='a newer summary','F004: the body version refreshes the summary.');
  const openAddress=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams({show:'open',item:'page-owner/C4'});
  await openAddress;
  await until(()=>document.querySelector('[data-review]').dataset.show==='open' && form('page-owner/C4'),'the Open form');
  conflict='/resolve-conflict';
  const stale=form('page-owner/C4');
  const conflictVersion=document.querySelector('[data-review]').dataset.version;
  stale.requestSubmit(stale.querySelector('button[value=works]'));
  await until(()=>!row('page-owner/C4') && !stale.dataset.sending &&
   document.querySelector('[data-review]').dataset.version!==conflictVersion,
   'the refreshed view without the completed row',stale);
  const conflictNotice=document.querySelector('[data-hidden-action-status]');
  check(document.querySelector('[data-review]').dataset.show==='open' &&
   !conflictNotice.hidden && conflictNotice.textContent.includes('This item is already Done.'),
   'F015: HTTP 409 keeps the reason when the completed item leaves Review.');
  await open(DRAFT_KEY);
  conflict='/revise-conflict';
  const oldQuestion=form(DRAFT_KEY),questionRevision=oldQuestion.elements.revision.value;
  check(recordDetails(row(DRAFT_KEY)).querySelector('input[name=choice]:checked')?.value===choiceValue,
   'The question has a selected choice before the save.');
  oldQuestion.requestSubmit(oldQuestion.querySelector('button[value=answer]'));
  await until(()=>form(DRAFT_KEY)?.elements.revision.value===REVISED_QUESTION &&
   !form(DRAFT_KEY).querySelector('button[value=answer]').disabled,'the revised question form');
  const newQuestion=form(DRAFT_KEY);
  check(newQuestion.elements.revision.value!==questionRevision,'Revision recovery loads a different revision.');
  check(newQuestion.elements.feedback.value==='Keep these draft words.','Revision recovery keeps the feedback.');
  check(newQuestion.elements.later.value==='2030-12-01T18:30','Revision recovery keeps the Later time.');
  check(recordDetails(row(DRAFT_KEY)).querySelector('input[name=choice]:checked')?.value===choiceValue,
   'Revision recovery keeps the selected choice.');
  check(newQuestion.querySelector('[data-send-status]').textContent.includes('This item changed.'),
   'Revision recovery shows the reason for refusal.');
  conflict='/revise-conflict-again';holdVersion=true;
  newQuestion.requestSubmit(newQuestion.querySelector('button[value=answer]'));
  await until(()=>releaseVersion,'the first recovery reload');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await until(()=>form(DRAFT_KEY)?.elements.revision.value===REVISED_AGAIN,'the revision from the second refresh');
  const latestQuestion=form(DRAFT_KEY);
  check(!latestQuestion.querySelector('button[value=answer]').disabled,
   'A newer revision has usable actions while the first reload is pending.');
  latestQuestion.elements.feedback.value='Keep these newer words.';
  latestQuestion.elements.feedback.dispatchEvent(new Event('input'));
  releaseVersion();
  await frame();await frame();
  check(latestQuestion.elements.feedback.value==='Keep these newer words.',
   'The first recovery preserves words typed into the newer revision.');
  conflict='/stall-conflict';timeoutVersion=true;
  latestQuestion.requestSubmit(latestQuestion.querySelector('button[value=answer]'));
  await until(()=>stalled,'the stalled recovery request');
  check(form(DRAFT_KEY).querySelector('button[value=answer]').disabled,
   'The stale revision is locked during its reload.');
  await until(()=>!form(DRAFT_KEY).querySelector('button[value=answer]').disabled,'the reload deadline',undefined,8000);
  check(document.querySelector('[data-page-status]').dataset.state==='Failed' &&
   document.querySelector('[data-review]').dataset.requestTimeoutSeconds==='3',
   'The configured recovery deadline releases the stale revision.');
  check(form(DRAFT_KEY).elements.feedback.value==='Keep these newer words.',
   'A timed-out reload keeps the current words.');
  const remountDraft=async()=>{
   const previous=form(DRAFT_KEY);
   close();
   document.activeElement.blur();
   scrollTo(0,document.documentElement.scrollHeight);
   await until(()=>!row(DRAFT_KEY),'the closed question to leave the window');
   await open(DRAFT_KEY);
   check(form(DRAFT_KEY)!==previous,'Review mounts a replacement form.');
   return form(DRAFT_KEY);
  };
  for(const [route,revision,words] of [
   ['/revise-conflict',REVISED_QUESTION,''],
   ['/revise-conflict-again',REVISED_AGAIN,'Words typed into the replacement form.']
  ]){
   conflict=route;holdVersion=true;releaseVersion=null;
   const staleQuestion=form(DRAFT_KEY);
   staleQuestion.requestSubmit(staleQuestion.querySelector('button[value=answer]'));
   await until(()=>releaseVersion,'the held recovery reload');
   dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
    await until(()=>form(DRAFT_KEY)?.elements.revision.value===revision,'the newer question form');
   const edited=words ? await remountDraft() : form(DRAFT_KEY);
   edited.elements.feedback.focus();
   edited.elements.feedback.value=words;
   edited.elements.feedback.dispatchEvent(new Event('input'));
   const replacement=await remountDraft();
   check(replacement.elements.feedback.value===words,'The replacement starts with the current words.');
   const status=replacement.querySelector('[data-send-status]');
   status.textContent='';
   releaseVersion();
   await until(()=>status.textContent.includes('This item changed.'),'the first recovery to finish',status);
   check(replacement.elements.feedback.value===words,'The first recovery keeps the current words.');
   check((await remountDraft()).elements.feedback.value===words,'The stored draft keeps the current words.');
  }
  conflict='/remove-conflict';
  await open('page-owner/C6');
  const removed=form('page-owner/C6');
  removed.elements.later.value='2030-12-02T18:30';
  const removalVersion=document.querySelector('[data-review]').dataset.version;
  removed.requestSubmit(removed.querySelector('button[value=later]'));
  await until(()=>!row('page-owner/C6') && document.querySelector('[data-review]').dataset.version!==removalVersion,
   'the refreshed view without the removed row');
  const notice=document.querySelector('[data-hidden-action-status]');
  check(notice && !notice.hidden && notice.textContent.includes('This item is no longer on the list.'),
   'F029: the page retains the conflict reason when refresh removes the card.');
 }catch(error){failures.push(String(error), ...window.coldErrors);}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        results: list[list[str]] = []
        requested_bodies: list[str] = []
        complete = threading.Event()
        current = [initial]
        with patch.object(review_items,'read',return_value=[
                replace(item,title='A newer summary') if item.key=='page-owner/C4' else item
                for item in changed]):
            newer = review_page.build_snapshot(self.root)
        with patch.object(review_items, 'read', return_value=[
                item for item in newer.records.values() if item.key != 'page-owner/C4']):
            accepted = review_page.build_snapshot(self.root)
        with patch.object(review_items, 'read', return_value=[
                replace(item, title='Which layout fits the release?') if item.key == key else item
                for item in accepted.records.values()]):
            revised = review_page.build_snapshot(self.root)
        with patch.object(review_items, 'read', return_value=[
                replace(item, title='Which layout fits the next release?') if item.key == key else item
                for item in revised.records.values()]):
            revised_again = review_page.build_snapshot(self.root)
        with patch.object(review_items, 'read', return_value=[
                item for item in revised_again.records.values() if item.key != 'page-owner/C6']):
            deleted = review_page.build_snapshot(self.root)
        probe = (probe.replace('WAIT_FOR_BROWSER_STATE', WAIT_FOR_BROWSER_STATE)
                 .replace('DRAFT_KEY', json.dumps(key)).replace('EXPECTED_TOKEN', json.dumps(expected_token))
                 .replace('EXPECTED_REVISION', json.dumps(expected_revision))
                 .replace('REVISED_QUESTION', json.dumps(review_page.record_revision(revised.records[key], revised.assets)))
                 .replace('REVISED_AGAIN', json.dumps(review_page.record_revision(revised_again.records[key], revised_again.assets))))
        counts = {'version':0,'index':0}
        spy = """<script>window.coldErrors=[];
addEventListener('error',event=>window.coldErrors.push(event.message));
addEventListener('unhandledrejection',event=>window.coldErrors.push(String(event.reason)));
const measuredFetch=fetch;window.fetch=async(url,options)=>{
 const fields=new URL(url,location.href).searchParams;
 if(fields.has('index') && fields.get('for-you')!=='1') throw new Error('The Review index request must include For you.');
 if(fields.get('body')==='page-owner/C399' && window.coldBodyMounts===undefined){
  window.coldBodyMounts=document.querySelectorAll('[data-list-item]').length;
  const response=measuredFetch(url,options);
  await new Promise(resolve=>{
   window.releaseColdBody=resolve;
   document.dispatchEvent(new Event('fixture-progress'));
  });
  return response;
 }
 return measuredFetch(url,options);
};</script>"""
        markup = (replace(initial, refresh_seconds=0).render(show='all').decode().replace('<head>','<head>'+spy,1)
                  .replace('</body>',probe+'<img hidden src="/complete"></body>'))

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                route=urlsplit(self.path)
                fields=parse_qs(route.query)
                snapshot=current[0]
                content_type='text/html; charset=utf-8'
                if route.path=='/complete':
                    _ = complete.wait(TEST_HANG_GUARD_SECS)
                    body=b''
                elif route.path=='/advance':
                    current[0]=fresh
                    body=b''
                elif route.path=='/advance-again':
                    current[0]=newer
                    body=b''
                elif route.path=='/resolve-conflict':
                    current[0]=accepted
                    body=b''
                elif route.path=='/revise-conflict':
                    current[0]=revised
                    body=b''
                elif route.path=='/revise-conflict-again':
                    current[0]=revised_again
                    body=b''
                elif route.path=='/stall-conflict':
                    body=b''
                elif route.path=='/remove-conflict':
                    current[0]=deleted
                    body=b''
                elif route.path=='/reset-counts':
                    counts.update(version=0,index=0)
                    body=b''
                elif route.path=='/counts':
                    body=json.dumps(counts).encode()
                    content_type='application/json'
                elif 'body' in fields:
                    requested_bodies.append(fields['body'][0])
                    body=snapshot.body(fields['body'][0])
                elif 'index' in fields:
                    counts['index']+=1
                    body=snapshot.index_response()
                    content_type='application/json'
                elif 'version' in fields:
                    counts['version']+=1
                    body=json.dumps({'version':snapshot.version,'facts':snapshot.facts()}).encode()
                    content_type='application/json'
                else:
                    body=markup.encode()
                self.send_response(200)
                self.send_header('Content-Type',content_type)
                if 'body' in fields:
                    self.send_header('X-Review-Version',snapshot.version)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                _ = self.wfile.write(body)

            def do_POST(self) -> None:
                results.append(cast(list[str],json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                complete.set()
                self.send_response(204)
                self.end_headers()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(('127.0.0.1',0),Probe) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory(dir=self.fixture.temporary) as profile:
                    result=run_process_group(['firefox','--headless','--no-remote','--profile',profile,
                        '--window-size','1440,900','--screenshot',str(self.fixture.temporary/'window.png'),
                        f'http://127.0.0.1:{server.server_port}/#item=page-owner%2FC399'],
                        timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode,0,result.stderr)
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)
                self.assertFalse(thread.is_alive(), 'The windowed Review server did not stop.')
        self.assertEqual(results,[[]])
        self.assertEqual(
            set(requested_bodies),
            {'page-owner/C399', 'page-owner/C381', key, 'page-owner/C2', 'page-owner/C4', 'page-owner/C6'},
            'Only opened keys request a body.',
        )

    def browser_snapshot(self, records: list[review_items.ReviewItem]) -> tuple[str, dict[str, object]]:
        """Render the page for these records, with the data its fetches read."""
        with patch.object(review_items, 'read', return_value=records):
            built = review_page.build_snapshot(self.root)
        return built.render(show='all').decode(), {
            'version': built.version, 'facts': built.facts(), 'index': built.index_response().decode(),
            'bodies': {key: built.body(key).decode() for key in built.records}}

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_loads_the_new_card_before_or_after_the_index(self) -> None:
        _ = self.question()
        items = review_items.read(self.root)
        question = next(item for item in items if item.type == review_items.ReviewType.QUESTION)
        change = next(item for item in items if item.type == review_items.ReviewType.CHANGE)
        records = [replace(question, key='change-1'),
                   *(replace(change, key=f'change-{number}', id=f'C{number}') for number in range(2, 5))]
        with patch.object(review_page, 'user_zone', return_value=ZoneInfo('America/New_York')):
            page, initial = self.browser_snapshot(records)
        snapshots = {'initial': initial}
        omitted = {'removed': {'C3'}, 'gone': {'C3', 'C4'}, 'closed': {'C2', 'C3', 'C4'}}
        with patch.object(review_page, 'user_zone', return_value=ZoneInfo('UTC')):
            for version in ('fresh', 'third', 'fourth', 'fifth', 'sixth', 'removed', 'gone', 'closed'):
                _, snapshots[version] = self.browser_snapshot([
                    replace(record, title=f'{version} {record.id}') for record in records
                    if record.id not in omitted.get(version, ())])
        page = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', page, count=1)
        steps = r"""
  const requests=[],content=document.querySelector('[data-review]'),fixtureFetch=window.fetch;
  const response=(key,name)=>key in snapshots[name].bodies ? new Response(
   snapshots[name].bodies[key]+'<span data-fixture-body="'+name+'"></span>',
   {headers:{'X-Review-Version':snapshots[name].version}}) :
   new Response(null,{status:404,headers:{'X-Review-Version':snapshots[name].version}});
  let holdBody='',holdIndex=false,releaseBody,releaseIndex;
  window.fetch=async(url,options)=>{
   const query=new URL(url,location.href).searchParams,body=query.get('body');
   if(body){
    requests.push(body);
    if(holdBody===body) return new Promise(resolve=>{releaseBody=name=>resolve(response(body,name));});
    return response(body,shown);
   }
   if(query.has('index') && holdIndex)
    return new Promise(resolve=>{releaseIndex=name=>resolve(new Response(snapshots[name].index));});
   return fixtureFetch(url,options);
  };
  const form=number=>recordDetails(row(number))?.querySelector('[data-review-form]');
  const marker=number=>recordDetails(row(number))?.querySelector('[data-fixture-body]')?.dataset.fixtureBody;
  const reads=number=>requests.filter(key=>key==='change-'+number).length;
  const at=name=>content.dataset.version===snapshots[name].version;
  const reload=()=>dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  const click=async(number,ready)=>{
   const start=performance.now();
   summary(number).querySelector('.list-toggle').click();
   while(!ready()){
    if(performance.now()-start>=1000) throw new Error('Card '+number+' took more than one second.');
    await new Promise(resolve=>requestAnimationFrame(resolve));
   }
   check(performance.now()-start<1000,'The card result shows in under one second.');
  };
  shown='fresh';holdIndex=true;
  await click(1,()=>marker(1)==='fresh');
  check(at('initial'),'The new body shows before the index arrives.');
  const draft=form(1),words=draft.elements.feedback;
  words.value='Keep these words.';words.dispatchEvent(new Event('input',{bubbles:true}));
  await until(()=>releaseIndex,'the guarded index request');
  const laterCases={
   initial:{zone:'America/New_York',firstTime:'10:15',disabled:false},
   fresh:{zone:'UTC',firstTime:'15:15',disabled:true},
  };
  const openLater=name=>{
   const {zone,firstTime,disabled}=laterCases[name],now=Date.now;
   Date.now=()=>Date.parse('2026-11-30T15:00:00Z');
   try{
    draft.elements.later.value='2026-11-30T16:00';
    draft.querySelector('[data-later-toggle]').click();
    draft.querySelector('[data-later-choose]').click();
    check(content.dataset.zone===zone && draft.querySelector('[data-later-times]').firstChild.textContent===firstTime,
     'The retained Later picker offers times in '+zone+'.');
    const time=draft.querySelector('[data-later-time]');
    time.value='12:00';time.dispatchEvent(new Event('input',{bubbles:true}));
    check(draft.querySelector('[value="later"]').disabled===disabled,
     'Later checks whether noon is in the future in '+zone+'.');
   }finally{Date.now=now;}
  };
  openLater('initial');
  check(dateTimePickerOpen(draft),'The date picker opens while the index request waits.');
  holdIndex=false;releaseIndex('fresh');releaseIndex=null;
  await frame();
  check(at('initial') && form(1)===draft && dateTimePickerOpen(draft),
   'The pending index leaves the open date picker in place.');
  closeDateTimePicker(draft.querySelector('[data-later-menu]'),false);
  words.focus();words.setSelectionRange(2,6);
  reload();
  await until(()=>at('fresh') && marker(1)==='fresh','the matching index');
  check(reads(1)===1 && recordIsOpen(row(1)),'A matching index reuses the first body and keeps the card open.');
  check(form(1)===draft,'The matching index keeps the same form.');
  const restored=form(1).elements.feedback;
  check(restored.value===words.value && document.activeElement===restored &&
   restored.selectionStart===2 && restored.selectionEnd===6,'The refresh keeps the draft, focus, and selection.');
  openLater('fresh');
  closeDateTimePicker(draft.querySelector('[data-later-menu]'),false);

  shown='third';holdBody='change-2';
  summary(2).querySelector('.list-toggle').click();
  await until(()=>releaseBody,'the pending card request');
  reload();
  await until(()=>at('third'),'the index that arrives first');
  holdBody='';releaseBody('third');releaseBody=null;
  await until(()=>marker(2)==='third','the original card response');
  check(reads(2)===1,'An index that arrives first keeps the pending body request.');

  shown='fourth';holdIndex=true;
  await click(3,()=>marker(3)==='fourth');
  await until(()=>releaseIndex,'the index request after the fourth body');
  check(at('third'),'A third version can arrive after the card already shows.');
  shown='fifth';holdIndex=false;releaseIndex('fifth');releaseIndex=null;
  await until(()=>at('fifth') && marker(3)==='fifth','the card from the later index');
  check(reads(3)===2,'The later index requests its body once.');

  shown='sixth';holdBody='change-4';
  summary(4).querySelector('.list-toggle').click();
  await until(()=>releaseBody,'the held reply from a different snapshot');
  reload();
  await until(()=>at('sixth'),'the newer index');
  let staleDisplayed=false;
  const bodyChanges=new MutationObserver(()=>{if(marker(4)==='fourth') staleDisplayed=true;});
  bodyChanges.observe(content,{childList:true,subtree:true});
  holdBody='';releaseBody('fourth');releaseBody=null;
  await until(()=>marker(4)==='sixth','the body that matches the newer index');
  bodyChanges.disconnect();
  check(!staleDisplayed,'The old body never displays beside the newer index.');
  check(reads(4)===2,'The refresh replaces the older reply with one request for the current body.');

  holdBody='change-1';
  summary(1).querySelector('.list-toggle').click();
  await until(()=>releaseBody,'the body for the previous click');
  await click(2,()=>marker(2)==='sixth');
  const finishPrevious=releaseBody;releaseBody=null;holdBody='';
  finishPrevious('fourth');await frame();
  check(recordIsOpen(row(2)) && marker(2)==='sixth' && !recordIsOpen(row(1)),
   'The response for the previous click leaves the current card open.');

  shown='removed';holdIndex=true;
  const notice=document.querySelector('[data-missing-item]');
  await click(3,()=>!notice.hidden);
  check(at('sixth') && !document.querySelector('[data-record-inline-view]'),
   'The missing card closes before the index arrives.');
  await until(()=>releaseIndex,'the index after the missing response');
  holdIndex=false;releaseIndex('removed');
  await until(()=>at('removed'),'the index without the item');
  check(!row(3) && !notice.hidden && notice.textContent==='This review is no longer on the Review page.',
   'The page keeps the missing item notice after the refresh.');

  shown='gone';holdBody='change-4';
  summary(4).querySelector('.list-toggle').click();
  await until(()=>releaseBody,'the pending request for the removed card');
  reload();
  await until(()=>at('gone'),'the index that removes the loading card');
  check(!notice.hidden && !document.querySelector('[data-record-inline-view]'),
   'An index that removes the loading card closes it and shows the notice.');
  holdBody='';releaseBody('gone');await frame();
  check(reads(4)===3 && !notice.hidden,'The late missing response leaves the notice in place.');

  await click(2,()=>marker(2)==='gone');
  shown='closed';reload();
  await until(()=>at('closed'),'the index that removes the loaded card');
  check(!notice.hidden && !document.querySelector('[data-record-inline-view]'),
   'An index that removes a loaded card closes it and shows the notice.');
"""
        self.assertEqual(firefox_probe_results(snapshot_probe_page(page, snapshots, steps)), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_loads_the_new_body_while_another_send_is_held(self) -> None:
        _ = write_config_fixture(self.fixture.temporary / 'config/review_links.conf', 'undo_seconds=30\n')
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        first = replace(change, key='change-1', id='C1')
        second = replace(change, key='change-2', id='C2')
        page, initial = self.browser_snapshot([first, second])
        _, fresh = self.browser_snapshot([replace(first, title='The updated Change'), second])
        page = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', page, count=1)
        steps = UNDO_ACTION_SCRIPT + r"""
  const undo=fixtureUndo();
  const fixtureFetch=window.fetch,content=document.querySelector('[data-review]');
  let versionReads=0;
  window.fetch=(url,options)=>{
   const query=new URL(url,location.href).searchParams;
   if(!query.has('body') && !query.has('index') && query.has('version')) versionReads++;
   return fixtureFetch(url,options);
  };
  summary(2).querySelector('.list-toggle').click();
  const form=await readyReviewCard(row(2));
  form.querySelector('[value="validate"]').click();
  const toast=()=>window.top.document.querySelector('[data-send-undo]');
  await until(()=>toast()?.querySelector('button'),'the held send');
  const held=toast();
  shown='fresh';
  summary(1).querySelector('.list-toggle').click();
  await readyReviewCard(row(1));await frame();
  check(content.dataset.version===snapshots.initial.version && versionReads===0,
   'The held send blocks the index refresh.');
  check(toast()===held && undo.posts.length===1,'The new body leaves the other send held.');
  await undo.undo();
  await until(()=>content.dataset.version===snapshots.fresh.version,'the refresh after Undo');
  check(recordIsOpen(row(1)) && undo.posts.length===1,'Undo keeps the other card open and sends no second action.');
"""
        self.assertEqual(firefox_probe_results(snapshot_probe_page(
            page, {'initial': initial, 'fresh': fresh}, steps)), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_loads_a_saved_result_during_a_body_version_difference(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        first = replace(change, key='change-1', id='C1', state=review_items.ReviewState.OPEN)
        page, initial = self.browser_snapshot([first])
        page = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', page, count=1)
        steps = r"""
  const fixtureFetch=window.fetch,reads=[],content=document.querySelector('[data-review]');
  const validating=VALIDATING,action=validating ? 'question' : 'feedback';
  let reply;
  window.fetch=(url,options={})=>{
   const query=new URL(url,location.href).searchParams;
   if(options.method==='POST')
    return new Promise(resolve=>{reply=()=>resolve(Response.json({answer:'saved',version:snapshots.fresh.version,
     state:validating ? 'Validating' : 'Waiting',detail_revision:'saved',delivery_pending:false}));});
   if(query.has('saved')){
    reads.push(query.get('saved'));
    const body=snapshots.fresh.bodies['change-1']+'<p data-saved-fixture>The saved result.</p>';
    return Promise.resolve(new Response(body,{headers:{'X-Review-Version':snapshots.fresh.version}}));
   }
   if(!query.has('body') && query.has('version')) return new Promise(()=>{});
   return fixtureFetch(url,options);
  };
  shown='fresh';
  summary(1).querySelector('.list-toggle').click();
  const form=await readyReviewCard(row(1));
  form.elements.feedback.value='The submitted draft.';
  form.elements.feedback.dispatchEvent(new Event('input',{bubbles:true}));
  await until(()=>!document.querySelector('[data-record-motion]'),'the opening animation');
  const button=form.querySelector('[value="'+action+'"]');
  if(validating)
   check(content.dataset.version===snapshots.initial.version && row(1).dataset.state==='Validating' &&
    summary(1).querySelector('.list-state').textContent==='Validating' && !button.disabled,
    'The new body enables Ask with the Validating state before the index arrives.');
  form.requestSubmit(button);
  await until(()=>reply,'the submitted action');
  if(validating)
   check(!document.querySelector('[data-record-motion-key="change-1"]') && recordIsOpen(row(1)),
    'Ask leaves the Validating card open while the send waits.');
  reply();
  await until(()=>recordDetails(row(1))?.querySelector('[data-saved-fixture]'),'the saved detail');
  check(reads.join()==='1' && content.dataset.version===snapshots.initial.version && recordIsOpen(row(1)),
   'The saved result uses its own body request while the index still has the earlier version.');
"""
        for state in (review_items.ReviewState.OPEN, review_items.ReviewState.VALIDATING):
            with self.subTest(state=state):
                validating = state == review_items.ReviewState.VALIDATING
                _, fresh = self.browser_snapshot([replace(first, title='The updated Change', state=state,
                    actions=('question',) if validating else first.actions)])
                self.assertEqual(firefox_probe_results(snapshot_probe_page(
                    page, {'initial': initial, 'fresh': fresh},
                    steps.replace('VALIDATING', json.dumps(validating)))), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_refresh_keeps_the_open_row_at_the_same_pixel(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        assert isinstance(change.source, review_items.ChangeSource)
        source = change.source

        def record(number: int, title: str, proof: str = 'The short proof.',
                   level: str = urgency.NORMAL) -> review_items.ReviewItem:
            return replace(change, key=f'change-{number}', id=f'C{number}', title=title, source_unit=change.owner,
                           state=review_items.ReviewState.OPEN, urgency=urgency.Urgency(level),
                           source=replace(source, row={**source.row, 'What you see': proof}))

        # The row above loses its urgency line; the open row takes a new title and a longer proof.
        initial_records = [record(number, f'Review change {number}') for number in range(1, 31)]
        initial_records[13] = record(14, 'The row above blocks work', level=urgency.BLOCKS)
        fresh_records = [*initial_records]
        fresh_records[13] = record(14, 'Short title')
        fresh_records[14] = record(15, 'The refreshed open title',
                                   'The refreshed proof. ' + 'The producer reports another completed check. ' * 20)

        (page, initial), (_, fresh) = self.browser_snapshot(initial_records), self.browser_snapshot(fresh_records)
        self.assertNotEqual(initial['version'], fresh['version'])
        steps = r"""
  frameElement.style.width='1100px';frameElement.style.height='700px';
  await frame();
  row(15).querySelector('.list-toggle').click();
  const form=await readyReviewCard(row(15));
  const words=form.elements.feedback;
  words.value='Keep these words.';words.dispatchEvent(new Event('input',{bubbles:true}));
  words.focus({preventScroll:true});words.setSelectionRange(2,5);
  const currentTop=()=>summary(15).getBoundingClientRect().top;
  const nextWindowRedraw=()=>new Promise(resolve=>{
   const observer=new MutationObserver(()=>{observer.disconnect();resolve();});
   observer.observe(document.querySelector('[data-window-before]'),{childList:true});
  });
  const windowRedrawn=nextWindowRedraw();
  scrollBy({top:currentTop()-180,behavior:'instant'});
  await windowRedrawn;
  const top=currentTop(),scrolled=scrollY,heightAbove=summary(14).getBoundingClientRect().height;
  const heightOpen=row(15).getBoundingClientRect().height;
  check(scrolled>0 && summary(14).getBoundingClientRect().top<top && top>0 && top<innerHeight,
   'The open row is below the top and below the changed row.');
  // Delay the version response until the next virtual table redraw.
  const redrawBeforeVersion=nextWindowRedraw(),snapshotFetch=window.fetch;
  window.fetch=async(url,options)=>{
   if(new URL(url,location.href).searchParams.get('version')==='1'){
    window.fetch=snapshotFetch;
    await redrawBeforeVersion;
   }
   return snapshotFetch(url,options);
  };
  refresh();
  await until(()=>recordDetails(row(15))?.textContent.includes('The refreshed proof.'),'the refreshed open row');
  await frame();
  check(summary(14).getBoundingClientRect().top<top,'The changed row stays above the open row.');
  // Firefox lays out in 1/60 px units, so a match is closer than 0.1 px.
  const checkTop=label=>check(recordIsOpen(row(15)) && Math.abs(currentTop()-top)<0.1,
   label+' Expected '+top+' px. Found '+currentTop()+' px.');
  checkTop('The open row keeps its position after the refresh.');
  for(let count=0;count<5;count++) await frame();
  checkTop('The open row keeps its position after the window redraws.');
  check(summary(14).getBoundingClientRect().height<heightAbove && summary(14).textContent.includes('Short title'),
   'The row above shows its new title and is shorter. Before '+heightAbove+' px. After '+
   summary(14).getBoundingClientRect().height+' px.');
  check(row(15).getBoundingClientRect().height>heightOpen && summary(15).textContent.includes('The refreshed open title'),
   'The open row shows its new title and new proof and is taller.');
  const field=recordDetails(row(15)).querySelector('[data-review-form]')?.elements.feedback;
  check(field?.value==='Keep these words.' && document.activeElement===field &&
   field.selectionStart===2 && field.selectionEnd===5,'The draft, its focus and its selection stay.');
"""
        page_bytes = snapshot_probe_page(page, {'initial': initial, 'fresh': fresh}, steps)
        self.assertEqual(firefox_probe_results(page_bytes), [[]])

    def long_log_records(self, extra: int = 0) -> list[review_items.ReviewItem]:
        """Return three change rows; change-2 has a log of 40 entries plus extra newer entries."""
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        start = datetime(2026, 11, 1, 9, tzinfo=ZoneInfo('UTC'))
        log = tuple(review_events.ReviewLog(start + timedelta(minutes=index), change.owner,
                                            f'Log entry {index}. The producer reports a completed check.')
                    for index in range(40 + extra))
        return [replace(change, key=f'change-{number}', id=f'C{number}', title=f'Review change {number}',
                        source_unit=change.owner, state=review_items.ReviewState.OPEN,
                        log=log if number == 2 else change.log)
                for number in range(1, 4)]

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_review_log_scrolls_in_its_own_area(self) -> None:
        _ = write_config_fixture(self.fixture.temporary / 'config/review_links.conf', 'list_log_max_height_rem=9\n')
        page, initial = self.browser_snapshot(self.long_log_records())
        self.assertIn('style="--list-log-max-height:9rem"', page)
        steps = r"""
  frameElement.style.width='1440px';frameElement.style.height='900px';
  await frame();
  row(2).querySelector('.list-toggle').click();
  await readyReviewCard(row(2));
  const rem=parseFloat(getComputedStyle(document.documentElement).fontSize);
  for(const width of [1440,600]){
   frameElement.style.width=width+'px';
   await frame();
   const panel=recordDetails(row(2)).querySelector('.list-panel'),left=panel.firstElementChild.getBoundingClientRect();
   const log=panel.querySelector('.list-log-scroll'),box=log.getBoundingClientRect();
   check(getComputedStyle(log).overflowY==='auto' && log.scrollHeight>log.clientHeight,
    width+' px: The log scrolls in its own area.');
   if(width>960) check(box.left>=left.right && box.bottom<=left.bottom+1,
    width+' px: The log sits beside the details and ends with them. Log bottom '+box.bottom+' px. Details bottom '+
    left.bottom+' px.');
   else check(Math.abs(log.clientHeight-9*rem)<1,
    width+' px: The log is 9rem tall from the config. Found '+log.clientHeight+' px.');
  }
"""
        self.assertEqual(firefox_probe_results(snapshot_probe_page(page, {'initial': initial}, steps)), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_review_refresh_keeps_the_open_log_position(self) -> None:
        (page, initial), (_, fresh) = (self.browser_snapshot(self.long_log_records()),
                                       self.browser_snapshot(self.long_log_records(extra=1)))
        self.assertNotEqual(initial['version'], fresh['version'])
        for width in (1440, 600):
            with self.subTest(width=width):
                steps = r"""
  frameElement.style.width='WIDTHpx';frameElement.style.height='800px';
  await frame();
  row(2).querySelector('.list-toggle').click();
  await readyReviewCard(row(2));
  const openLog=()=>recordDetails(row(2))?.querySelector('.list-log-scroll');
  const log=openLog(),entries=log.querySelectorAll('[data-log-index]').length;
  log.scrollTop=Math.round(log.scrollHeight/2);
  await frame();
  const logTop=log.getBoundingClientRect().top;
  const first=[...log.querySelectorAll('[data-log-index]')].find(entry=>entry.getBoundingClientRect().bottom>logTop);
  const index=first.dataset.logIndex,offset=first.getBoundingClientRect().top-logTop;
  check(log.scrollTop>0,'The log scrolls before the refresh.');
  refresh();
  await until(()=>openLog()?.querySelectorAll('[data-log-index]').length>entries,'the new log entry');
  await frame();
  const freshLog=openLog(),entry=freshLog.querySelector('[data-log-index="'+index+'"]');
  const found=entry.getBoundingClientRect().top-freshLog.getBoundingClientRect().top;
  check(freshLog.scrollTop>0 && Math.abs(found-offset)<1,
   'The log keeps its first visible entry at the same place. Expected '+offset+' px, found '+found+' px.');
""".replace('WIDTH', str(width))
                markup = snapshot_probe_page(page, {'initial': initial, 'fresh': fresh}, steps)
                self.assertEqual(firefox_probe_results(markup), [[]])

    def assert_saved_detail_keeps_the_open_log_position(self, reply: str) -> None:
        """Save feedback on the open row with a scrolled log; the new detail has one more log entry."""
        (page, initial), (_, fresh) = (self.browser_snapshot(self.long_log_records()),
                                       self.browser_snapshot(self.long_log_records(extra=1)))
        steps = r"""
  frameElement.style.width='1440px';frameElement.style.height='800px';
  await frame();
  const pageFetch=window.fetch;
  window.fetch=async(url,options={})=>{
   if(options.method!=='POST') return pageFetch(url,options);
   shown='fresh';
   return Response.json({answer:'saved',version:snapshots.fresh.version,delivery_pending:false,state:'Waiting',REPLY});
  };
  row(2).querySelector('.list-toggle').click();
  const form=await readyReviewCard(row(2));
  const openLog=()=>recordDetails(row(2))?.querySelector('.list-log-scroll');
  const log=openLog(),entries=log.querySelectorAll('[data-log-index]').length;
  log.scrollTop=Math.round(log.scrollHeight/2);
  await frame();
  const logTop=log.getBoundingClientRect().top;
  const first=[...log.querySelectorAll('[data-log-index]')].find(entry=>entry.getBoundingClientRect().bottom>logTop);
  const index=first.dataset.logIndex,offset=first.getBoundingClientRect().top-logTop;
  check(log.scrollTop>0,'The log scrolls before the save.');
  form.elements.feedback.value='Check the log.';
  form.elements.feedback.dispatchEvent(new Event('input',{bubbles:true}));
  form.requestSubmit(form.querySelector('[value="feedback"]'));
  await until(()=>openLog()?.querySelectorAll('[data-log-index]').length>entries,'the saved log entry');
  await frame();
  const savedLog=openLog(),entry=savedLog.querySelector('[data-log-index="'+index+'"]');
  const found=entry.getBoundingClientRect().top-savedLog.getBoundingClientRect().top;
  check(savedLog!==log,'The saved detail replaces the log.');
  check(savedLog.scrollTop>0 && Math.abs(found-offset)<1,
   'The log keeps its first visible entry at the same place. Expected '+offset+' px, found '+found+' px.');
""".replace('REPLY', reply)
        markup = snapshot_probe_page(page, {'initial': initial, 'fresh': fresh}, steps)
        self.assertEqual(firefox_probe_results(markup), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_review_save_keeps_the_open_log_position(self) -> None:
        self.assert_saved_detail_keeps_the_open_log_position("detail_html:snapshots.fresh.bodies['change-2']")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_review_detail_load_keeps_the_open_log_position(self) -> None:
        # The save response has no detail, so the page loads the saved detail.
        self.assert_saved_detail_keeps_the_open_log_position("detail_revision:'saved'")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_saved_change_feedback_shows_its_state_and_keeps_its_detail(self) -> None:
        # 120 rows let the window drop the rows near the top while row 15 stays open.
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        records = [replace(change, key=f'change-{number}', id=f'C{number}', title=f'Review change {number}',
                           state=review_items.ReviewState.OPEN) for number in range(1, 121)]
        # The feedback hands the change back, so the next server index shows it Waiting.
        waiting = [replace(record, state=review_items.ReviewState.WAITING) if record.key == 'change-15' else record
                   for record in records]
        (page, initial), (_, fresh) = self.browser_snapshot(records), self.browser_snapshot(waiting)
        steps = r"""
  frameElement.style.width='1100px';frameElement.style.height='700px';
  await frame();
  const pageFetch=window.fetch;
  window.fetch=async(url,options={})=>{
   if(options.method!=='POST') return pageFetch(url,options);
   const saved=document.createElement('template');saved.innerHTML=snapshots.initial.bodies['change-15'];
   saved.content.querySelector('form').remove();
   saved.content.querySelector('[data-review-body]').insertAdjacentHTML('afterbegin','<p>The saved words.</p>');
   return Response.json({answer:'saved',version:snapshots.fresh.version,delivery_pending:false,state:'Waiting',
    detail_html:saved.innerHTML});
  };
  row(15).querySelector('.list-toggle').click();
  const form=await readyReviewCard(row(15));
  form.elements.feedback.value='Check the header.';
  form.elements.feedback.dispatchEvent(new Event('input',{bubbles:true}));
  form.requestSubmit(form.querySelector('[value="feedback"]'));
  const loaded=()=>Boolean(recordDetails(row(15))?.querySelector('[data-review-body]')) &&
   !recordDetails(row(15)).querySelector('[data-record-loading]:not([hidden])');
  const shows=()=>loaded() && recordDetails(row(15)).textContent.includes('The saved words.');
  await until(shows,'the saved detail');
  check(recordIsOpen(row(15)),'The row stays open through the save.');
  const content=document.querySelector('[data-review]');
  // A refresh waits for the save to end, so ask again until the new snapshot shows.
  await until(()=>{refresh();return content.dataset.version===snapshots.fresh.version;},'the refresh');
  await frame();
  const state=()=>summary(15)?.querySelector('.list-state')?.textContent;
  check(recordIsOpen(row(15)) && loaded(),'The open row shows its detail after the refresh.');
  check(state()==='Waiting','The open row shows its saved state after the refresh. Found '+state()+'.');
  // Scroll to the page end farther from the open row; the saved row can sort to either end.
  const pageEnd=document.documentElement.scrollHeight;
  const far=row(15).getBoundingClientRect().top+scrollY>pageEnd/2 ? 0 : pageEnd;
  const mountedKeys=()=>[...document.querySelectorAll('[data-list-item]')].map(item=>item.dataset.key);
  const near=mountedKeys();
  scrollTo({top:far,behavior:'instant'});
  for(let count=0;count<3;count++) await frame();
  // The window drops the rows near the open row, but the open row stays mounted, so its detail never reloads.
  const box=row(15)?.getBoundingClientRect();
  check(near.some(key=>!mountedKeys().includes(key)),'The window drops the rows near the open row when the page scrolls away.');
  check(box && (box.bottom<0 || box.top>innerHeight) && recordIsOpen(row(15)) && loaded(),
   'The open row stays mounted with its detail outside the viewport.');
  scrollTo({top:pageEnd-far,behavior:'instant'});
  for(let count=0;count<3;count++) await frame();
  row(15).scrollIntoView({block:'center',behavior:'instant'});
  for(let count=0;count<3;count++) await frame();
  check(recordIsOpen(row(15)) && loaded(),'The open row shows its detail after the page scrolls away and back.');
  check(state()==='Waiting','The open row keeps its saved state after the page scrolls away and back. Found '+state()+'.');
"""
        markup = snapshot_probe_page(page, {'initial': initial, 'fresh': fresh}, steps)
        self.assertEqual(firefox_probe_results(markup), [[]])

    def assert_own_save_keeps_the_open_row(self, opener: str) -> None:
        """Open change-15 with the opener, save feedback, and check the row stays open at the same pixel."""
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        records = [replace(change, key=f'change-{number}', id=f'C{number}', title=f'Review change {number}',
                           state=review_items.ReviewState.OPEN) for number in range(1, 31)]
        waiting = [replace(record, state=review_items.ReviewState.WAITING) if record.key == 'change-15' else record
                   for record in records]
        (page, initial), (_, fresh) = self.browser_snapshot(records), self.browser_snapshot(waiting)
        steps = r"""
  frameElement.style.width='1100px';frameElement.style.height='700px';
  await frame();
  const pageFetch=window.fetch;
  window.fetch=async(url,options={})=>{
   if(options.method!=='POST') return pageFetch(url,options);
   const saved=document.createElement('template');saved.innerHTML=snapshots.initial.bodies['change-15'];
   saved.content.querySelector('form').remove();
   saved.content.querySelector('[data-review-body]').insertAdjacentHTML('afterbegin','<p>The saved words.</p>');
   return Response.json({answer:'saved',version:snapshots.fresh.version,delivery_pending:false,state:'Waiting',
    detail_html:saved.innerHTML});
  };
  OPENER
  await until(()=>row(15) && recordIsOpen(row(15)),'the open row');
  const form=await readyReviewCard(row(15));
  form.elements.feedback.value='Check the header.';
  form.elements.feedback.dispatchEvent(new Event('input',{bubbles:true}));
  const top=row(15).getBoundingClientRect().top;
  form.requestSubmit(form.querySelector('[value="feedback"]'));
  const shows=()=>row(15) && recordDetails(row(15))?.textContent.includes('The saved words.');
  await until(()=>shows() || !row(15),'the saved detail or a removed row');
  for(let count=0;count<5;count++) await frame();
  check(Boolean(row(15)) && recordIsOpen(row(15)),'After the save, the open row is still on the page.');
  check(!row(15) || Math.abs(row(15).getBoundingClientRect().top-top)<1,'After the save, the open row stays at the same pixel.');
""".replace('OPENER', opener)
        markup = snapshot_probe_page(page, {'initial': initial, 'fresh': fresh}, steps)
        self.assertEqual(firefox_probe_results(markup), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_own_save_keeps_a_row_opened_by_an_item_link(self) -> None:
        self.assert_own_save_keeps_the_open_row("location.hash=new URLSearchParams({item:'change-15'});")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_own_save_keeps_a_row_opened_by_a_click(self) -> None:
        self.assert_own_save_keeps_the_open_row("row(15).querySelector('.list-toggle').click();")

    def handed_change_page(self, *, stale: bool = False) -> bytes:
        """Return a probe page whose change-15 feedback save waits for finishSave().

        With stale, the page also serves a 'stale' snapshot: a new version that still shows change-15 Open.
        """
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        records = [replace(change, key=f'change-{number}', id=f'C{number}', title=f'Review change {number}',
                           state=review_items.ReviewState.OPEN) for number in range(1, 31)]
        waiting = [replace(record, state=review_items.ReviewState.WAITING) if record.key == 'change-15' else record
                   for record in records]
        (page, initial), (_, fresh) = self.browser_snapshot(records), self.browser_snapshot(waiting)
        snapshots = {'initial': initial, 'fresh': fresh}
        if stale:
            renamed = [replace(record, title='Renamed change 3') if record.key == 'change-3' else record
                       for record in records]
            snapshots['stale'] = self.browser_snapshot(renamed)[1]
        return snapshot_probe_page(page, snapshots, r"""
  frameElement.style.width='1100px';frameElement.style.height='700px';
  await frame();
  const pageFetch=window.fetch;
  let finishSave;
  window.fetch=async(url,options={})=>{
   if(options.method!=='POST') return pageFetch(url,options);
   const saved=document.createElement('template');saved.innerHTML=snapshots.initial.bodies['change-15'];
   saved.content.querySelector('form').remove();
   saved.content.querySelector('[data-review-body]').insertAdjacentHTML('afterbegin','<p>The saved words.</p>');
   const reply=Response.json({answer:'saved',version:snapshots.fresh.version,delivery_pending:false,state:'Waiting',
    detail_html:saved.innerHTML});
   return new Promise(resolve=>{finishSave=()=>resolve(reply);});
  };
  const follow=async key=>{
   const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
   location.hash=new URLSearchParams({item:key});
   await changed;
   for(let count=0;count<10;count++) await frame();
  };
  const open15=()=>Boolean(row(15)) && recordIsOpen(row(15));
  await follow('change-15');
  await until(open15,'the open row');
  const form=await readyReviewCard(row(15));
  const saved=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{if(!form.dataset.sending){observer.disconnect();resolve();}});
   observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
  });
  form.elements.feedback.value='Check the header.';
  form.elements.feedback.dispatchEvent(new Event('input',{bubbles:true}));
  form.requestSubmit(form.querySelector('[value="feedback"]'));
  AFTER_SUBMIT
""")

    def assert_relink_after_own_save_hides_the_row(self, leave: str) -> None:
        """Save feedback on a linked change, leave its row, then link to it again before the next snapshot."""
        steps = r"""
  finishSave();await saved;
  for(let count=0;count<10;count++) await frame();
  check(open15(),'The own save keeps the linked row open.');
  LEAVE
  for(let count=0;count<10;count++) await frame();
  check(!row(15),'The handed change leaves the page after its row closes.');
  await follow('change-15');
  check(!row(15),'A new link to the handed change shows no row before the next snapshot.');
""".replace('LEAVE', leave)
        markup = self.handed_change_page().decode().replace('AFTER_SUBMIT', steps).encode()
        self.assertEqual(firefox_probe_results(markup), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_relink_after_closing_an_own_save_row_hides_the_handed_change(self) -> None:
        self.assert_relink_after_own_save_hides_the_row("row(15).querySelector('.list-toggle').click();")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_relink_after_following_another_item_hides_the_handed_change(self) -> None:
        self.assert_relink_after_own_save_hides_the_row("await follow('change-3');")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_own_save_row_stays_open_through_a_stale_snapshot(self) -> None:
        steps = r"""
  finishSave();await saved;
  for(let count=0;count<10;count++) await frame();
  check(open15(),'The own save keeps the linked row open.');
  const top=row(15).getBoundingClientRect().top;
  const content=document.querySelector('[data-review]');
  const shownMessage=()=>dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  const stays=label=>{
   check(open15(),'After '+label+', the own row is still open.');
   check(!row(15) || Math.abs(row(15).getBoundingClientRect().top-top)<1,'After '+label+', the own row stays at the same pixel.');
  };
  shown='stale';shownMessage();
  await until(()=>content.dataset.version===snapshots.stale.version,'the stale refresh');
  for(let count=0;count<10;count++) await frame();
  stays('a snapshot that still shows the old change');
  shownMessage();
  for(let count=0;count<30;count++) await frame();
  stays('a second refresh of the same snapshot');
  // The All chip keeps the Waiting row listed; a chip that hides the row closes it by the filter rule.
  document.querySelector('[data-filter-row="show"] [data-filter-value="all"]').click();
  for(let count=0;count<10;count++) await frame();
  stays('a chip click');
"""
        markup = self.handed_change_page(stale=True).decode().replace('AFTER_SUBMIT', steps).encode()
        self.assertEqual(firefox_probe_results(markup), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_own_save_row_stays_open_after_a_link_to_the_same_item(self) -> None:
        steps = r"""
  finishSave();await saved;
  for(let count=0;count<10;count++) await frame();
  check(open15(),'The own save keeps the linked row open.');
  const top=row(15).getBoundingClientRect().top;
  const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams({item:'change-15',show:'all'});
  await changed;
  for(let count=0;count<10;count++) await frame();
  check(open15(),'After a link to the same open item, the own row is still open.');
  check(!row(15) || Math.abs(row(15).getBoundingClientRect().top-top)<1,'After the same-item link, the own row stays at the same pixel.');
"""
        markup = self.handed_change_page().decode().replace('AFTER_SUBMIT', steps).encode()
        self.assertEqual(firefox_probe_results(markup), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_deferred_link_after_change_feedback_waits_for_the_snapshot(self) -> None:
        steps = r"""
  await follow('change-3');
  await follow('change-15');
  finishSave();await saved;
  for(let count=0;count<10;count++) await frame();
  check(!row(15),'The deferred link keeps the handed change hidden until the next snapshot.');
  const content=document.querySelector('[data-review]');
  await until(()=>{refresh();return content.dataset.version===snapshots.fresh.version;},'the refresh');
  await until(()=>Boolean(row(15)),'the handed change after the snapshot');
  check(row(15).dataset.state==='Waiting','The snapshot shows the handed change as Waiting.');
"""
        markup = self.handed_change_page().decode().replace('AFTER_SUBMIT', steps).encode()
        self.assertEqual(firefox_probe_results(markup), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_tab_reaches_adjacent_rows_after_a_distant_scroll(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        records = [replace(change, key=f'page-owner/C{number}', id=f'page-owner/C{number}')
                   for number in range(1, 401)]
        with patch.object(review_items, 'read', return_value=records):
            page = self.fixture.temporary / 'tab-review.html'
            _ = page.write_bytes(review_page.build_snapshot(self.root).render())
        with self.firefox_commands(page) as command:
            cross_window = command('WebDriver:ExecuteAsyncScript', args=[], script=r"""
const done=arguments[0];
const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
void (async()=>{
 scrollTo(0,17000);
 await frame();
 const toggle=document.querySelectorAll('[data-list-item] .list-toggle')[10];
 toggle.focus({preventScroll:true});
 scrollTo(0,0);
 await frame();
 const focusedAbove=document.activeElement===toggle;
 scrollTo(0,document.documentElement.scrollHeight);
 await frame();
 done([focusedAbove,document.activeElement===toggle]);
})();
""")
            self.assertEqual(cross_window, {'value': [True, True]},
                             'F087: focus stays on the same control across both scrollbar jumps.')
            for initial, expected, modifier in ((1, 3, ''), (4, 2, '\ue008')):
                with self.subTest(initial=initial, expected=expected):
                    _ = command('WebDriver:ExecuteAsyncScript', args=[initial], script=r"""
const [number, done] = arguments;
const row = id => document.querySelector('[data-key="page-owner/C'+id+'"]');
scrollTo(0,0);
function focusRow(){
 if(!row(number)){requestAnimationFrame(focusRow);return;}
 row(number).querySelector('.list-toggle').focus();
 scrollTo(0,document.documentElement.scrollHeight);
 requestAnimationFrame(waitForBottom);
}
function waitForBottom(){
 if(row(400)){
  window.resumeReviewFrames=window.requestAnimationFrame;
  window.requestAnimationFrame=()=>0;
  done();
 }
 else requestAnimationFrame(waitForBottom);
}
requestAnimationFrame(focusRow);
""")
                    keys = ([('keyDown', modifier)] if modifier else []) + [
                        ('keyDown', '\ue004'), ('keyUp', '\ue004')]*2 + ([('keyUp', modifier)] if modifier else [])
                    try:
                        _ = command('WebDriver:PerformActions', actions=[{'type':'key', 'id':'keyboard',
                            'actions':[{'type':kind, 'value':key} for kind,key in keys]}])
                    finally:
                        _ = command('WebDriver:ExecuteScript', args=[], script=
                                    'window.requestAnimationFrame=window.resumeReviewFrames;')
                    focused = command('WebDriver:ExecuteScript', args=[], script=
                        'return document.activeElement.closest("[data-list-item]")?.dataset.key;')
                    self.assertEqual(focused, {'value': f'page-owner/C{expected}'},
                                     'F088: two Tab presses before the next frame reach two adjacent rows.')

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_selected_chips_keep_text_contrast_on_hover_and_press(self) -> None:
        review = review_page.build_snapshot(self.root).render().decode()
        list_chip = record_table.chip_row('Show', [record_table.Chip('open', 'Open', 3)], 'open').replace('id="', 'id="fixture-')
        page = self.fixture.temporary / 'chip-contrast.html'
        _ = page.write_text(review.replace('<div data-review ', list_chip + '<div data-review ', 1))
        with self.firefox_commands(page) as command:
            for theme in ('dark', 'light'):
                for selector in ('#filter-show-open', '#fixture-filter-show-open'):
                    position = cast(dict[str, list[int]], command('WebDriver:ExecuteScript',
                        args=[theme, selector], script=r"""
const [theme, selector] = arguments;
document.documentElement.dataset.theme = theme;
const {x, y, width, height} = document.querySelector(selector).getBoundingClientRect();
return [Math.floor(x + width / 2), Math.floor(y + height / 2)];
"""))
                    x, y = position['value']
                    for state in ('hover', 'active'):
                        with self.subTest(theme=theme, selector=selector, state=state):
                            actions: list[dict[str, int | str]] = [
                                {'type': 'pointerMove', 'origin': 'viewport', 'x': x, 'y': y, 'duration': 0}]
                            if state == 'active':
                                actions.append({'type': 'pointerDown', 'button': 0})
                            _ = command('WebDriver:PerformActions', actions=[{
                                'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'}, 'actions': actions}])
                            result = cast(dict[str, float | None], command('WebDriver:ExecuteScript',
                                args=[selector, state], script=r"""
const [selector, state] = arguments;
const chip = document.querySelector(selector);
if (!chip.matches('[aria-pressed=true]:' + state)) throw new Error('The chip state is missing.');
const {color, backgroundColor, boxShadow} = getComputedStyle(chip);
const tint = boxShadow.match(/(?:rgba?|color)\([^)]*\)/g)?.at(-1);
if (!tint) throw new Error('The chip tint is missing.');
const canvas = document.createElement('canvas').getContext('2d');
function luminance(...layers) {
 canvas.clearRect(0, 0, 1, 1);
 for (const layer of layers) {
  canvas.fillStyle = layer;
  canvas.fillRect(0, 0, 1, 1);
 }
 const rgb = Array.from(canvas.getImageData(0, 0, 1, 1).data).slice(0, 3).map(value => {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
 });
 return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
}
const ink = luminance(color), fill = luminance(backgroundColor, tint);
return (Math.max(ink, fill) + 0.05) / (Math.min(ink, fill) + 0.05);
"""))
                            ratio = result['value']
                            assert ratio is not None
                            self.assertGreaterEqual(ratio, 4.5)
                    _ = command('WebDriver:PerformActions', actions=[{
                        'type': 'pointer', 'id': 'mouse', 'parameters': {'pointerType': 'mouse'},
                        'actions': [{'type': 'pointerMove', 'origin': 'viewport', 'x': 2, 'y': 2, 'duration': 0}]}])
                    _ = command('WebDriver:ReleaseActions')

    @contextmanager
    def firefox_commands(self, page: Path) -> Generator[Callable[..., object]]:
        profile = self.fixture.temporary / 'marionette-profile'
        profile.mkdir()
        _ = (profile/'user.js').write_text('user_pref("marionette.port", 0);\n')
        with subprocess.Popen(['firefox', '--headless', '--no-remote', '--profile', str(profile), '--marionette',
                               'about:blank'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              start_new_session=True) as browser:
            try:
                with self.marionette_commands(browser) as command:
                    _ = command('WebDriver:Navigate', url=page.as_uri())
                    yield command
            finally:
                stop_process_group(browser)

    @contextmanager
    def marionette_commands(self, browser: subprocess.Popen[bytes]) -> Generator[Callable[..., object]]:
        output = browser.stdout
        assert output is not None
        log = b''
        port = 0
        with selectors.DefaultSelector() as startup:
            _ = startup.register(output, selectors.EVENT_READ)
            while startup.select(timeout=15):
                chunk = os.read(output.fileno(), 4096)
                if not chunk:
                    break
                log += chunk
                if match := re.search(rb'Listening on port (\d+)\b', log):
                    port = int(match[1])
                    break
        self.assertGreater(port, 0, log.decode(errors='replace'))

        with socket.create_connection(('127.0.0.1', port), timeout=15) as connection, connection.makefile('rb') as reader:
            def receive() -> object:
                length = bytearray()
                while (character := reader.read(1)) != b':':
                    if not character:
                        self.fail('Firefox closed the Marionette connection.')
                    length.extend(character)
                return cast(object, json.loads(reader.read(int(length))))

            sequence = 0

            def command(name: str, **parameters: object) -> object:
                nonlocal sequence
                sequence += 1
                payload = json.dumps([0, sequence, name, parameters]).encode()
                connection.sendall(str(len(payload)).encode()+b':'+payload)
                message = cast(list[object], receive())
                self.assertEqual(message[:3], [1, sequence, None], message)
                return message[3]

            _ = receive()
            _ = command('WebDriver:NewSession', capabilities={})
            yield command

    def test_reader_projects_states_without_copying_the_records(self) -> None:
        key = self.question()
        path = self.question_path(key)
        before = path.read_bytes()
        ready = self.investigation()
        waiting = self.investigation(False)
        items = review_items.read(self.root)
        self.assertEqual({item.type for item in items},{review_items.ReviewType.QUESTION,review_items.ReviewType.CHANGE,review_items.ReviewType.INVESTIGATION})
        question = next(item for item in items if item.key == key)
        self.assertEqual(question.id, f'page-owner/{path.stem}')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(question.state,review_items.ReviewState.OPEN)
        states = {item.key:item.state for item in items}
        self.assertEqual(states[f'investigation/{ready.owner}/{ready.id}'],review_items.ReviewState.OPEN)
        self.assertEqual(states[f'investigation/{waiting.owner}/{waiting.id}'],review_items.ReviewState.WAITING)
        _ = investigation.mark_read(ready.owner,ready.id)
        self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key.endswith('/'+ready.id)),review_items.ReviewState.DONE)

    def test_reader_retains_questions_without_an_owner(self) -> None:
        key = self.question()
        members = staff.snapshot()
        question_owner = next(row.id for row in members.rows if row.name == 'page-owner')
        without_owner = staff.StaffSnapshot(tuple(row for row in members.rows if row.id != question_owner))
        with patch.object(staff, 'snapshot', return_value=without_owner):
            orphan = next((item for item in review_items.read(self.root) if item.key == key), None)
            self.assertIsNotNone(orphan, 'F015: retain a question without an owner or production document')
            assert orphan is not None
            self.assertTrue(orphan.for_you(review_items.moment('2026-11-17T12:00')))
            self.assertEqual(orphan.actions, ())
            self.assertIn('Owner unavailable', review_page.render(self.root).decode())
            self.assertTrue(any(item.startswith(key+'/') for item in review_page.waiting_items(console.NewsRequest(self.root))))
            until = datetime.now(user_zone()).replace(second=0, microsecond=0)+timedelta(days=1)
            body = self.body(key, 'later', 'Read the ownerless question tomorrow.')+'&'+urlencode({'later':until.isoformat()})
            _, result = review_page.submit(self.root, body, self.status)
            self.assertIn('later until', result)
            saved = next(item for item in review_items.read(self.root) if item.key == key)
            self.assertEqual(saved.state, orphan.state)
            self.assertFalse(saved.for_you(datetime.now(user_zone())))
            self.assertTrue(saved.for_you(until+timedelta(seconds=1)))
            self.assertEqual(review_later.times()[key], until)
            self.assertEqual([(entry.who, entry.words) for entry in saved.log if entry.words.startswith('Later until ')],
                             [('nate', 'Later until '+until.isoformat()+': Read the ownerless question tomorrow.')],
                             'F024: Later keeps the action and words on an ownerless question')

    def test_question_ids_remain_after_closure_and_later_registration(self) -> None:
        key = self.question('First question?')
        first, = update_registration.selected_question_records([self.question_path(key)], (), None)
        update_registration.write_question_record(first, assign_id=True)
        original = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(original.id, 'page-owner/Q1', 'F013: show the native ID on the first read')
        self.assertIsInstance(original.source,review_items.QuestionSource)
        assert isinstance(original.source,review_items.QuestionSource)
        record = original.source.record
        update_registration.write_question_record(replace(record,status='closed'))
        second = self.question('Second question?')
        registered, = update_registration.selected_question_records([self.question_path(second)], (), None)
        update_registration.write_question_record(registered, assign_id=True)
        items = review_items.read(self.root)
        self.assertEqual(next(item.id for item in items if item.key == key),original.id)
        self.assertEqual(len({item.id for item in items if item.type == review_items.ReviewType.QUESTION}),2)

    def test_signed_answers_use_the_existing_delivery_path(self) -> None:
        key = self.question()
        delivery, answer = review_page.submit(self.root,self.body(key,'answer','Use the first layout.'),self.status)
        self.assertIsInstance(delivery,change_page.MessageDelivery)
        self.assertEqual(answer,'Use the first layout.')
        question = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertIn('Use the first layout.',question.log[-1].words)
        self.deliver_review_message(delivery,self.status)
        self.assertTrue(any('Use the first layout.' in message for _,_,message in self.fixture.messages))
        self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == key),review_items.ReviewState.DONE)
        ready = self.investigation()
        key = f'investigation/{ready.owner}/{ready.id}'
        _,answer = review_page.submit(self.root,self.body(key,'read'),self.status)
        self.assertEqual(answer,'marked read')
        self.assertEqual(investigation.load(ready.owner,ready.id).state,investigation.InvestigationState.READ)

    def test_review_show_row_starts_with_all(self) -> None:
        markup = review_page.render(self.root).decode()
        group = markup.split('data-filter-row="show"', 1)[1].split('</div>', 1)[0]
        self.assertEqual(re.findall(r'data-filter-value="([^"]+)"[^>]*>([^<]+) <b>', group),
                         [('all', 'All'), ('open', 'Open'), ('Done', 'Done')])

    def test_for_you_shows_completion_labels_and_keeps_done_for_automatic_actions(self) -> None:
        store = ActionStore()
        opened = store.add(title='Allow AWS price reads', command='deploy.py up --approve-resources',
                           machine='natedev', cwd='/fixture', requested_by=self.fixture.unit.id,
                           reason='AWS refused the price read.', waits='Test runs on AWS', concerns='aws',
                           dedupe_key='aws-price-read', completion='automatic')
        automatic = for_you.action_row(opened, self.fixture.unit.name)
        manual = replace(automatic, key=f'action/{uuid4()}', completion='manual')
        rebuild = replace(automatic, key='rebuild/natedev', kind='rebuild')
        done_button = b'<button data-send-receiver="" name="outcome" value="done"'
        with patch.object(for_you, 'read', return_value=for_you.ForYouSnapshot((automatic, manual, rebuild), ())):
            snapshot = review_page.build_snapshot(self.root)
            markup = snapshot.body(automatic.key)
            self.assertIn(b'<dt>Completion</dt><dd>Automatic</dd>', markup)
            self.assertIn(done_button, markup)
            self.assertIn(b'<dt>Completion</dt><dd>Manual</dd>', snapshot.body(manual.key))
            self.assertIn(done_button, snapshot.body(manual.key))
            self.assertIn(b'<dt>Completion</dt><dd>Automatic after rebuild</dd>', snapshot.body(rebuild.key))
            self.assertNotIn(done_button, snapshot.body(rebuild.key))
            delivery, result = review_page.submit(self.root, self.card_body(snapshot, automatic.key, 'done'),
                                                  self.status, snapshot=snapshot)
        self.assertIsInstance(delivery, change_page.NoMessageDelivery)
        self.assertEqual(result, 'Done')
        saved = store.get(automatic.key)
        self.assertEqual((saved.status, saved.completion, saved.evidence), ('done', 'automatic', 'Marked Done in Review.'))

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_for_you_notifier_copy_done_and_rebuild_keep_the_review_draft(self) -> None:
        _ = self.question()
        opened = for_you.ForYouRow(f'action/{uuid4()}', 'action', 'Restore GitHub access', 'cd /tmp\ngithub-warmup',
                                   'natedev', '/tmp', 'builder-unit', 'The login needs your browser.',
                                   'The release check', '2026-11-17T15:00:00+00:00', 'open', 'manual', '')
        completed = replace(opened, key=f'action/{uuid4()}', title='Completed action', status='done')
        rebuild = replace(opened, key='rebuild/natedev', kind='rebuild', title='Rebuild natedev', completion='automatic')
        with patch.object(for_you, 'read', return_value=for_you.ForYouSnapshot((opened, completed, rebuild), ())):
            payload = console.for_you_payload()
            review = browser_markup(self.root)
        review = review.replace('<head>', r"""<head><script>
window.copied='';
Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copied=text;}}});
window.fetch=async(_url,options)=>options?.method==='POST'
 ? Response.json({answer:'Done',state:'Done'})
 : new Response(null,{status:304});
</script>""", 1)
        pages = (console.ConsolePage('review', 'Review', 'inbox', 'Review',
                                     console.AvailablePage('/console/review.html')),
                 console.ConsolePage('studio', 'Studio', 'clapperboard', 'The Studio',
                                     console.AvailablePage(page_frame.STUDIO_PAGE_PATH)))
        list_counts = ''.join(list_page.count_link(alias, path+'#showrunner=page-owner', alias)
                              for alias, path in (('changes', '/console/changes.html'),
                                                  ('questions', '/console/changes.html?type=questions')))
        list_html = change_page.document(list_counts+'<img hidden src="/list-load">', 'List fixture')
        error_html = change_page.document('<p data-review-unavailable>The Review page is not available.</p>')
        shell = console.render(pages, console.AvailablePage('/console/rebuilds.html')).decode()
        shell = shell.replace('<head>', r"""<head><script>
const sendProbe=fetch;
window.fetch=(url,options)=>String(url).includes('for-you.json') ? Promise.resolve(Response.json(NOTIFIER)) :
 String(url).endsWith('/console/news') ? Promise.resolve(Response.json({new:[]})) : sendProbe(url,options);
</script>""".replace('NOTIFIER', json.dumps(payload).replace('</', '<\\/')), 1)
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 // The limit only names a stuck step; each wait ends when its condition holds.
 const until=async(predicate,label)=>{
  const end=performance.now()+20000;
  while(!predicate()){
   if(performance.now()>end) throw new Error('Timed out waiting for '+label+'.');
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 const frame=document.querySelector('[data-console-page=review]');
 const review=()=>frame.contentDocument;
 const row=key=>review().querySelector('[data-list-item][data-key="'+key+'"]');
 const chip=value=>review().querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]');
 const count=value=>Number(chip(value).querySelector('b').textContent);
 const rows=()=>[...review().querySelectorAll('[data-list-item][data-type="for-you"]:not([hidden])')];
 const expanded=key=>row(key)?.querySelector('.list-toggle').getAttribute('aria-expanded')==='true';
 const detail=key=>frame.contentWindow.recordDetails(row(key));
 const draft=()=>detail('page-owner/C1')?.querySelector('textarea');
 const words='Keep these unsent words.';
 const nav=page=>document.querySelector('[data-page='+page+']');
 const list=()=>document.querySelector('[data-console-page=studio]')?.contentDocument;
 const aliasLink=alias=>list()?.querySelector('a[href*="page='+alias+'&"]');
 const selectedType=type=>review().querySelector('[data-filter-row="type"] [data-filter-value="'+type+'"][aria-pressed="true"]');
 try{
  await until(()=>review().readyState==='complete' && review().querySelector('[data-review-unavailable]'),
   'the failed Review document');
  const failed=review();
  check(typeof frame.contentWindow.consoleNavigate==='undefined','The error page does not register navigation.');
  nav('studio').click();
  // The console intercepts List links only after its ready handler runs, a task after readyState changes.
  let listConnected=false;
  document.querySelector('[data-console-page=studio]').addEventListener('console-document-ready',
   ()=>{listConnected=true;},{once:true});
  await until(()=>listConnected && list()?.readyState==='interactive' && aliasLink('questions'),
   'the connected interactive List document');
  aliasLink('questions').click();
  await until(()=>review()!==failed && review().querySelector('[data-review-loading]'),'the second Review request');
  check(review().readyState==='loading','The second Review document waits for the fixture.');
  check(frame.contentWindow.location.hash.startsWith('#') && !frame.contentWindow.location.search,
   'The alias reloads Review at a fragment URL.');
  nav('studio').click();
  aliasLink('questions').click();
  nav('studio').click();
  aliasLink('changes').click();
  await fetch('/release-pages');
  await until(()=>selectedType('change'),'the last requested alias');
  check(review()!==failed && !frame.hidden,'The alias replaces the 503 document.');
  const original=review();
  row('page-owner/C1').querySelector('.list-toggle').click();
  await until(draft,'the draft field');
  draft().value=words;
  draft().dispatchEvent(new frame.contentWindow.Event('input',{bubbles:true}));
  [...document.querySelectorAll('[data-for-you-toggle]')].find(button=>button.getClientRects().length).click();
  const cardLink=()=>document.querySelector('[data-for-you-entries] [data-key="'+KEY+'"] a');
  await until(cardLink,'the notifier card link');
  cardLink().click();
  await until(()=>expanded(KEY) && detail(KEY)?.querySelector('[data-copy-code]'),'the selected action body');
  check(review()===original,'The notifier keeps the Review document.');
  const chips=[...review().querySelectorAll('[data-filter-row="type"] [data-filter-value]')];
  check(chips[0].dataset.filterValue==='all' && chips[1].dataset.filterValue==='for-you','For you follows All.');
  check(chips[1].getAttribute('aria-pressed')==='true' && chips[1].querySelector('b').textContent==='2',
        'The notifier selects For you with two open rows.');
  detail(KEY).querySelector('[data-copy-code]').click();
  check(frame.contentWindow.copied===COMMAND && detail(KEY).querySelector('[value=done]')?.disabled===false,
        'Copy keeps the action open.');
  check(count('open')===2 && rows().length===2 && count('Done')===1,'The initial counts match the rows.');
  detail(KEY).querySelector('[value=done]').click();
  const notice=()=>review().querySelector('[data-hidden-action-status]').textContent;
  await until(()=>notice()==='Saved: Done','the saved notice');
  check(!row(KEY) && count('open')===1 && rows().length===1,'Open loses the completed action and its count.');
  chip('Done').click();
  check(count('Done')===2 && rows().length===2 && row(KEY)?.dataset.state==='Done' && row(COMPLETED),
        'Done gains the completed action and its count.');
  chip('all').click();
  check(count('all')===3 && rows().length===3 && row(KEY),'All keeps the completed action.');
  row(REBUILD).querySelector('.list-toggle').click();
  const details=()=>detail(REBUILD)?.querySelector('a[target="_top"]');
  await until(details,'the rebuild details link');
  details().click();
  check(new URL(location.href).searchParams.get('page')==='rebuilds' && frame.hidden && review()===original,
        'Rebuild details selects Rebuilds and keeps the Review frame.');
  document.querySelector('[data-page=review]').click();
  check(expanded(REBUILD),'Review keeps the selected rebuild card.');
  for(const [alias,type] of [['questions','question'],['changes','change']]){
   nav('studio').click();
   aliasLink(alias).click();
   await until(()=>selectedType(type),'the Review filter for '+alias);
   check(new URL(location.href).searchParams.get('page')==='review' && !frame.hidden && review()===original,
    'The List '+alias+' link selects the existing Review frame.');
   check(review().querySelector('[data-filter-row="owner"] [data-filter-value][aria-pressed="true"]').dataset.ownerName==='page-owner',
    'The List '+alias+' link keeps the selected owner.');
  }
  row('page-owner/C1').querySelector('.list-toggle').click();
  await until(draft,'the restored draft field');
  check(draft().value===words,'Review keeps the draft after navigation.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        for name, value in (('KEY', opened.key), ('COMMAND', opened.command), ('COMPLETED', completed.key),
                            ('REBUILD', rebuild.key)):
            probe = probe.replace(name, json.dumps(value))
        shell_html = shell.replace('</body>', probe+'<img hidden src="/complete"></body>', 1).encode()
        review_head, review_body = review.split('</head>', 1)
        review_start = (review_head+'</head><body><span hidden data-review-loading></span>').encode()
        review_end = review_body.removeprefix('<body>').encode()
        results: list[list[str]] = []
        review_requests: list[tuple[int, str]] = []
        released, complete = threading.Event(), threading.Event()

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == '/complete':
                    _ = complete.wait(TEST_HANG_GUARD_SECS)
                elif path == '/list-load':
                    _ = released.wait(TEST_HANG_GUARD_SECS)
                elif path == '/release-pages':
                    released.set()
                status = 200
                if path == '/console/review.html':
                    status = 200 if review_requests else 503
                    review_requests.append((status, self.path))
                    body = review_start if status == 200 else error_html
                else:
                    body = {'/console/': shell_html, page_frame.STUDIO_PAGE_PATH: list_html,
                            '/console/rebuilds.html': b'<h1>Rebuilds</h1>'}.get(path, b'')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                _ = self.wfile.write(body)
                if path == '/console/review.html' and status == 200:
                    _ = released.wait(TEST_HANG_GUARD_SECS)
                    _ = self.wfile.write(review_end)

            def do_POST(self) -> None:
                results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                released.set()
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
                with tempfile.TemporaryDirectory() as profile:
                    done = run_process_group(['firefox', '--headless', '--no-remote', '--profile', profile,
                        '--window-size', '1440,900', '--screenshot', str(Path(profile)/'page.png'),
                        f'http://127.0.0.1:{server.server_port}/console/?page=review'],
                        timeout=45)
                self.assertEqual(done.returncode, 0, done.stderr)
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)
        self.assertEqual(results, [[]])
        self.assertEqual(review_requests, [(503, review_page.PAGE_PATH), (200, review_page.PAGE_PATH)])

    def test_validating_change_has_a_card_and_uses_only_a_saved_handoff_time(self) -> None:
        key = 'page-owner/C1'
        until = datetime.now(user_zone()) + timedelta(days=1)
        review_later.save(key, until)
        other = 'page-owner/C2'
        legacy_time = until.replace(tzinfo=None)
        review_later.save(other, legacy_time)
        with patch.object(time, 'time', return_value=1794902400):
            delivery, _ = review_page.submit(self.root, self.body(key, 'validate'), self.status)
        self.deliver_review_message(delivery, self.status)
        with self.subTest(later='cleared by hand-off'):
            self.assertNotIn(key, review_later.times())
            self.assertEqual(review_later.stored_times(review_later.path())[other], legacy_time.isoformat())
        review_later.save(key, until)
        snapshot = review_page.build_snapshot(self.root)
        entry = next(item for item in snapshot.entries if item['key'] == key)
        self.assertEqual(entry['state'], review_items.ReviewState.VALIDATING)
        self.assertEqual(entry['hiddenUntil'], 0)
        self.assertEqual(entry['ownerName'], 'page-owner')
        self.assertEqual(entry.get('handoff', ('', ''))[1], 'Nov 17 03:00 EST')
        card = snapshot.body(key).decode()
        for text in ('How to check', 'Proof', 'Log · newest first', 'Your words',
                     'Ask a question', 'data-validating-age datetime="2026-11-17T03:00:00-05:00"'):
            self.assertIn(text, card)
        self.assertRegex(card, r'Sends to <span[^>]*data-color-text="role.showrunner"[^>]*>page-owner</span>')
        self.assertNotIn('data-later-toggle', card)
        path = self.status.path(self.fixture.page, 'C1')
        fields = cast(dict[str, object], json.loads(path.read_text()))
        _ = fields.pop('handed_at')
        _ = path.write_text(json.dumps(fields))
        snapshot = review_page.build_snapshot(self.root)
        self.assertNotIn('handoff', next(item for item in snapshot.entries if item['key'] == key))
        card = snapshot.body(key).decode()
        self.assertIn('class="review-handoff">With ', card)
        self.assertNotIn('Validating for', card)
        self.assertFalse(any(value.startswith(key+'/') for value in
                             review_page.waiting_items(console.NewsRequest(self.root))))

    def test_validating_question_keeps_the_handoff_and_returns_the_answer(self) -> None:
        _ = self.enterContext(patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed.")))
        key = 'page-owner/C1'
        check, _ = review_page.submit(self.root, self.body(key, 'validate', 'Check the queued step.'), self.status)
        assert isinstance(check, change_page.MessageDelivery)
        change_page.deliver(check, self.status)
        path = self.status.path(check.page, check.row)
        handed_at = change_page.read_handed_at(path)
        validation = self.status.slots(check.page, check.row).validation
        self.fixture.messages.clear()
        destination = self.fixture.temporary/'destination.md'
        _ = destination.write_text(self.fixture.production.read_text())
        runner = fixture_member('showrunner', name='destination', doc=staff.LiveDoc(local_machine(), destination))
        _ = staff.move(self.fixture.unit.id, staff.ReportsTo(runner.id))
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        question = 'What is holding up validation?'
        current = cache.get()
        self.assertEqual(next(entry['ownerName'] for entry in current.entries if entry['key'] == key), 'page-owner')
        self.assertRegex(current.body(key).decode(), r'Sends to <span[^>]*data-color-text="role.showrunner"[^>]*>page-owner</span>')
        fields = self.card_body(current, key, 'question', question)
        sent = cache.save(fields, self.status)
        self.assertEqual(sent.answer, 'question saved')
        with patch.object(change_page, 'send_message', side_effect=RuntimeError('The recipient is offline.')):
            self.deliver_review_message(sent.delivery, self.status)
        self.assertEqual(self.fixture.messages, [])
        reopened_status = change_page.DeliveryStatus(self.root)
        unsent = reopened_status.slots(check.page, check.row).question
        assert isinstance(unsent, change_page.OpenQuestion)
        self.assertIsInstance(unsent.delivery, change_page.DeliveryIntent)
        self.assertEqual(cache.save(self.card_body(cache.get(), key, 'question', question), self.status).answer,
                         'question saved')
        capture = self.fixture.capture_message

        def reject_alert(recipient: str, sender: str, message: str, **_options: str) -> None:
            if 'delivery needs attention' in message:
                raise RuntimeError('The alert recipient is offline.')
            capture(recipient, sender, message)

        with patch.object(change_page, 'send_message', new=reject_alert):
            change_page.recover_pending(reopened_status)
            change_page.recover_pending(reopened_status)
        self.assertEqual(len(self.fixture.messages), 1)
        target, _, message = self.fixture.messages[0]
        self.assertEqual(target, 'page-owner')
        self.assertIn('Question: '+question, message)
        self.assertIn('Production: page-owner', message)
        self.assertIn('answer-question --item page-owner/C1', message)
        slots = self.status.slots(check.page, check.row)
        self.assertEqual(slots.validation, validation)
        self.assertEqual(change_page.read_handed_at(path), handed_at)
        assert isinstance(slots.question, change_page.OpenQuestion)
        pending, _ = cache.publish_record(cache.get(), key)
        self.assertEqual(pending.records[key].state, review_items.ReviewState.VALIDATING)
        pending_card = pending.body(key).decode()
        validator = r'<span[^>]*data-color-text="role.showrunner"[^>]*>page-owner</span>'
        self.assertRegex(pending_card, 'Question for ' + validator)
        self.assertIn(question, pending_card)
        self.assertNotRegex(pending_card, 'Answer from ' + validator)
        self.assertEqual([event.words for event in pending.records[key].log][-2:],
                         ['Asked page-owner to validate', 'Asked a question about validation'])
        retry = cache.save(self.card_body(pending, key, 'question', question), self.status)
        self.assertIsInstance(retry.delivery, change_page.NoMessageDelivery)
        with self.assertRaisesRegex(question_record.AnswerRefused, 'already has an open question'):
            _ = cache.save(self.card_body(pending, key, 'question', 'Send an update?'), self.status)
        self.assertEqual(len(self.fixture.messages), 1)
        words = 'The macOS release build is still running.'
        self.assertTrue(change_page.answer_item(key, words, slots.question.question_id, self.status))
        self.assertFalse(change_page.answer_item(key, words, slots.question.question_id, self.status))
        answered, _ = cache.publish_record(cache.get(), key)
        self.assertEqual(answered.records[key].state, review_items.ReviewState.VALIDATING)
        self.assertEqual(self.status.slots(check.page, check.row).validation, validation)
        self.assertEqual(change_page.read_handed_at(path), handed_at)
        for snapshot in (answered, review_page.build_snapshot(self.root)):
            with self.subTest(version=snapshot.version):
                self.assertEqual(snapshot.records[key].state, review_items.ReviewState.VALIDATING)
                card = snapshot.body(key).decode()
                for label in ('Question for ', 'Answer from '):
                    self.assertEqual(len(re.findall(label + validator, card)), 1)
                for text in (question, words):
                    self.assertIn(text, card)
                    self.assertEqual(card.count(text), 1)
                self.assertEqual([event.words for event in snapshot.records[key].log][-3:],
                                 ['Asked page-owner to validate', 'Asked a question about validation',
                                  'Answered the question'])
        with self.subTest(state='unread validation answer'):
            members = staff.snapshot()
            now = datetime.now(user_zone())
            self.assertTrue(answered.records[key].for_you(now))
            self.assertIn(key, {item.key for item in review_items.read_open(self.root)})
            document = self.fixture.production
            entries = next_item.read_queue('change', document, record=members).entries
            self.assertEqual({entry.key for entry in entries}, {'C1'})
            self.assertEqual(footer_counts.change_counts(document, members), (1, 0))
            marked = cache.save(self.card_body(answered, key, 'read'), self.status)
            self.assertEqual(marked.answer, 'marked read')
            self.assertNotIn(key, {item.key for item in review_items.read_open(self.root)})
            self.assertEqual(next_item.read_queue('change', document, record=members).entries, [])
            self.assertEqual(footer_counts.change_counts(document, members), (0, 0))
        result = change_review.save_verdict(check.page.review_list, check.row, 'works', self.fixture.production,
                                            validation_id=check.attempt)
        self.assertIsInstance(result, change_review.VerdictSaved)
        closed, item = cache.publish_record(cache.get(), key)
        assert isinstance(item, review_items.ReviewItem)
        self.assertEqual(item.state, review_items.ReviewState.DONE)
        self.assertNotIn(key, closed.records)
        self.assertNotIn(key, {entry["key"] for entry in closed.entries})

        _ = staff.move(self.fixture.unit.id, staff.ReportsTo(check.recipients[0]))
        fault_key = 'page-owner/C2'
        check, _ = review_page.submit(self.root, self.body(fault_key, 'validate'), self.status)
        assert isinstance(check, change_page.MessageDelivery)
        change_page.deliver(check, self.status)
        current, _ = cache.publish_record(cache.get(), fault_key)
        pending = cache.save(self.card_body(current, fault_key, 'question', question), self.status)
        self.deliver_review_message(pending.delivery, self.status)
        result = change_review.save_verdict(check.page.review_list, check.row, 'not works: the gate failed',
                                            self.fixture.production, validation_id=check.attempt)
        self.assertIsInstance(result, change_review.VerdictSaved)
        reopened, _ = cache.publish_record(cache.get(), fault_key)
        self.assertEqual(reopened.records[fault_key].state, review_items.ReviewState.OPEN)
        self.assertIsInstance(self.status.slots(check.page, check.row).question, change_page.NoQuestion)

    def test_validation_uses_the_saved_validator_after_an_owner_change(self) -> None:
        key = 'page-owner/C1'
        validator = staff.snapshot().by_doc(local_machine(), self.fixture.production)
        assert validator is not None
        record = self.status.path(self.fixture.page, 'C1')
        fields: change_page.StoredDeliverySlots = {'validation': {
            'attempt': 'a1b2', 'state': 'sent', 'feedback': 'Check the queued step.', 'recipients': [validator.id]}}
        _ = record.write_text(json.dumps(fields))
        destination = self.fixture.temporary/'destination.md'
        _ = destination.write_text(self.fixture.production.read_text())
        runner = fixture_member('showrunner', name='destination', doc=staff.LiveDoc(local_machine(), destination))
        _ = staff.move(self.fixture.unit.id, staff.ReportsTo(runner.id))

        snapshot = review_page.build_snapshot(self.root)
        self.assertEqual(snapshot.records[key].owner, validator.id)
        self.assertRegex(snapshot.body(key).decode(), r'Sends to <span[^>]*data-color-text="role.showrunner"[^>]*>page-owner</span>')
        body = self.card_body(snapshot, key, 'question', 'Which check is still running?')
        question, _ = review_page.submit(self.root, body, self.status, snapshot=snapshot)
        assert isinstance(question, change_page.MessageDelivery)
        self.assertEqual(question.recipients, (validator.id,))
        change_page.deliver(question, self.status)
        self.assertEqual([recipient for recipient, _, _ in self.fixture.messages], ['page-owner'])
        self.assertEqual(self.status.validators(self.fixture.page, 'C1'), (validator.id,))
        self.assertEqual(self.status.slots(self.fixture.page, 'C1').validation,
                         change_page.ValidationRequested('a1b2', 'Check the queued step.'))

    def test_change_feedback_returns_after_a_showrunner_note(self) -> None:
        key = 'page-owner/C1'
        delivery, _ = review_page.submit(self.root, self.body(key, 'feedback', 'Adjust the spacing.\n'), self.status)
        assert isinstance(delivery, change_page.MessageDelivery)
        pending = next(item for item in review_items.read(self.root) if item.key == key)
        with self.subTest(state='waiting for feedback'):
            self.assertEqual(pending.state, review_items.ReviewState.WAITING)
            self.assertEqual(pending.actions, ())
            snapshot = review_page.build_snapshot(self.root)
            card = snapshot.body(key).decode()
            self.assertIn('Adjust the spacing.', card)
            self.assertNotIn('Ask a question', card)
            self.assertNotIn('value="question"', card)
        request = self.status.slots(delivery.page, delivery.row).question
        assert isinstance(request, change_page.OpenFeedback)
        change_page.deliver(delivery, self.status)
        now = datetime.now(user_zone())
        until = now + timedelta(days=1)
        review_later.save(key, until)
        self.assertTrue(change_page.answer_item(key, 'The spacing is adjusted.', request.question_id, self.status))
        returned = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(returned.state, review_items.ReviewState.OPEN)
        with self.subTest(finding='F007'):
            self.assertTrue(returned.for_you(now), 'The returned feedback is visible in Open.')
            # Mark read returns the item to Later.
            self.assertEqual(review_later.times()[key], until)
        with self.subTest(finding='F008'):
            self.assertEqual([entry.words.rstrip() for entry in returned.log[1:]],
                             ['feedback: Adjust the spacing.', 'Answer: The spacing is adjusted.'])
        with self.subTest(state='read after Later'):
            snapshot = review_page.build_snapshot(self.root)
            entries = unpack_index(review_page.index_json(snapshot.entries))
            self.assertTrue(next(entry for entry in entries if entry['key'] == key).get('unreadAnswer'))
            if shutil.which('firefox'):
                proof = """<script>
const row=document.querySelector('[data-list-item][data-key="'+KEY+'"]');
const marked=row && !row.hidden && row.querySelector('[data-new-answer]')?.textContent==='New answer';
void fetch('/results',{method:'POST',body:JSON.stringify(marked ? [] : ['The Open Change needs a New answer mark.'])});
</script>""".replace('KEY', json.dumps(key))
                markup = snapshot.render().replace(b'</body>', proof.encode() + b'</body>')
                self.assertEqual(firefox_probe_results(markup), [[]])
            body = self.card_body(snapshot, key, 'read')
            _, result = review_page.submit(self.root, body, self.status, snapshot=snapshot)
            self.assertEqual(result, 'marked read')
            read = review_page.build_snapshot(self.root).records[key]
            self.assertFalse(read.unread_answer)
            self.assertTrue(read.hidden_until(now))
            self.assertFalse(read.for_you(now))
            self.assertEqual(review_later.times()[key], until)

    def test_legacy_feedback_gets_a_review_log_on_return(self) -> None:
        key = 'page-owner/C1'
        request = self.status.ask(self.fixture.page, 'C1', 'Adjust the spacing.', kind='feedback')
        assert isinstance(request, change_page.QuestionDelivery)
        row = change_page.answer_row(self.fixture.page, 'C1')
        self.assertFalse(row.get('Review log'))
        self.assertTrue(change_page.answer_item(key, 'The spacing is adjusted.', request.question_id, self.status))
        row = change_page.answer_row(self.fixture.page, 'C1')
        events = review_events.decode(row['Review log'])
        self.assertEqual([event.words for event in events],
                         ['feedback: Adjust the spacing.', 'Answer: The spacing is adjusted.'])

    def test_waiting_change_feedback_matches_the_request_id(self) -> None:
        page = self.fixture.page
        words = 'Adjust the spacing.'
        answer = 'The spacing is adjusted.'
        with self.status.locked():
            slots = self.status.slots(page, 'C1')
            legacy = change_page.CompletedFeedback('legacy-feedback', words, answer)
            self.status.write_slots(page, 'C1', replace(slots, question=legacy))
        change_review.record_review_log(page.review_list, 'C1', 'Answer: ' + answer, page.production)
        returned = next(item for item in review_items.read(self.root) if item.key == 'page-owner/C1')
        self.assertEqual([event.words for event in returned.log[1:]],
                         ['feedback: ' + words, 'Answer: ' + answer])

        key = 'page-owner/C2'
        delivery, _ = review_page.submit(self.root, self.body(key, 'feedback', words), self.status)
        assert isinstance(delivery, change_page.MessageDelivery)
        slots = self.status.slots(delivery.page, delivery.row)
        request = slots.question
        assert isinstance(request, change_page.OpenFeedback)
        waiting = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual([(event.words, event.feedback_request_id) for event in waiting.log[1:]],
                         [('feedback: ' + words, request.question_id)])

        later = change_page.OpenFeedback('later-feedback', words, change_page.NoDelivery())
        with self.status.locked():
            self.status.write_slots(delivery.page, delivery.row, replace(slots, question=later))
        waiting = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertCountEqual([(event.words, event.feedback_request_id) for event in waiting.log[1:]],
                              [('feedback: ' + words, request.question_id), ('feedback: ' + words, later.question_id)])

        key = 'page-owner/C3'
        legacy_open = change_page.OpenFeedback('untagged-feedback', words, change_page.NoDelivery())
        change_review.record_review_log(page.review_list, 'C3', 'feedback: ' + words, page.production)
        with self.status.locked():
            slots = self.status.slots(page, 'C3')
            self.status.write_slots(page, 'C3', replace(slots, question=legacy_open))
        waiting = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual([event.words for event in waiting.log[1:]], ['feedback: ' + words])
        self.assertEqual(review_page.build_snapshot(self.root).body(key).count(words.encode()), 1)

        self.assertTrue(change_page.answer_item(key, answer, legacy_open.question_id, self.status))
        returned = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual([event.words for event in returned.log[1:]], ['feedback: ' + words, 'Answer: ' + answer])
        self.assertEqual(review_page.build_snapshot(self.root).body(key).count(words.encode()), 1)

    def test_feedback_send_reuses_the_log_entry_after_a_slot_write_failure(self) -> None:
        keeper = fixture_member('producer', name='morty')
        page = self.fixture.page
        key = 'page-owner/C1'
        words = 'Adjust the spacing.'
        before = review_page.build_snapshot(self.root)
        body = self.card_body(before, key, 'feedback', words)
        with patch.object(self.status, 'write_slots', side_effect=OSError('The delivery slot cannot be saved.')):
            with self.assertRaisesRegex(OSError, 'The delivery slot cannot be saved.'):
                _ = review_page.submit(self.root, body, self.status, snapshot=before)
        row = change_page.answer_row(page, 'C1')
        entry, = review_events.decode(row['Review log'])
        self.assertTrue(entry.feedback_request_id)
        self.assertIsInstance(self.status.slots(page, 'C1').question, change_page.NoQuestion)

        restarted = change_page.DeliveryStatus(self.root)
        refreshed = review_page.build_snapshot(self.root)
        changed = self.card_body(before, key, 'feedback', 'Use a different size.')
        with self.assertRaises(review_items.ReviewRecordChanged):
            _ = review_page.submit(self.root, changed, restarted, snapshot=refreshed)
        delivery, _ = review_page.submit(self.root, body, restarted, snapshot=refreshed)
        assert isinstance(delivery, change_page.MessageDelivery)
        request = restarted.slots(page, 'C1').question
        assert isinstance(request, change_page.OpenFeedback)
        self.assertEqual(request.question_id, entry.feedback_request_id)
        row = change_page.answer_row(page, 'C1')
        first, moved = review_events.decode(row['Review log'])
        self.assertEqual(first, entry)
        item, = the_list.records(keeper.id)
        self.assertEqual(moved.words, f'moved to List item {item.number}')
        waiting = review_page.build_snapshot(self.root)
        self.assertEqual(waiting.render_body(waiting.records[key], waiting.assets).count(words.encode()), 1)

    def test_both_change_pages_refuse_a_second_handoff(self) -> None:
        key = 'page-owner/C1'
        delivery, _ = review_page.submit(self.root, self.body(key, 'validate', 'Check this.'), self.status)
        assert isinstance(delivery, change_page.MessageDelivery)
        change_page.deliver(delivery, self.status)
        path = self.status.path(delivery.page, delivery.row)
        before = (path.read_bytes(), self.fixture.review_list.read_bytes(), list(self.fixture.messages))
        for outcome in ('validate', 'question', 'feedback'):
            with self.subTest(outcome=outcome):
                with self.assertRaisesRegex(question_record.AnswerRefused, 'already with the showrunner'):
                    _ = review_page.submit(self.root, self.body(key, outcome, 'Check this.'), self.status)
                with self.assertRaisesRegex(question_record.AnswerRefused, 'already with the showrunner'):
                    _ = change_page.submit(self.fixture.page, urlencode(self.fixture.answer(
                        outcome=outcome, feedback='Check this.')), self.status)
                self.assertEqual((path.read_bytes(), self.fixture.review_list.read_bytes(), self.fixture.messages), before)

    def test_showrunner_check_returns_a_fault_and_closes_a_success(self) -> None:
        key = 'page-owner/C1'
        delivery,_ = review_page.submit(self.root,self.body(key,'validate'),self.status)
        self.deliver_review_message(delivery,self.status)
        self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == key),review_items.ReviewState.VALIDATING)
        assert isinstance(delivery, change_page.MessageDelivery)
        now = datetime.now(user_zone())
        review_later.save(key, now + timedelta(days=1))
        _ = change_review.save_verdict(self.fixture.review_list, 'C1', 'not works: the spacing differs',
                                       self.fixture.production, validation_id=delivery.attempt)
        returned = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(returned.state, review_items.ReviewState.OPEN)
        self.assertIn('the spacing differs', returned.log[-1].words)
        with self.subTest(finding='F007'):
            self.assertTrue(returned.for_you(now), 'The fault is visible in Open.')
            self.assertNotIn(key, review_later.times())
        delivery, _ = review_page.submit(self.root, self.body('page-owner/C2', 'validate'), self.status)
        assert isinstance(delivery, change_page.MessageDelivery)
        change_page.deliver(delivery, self.status)
        before = list(self.fixture.messages)
        self.assertIsInstance(change_review.save_verdict(self.fixture.review_list, 'C2', 'works',
            self.fixture.production, validation_id=delivery.attempt), change_review.VerdictSaved)
        self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == 'page-owner/C2'),
                         review_items.ReviewState.DONE)
        self.assertEqual(self.fixture.messages, before)
        selected = replace(self.fixture.page, rows=('C2',))
        done = '^this row already has a saved answer$'
        saved = self.fixture.review_list.read_bytes()
        for outcome in change_page.CHANGE_HANDOFFS:
            with self.subTest(outcome=outcome, page='Review'):
                with self.assertRaisesRegex(question_record.AnswerRefused, done):
                    _ = review_page.submit(self.root, self.body('page-owner/C2', outcome, 'Use the new proof.'), self.status)
            with self.subTest(outcome=outcome, page='selected change'):
                body = urlencode(self.fixture.answer('C2', outcome, 'Use the new proof.'))
                with self.assertRaisesRegex(question_record.AnswerRefused, done):
                    _ = change_page.submit(selected, body, self.status)
        self.assertEqual(self.fixture.review_list.read_bytes(), saved)
        self.assertEqual(self.fixture.messages, before)

    def test_validation_messages_keep_the_exchange_id_after_recovery(self) -> None:
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                case = ReviewPageTests('runTest')
                case.setUp()
                try:
                    delivery, _ = review_page.submit(case.root, case.body('page-owner/C1', 'validate', 'Check this case.'), case.status)
                    assert isinstance(delivery, change_page.MessageDelivery)
                    if recovered:
                        change_page.recover_pending(change_page.DeliveryStatus(case.root))
                        message = case.fixture.messages[-1][2]
                    else:
                        message = delivery.message
                    self.assertIn('Validation id: '+delivery.attempt, message)
                    self.assertIn('--validation '+delivery.attempt, message)
                    self.assertIn('--production page-owner', message)
                    self.assertIn('--row C1', message)
                    self.assertIn('Check this case.', message)
                finally:
                    case.doCleanups()

    def test_page_reuses_the_list_table_and_has_only_this_phases_actions(self) -> None:
        _ = self.question()
        _ = self.legacy_investigation()
        source = review_page.render(self.root).decode()
        self.assertIn(record_table.STYLE,source)
        self.assertIn(record_table.SCRIPT,source)
        for label in review_page.HEADERS:
            self.assertIn(label+'<span data-sort-mark',source)
        for kind in ('question','change','investigation'):
            self.assertIn(f'data-filter-value="{kind}"',source)
        cards = ''.join(review_page.build_snapshot(self.root).body(item.key).decode() for item in review_items.read(self.root))
        self.assertRegex(cards, r'<button[^>]*value="works"[^>]*>Works<template data-send-with-words')
        for action in ('Answer','Does not work','Ask showrunner to check','Ask question','Send feedback','Mark read'):
            self.assertIn('>'+action+'</button>',cards)
        self.assertIn('data-later-toggle>Later <span',cards)
        self.assertIn('createPageRefresh(refresh,()=>content.dataset.refreshSeconds)',source)
        self.assertIn('class="lucide lucide-microscope"',source)
        self.assertIn('data-refresh-seconds="15.0"',source)

    def test_open_state_replaces_for_you_without_changing_the_user_totals(self) -> None:
        _ = self.decisions()
        _ = self.question()
        _ = self.legacy_investigation()
        _ = self.investigation(False)
        records = review_items.read(self.root)
        actionable = [item for item in records if item.for_you(datetime.now(user_zone()))]
        self.assertEqual(len(actionable), 7, 'The user totals retain all seven actionable records.')
        self.assertEqual({item.type.value for item in actionable},
                         {'question', 'change', 'investigation', 'proposal', 'efficiency'})
        self.assertEqual({item.state.value for item in actionable}, {'Open'})
        self.assertEqual(tuple(state.value for state in review_items.ReviewState), ('Open', 'Validating', 'Waiting', 'Done'))
        self.assertTrue(all(item.actions for item in actionable))
        self.assertEqual(review_items.read(self.root, show='open'), actionable)
        self.assertEqual(review_page.SHOWS,
                         (('all', 'All'), ('open', 'Open'), ('Done', 'Done')))
        for spelling in ('For you', 'for-you', 'for you'):
            with self.subTest(spelling=spelling):
                markup = review_page.render(self.root, show=spelling).decode()
                self.assertIn('data-show="open"', markup)
                self.assertNotIn('data-state="For you"', markup)
                self.assertNotIn('data-filter-value="For you"', markup)
                self.assertNotIn('data-state="Waiting"', markup)

    def test_open_decisions_keep_their_actions_counts_and_queues(self) -> None:
        keys = self.decisions()
        _ = self.question()
        _ = self.investigation()
        _ = self.investigation(False)
        doc = self.fixture.production
        _ = doc.write_text(doc.read_text()+'Status: PRODUCTION — running\n')
        members = staff.snapshot()
        owner = next(row for row in members.rows if row.name == 'page-owner')
        identities = update_registration.QuestionStaff.for_record(members)
        items = {item.key:item for item in review_items.read(self.root)}
        for key in keys:
            with self.subTest(key=key):
                item = items[key]
                self.assertEqual(item.state, 'Open', 'A proposal or efficiency waits in Open.')
                self.assertEqual(item.actions, review_items.DECISION_ACTIONS)
                self.assertTrue(item.for_you(datetime.now(user_zone())), 'Open decisions remain eligible for automation.')
                self.assertIn(item, review_items.read(self.root, show='open'))
                self.assertIn(item, review_items.read(self.root, show='For you'))
                markup = review_page.render(self.root).decode()
                payload = markup.split('data-review-index>',1)[1].split('</script>',1)[0]
                row = next(entry for entry in unpack_index(payload) if entry['key']==key)
                self.assertEqual(row['state'],review_items.ReviewState.OPEN)
                self.assertTrue(all('value="'+action+'"' in review_page.build_snapshot(self.root).body(key).decode()
                                    for action in review_items.DECISION_ACTIONS))
                self.assertIn(key, review_page.render(self.root, show='For you').decode())
        with self.subTest(check='queue and footer totals'):
            counts = footer_counts.read_counts(doc, questions=1, record=members)
            self.assertEqual((counts.proposals, counts.efficiency), (1, 1))
            for kind in ('proposal', 'efficiency'):
                queue = next_item.read_queue(kind, doc)
                self.assertEqual(len(queue.entries), 1)
            instance = self.fixture.temporary / ('showrunner-'+owner.id)
            instance.mkdir()
            line, _ = counts_row.row(instance, [], identities=identities)
            self.assertEqual(line, footer_counts.counts_line(counts), 'The status line keeps the footer totals.')

    def test_show_chips_replace_the_review_select(self) -> None:
        _ = self.question()
        _ = self.investigation(False)
        for show, _ in review_page.SHOWS:
            with self.subTest(show=show):
                source = review_page.render(self.root, show=show).decode()
                for label in ('Type', 'Show', 'Owner'):
                    key = label.lower()
                    self.assertIn(f'<div class="record-filter-row" role="group" aria-label="{label}"'
                                  + f' data-filter-row="{key}"><span aria-hidden="true">{label}</span>'
                                  + f'<div id="list-{key}">', source)
                status = source.index('<p class="page-status"')
                words = source.index('id="list-search"')
                self.assertIn('</p><div class="record-filter-words">', source[status:words])
                self.assertLess(words, source.index('role="group" aria-label="Type"'))
                self.assertIn('<label class="list-search-label" for="list-search">Search</label>', source)
                group = source.partition('data-filter-row="show"')[2].partition('</div>')[0]
                self.assertTrue(group, 'Show has a second chip row')
                self.assertNotIn('<select id="list-show"', source)
                self.assertNotIn('<label for="list-show">', source)
                self.assertEqual(group.count('aria-pressed="true"'), 1)
                self.assertIn(f'data-filter-value="{show}" aria-pressed="true"', group)
                for value, label in review_page.SHOWS:
                    self.assertIn(f'data-filter-value="{value}"', group)
                    self.assertIn('>'+label+' <b', group)

    def test_review_and_list_emit_the_same_owner_chip_markup_for_the_same_input(self) -> None:
        snapshot = review_page.build_snapshot(self.root)
        entry = snapshot.entries[0]
        owner = record_table.Chip(f'staff:{entry["owner"]}', entry['ownerHtml'] or html.escape(entry['ownerName']),
                                  owner_name=entry['ownerName'])
        snapshot = replace(snapshot, entries=[entry])
        review = review_page.render_shell(snapshot, review_page.index_json(snapshot.entries), time.perf_counter(),
                                          snapshot.read_at).decode()
        with patch.object(list_page, 'staff_chips', return_value=[owner]):
            list_markup = list_page.render(self.root, include_counts=False).decode()
        start = '<div class="record-filter-row" role="group" aria-label="Owner" data-filter-row="owner">'
        self.assertEqual(list_markup.split(start, 1)[1].split('</div></div>', 1)[0],
                         review.split(start, 1)[1].split('</div></div>', 1)[0])

    def test_filter_index_has_only_filter_and_sort_values(self) -> None:
        _ = self.question('A title with </script><b>text</b> & words?')
        waiting = self.investigation(False)
        done = self.investigation()
        _ = investigation.mark_read(done.owner, done.id)
        records = review_items.read(self.root)
        members = staff.snapshot()
        entries = review_page.filter_index(records, members)
        active = [item for item in records if item.state != review_items.ReviewState.DONE]
        self.assertEqual(len(entries), len(active))
        self.assertEqual({entry['state'] for entry in entries}, {'Open', 'Waiting'})
        self.assertIn(f'investigation/{waiting.owner}/{waiting.id}', {entry['key'] for entry in entries})
        self.assertEqual(set(entries[0]), {'key', 'id', 'type', 'owner', 'sourceOwner', 'ownerName',
                                        'title', 'state', 'filed', 'updated', 'hiddenUntil', 'sourceName', 'urgency', 'ownerHtml', 'sourceHtml'})
        self.assertEqual([entry['updated'] for entry in entries], [item.times.updated.isoformat() for item in active])
        encoded = review_page.index_json(entries)
        self.assertNotIn('<', encoded)
        self.assertNotIn('&', encoded)
        self.assertEqual(unpack_index(encoded), entries)
        for show, _ in review_page.SHOWS:
            source = review_page.render(self.root, show=show).decode()
            payload = source.split('data-review-index>', 1)[1].split('</script>', 1)[0]
            self.assertEqual(unpack_index(payload), entries, 'Every Show carries the same complete index.')
            self.assertNotIn('<span data-list-count', source)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_show_chips_keep_counts_address_reload_and_refresh(self) -> None:
        self.maxDiff: int | None = None
        proposals = self.decisions()
        question = self.question()
        ready = self.legacy_investigation()
        waiting = self.investigation(False)
        finished = self.investigation()
        _ = investigation.mark_read(finished.owner, finished.id)
        with patch.object(time, 'time', return_value=1794902400):
            _ = review_page.submit(self.root, self.body('page-owner/C1', 'validate'), self.status)
        review_later.save('page-owner/C1', datetime.now(user_zone()) + timedelta(days=1))
        _ = change_review.append_rows(self.fixture.review_list,
            pictured_changes([change_review.Visible('Check the setup.', 'Follow the setup.', 'It completes.')]),
            datetime.now(user_zone()).date(), '', self.fixture.production,
            producers=[self.fixture.unit.id], record=staff.snapshot())
        waiting_change = 'page-owner/C4'
        _ = review_page.submit(self.root, self.body(waiting_change, 'question', 'Which setup did you use?'), self.status)
        validating_keys = ['page-owner/C1']
        waiting_investigation = f'investigation/{waiting.owner}/{waiting.id}'
        done_key = f'investigation/{finished.owner}/{finished.id}'
        open_keys = ['page-owner/C2', 'page-owner/C3', question, f'investigation/{ready.owner}/{ready.id}', *proposals]
        expected = {'open':open_keys, 'Done':[],
                    'validationKey':validating_keys[0], 'doneKey':done_key,
                    'waitingInvestigation':waiting_investigation, 'waitingChange':waiting_change,
                    'all':open_keys+validating_keys+[waiting_investigation, waiting_change]}
        pages = {show: browser_markup(self.root, show=show) for show, _ in review_page.SHOWS}
        setup = r"""<script>
const nativeFetch=fetch,nativeTimeout=setTimeout;
let reviewClock=1794907200000;
Date.now=()=>reviewClock;
let updateAges;
window.setInterval=callback=>{if(callback.name==='updateHandoffAges') updateAges=callback;return 0;};
window.clearInterval=()=>{};
window.refreshRequests=[];
const showPages=PAGES;
window.fetch=async(url,options)=>{
 if(options?.method==='POST') return {headers:new Headers(),ok:true,json:async()=>({answer:'saved'})};
 window.refreshRequests.push(url);
 const show=new URL(url,location.href).searchParams.get('show') || 'open';
 return {ok:true,text:async()=>showPages[show]};
};
</script>""".replace('PAGES', json.dumps(pages).replace('</', '<\\/'))
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>nativeTimeout(resolve,80));
 const chip=(value,doc=document)=>doc.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]');
 const pressed=(doc=document)=>[...doc.querySelectorAll('[data-filter-row="show"] [data-filter-value][aria-pressed="true"]')];
 const selected=(value,doc=document)=>pressed(doc).length===1 && pressed(doc)[0].dataset.filterValue===value;
 const count=(value,doc=document)=>chip(value,doc)?.querySelector('b');
 const mounted=(doc=document)=>[...doc.querySelectorAll('[data-list-item]')];
 const noMountedKey=(key,doc=document)=>mounted(doc).every(row=>row.dataset.key!==key);
 const noMountedState=(state,doc=document)=>mounted(doc).every(row=>row.dataset.state!==state);
 const exactKeys=(value,doc=document)=>{
  const actual=[...doc.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).map(row=>row.dataset.key).sort();
  return JSON.stringify(actual)===JSON.stringify(EXPECTED[value].slice().sort());
 };
 try{
  await flush();
  check([...document.querySelectorAll('[data-filter-row="show"] [data-filter-value]')].map(button=>button.dataset.filterValue).join('|')===
    'all|open|Done','The Show chips appear in the required order.');
  check(selected('open'),'exactly one Open chip starts pressed');
  check(exactKeys('open'),'F020: Open mounts the exact Open keys');
  check(noMountedKey(EXPECTED.doneKey),'F064: Open does not mount the Done investigation');
  check(noMountedKey(EXPECTED.waitingInvestigation),'F064: Open does not mount the Waiting investigation');
  check(noMountedState('Done'),'F064: the initial Open view mounts no Done rows');
  check(noMountedKey(EXPECTED.validationKey),'Open must not mount the active check.');
  check(count('open')?.textContent==='6' && count('Done')?.textContent==='0' && count('all')?.textContent==='9',
    'F001: All counts the active checks outside Open.');
  check(getComputedStyle(chip('all')).opacity==='1','A nonzero All chip has full opacity.');
  const search=document.querySelector('#list-search');
  search.focus();
  search.value='no matching title';search.dispatchEvent(new Event('input'));
  check(document.activeElement===search && search.type==='search' && search.tabIndex===0 &&
   new URLSearchParams(location.hash.slice(1)).get('q')==='no matching title',
   'The search input keeps focus and saves the query in the link.');
  check(count('all')?.textContent==='0' && Number(getComputedStyle(chip('all')).opacity)<1,
   'A zero All chip is dim.');
  search.value='';search.dispatchEvent(new Event('input'));
  {
   chip('all').click();await flush();await flush();
   const row=mounted().find(row=>row.dataset.key===EXPECTED.validationKey),toggle=row?.querySelector('.list-toggle');
   check(selected('all') && exactKeys('all') && row?.dataset.state==='Validating',
    'All shows the active check with the other records.');
   check(document.querySelector('[data-filter-row="owner"] [data-filter-value="all"] b')?.textContent==='9' &&
    document.querySelector('[data-filter-row="type"] [data-filter-value="all"] b')?.textContent==='9' &&
    document.querySelector('[data-filter-row="type"] [data-filter-value="change"] b')?.textContent==='4',
    'Owner and Type count the active check in All.');
   check(row?.dataset.parked==='false' && toggle?.tagName==='BUTTON',
    'The active check can open without a Later state.');
   const handoff=row?.querySelector('[data-label="Item"] .review-handoff');
   check(handoff?.textContent==='With page-owner · Validating for 1 h 20 m',
    'The row shows the validator and elapsed time.');
   toggle?.click();
   const form=await readyReviewCard(row);
   check(recordIsOpen(row) && form.textContent.includes('Sends to page-owner') &&
    form.querySelector('[value="question"]')?.textContent==='Ask a question' &&
    recordDetails(row).querySelector('.review-handoff')?.textContent===handoff?.textContent,
    'Open the card with the same elapsed time and send label.');
   reviewClock+=60000;updateAges();
   check(handoff?.textContent==='With page-owner · Validating for 1 h 21 m' &&
    recordDetails(row).querySelector('.review-handoff')?.textContent===handoff?.textContent,
    'The row and card update without a server read.');
   toggle?.click();
   reviewClock-=60000;
   const owner=handoff?.querySelector('span');
   check(owner && getComputedStyle(owner).whiteSpace==='nowrap','The showrunner name stays together.');
   if(innerWidth>960) check(handoff &&
    handoff.getBoundingClientRect().height<=parseFloat(getComputedStyle(handoff).lineHeight)+1,
    'The hand-off text fits on one desktop line.');
   check(/^\d+ (d|h|min)$/.test(row?.querySelector('[data-label="Age"]')?.textContent.trim() || ''),
    'The Age cell retains the age.');
   chip('open').click();await flush();await flush();
  }
  const decisions=()=>[...document.querySelectorAll('[data-list-item]')].filter(row=>
   ['proposal','efficiency'].includes(row.dataset.type));
  check(decisions().length===2 && decisions().every(row=>row.dataset.state==='Open' && !row.hidden &&
   row.querySelector('.list-state').textContent==='Open'),'decision rows use Open in their badge and data-state');
  check(!document.querySelector('[data-list-count]'),
   'The Show chips replace the right count text.');
  const beforeRequests=window.refreshRequests.length,beforeAddress=location.href;
  chip('open')?.click();await flush();
  check(selected('open') && window.refreshRequests.length===beforeRequests && location.href===beforeAddress,
    'pressing the selected chip changes nothing');
  chip('all')?.focus();chip('all')?.click();await flush();await flush();
  const handed=document.querySelector('[data-key="page-owner/C1"]');
  check(handed && !handed.hidden && handed.textContent.includes('With page-owner · Validating for') &&
   handed.querySelector('button.list-toggle'),'All shows the active check and can open its card.');
  check(count('all')?.textContent==='9','All counts the active records.');
  check(noMountedKey(EXPECTED.doneKey) &&
    document.querySelector('[data-filter-row="type"] [data-filter-value="investigation"] b').textContent==='2' &&
    document.querySelector('[data-filter-row="owner"] [data-filter-value="all"] b').textContent==='9',
    'All excludes the completed investigation from the rows, Type counts and Owner counts.');
  check(document.activeElement===chip('all'),'F002: the pressed Show chip keeps focus after the load');
  const ordinary=document.querySelector('[data-key="'+EXPECTED.waitingChange+'"]');
  ordinary?.querySelector('button.list-toggle')?.click();
  const ordinaryForm=await readyReviewCard(ordinary);
  check(ordinary?.dataset.state==='Waiting' && !ordinary.hidden && !ordinaryForm.querySelector('[value="question"]') &&
   !ordinary.querySelector('.review-handoff'), 'An ordinary question stays in Waiting without validation controls.');
  ordinary?.querySelector('button.list-toggle')?.click();
  chip('Done')?.focus();chip('Done')?.click();await flush();await flush();
  check(exactKeys('Done'),'F020: Done mounts the exact keys');
  check(count('Done')?.textContent==='0','F001: Done retains its count after the load');
  check(document.activeElement===chip('Done'),'F002: the pressed Show chip keeps focus after the load');
  chip('all')?.focus();chip('all')?.click();await flush();await flush();
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(document.activeElement===chip('all'),'F002: the Show chip keeps focus after refresh');
  const typeChip=()=>document.querySelector('[data-filter-row="type"] [data-filter-value="change"]');
  typeChip().focus();typeChip().click();
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(document.activeElement===typeChip(),'F002: the type chip keeps focus after refresh');
  document.querySelector('[data-filter-row="type"] [data-filter-value="all"]').click();
  chip('Done')?.click();await flush();await flush();
  check(selected('Done') && new URLSearchParams(location.hash.slice(1)).get('show')==='Done',
    'A Show chip changes the pressed state and address.');
  check(exactKeys('Done'),'Done selects only completed actions.');
  check(['all','open','Done'].map(value=>count(value)?.textContent).join('|')==='9|6|0',
    'Show counts exclude the completed investigation.');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(selected('Done') && new URL(window.refreshRequests.at(-1),location.href).searchParams.has('version'),
    'refresh checks the version and retains the pressed Show view');
  const reload=document.createElement('iframe');reload.src='/reload'+location.hash;
  const loaded=new Promise(resolve=>reload.addEventListener('load',resolve,{once:true}));document.body.append(reload);await loaded;
  await flush();await flush();
  check(selected('Done',reload.contentDocument) && reload.contentDocument.querySelector('[data-review]').dataset.show==='Done',
    'a reload keeps the shared Show address');reload.remove();
  for(const value of ['Done','all','open']){
   chip(value)?.click();await flush();await flush();
   check(selected(value),'exactly one chip stays pressed for '+value);
   check(exactKeys(value),'F020: the selected view mounts its exact keys for '+value);
   check(noMountedKey(EXPECTED.doneKey) && noMountedState('Done'),'Done stays hidden under '+value+'.');
   if(value==='open') check(decisions().length===2 && decisions().every(row=>!row.hidden),
    'Open retains the two decision rows after a Show load');
  }
  chip('all')?.click();await flush();await flush();
  check(['all','open','Done'].map(value=>count(value)?.textContent).join('|')==='9|6|0' &&
    [...document.querySelectorAll('[data-filter-row="show"] [data-filter-value] b')].every(number=>!number.hidden),
    'All supplies the count for every Show chip');
  const rows=[...document.querySelectorAll('[data-review] [data-filter-row]')];
  const groups=rows.map(row=>row.querySelector(':scope > div'));
  const buttons=[...document.querySelectorAll('[data-filter-row="show"] [data-filter-value]')];
  const labels=groups.map(group=>group.previousElementSibling);
  const words=search.getBoundingClientRect(),typeRow=groups[0].parentElement.getBoundingClientRect();
  const line=document.querySelector('[data-page-status]').getBoundingClientRect();
  const stacked=matchMedia('(max-width:800px)').matches;
  check(words.bottom<=typeRow.top && Math.abs(words.right-typeRow.right)<1,
   'The search input is above Type and ends at the right edge.');
  check(stacked ? words.top>=line.bottom && Math.abs(words.left-typeRow.left)<1 :
   words.left>=line.right &&
   Math.abs((words.top+words.bottom)-(line.top+line.bottom))<2,
   'The search input shares the status row on desktop and spans the row below the status on a phone.');
  check(labels.map(label=>label.textContent).join('|')==='Type|Show|Owner' &&
    rows.every((row,index)=>row.getAttribute('aria-label')===labels[index].textContent),
    'The three filter rows have visible and accessible labels');
  const narrow=matchMedia('(max-width:480px)').matches;
  check(groups.every((group,index)=>{
    const chips=group.getBoundingClientRect(),label=labels[index].getBoundingClientRect();
    return narrow ? chips.left===label.left && chips.top>=label.bottom : chips.left>=label.right;
  }),'Chips align to the right of the label on desktop and below it on narrow screens');
  check(groups.length===3 && groups[1].getBoundingClientRect().top>=groups[0].getBoundingClientRect().bottom &&
    groups[2].getBoundingClientRect().top>=groups[1].getBoundingClientRect().bottom,
    'Show follows Type and Owner follows Show');
  check(buttons.length===3 && buttons.every(button=>button.type==='button' && button.tabIndex===0),
    'Show chips use native keyboard buttons in tab order');
  chip('all')?.focus();
  check(document.activeElement===chip('all'),'a Show chip accepts keyboard focus');
  check(document.documentElement.scrollWidth<=innerWidth && groups.every(group=>
    getComputedStyle(group).gap==='8px' && [...group.querySelectorAll('button')].every(button=>{
     const box=button.getBoundingClientRect();return box.left>=0 && box.right<=innerWidth;
    })),'the three chip rows wrap with even spacing and no clipping');

  for(const [address,value,ownerSort] of [['?show=Waiting','open'],['?show=validating','open'],
      ['?state=Waiting','open'],['#state=Waiting','open'],['?state=Waiting&show=open','open'],
      ['?show=all#state=Validating','all'],['?state=Waiting#show=Validating','open'],
      ['?show=Waiting#show=Validating','open'],
      ['?show=Waiting&sort=Age&dir=asc#state=Validating&sort=Owner&dir=desc','open',true]]){
   const linked=document.createElement('iframe');
   linked.src='/reload'+address;
   const loaded=new Promise(resolve=>linked.addEventListener('load',resolve,{once:true}));
   document.body.append(linked);await loaded;await flush();await flush();
   check(selected(value,linked.contentDocument) && exactKeys(value,linked.contentDocument),
    'A direct link selects the Show chip and shows its items: '+address);
   if(ownerSort) check(linked.contentDocument.querySelector('[data-sort="2"]').parentElement
    .getAttribute('aria-sort')==='descending',
    'The Review link sorts by Owner in descending order.');
   linked.remove();
  }
  for(const spelling of ['For you','for-you','for you','FOR YOU']){
   const legacyShow=document.createElement('iframe');
   legacyShow.src='/reload#'+new URLSearchParams({show:spelling});
   const loadedShow=new Promise(resolve=>legacyShow.addEventListener('load',resolve,{once:true}));
   document.body.append(legacyShow);await loadedShow;await flush();await flush();
   const legacyPage=legacyShow.contentDocument;
   check(selected('open',legacyPage) &&
    [...legacyPage.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).every(row=>row.dataset.state==='Open') &&
    new URLSearchParams(legacyShow.contentWindow.location.hash.slice(1)).get('show')==='open',
    'the legacy Show spelling opens Open: '+spelling);
   legacyShow.remove();
  }
  const ownerChip=(value,doc=document)=>doc.querySelector('[data-filter-row="owner"] [data-filter-value="'+(value ? CSS.escape(staffChipValue(value)) : 'all')+'"]');
  const ownerChips=()=>[...document.querySelectorAll('[data-filter-row="owner"] [data-filter-value]:not([hidden])')];
  const selectedOwner=(doc=document)=>doc.querySelector('[data-filter-row="owner"] [data-filter-value][aria-pressed="true"]');
  const chooseOwner=value=>ownerChip(value).click();
  const chooseType=async value=>{
   const button=document.querySelector('[data-filter-row="type"] [data-filter-value="'+(value || 'all')+'"]');
   if(!button.hidden){button.click();return;}
   const params=new URLSearchParams(location.hash.slice(1));params.set('type',value);
   const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
   location.hash=params;await changed;
  };
  chip('open').click();await flush();await flush();
  const builder=document.querySelector('[data-type="investigation"]').dataset.sourceOwner;
  const builderChip=ownerChip(builder),builderName=builderChip.querySelector('.staff-name');
  check(builderName?.dataset.colorText==='role.unit-director' &&
    getComputedStyle(builderName).color!==getComputedStyle(builderChip).color,
    'An unselected Owner name uses its role color');
  chooseOwner(builder);
  check(builderChip.getAttribute('aria-pressed')==='true' &&
    getComputedStyle(builderName).color===getComputedStyle(builderChip).color,
    'A selected Owner name inherits the chip color');
  await chooseType('question');
  check(ownerChips().length===2 && ownerChips()[0].textContent==='All 1' &&
    ownerChips().some(button=>button.textContent==='page-owner 1') &&
    ownerChip(builder).hidden,
    'F007: Owner lists only owners with the selected type and words in any Show.');
  check(filterControl.ownerId(selectedOwner())==='' && !new URLSearchParams(location.hash.slice(1)).get('owner') &&
    ['all','open','Done'].map(value=>count(value)?.textContent).join('|')==='1|1|0',
    'F007: an owner without the selected type resets to All with matching counts.');
  check([...document.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1,
    'F007: the question appears after the owner resets.');
  chooseType('investigation');
  check(ownerChips().some(button=>filterControl.ownerId(button)===builder) && ownerChips()[0].textContent==='All 1',
    'a type change restores owners and counts');
  chooseOwner(builder);chip('all').click();await flush();await flush();
  check(ownerChips()[0].textContent==='All 2' && filterControl.ownerId(selectedOwner())===builder,
    'Show limits Owner counts and retains an owner with matching loaded rows');
  chip('open').click();await flush();await flush();
  await chooseType('question');chooseOwner(filterControl.ownerId(ownerChips().find(button=>button.dataset.ownerName==='page-owner')));
  chip('Done').click();await flush();await flush();
  check(ownerChips().length===2 && ownerChips()[0].textContent==='All 0' && filterControl.ownerId(selectedOwner())!=='' &&
    ownerChips()[1].textContent==='page-owner 0',
    'Show retains the selected owner with a zero count.');
  chip('open').click();await flush();await flush();
  check(ownerChips().length===2 && ownerChips()[0].textContent==='All 1','Show restores matching owners');
  chooseType('investigation');chooseOwner(builder);
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(filterControl.ownerId(selectedOwner())===builder && ownerChips()[0].textContent==='All 1' &&
    ownerChips().every(button=>button.querySelector('b').textContent!=='0'),'refresh retains the matching owner list and selection');
  const legacy=document.createElement('iframe');
  legacy.src='/reload#'+new URLSearchParams({owner:builder,type:'question',show:'open'});
  const legacyLoaded=new Promise(resolve=>legacy.addEventListener('load',resolve,{once:true}));document.body.append(legacy);
  await legacyLoaded;await flush();await flush();
  check(filterControl.ownerId(selectedOwner(legacy.contentDocument))==='' &&
    [...legacy.contentDocument.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1 &&
    !new URLSearchParams(legacy.contentWindow.location.hash.slice(1)).get('owner'),
    'F007: a legacy owner without the selected type resets to All.');legacy.remove();
  await chooseType('question');
  const navigated=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams({owner:'builder-unit',type:'investigation',show:'open'});
  await navigated;await flush();await flush();
  check(filterControl.ownerId(selectedOwner())===builder && [...document.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1,
    'F004: hash navigation resolves an owner name whose chip was hidden by Questions');
  const translated=document.createElement('iframe');
  translated.src='/reload#'+new URLSearchParams({legacy:'investigation',owner:'no-investigations',type:'investigation',show:'not-read'});
  const translatedLoaded=new Promise(resolve=>translated.addEventListener('load',resolve,{once:true}));
  document.body.append(translated);await translatedLoaded;await flush();await flush();
  const translatedPage=translated.contentDocument,translatedAddress=new URLSearchParams(translated.contentWindow.location.hash.slice(1));
  check(filterControl.ownerId(selectedOwner(translatedPage))==='' &&
    selectedOwner(translatedPage).textContent==='All 1' &&
    !translatedAddress.get('owner') && !translatedAddress.has('source-owner') &&
    [...translatedPage.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1 &&
    count('open',translatedPage)?.textContent==='1',
    'F003: a legacy investigation reset clears both owner constraints and shows the All rows');
  translated.remove();
  await chooseType('question');chooseOwner(filterControl.ownerId(ownerChips().find(button=>button.dataset.ownerName==='page-owner')));
  const questionOwner=filterControl.ownerId(selectedOwner());
  const hasEmptyOwnerSelection=()=>ownerChips().length===2 && ownerChips()[0].textContent==='All 0' &&
   filterControl.ownerId(selectedOwner())===questionOwner && selectedOwner().textContent==='page-owner 0';
  const question=document.querySelector('[data-type="question"]');question.querySelector('.list-toggle').click();await readyReviewCard(question);
  const form=recordDetails(question).querySelector('[data-review-form]');form.elements.feedback.value='Use this layout.';
  form.querySelector('[value="answer"]').click();await new Promise(resolve=>nativeTimeout(resolve,1100));
  check(hasEmptyOwnerSelection() && !question.isConnected && count('all').textContent==='0',
    'A save removes the completed question from the Owner and Show counts.');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(hasEmptyOwnerSelection() && count('all').textContent==='0',
    'Refresh keeps the completed question out of the counts.');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>""".replace('EXPECTED', json.dumps(expected))
        for width in (1440, 390):
            with self.subTest(width=width):
                base = pages['open'].replace('<head>', '<head>'+setup, 1)
                body = base.replace('</body>', probe+'</body>').encode()
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        path = urlsplit(self.path).path
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        if path == '/complete':
                            _ = complete.wait(20)
                        else:
                            _ = self.wfile.write(base.encode() if path == '/reload' else body if path == '/page' else
                                b'<iframe src="/page" style="border:0;width:100vw;height:100vh"></iframe><img hidden src="/complete">')

                    def do_POST(self) -> None:
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
                        with tempfile.TemporaryDirectory(dir=self.fixture.temporary) as profile:
                            done = run_process_group(['firefox', '--headless', '--no-remote', '--profile', profile,
                                '--window-size', f'{width},900', '--screenshot', str(self.fixture.temporary/'show-chips.png'),
                                f'http://127.0.0.1:{server.server_port}/'], timeout=30)
                        self.assertEqual(done.returncode, 0, done.stderr)
                    finally:
                        server.shutdown()
                        thread.join(TEST_HANG_GUARD_SECS)
                self.assertEqual(results, [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_filter_counts_share_the_complete_index(self) -> None:
        _ = self.question()
        _ = self.investigation(False)
        parked = investigation.file(self.fixture.unit.id, 'Later count fixture', 'Read the log.',
                                    waiting_why='The owner checks the log.')
        deadline = datetime(2099, 1, 1, tzinfo=user_zone())
        review_later.save(f'investigation/{parked.owner}/{parked.id}', deadline)
        _ = review_page.submit(self.root, self.body('page-owner/C1', 'works'), self.status)
        pages = {show: browser_markup(self.root, show=show) for show, _ in review_page.SHOWS}
        setup = r"""<script>
const nativeFetch=fetch,nativeTimeout=setTimeout;
window.setInterval=()=>0;window.clearInterval=()=>{};
const showPages=PAGES;
window.fetch=async(url,options)=>{
 if(options?.method==='POST'){window.didSave=true;return {headers:new Headers(),ok:true,json:async()=>({answer:'saved'})};}
 const show=new URL(url,location.href).searchParams.get('show') || 'open';
 const page=new DOMParser().parseFromString(showPages[show],'text/html');
 if(window.didSave){const payload=page.querySelector('[data-review-index]'),entries=JSON.parse(payload.textContent);
  entries.r=entries.r.filter(entry=>entry[2]!==0);
  entries.r.find(entry=>entry[0]==='page-owner/C2')[6]=2;payload.textContent=JSON.stringify(entries);}
 return {ok:true,text:async()=>page.documentElement.outerHTML};
};
</script>""".replace('PAGES', json.dumps(pages).replace('</', '<\\/'))
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>nativeTimeout(resolve,120));
 const show=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]');
 const type=value=>document.querySelector('[data-filter-row="type"] [data-filter-value="'+value+'"]');
 const counts=selector=>[...document.querySelectorAll(selector+' b')].map(node=>Number(node.textContent)).join('|');
 const search=value=>{const field=document.querySelector('#list-search');field.value=value;field.dispatchEvent(new Event('input'));};
 try{
  await flush();
  check(!document.querySelector('[data-list-count]'),'The Show chips replace the right count text.');
  check(counts('[data-filter-row="show"] [data-filter-value]')==='4|3|0','The index counts states outside the rendered view.');
  const allCount=show('all').querySelector('b');
  const nativeNow=Date.now,deadline=Date.parse(LATER_DEADLINE);
  let crossedDeadline=false;
  try{
   Date.now=()=>deadline-1;
   Object.defineProperty(allCount,'textContent',{configurable:true,set(value){
    delete this.textContent;
    this.textContent=value;
    Date.now=()=>deadline;
    crossedDeadline=true;
   }});
   search('Later count fixture');
   check(crossedDeadline && counts('[data-filter-row="show"] [data-filter-value]')==='0|0|0',
    'Show counts add up when a Later item becomes eligible after the All count.');
   search('Later count fixture');
   check(counts('[data-filter-row="show"] [data-filter-value]')==='1|0|0','The next render counts the Waiting item.');
  }finally{
   delete allCount.textContent;
   Date.now=nativeNow;
   search('');
  }
  const ownerChip=value=>document.querySelector('[data-filter-row="owner"] [data-filter-value="'+(value ? CSS.escape(staffChipValue(value)) : 'all')+'"]');
  const selectedOwner=()=>document.querySelector('[data-filter-row="owner"] [data-filter-value][aria-pressed="true"]');
  const builder=document.querySelector('[data-type="change"]').dataset.sourceOwner;
  const chooseOwner=value=>ownerChip(value).click();
  show('all').click();await flush();await flush();
  chooseOwner(builder);
  check(counts('[data-filter-row="show"] [data-filter-value]')==='1|0|0','Show counts use the selected owner.');
  check(type('change').querySelector('b').textContent==='2' && type('investigation').querySelector('b').textContent==='1',
   'Type counts include the rows that appear if the Owner filter returns to All.');
  type('change').click();
  check(filterControl.ownerId(selectedOwner())==='' && counts('[data-filter-row="show"] [data-filter-value]')==='2|2|0',
   'F007: an owner without the selected type resets to All.');
  type('investigation').click();
  chooseOwner(builder);show('open').click();await flush();await flush();
  check(filterControl.ownerId(selectedOwner())===builder && counts('[data-filter-row="show"] [data-filter-value]')==='1|0|0' &&
   [...document.querySelectorAll('[data-list-item]')].every(row=>row.hidden),
   'F007: the user case keeps an owner with rows in another Show.');
  show('all').click();await flush();await flush();
  check(counts('[data-filter-row="show"] [data-filter-value]')==='1|0|0','Show counts combine the type and owner.');
  search('gate');
  check(counts('[data-filter-row="show"] [data-filter-value]')==='1|0|0' && ownerChip('').textContent==='All 1',
   'All counts combine the words, owner, type and Show filters.');
  search('');chooseOwner('');type('all').click();show('open').click();await flush();await flush();
  type('change').click();
  check(counts('[data-filter-row="show"] [data-filter-value]')==='2|2|0','Show counts use the selected type.');
  search('Numbered');
  check(counts('[data-filter-row="show"] [data-filter-value]')==='1|1|0','Show counts use the words filter.');
  check(type('change').querySelector('b').textContent==='1' && type('question').querySelector('b').textContent==='0',
   'Type counts use the words filter and Show.');
  check(selectedOwner().textContent==='All 1',
   'Owner counts use the type, Show and words.');
  search('no matching title');
  check(counts('[data-filter-row="show"] [data-filter-value]')==='0|0|0' && [...document.querySelectorAll('[data-filter-row="type"] [data-filter-value] b')].every(n=>n.textContent==='0') &&
   selectedOwner().textContent==='All 0',
   'An empty filtered view has zero in every applicable count.');
  search('');show('all').click();await flush();await flush();
  check(type('change').querySelector('b').textContent==='2','Type counts include the selected Show.');
  check(type('question').hidden,'Questions are absent from the Review Type choices.');
  const place=new URLSearchParams(location.hash.slice(1));place.set('type','question');
  const navigated=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=place;await navigated;
  const row=document.querySelector('[data-list-item][data-type="question"]');
  row.querySelector('.list-toggle').click();await readyReviewCard(row);
  const form=recordDetails(row).querySelector('form');form.elements.feedback.value='Use the first layout.';
  form.requestSubmit(form.querySelector('[value="answer"]'));
  await new Promise(resolve=>nativeTimeout(resolve,1100));
  check(counts('[data-filter-row="show"] [data-filter-value]')==='0|0|0','A save removes the completed entry from all Show counts.');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();await flush();
  check(counts('[data-filter-row="show"] [data-filter-value]')==='0|0|0','A refresh retains the saved state from the server index.');
  type('change').click();
  check(counts('[data-filter-row="show"] [data-filter-value]')==='2|1|0','A refresh replaces an unsaved entry in the index.');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/result',{method:'POST',body:JSON.stringify(failures)});
})();</script>""".replace('LATER_DEADLINE', json.dumps(deadline.isoformat()))
        for width in (1440, 390):
            with self.subTest(width=width):
                base = pages['open'].replace('<head>', '<head>'+setup, 1)
                body = base.replace('</body>', probe+'</body>').encode()
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        if self.path == '/complete':
                            _ = complete.wait(20)
                        else:
                            _ = self.wfile.write(base.encode() if self.path == '/reload' else body if self.path == '/page' else
                                b'<iframe src="/page" style="border:0;width:100vw;height:100vh"></iframe><img hidden src="/complete">')

                    def do_POST(self) -> None:
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
                        with tempfile.TemporaryDirectory(dir=self.fixture.temporary) as profile:
                            done = run_process_group(['firefox', '--headless', '--no-remote', '--profile', profile,
                                '--window-size', f'{width},900', '--screenshot', str(self.fixture.temporary/'show-chips.png'),
                                f'http://127.0.0.1:{server.server_port}/'], timeout=30)
                        self.assertEqual(done.returncode, 0, done.stderr)
                    finally:
                        server.shutdown()
                        thread.join(TEST_HANG_GUARD_SECS)
                self.assertEqual(results, [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_question_chip_counts_match_rows_while_a_saved_followup_awaits_refresh(self) -> None:
        answered = self.question()
        remaining = self.question('What should the header say?')
        other_source = fixture_member('unit', name='other-source', parent=self.fixture.unit.parent)
        _ = self.fixture.file_user_question('What should the footer say?', asker=other_source)
        other_owner = fixture_member('showrunner', name='other-owner')
        other_question = self.fixture.file_user_question('Which label should the button use?', asker=other_source)
        records = [replace(item, owner=other_owner.id) if item.key == other_question else item
                   for item in review_items.read(self.root)]
        with patch.object(review_items, 'read', return_value=records):
            markup = browser_markup(self.root)
        place = '#' + urlencode({'show': 'all', 'type': 'question', 'owner': answered.split('/')[1],
                                  'source-owner': self.fixture.unit.id})
        setup = '<script>history.replaceState(null, "", '+json.dumps(place)+');</script>'
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const content=document.querySelector('[data-review]'),fetchPage=window.fetch;
 const answered=ANSWERED,remaining=REMAINING;
 const rows=()=>[...content.querySelectorAll('[data-list-item]:not([hidden])')];
 const chip=(kind,value)=>content.querySelector('[data-filter-row="'+kind+'"] [data-filter-value="'+CSS.escape(value)+'"]');
 const ownerChip=id=>chip('owner',id ? staffChipValue(id) : 'all');
 const checkCounts=expected=>{
  const shown=rows().length;
  check(shown===expected,'Expected '+expected+' visible Questions, got '+shown+'.');
  for(const kind of ['type','show','owner']){
   const selected=content.querySelector('[data-filter-row="'+kind+'"] [data-filter-value][aria-pressed="true"]');
   check(selected?.querySelector('b')?.textContent===String(shown),
    'The selected '+kind+' chip counts the visible Questions.');
  }
 };
 let releaseRefresh;
 const refreshResponse=new Promise(resolve=>{releaseRefresh=resolve;});
 window.fetch=(url,options)=>{
  if(options?.method==='POST' && options.body.get('row')===answered && options.body.get('outcome')==='question')
   return Promise.resolve(Response.json({answer:'question sent',state:'Waiting',type:'question',later:0}));
  const params=new URL(url,location.href).searchParams;
  if(params.has('version') && !params.has('body')) return refreshResponse;
  return fetchPage(url,options);
 };
 try{
  frameElement.style.width='1280px';frameElement.style.height='720px';
  check(content.dataset.show==='all' &&
   chip('type','question').getAttribute('aria-pressed')==='true',
   'Review starts with Show All and the Question type.');
  const row=rows().find(item=>item.dataset.key===answered);
  const owner=ownerChip(row.dataset.owner);
  check(owner.getAttribute('aria-pressed')==='true' && owner.querySelector('b').textContent==='2',
   'The selected Owner chip excludes the Question from the other source.');
  checkCounts(2);
  row.querySelector('.list-toggle').click();
  const form=await readyReviewCard(row);
  const saved=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{
    if(!form.dataset.sending){observer.disconnect();resolve();}
   });
   observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
  });
  form.elements.feedback.value='Can the compact layout show all controls?';
  form.requestSubmit(form.querySelector('[value="question"]'));
  if(!form.dataset.sending) throw new Error('The Question follow-up did not start.');
  await saved;
  const status=form.querySelector('[data-send-status]');
  check(status.textContent==='' && status.hidden,'The saved Question follow-up leaves the status empty and hidden.');
  check(rows()[0]?.dataset.key===remaining,'The other Question stays visible.');
  checkCounts(1);
  check(!chip('show','Waiting'), 'Review has no Waiting chip.');
  const other=ownerChip(OTHER_OWNER),all=ownerChip('');
  const otherCount=Number(other.querySelector('b').textContent),allCount=Number(all.querySelector('b').textContent);
  check(otherCount===1 && allCount===3,'The other Owner and All counts include Questions from the other source.');
  other.click();
  check(other.getAttribute('aria-pressed')==='true' && !new URLSearchParams(location.hash.slice(1)).has('source-owner'),
   'Selecting another Owner clears the source filter.');
  check(rows().length===otherCount && rows()[0]?.dataset.key===OTHER_QUESTION,
   'The other Owner count equals the number of Questions after the click.');
  all.click();
  check(rows().length===allCount,'The All count equals the number of Questions after the click.');
 }catch(error){failures.push(String(error));}
 finally{releaseRefresh(Response.json({version:content.dataset.version,facts:pageRefreshFacts()}));}
 await fetchPage('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''.replace('ANSWERED', json.dumps(answered)).replace('REMAINING', json.dumps(remaining)).replace(
            'OTHER_OWNER', json.dumps(other_owner.id)).replace('OTHER_QUESTION', json.dumps(other_question))
        page = markup.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>', 1)
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_each_review_chip_counts_the_rows_after_its_click(self) -> None:
        question = self.question()
        other_question = self.question('What should the other owner check?')
        _ = self.investigation(False)
        _ = self.file_response(self.ready_submission_recipient().id)
        opened = for_you.ForYouRow(f'action/{uuid4()}', 'action', 'Restore access', '', 'natedev',
                                  '/tmp', 'builder-unit', '', '', '2026-11-17T15:00:00+00:00',
                                  'open', 'manual', '')
        completed = replace(opened, key=f'action/{uuid4()}', title='Completed action', status='done')
        other = fixture_member('showrunner', name='other-owner')
        records = [replace(item, owner=other.id)
                   if item.key == other_question or item.type == review_items.ReviewType.CHANGE else item
                   for item in review_items.read(self.root)]
        with (patch.object(review_items, 'read', return_value=records),
              patch.object(for_you, 'read', return_value=for_you.ForYouSnapshot((opened, completed), ()))):
            markup = browser_markup(self.root, show='all')
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const review=document.querySelector('[data-review]');
 const rows=()=>[...review.querySelectorAll('[data-list-item]:not([hidden])')];
 const chip=(group,value)=>review.querySelector('[data-filter-row="'+group+'"] [data-filter-value="'+
  CSS.escape(group==='owner' ? staffChipValue(value) : value)+'"]');
 const question=QUESTION,owner=question.split('/')[1];
 const place=(type='question',show='all')=>{
  history.replaceState({recordTable:{opened:[]}},'','#'+new URLSearchParams({show,type,owner}));
  dispatchEvent(new HashChangeEvent('hashchange'));
 };
 const clickAndCheck=button=>{
  const label=button.textContent.trim(),count=Number(button.querySelector('b').textContent);
  button.click();
  check(rows().length===count,label+' shows '+rows().length+' rows after the click.');
 };
 const fetchPage=window.fetch;
 window.fetch=(url,options)=>{
  if(options?.method==='POST' && options.body.get('row')===question && options.body.get('outcome')==='later')
   return Promise.resolve(Response.json({answer:'later',state:'Open',later:Date.now()+3600000}));
  const params=new URL(url,location.href).searchParams;
  if(params.has('version') && !params.has('body'))
   return Promise.resolve(Response.json({version:review.dataset.version,facts:pageRefreshFacts()}));
  return fetchPage(url,options);
 };
 try{
  frameElement.style.width='1280px';frameElement.style.height='900px';
  window.matchMedia=()=>({matches:true});
  place();
  check(rows().length===1 && rows()[0].dataset.key===question,'The Question starts in the selected view.');
  const buttons=[...review.querySelectorAll('[data-filter-row] [data-filter-value]')].filter(button=>!button.hidden);
  for(const button of buttons){place();clickAndCheck(button);}
  place('investigations');clickAndCheck(chip('type','investigations'));
  place();
  clickAndCheck(chip('type','for-you'));
  check(review.dataset.show==='open' && rows().length===1 && rows()[0].dataset.state==='Open',
   'For you opens the single Open action from All.');
  clickAndCheck(chip('show','all'));
  check(rows().length===2,'For you in All shows both actions.');
  clickAndCheck(chip('type','for-you'));
  check(review.dataset.show==='all','The selected For you chip keeps All.');
  place('response','Validating');
  clickAndCheck(chip('type','response'));
  check(review.dataset.show==='open' && rows().length===1 && rows()[0].dataset.type==='response',
   'Responses opens the single response from Validating.');
  place();
  const [row]=rows();
  row.querySelector('.list-toggle').click();
  const form=await readyReviewCard(row);
  const saved=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{
    if(!form.dataset.sending){observer.disconnect();resolve();}
   });
   observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
  });
  form.querySelector('[data-later-toggle]').click();
  form.querySelector('[data-later-preset="soon"]').click();
  if(!form.dataset.sending) throw new Error('Later did not start.');
  await saved;
  check(rows().length===0 && !review.querySelector('[data-empty]').hidden,'Later empties the selected Question view.');
  for(const button of [chip('type','question'),chip('show','all'),chip('owner',owner)]){
   check(button.getAttribute('aria-pressed')==='true' && button.querySelector('b').textContent==='0',
    'The selected chip displays zero in the empty view.');
   clickAndCheck(button);
  }
  for(const button of buttons){place();if(!button.hidden) clickAndCheck(button);}
 }catch(error){failures.push(String(error));}
 await fetchPage('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''.replace('QUESTION', json.dumps(question))
        self.assertEqual(firefox_probe_results(markup.replace('</body>', probe+'</body>', 1).encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_owner_counts_follow_the_type_and_show_chips(self) -> None:
        _ = self.decisions()
        _ = self.question()
        with patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed.")):
            _ = self.investigation()
        _ = investigation.file(self.fixture.unit.id, 'Check the build', 'Which step stopped?',
                               waiting_why='The owner checks the build.', kind=investigation.InvestigationKind.BUG)
        owner = self.ready_submission_recipient()
        _ = self.file_response(owner.id)
        examples = {item.type: item for item in review_items.read(self.root)}
        owners = {fixture_member('showrunner', name=name).id: name
                  for name in ('natedev', 'second-owner', 'third-owner')}
        state_type = review_items.ReviewState
        records: list[review_items.ReviewItem] = []
        for owner_index, owner in enumerate(owners):
            for type_index, kind in enumerate(review_items.REVIEW_TYPES):
                active = state_type.VALIDATING if kind == review_items.ReviewType.CHANGE else state_type.WAITING
                states = ((state_type.OPEN, state_type.DONE) if kind == review_items.ReviewType.RESPONSE
                          else (state_type.OPEN, active, state_type.DONE))
                record_states = list(states)
                for offset in range(owner_index + type_index):
                    record_states.append(states[(type_index + offset) % len(states)])
                for number, state in enumerate(record_states, 1):
                    key = f'{kind}/{owner}/{number}'
                    records.append(replace(examples[kind], key=key, id=key, owner=owner, source_unit=owner,
                                           state=state, actions=()))

        validating = next(item for item in records if item.state == state_type.VALIDATING)
        answered_key = 'change/validation-answer'
        records.append(replace(validating, key=answered_key, id=answered_key, answer_time=datetime.now(user_zone())))

        show_states = {'open': (state_type.OPEN,),
                       'all': review_items.selected_states('active')}
        expected: dict[str, dict[str, dict[str, int]]] = {}
        for show, selected_states in show_states.items():
            expected[show] = {}
            for kind in ('', *review_items.REVIEW_TYPES):
                if kind == review_items.ReviewType.BUG:
                    continue
                counts = {str(owner): sum(item.owner == owner and
                                          (item.state in selected_states or show == 'open' and item.key == answered_key) and
                                          (not kind or item.type == kind) for item in records)
                          for owner in owners}
                expected[show][kind] = {'': sum(counts.values()), **counts}

        active = [item for item in records if item.state != state_type.DONE]
        with patch.object(review_items, 'read', return_value=active):
            page = review_page.render(self.root).decode()

        setup = r"""<script>
const nativeFetch=fetch;
let reads=0;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.fetch=async()=>{reads++;throw new Error('An index filter requested the server.');};
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const expected=EXPECTED;
 const chip=(group,value)=>document.querySelector('[data-filter-row="'+group+'"] [data-filter-value="'+
  (group==='owner' && value ? CSS.escape(staffChipValue(value)) : value || 'all')+'"]');
 const selected=group=>document.querySelector('[data-filter-row="'+group+'"] [data-filter-value][aria-pressed="true"]')
  ?.dataset.filterValue;
 const count=owner=>chip('owner',owner)?.querySelector('b')?.textContent;
 const counts=()=>Object.keys(expected.open['']).map(count).join('|');
 const checkCounts=(show,type)=>{
  const wanted=expected[show][type];
  const visible=[...document.querySelectorAll('[data-filter-row="owner"] [data-filter-value]:not([hidden])')];
  check(visible.length===Object.keys(wanted).length && visible.every(button=>Object.hasOwn(wanted,filterControl.ownerId(button))),
   show+' / '+(type || 'All types')+': the Owner chips match the fixture owners.');
  for(const [owner,total] of Object.entries(wanted)){
   const label=chip('owner',owner)?.dataset.ownerName || owner || 'All';
   check(count(owner)===String(total),
    show+' / '+(type || 'All types')+' / '+label+': expected '+total+', got '+count(owner));
  }
 };
 const changeShow=show=>{
  chip('show',show).click();
  check(document.querySelector('[data-review]').dataset.show===show,'Show updates the current index to '+show+'.');
 };
 try{
  const beforeType=counts();
  chip('type','change').click();
  check(selected('type')==='change' && selected('show')==='open' && counts()!==beforeType,
   'The Owner counts change when only Type changes.');
  const beforeShow=counts();
  changeShow('all');
  check(selected('type')==='change' && selected('show')==='all' && counts()!==beforeShow,
   'The Owner counts change when only Show changes.');
  checkCounts('all','change');
  chip('type','').click();checkCounts('all','');
  const allTypes=counts();
  chip('type','change').click();checkCounts('all','change');
  check(counts()!==allTypes,'Type alone changes the Owner counts in Show All.');
  const allStates=counts();
  changeShow('open');checkCounts('open','change');
  check(counts()!==allStates,'Show alone changes the Owner counts for Changes.');
  for(const [show,types] of Object.entries(expected)){
   if(selected('show')!==show) changeShow(show);
   for(const type of Object.keys(types)){
    const button=chip('type',type);
    if(button.hidden){
     const address=new URLSearchParams({type,show});
     history.replaceState({recordTable:{opened:[]}},'','#'+address);
     dispatchEvent(new HashChangeEvent('hashchange'));
    }else{
     button.click();
    }
    check(selected('type')===(type || 'all'), 'Review selects the requested type.');
    checkCounts(show,type);
   }
  }
  await new Promise(resolve=>requestAnimationFrame(resolve));
  check(reads===0,'Owner counts update without a server request.');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>""".replace('EXPECTED', json.dumps(expected))
        markup = page.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>', 1)
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    def test_server_supplies_the_selected_show_and_complete_index(self) -> None:
        ready = self.investigation()
        waiting = self.investigation(False)
        _ = investigation.mark_read(ready.owner, ready.id)
        done_key = f'investigation/{ready.owner}/{ready.id}'
        waiting_key = f'investigation/{waiting.owner}/{waiting.id}'
        done = review_page.render(self.root, show='Done').decode()
        self.assertIn('data-show="Done"', done)
        self.assertIn('data-filter-value="Done" aria-pressed="true"', done)
        all_items = review_page.render(self.root, show='all').decode()
        self.assertNotIn(done_key,{entry['key'] for entry in unpack_index(all_items.split('data-review-index>',1)[1].split('</script>',1)[0])})
        self.assertIn(waiting_key,{entry['key'] for entry in unpack_index(all_items.split('data-review-index>',1)[1].split('</script>',1)[0])})
        for requested, show in (('open', 'open'), ('Waiting', 'open'), ('Validating', 'open'),
                                ('validating', 'open'), ('all', 'all')):
            with self.subTest(show=requested):
                source = review_page.render(self.root, show=requested).decode()
                self.assertIn(f'data-show="{show}"', source)
                self.assertIn(f'data-filter-value="{show}" aria-pressed="true"', source)
                owner_menu = source.partition('id="list-owner"')[2].partition('</div>')[0]
                self.assertIn(f'data-filter-value="staff:{waiting.owner}"', owner_menu,
                              'F002: the unit Owner option remains when its Show view has no rows')

    def test_move_review_bugs_keeps_the_original_words_and_image(self) -> None:
        from base64 import b64decode

        from scripts.review_links.test_review_submissions import IMAGE, PNG

        owner = self.ready_submission_recipient()
        keeper = fixture_member("producer", name="morty")
        words = "The menu clips its final choice.\n\nClose it and open it again."
        request = replace(self.submission_request(new_item_dialog.SubmissionKind.BUG, owner.id),
                          words=words, images=(IMAGE,))
        old = review_submissions.submit(request)
        key = review_items.submission_review_key(old)
        self.assertTrue(any(entry["key"] == key for entry in review_page.build_snapshot(self.root).entries))
        save = investigation._save  # pyright: ignore[reportPrivateUsage]

        def fail_archive(store: investigation.Investigations, item: investigation.Investigation,
                         path: Path) -> investigation.Investigation:
            if isinstance(item.list_destination, investigation.ListDestination):
                raise OSError("The fixture interrupted the archive write.")
            return save(store, item, path)

        scoped = new_item_dialog.read_submission_key(old.key)
        self.assertTrue(investigation.answer_submission(old.path, scoped, "Check the final choice.", old.reply_token,
                                                        owner=owner.id))
        with patch.object(investigation, "_save", side_effect=fail_archive):
            with self.assertRaisesRegex(OSError, "interrupted the archive write"):
                _ = the_list.move_review_bugs(staff.snapshot())
        partial, = the_list.records(keeper.id)
        self.assertEqual(partial.number, 1)
        with self.assertRaisesRegex(investigation.InvestigationError, "A move to the List is pending"):
            _ = investigation.mark_read(owner.id, old.id)
        during = review_page.build_snapshot(self.root)
        self.assertIn(key, during.records)
        origin = review_items.submission_origin(during.records[key])
        assert isinstance(origin, new_item_dialog.ReviewSubmission)
        picture, = origin.images
        self.assertIn(review_page.attachment_url(key, picture), list_page.render(self.root).decode())
        self.assertEqual((review_submissions.image_folder(origin) / picture.id / picture.file).read_bytes(),
                         b64decode(PNG))
        with patch.object(staff, "caller_identity", return_value=keeper.current_session):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(the_list.main(["move-review-bugs"]), 0)
            self.assertEqual(output.getvalue(), "moved to List item 1\n")
            moved, = the_list.records(keeper.id)
            done = investigation.load(owner.id, old.id)
            self.assertEqual(done.state, investigation.InvestigationState.READ)
            self.assertEqual(done.log[-1].words, "moved to List item 1")
            self.assertEqual(done.question, words)
            self.assertIsInstance(moved.report, the_list.ListReport)
            assert isinstance(moved.report, the_list.ListReport)
            self.assertEqual(moved.report.words, words)
            after = review_page.build_snapshot(self.root)
            self.assertFalse(any(entry["key"] == key for entry in after.entries))
            origin = review_items.submission_origin(after.records[key])
            assert isinstance(origin, new_item_dialog.ReviewSubmission)
            image, = origin.images
            folder = review_submissions.image_folder(origin)
            self.assertEqual((folder / image.id / image.file).read_bytes(), b64decode(PNG))
            page = list_page.render(self.root, include_counts=False).decode()
            self.assertIn(review_page.attachment_url(key, image), page)
            self.assertIn("The menu clips its final choice.", page)
            self.assertIn("Close it and open it again.", page)
            second = io.StringIO()
            with redirect_stdout(second):
                self.assertEqual(the_list.main(["move-review-bugs"]), 0)
            self.assertEqual(second.getvalue(), "")
            self.assertEqual(the_list.records(keeper.id), [moved])
            self.assertEqual(investigation.load(owner.id, old.id), done)

    def test_legacy_bug_move_keeps_review_visible_until_the_native_save(self) -> None:
        keeper = fixture_member("producer", name="morty")
        native = investigation.file(self.fixture.unit.id, "Check the menu", "The last entry clips.",
                                    waiting_why="The owner checks the menu.", kind=investigation.InvestigationKind.BUG)
        path = investigation.review_record_path(native.owner, native.id)
        prior = path.read_bytes()
        key = review_items.investigation_record_key(native)
        members = staff.snapshot()
        with patch.object(investigation, "_save", side_effect=OSError("The intent write stopped.")):
            with self.assertRaisesRegex(OSError, "The intent write stopped"):
                _ = the_list.move_review_bugs(members)
        self.assertEqual(path.read_bytes(), prior)
        self.assertEqual(the_list.records(keeper.id), [])

        invalid = the_list.folder(keeper.id) / "L9.md"
        invalid.parent.mkdir(parents=True, exist_ok=True)
        _ = invalid.write_text("Invalid row.")
        with self.assertRaises(the_list.IncompleteListScan):
            _ = the_list.move_review_bugs(members)
        pending = investigation.PendingListMove(investigation.ListFilingMode.MOVE_BUG)
        self.assertEqual(investigation.load(native.owner, native.id), replace(native, list_move=pending))
        self.assertIn(key, {item["key"] for item in review_page.build_snapshot(self.root).entries})
        invalid.unlink()

        with patch.object(the_list, "_save", side_effect=OSError("The List write stopped.")):
            with self.assertRaisesRegex(OSError, "The List write stopped"):
                _ = the_list.move_review_bugs(members)
        destination = investigation.ListDestination(keeper.id, 1)
        self.assertEqual(investigation.load(native.owner, native.id),
                         replace(native, list_move=replace(pending, destination=destination)))
        self.assertEqual(the_list.records(keeper.id), [])
        extra = the_list.add(keeper.id, "Keep another row", owner=native.owner, source="nate", changes={})
        self.assertEqual(extra.number, 2)
        self.assertEqual(the_list.move_review_bugs(members), [destination])
        saved, other = the_list.records(keeper.id)
        self.assertEqual((saved.number, other), (destination.number, extra))
        moved = investigation.load(native.owner, native.id)
        self.assertIs(moved.purpose, investigation.InvestigationPurpose.MOVING_LEGACY_BUG)
        self.assertIs(moved.list_move, investigation.NoListMove.ABSENT)
        self.assertNotIn(key, {item["key"] for item in review_page.build_snapshot(self.root).entries})

    def test_pending_bug_move_refuses_review_actions_until_its_list_row_is_repaired(self) -> None:
        keeper = fixture_member("producer", name="morty")
        native = investigation.file(self.fixture.unit.id, "Check the menu", "The last entry clips.",
                                    waiting_why="The owner checks the menu.", kind=investigation.InvestigationKind.BUG)
        with patch("scripts.review_links.answer_sources.file_saved_investigation"):
            native = investigation.answer(native.owner, native.id, "Check the last entry.")
        key = review_items.investigation_record_key(native)
        members = staff.snapshot()
        save = investigation._save  # pyright: ignore[reportPrivateUsage]

        def fail_completion(store: investigation.Investigations, item: investigation.Investigation,
                            path: Path) -> investigation.Investigation:
            if isinstance(item.list_destination, investigation.ListDestination):
                raise OSError("The completion write stopped.")
            return save(store, item, path)

        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        initial = cache.get()
        request = self.card_body(initial, key, "read", "")
        with patch.object(investigation, "_save", side_effect=fail_completion):
            with self.assertRaisesRegex(OSError, "The completion write stopped"):
                _ = the_list.move_review_bugs(members)
        row, = the_list.records(keeper.id)
        destination = investigation.ListDestination(keeper.id, row.number)
        state = investigation.load(native.owner, native.id)
        self.assertEqual(state.list_move, investigation.PendingListMove(investigation.ListFilingMode.MOVE_BUG, destination))
        self.assertEqual(state.purpose, native.purpose)

        path = the_list.folder(keeper.id) / f"{row.id}.md"
        contents = path.read_bytes()
        _ = path.write_bytes(b"Unreadable List row.")
        fresh, card = cache.publish_record(initial, key, members=members)
        assert isinstance(card, review_items.ReviewItem)
        self.assertFalse(review_items.on_list(card))
        self.assertIn(key, {entry["key"] for entry in fresh.entries})
        self.assertIn(key, {entry["key"] for entry in review_page.build_snapshot(self.root).entries})
        with self.assertRaisesRegex(review_items.ReviewRecordChanged, "A move to the List is pending"):
            _ = review_page.submit(self.root, request, self.status, snapshot=fresh)
        with self.assertRaisesRegex(investigation.InvestigationError, "A move to the List is pending"):
            _ = investigation.mark_read(native.owner, native.id)
        with self.assertRaisesRegex(investigation.InvestigationError, "A move to the List is pending"):
            _ = review_actions.move_wait_to_list(self.root, card, members)
        with self.assertRaises(the_list.IncompleteListScan):
            _ = the_list.move_review_bugs(members)
        self.assertEqual(investigation.load(native.owner, native.id), state)

        _ = path.write_bytes(contents)
        self.assertEqual(the_list.move_review_bugs(members), [destination])
        self.assertEqual(the_list.records(keeper.id), [row])
        closed = investigation.load(native.owner, native.id)
        self.assertIs(closed.list_move, investigation.NoListMove.ABSENT)
        self.assertIs(closed.state, investigation.InvestigationState.READ)
        self.assertEqual([event.words for event in closed.log if review_items.list_move_event(event)], ["moved to List item 1"])

    def test_pending_bug_move_keeps_the_card_and_hides_receipted_reply_copies(self) -> None:
        from scripts.review_links import answer_sources

        keeper = fixture_member("producer", name="morty")
        native = investigation.file(self.fixture.unit.id, "Check the menu", "The last entry clips.",
                                    waiting_why="The owner checks the menu.", kind=investigation.InvestigationKind.BUG)
        reply = "Check the last entry."
        with patch.object(answer_sources, "file_saved_investigation"):
            native = investigation.answer(native.owner, native.id, reply)
        key = review_items.investigation_record_key(native)
        with patch.object(the_list, "verify_investigation_move", side_effect=OSError("The readback stopped.")):
            with self.assertRaisesRegex(OSError, "The readback stopped"):
                _ = the_list.move_review_bugs(staff.snapshot())
        pending = investigation.load(native.owner, native.id)
        destination = investigation.ListDestination(keeper.id, 1)
        self.assertEqual(pending.list_move, investigation.PendingListMove(investigation.ListFilingMode.MOVE_BUG, destination))
        before = review_page.build_snapshot(self.root).body(key)
        self.assertEqual(before.count(reply.encode()), 2)

        answer_sources.file_saved_investigation(pending)
        row, = the_list.records(keeper.id)
        exchange, = row.filed_exchanges
        self.assertEqual(the_list.confirmed_exchanges(key, destination, row.filed_exchanges), frozenset({exchange}))
        after = review_page.build_snapshot(self.root)
        saved = after.records[key]
        self.assertFalse(review_items.on_list(saved))
        self.assertIs(saved.moved_report, investigation.NoListDestination.ABSENT)
        self.assertIn(key, {entry["key"] for entry in after.entries})
        card = after.body(key)
        self.assertNotIn(reply.encode(), card)
        self.assertIn(b"Answer filed on the List", card)

    def test_pending_wait_move_resumes_after_the_owner_answers(self) -> None:
        keeper = fixture_member("producer", name="morty")
        native = investigation.file(self.fixture.unit.id, "Check startup", "Read the startup log.",
                                    waiting_why="The owner checks the log.")
        key = review_items.investigation_record_key(native)
        members = staff.snapshot()
        with patch.object(the_list, "verify_investigation_move", side_effect=OSError("The readback stopped.")):
            with self.assertRaisesRegex(OSError, "The readback stopped"):
                _ = the_list.move_review_waits(self.root, members)
        row, = the_list.records(keeper.id)
        _ = investigation.answer(native.owner, native.id, "The service started.")
        pending = investigation.load(native.owner, native.id)
        self.assertIs(pending.state, investigation.InvestigationState.ANSWER_READY)
        self.assertIsInstance(pending.list_move, investigation.PendingListMove)
        self.assertIn(key, {entry["key"] for entry in review_page.build_snapshot(self.root).entries})
        self.assertEqual(the_list.move_review_waits(self.root, members), [investigation.ListDestination(keeper.id, row.number)])
        done = investigation.load(native.owner, native.id)
        self.assertIs(done.list_move, investigation.NoListMove.ABSENT)
        self.assertIs(done.state, investigation.InvestigationState.ANSWER_READY)
        self.assertEqual(done.answer, pending.answer)

    def test_review_displays_an_older_pending_bug_move(self) -> None:
        keeper = fixture_member("producer", name="morty")
        with investigation.file_for_list(investigation.InvestigationPurpose.MOVING_LEGACY_BUG):
            native = investigation.file(self.fixture.unit.id, "Check the menu", "The last entry clips.",
                                        waiting_why="The owner checks the menu.", kind=investigation.InvestigationKind.BUG)
        key = review_items.investigation_record_key(native)
        self.assertIn(key, {item["key"] for item in review_page.build_snapshot(self.root).entries})
        destination, = the_list.move_review_bugs(staff.snapshot())
        self.assertEqual(destination.keeper, keeper.id)
        self.assertNotIn(key, {item["key"] for item in review_page.build_snapshot(self.root).entries})

    def test_new_investigation_uses_the_shared_list_report(self) -> None:
        owner = self.ready_submission_recipient()
        keeper = fixture_member('producer', name='morty')
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        words = 'Check the startup delay.\n\nKeep the timing from the log.'
        request = replace(self.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, owner.id),
                          words=words)
        created = cache.create_submission(request)
        _ = cache.create_submission(request)
        row, = the_list.records(keeper.id)
        self.assertEqual((row.owner, row.state), (owner.id, the_list.ItemState.NEW))
        self.assertIsInstance(row.state_entered, datetime)
        assert isinstance(row.report, the_list.ListReport)
        self.assertEqual((row.report.kind, row.report.words, row.report.evidence_key),
                         (the_list.ReportKind.INVESTIGATION, words, created.key))
        self.assertFalse(any(entry['key'] == created.key for entry in cache.get().entries))
        self.assertIn('Keep the timing from the log.', list_page.render(self.root, include_counts=False).decode())

    def test_change_return_and_feedback_use_the_shared_list_report(self) -> None:
        keeper = fixture_member('producer', name='morty')
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        for key, outcome, words in (('page-owner/C1', 'not works', 'Keep the margin.'),
                                    ('page-owner/C2', 'feedback', 'Check the font.')):
            with self.subTest(outcome=outcome):
                saved = cache.save(self.card_body(cache.get(), key, outcome, words), self.status)
                self.assertIsInstance(saved.delivery, change_page.MessageDelivery)
                self.assertIsInstance(saved.detail, review_page.ClosedReviewDetail)
                self.assertFalse(any(entry['key'] == key for entry in cache.get().entries))
                row = next(item for item in the_list.records(keeper.id) if key in item.reviews)
                self.assertEqual(row.state, the_list.ItemState.NEW)
                assert isinstance(row.report, the_list.ListReport)
                self.assertEqual((row.report.kind, row.report.words), (the_list.ReportKind.CHANGE, words))

    def test_failed_list_filing_leaves_a_change_return_ready_to_retry(self) -> None:
        keeper = fixture_member('producer', name='morty')
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        key = 'page-owner/C1'
        words = 'Keep the margin.'
        request = self.card_body(cache.get(), key, 'not works', words)
        with patch.object(the_list, 'file_report', side_effect=OSError('The List write failed.')):
            with self.assertRaisesRegex(question_record.AnswerRefused, 'The answer could not be saved'):
                _ = cache.save(request, self.status)
        row = change_review.review_rows(self.fixture.review_list)[0]
        self.assertFalse(row.get('Verdict'))
        self.assertEqual(cache.get().records[key].state, review_items.ReviewState.OPEN)
        self.assertEqual(the_list.records(keeper.id), [])
        self.assertEqual(self.status.request_attempts, {})

        saved = cache.save(request, self.status)
        self.assertIsInstance(saved.detail, review_page.ClosedReviewDetail)
        report, = the_list.records(keeper.id)
        assert isinstance(report.report, the_list.ListReport)
        self.assertEqual((report.report.evidence_key, report.report.words), (key, words))

    def test_review_validation_files_a_list_item_owned_by_the_showrunner(self) -> None:
        keeper = fixture_member('producer', name='morty')
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        key = 'page-owner/C1'
        words = 'Check the dialog twice.'
        linked = the_list.add_review_item(keeper.id, 'Check the dialog', owner=self.fixture.unit.id,
                                          producer=keeper.id, unit='', words='The existing List item.',
                                          evidence='', review_key=key, source='morty',
                                          reservation=self.fixture.temporary/'validation-number')
        saved = cache.save(self.card_body(cache.get(), key, 'validate', words), self.status)
        assert isinstance(saved.delivery, change_page.MessageDelivery)
        owner, = saved.delivery.recipients
        member = staff.snapshot().by_id(owner)
        assert member is not None
        self.assertEqual(member.kind, 'showrunner')
        row, = the_list.records(keeper.id)
        self.assertEqual((row.number, row.owner, row.state, row.reviews),
                         (linked, owner, the_list.ItemState.NEW, (key,)))
        self.assertIsInstance(row.state_entered, datetime)
        assert isinstance(row.report, the_list.ListReport)
        self.assertEqual((row.report.kind, row.report.words), (the_list.ReportKind.VALIDATION, words))
        self.assertFalse(any(entry['key'] == key for entry in cache.get().entries))

    def test_change_return_retry_updates_the_words_after_a_failed_verdict_save(self) -> None:
        keeper = fixture_member('producer', name='morty')
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        key = 'page-owner/C1'
        first_words = 'Check the first margin.'
        new_words = 'Check the second margin.'
        with patch.object(change_review, 'save_verdict', side_effect=OSError('The verdict write failed.')):
            with self.assertRaisesRegex(question_record.AnswerRefused, 'The answer could not be saved'):
                _ = cache.save(self.card_body(cache.get(), key, 'not works', first_words), self.status)
        first, = the_list.records(keeper.id)
        self.assertEqual(first.report, the_list.ListReport(the_list.ReportKind.CHANGE, key, first_words))
        self.assertFalse(change_review.review_rows(self.fixture.review_list)[0].get('Verdict'))

        _ = cache.save(self.card_body(cache.get(), key, 'not works', new_words), self.status)
        saved, = the_list.records(keeper.id)
        self.assertEqual(saved.number, first.number)
        self.assertEqual(saved.report, the_list.ListReport(the_list.ReportKind.CHANGE, key, new_words))
        row = next(row for row in change_review.review_rows(self.fixture.review_list) if row['#'] == 'C1')
        self.assertEqual(row['Verdict'], 'not works: '+new_words)

    def test_failed_validation_shows_the_validators_words_in_list_details(self) -> None:
        _ = fixture_member('producer', name='morty')
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        key = 'page-owner/C1'
        saved = cache.save(self.card_body(cache.get(), key, 'validate', 'Check the margin.'), self.status)
        assert isinstance(saved.delivery, change_page.MessageDelivery)
        words = 'The last row covers the margin.'
        result = change_review.save_verdict(self.fixture.review_list, 'C1', words, self.fixture.production,
                                            validation_id=saved.delivery.attempt)
        self.assertIsInstance(result, change_review.VerdictSaved)
        self.assertNotIn(key, {item.key for item in review_items.read_open(self.root)})
        page = list_page.render(self.root, include_counts=False).decode()
        self.assertIn('<h3>Validation result</h3><p>'+words+'</p>', page)

    def test_edited_validation_retry_shows_the_validators_result(self) -> None:
        keeper = fixture_member('producer', name='morty')
        key = 'page-owner/C1'
        first = review_page.build_snapshot(self.root)
        request = self.card_body(first, key, 'validate', 'Check the first row.')
        with patch.object(self.status, 'write_slots', side_effect=OSError('The delivery slot cannot be saved.')):
            with self.assertRaisesRegex(OSError, 'The delivery slot cannot be saved.'):
                _ = review_page.submit(self.root, request, self.status, snapshot=first)
        original, = the_list.records(keeper.id)
        self.assertEqual(original.report, the_list.ListReport(
            the_list.ReportKind.VALIDATION, key, 'Check the first row.',
            validation_log=review_items.ValidationLogStart(1)))

        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        words = 'Check the last row.'
        saved = cache.save(self.card_body(cache.get(), key, 'validate', words), self.status)
        assert isinstance(saved.delivery, change_page.MessageDelivery)
        result_words = 'The last row covers the margin.'
        result = change_review.save_verdict(self.fixture.review_list, 'C1', result_words, self.fixture.production,
                                            validation_id=saved.delivery.attempt)
        self.assertIsInstance(result, change_review.VerdictSaved)
        html = list_page.render(self.root, include_counts=False).decode()
        self.assertTrue('<h3>Validation result</h3><p>'+result_words+'</p>' in html)
        updated, = the_list.records(keeper.id)
        self.assertEqual(updated.number, original.number)
        self.assertEqual(updated.report, the_list.ListReport(
            the_list.ReportKind.VALIDATION, key, words, validation_log=review_items.ValidationLogStart(2)))

    def test_validation_list_details_show_only_the_filed_attempt(self) -> None:
        _ = fixture_member('producer', name='morty')
        key = 'page-owner/C1'
        first, _ = review_page.submit(self.root, self.body(key, 'validate', 'Check the first margin.'), self.status)
        assert isinstance(first, change_page.MessageDelivery)
        prior_words = 'The first margin is too wide.'
        result = change_review.save_verdict(self.fixture.review_list, 'C1', prior_words, self.fixture.production,
                                            validation_id=first.attempt)
        self.assertIsInstance(result, change_review.VerdictSaved)

        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        second = cache.save(self.card_body(cache.get(), key, 'validate', 'Check the second margin.'), self.status)
        assert isinstance(second.delivery, change_page.MessageDelivery)
        pending = list_page.render(self.root, include_counts=False).decode()
        self.assertFalse('<h3>Validation result</h3>' in pending, 'The pending check has no result.')

        current_words = 'The second margin is too narrow.'
        result = change_review.save_verdict(self.fixture.review_list, 'C1', current_words, self.fixture.production,
                                            validation_id=second.delivery.attempt)
        self.assertIsInstance(result, change_review.VerdictSaved)
        completed = list_page.render(self.root, include_counts=False).decode()
        self.assertIn('<h3>Validation result</h3><p>'+current_words+'</p>', completed)
        self.assertFalse('<h3>Validation result</h3><p>'+prior_words+'</p>' in completed,
                         'The result omits the previous failure.')

    def test_moved_validation_shows_its_result_after_the_move_marker_write_fails(self) -> None:
        keeper = fixture_member('producer', name='morty')
        key = 'page-owner/C1'
        first, _ = review_page.submit(self.root, self.body(key, 'validate', 'Check the first margin.'), self.status)
        assert isinstance(first, change_page.MessageDelivery)
        first_result = change_review.save_verdict(self.fixture.review_list, 'C1', 'The first check failed.',
                                                  self.fixture.production, validation_id=first.attempt)
        self.assertIsInstance(first_result, change_review.VerdictSaved)

        second, _ = review_page.submit(self.root, self.body(key, 'validate', 'Check the next margin.'), self.status)
        assert isinstance(second, change_page.MessageDelivery)
        with patch.object(change_review, 'record_review_log', side_effect=OSError('The source write failed.')):
            with self.assertRaisesRegex(OSError, 'The source write failed'):
                _ = the_list.move_review_waits(self.root, staff.snapshot())
        saved, = the_list.records(keeper.id)
        pending = list_page.render(self.root, include_counts=False).decode()
        self.assertFalse('<h3>Validation result</h3>' in pending, 'The pending check has no result.')

        words = 'The second check failed.'
        result = change_review.save_verdict(self.fixture.review_list, 'C1', words, self.fixture.production,
                                            validation_id=second.attempt)
        self.assertIsInstance(result, change_review.VerdictSaved)
        self.assertEqual(the_list.move_review_waits(self.root, staff.snapshot()), [])
        self.assertEqual(the_list.records(keeper.id), [saved])

        third, _ = review_page.submit(self.root, self.body(key, 'validate', 'Check the last margin.'), self.status)
        assert isinstance(third, change_page.MessageDelivery)
        third_result = change_review.save_verdict(self.fixture.review_list, 'C1', 'The third check failed.',
                                                  self.fixture.production, validation_id=third.attempt)
        self.assertIsInstance(third_result, change_review.VerdictSaved)
        page = list_page.render(self.root, include_counts=False).decode()
        self.assertEqual(re.findall(r'<h3>Validation result</h3><p>(.*?)</p>', page), [words])

    def test_move_review_waits_retries_without_duplicate_items_or_log_lines(self) -> None:
        keeper = fixture_member('producer', name='morty')
        key = 'page-owner/C1'
        _ = review_page.submit(self.root, self.body(key, 'validate', 'Check the result.'), self.status)
        with patch.object(change_review, 'record_review_log', side_effect=OSError('The source write failed.')):
            with self.assertRaisesRegex(OSError, 'The source write failed'):
                _ = the_list.move_review_waits(self.root, staff.snapshot())
        partial, = the_list.records(keeper.id)
        report = investigation.file(self.fixture.unit.id, 'Check startup', 'Keep the full words.\n\nCheck the log.',
                                    waiting_why='A check is due.')
        before = {item.key:item for item in review_items.read(self.root)}
        report_key = review_items.investigation_record_key(report)
        self.assertEqual(before[key].state, review_items.ReviewState.VALIDATING)
        self.assertEqual(before[report_key].state, review_items.ReviewState.WAITING)
        with (patch.object(staff, 'caller_identity', return_value=keeper.current_session),
              patch.object(review_link, 'review_root', return_value=self.root)):
            with redirect_stdout(io.StringIO()) as first:
                self.assertEqual(the_list.main(['move-review-waits']), 0)
            with redirect_stdout(io.StringIO()) as repeat:
                self.assertEqual(the_list.main(['move-review-waits']), 0)
        self.assertEqual(len(first.getvalue().splitlines()), 2)
        self.assertEqual(repeat.getvalue(), '')
        rows = the_list.records(keeper.id)
        self.assertEqual(len(rows), 2)
        self.assertIn(partial, rows)
        current = review_page.build_snapshot(self.root)
        self.assertTrue({key, report_key}.isdisjoint(entry['key'] for entry in current.entries))
        for row in rows:
            assert isinstance(row.report, the_list.ListReport)
            moved_key = row.report.evidence_key
            record = current.records[moved_key]
            self.assertEqual(record.log[:-1], before[moved_key].log)
            self.assertEqual(record.log[-1].words, f'moved to List item {row.number}')
        native = investigation.load(report.owner, report.id)
        self.assertEqual(native.question, report.question)
        self.assertIsInstance(native.list_destination, investigation.ListDestination)
        self.assertEqual(partial.report, the_list.ListReport(the_list.ReportKind.VALIDATION, key, 'Check the result.',
                                                           validation_log=review_items.ValidationLogStart(1)))

    def test_move_review_waits_keeps_sources_that_open_after_the_scan(self) -> None:
        keeper = fixture_member('producer', name='morty')
        report = investigation.file(self.fixture.unit.id, 'Check the startup', 'Read the startup log.',
                                    waiting_why='A check is due.')
        key = 'page-owner/C1'
        delivery, _ = review_page.submit(self.root, self.body(key, 'question', 'Check the log.'), self.status)
        assert isinstance(delivery, change_page.MessageDelivery)
        question = self.status.slots(delivery.page, delivery.row).question
        assert isinstance(question, change_page.OpenQuestion)
        scanned = review_items.read(self.root)
        self.assertTrue(self.status.answer_question(delivery.page, delivery.row, question.question_id, 'The log is clear.'))
        _ = investigation.answer(report.owner, report.id, 'The service starts.')
        filed = the_list.records(keeper.id)
        with patch.object(review_items, 'read', return_value=scanned):
            self.assertEqual(the_list.move_review_waits(self.root, staff.snapshot()), [])
        self.assertEqual(the_list.records(keeper.id), filed)
        current = {item.key: item for item in review_items.read(self.root)}
        for identity in (key, review_items.investigation_record_key(report)):
            self.assertEqual(current[identity].state, review_items.ReviewState.OPEN)
            self.assertFalse(any(review_items.list_move_event(event) for event in current[identity].log))

    def test_migrated_investigation_reply_stays_on_the_list(self) -> None:
        keeper = fixture_member('producer', name='morty')
        owner = self.fixture.unit
        report = investigation.file(owner.id, 'Check the startup', 'Read the startup log.',
                                    waiting_why='A check is due.')
        destination, = the_list.move_review_waits(self.root, staff.snapshot())
        self.assertEqual(destination.keeper, keeper.id)
        _ = investigation.answer(owner.id, report.id, 'The service starts.')
        key = review_items.investigation_record_key(report)
        self.assertNotIn(key, {item.key for item in review_items.read_open(self.root)})
        _, queue = review_items.next_queue('investigation', owner, False, staff.snapshot())
        self.assertEqual(queue, [])
        self.assertEqual(review_items.ready_investigations(owner.id), 0)
        page = list_page.render(self.root, include_counts=False).decode()
        self.assertIn('Read the startup log.', page)
        self.assertIn('The service starts.', page)

    def test_migrated_change_keeps_the_request_and_reply_on_the_list(self) -> None:
        keeper = fixture_member('producer', name='morty')
        key = 'page-owner/C1'
        words = 'Check the final entry in the log.'
        delivery, _ = review_page.submit(self.root, self.body(key, 'question', words), self.status)
        assert isinstance(delivery, change_page.MessageDelivery)
        slots = self.status.slots(delivery.page, delivery.row)
        question = slots.question
        assert isinstance(question, change_page.OpenQuestion)
        self.status.write_slots(delivery.page, delivery.row, replace(slots, waiting_why=''))
        before = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(before.waiting_why, '')

        _ = the_list.move_review_waits(self.root, staff.snapshot())
        report, = the_list.records(keeper.id)
        self.assertEqual(report.report, the_list.ListReport(the_list.ReportKind.CHANGE, key, words))
        self.assertTrue(self.status.answer_question(delivery.page, delivery.row, question.question_id, 'The entry is present.'))
        self.assertNotIn(key, {item.key for item in review_items.read_open(self.root)})
        page = list_page.render(self.root, include_counts=False).decode()
        self.assertIn(words, page)
        self.assertIn('The entry is present.', page)

    def test_moved_efficiency_keeps_the_pending_question_words(self) -> None:
        keeper = fixture_member('producer', name='morty')
        _, key = self.decisions()
        words = 'Which check will this remove?\n\nKeep the crash check.'
        _ = review_page.submit(self.root, self.body(key, 'question', words), self.status)
        item = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(item.state, review_items.ReviewState.WAITING)

        _ = the_list.move_review_waits(self.root, staff.snapshot())
        row, = the_list.records(keeper.id)
        self.assertEqual(row.report, the_list.ListReport(the_list.ReportKind.EFFICIENCY, key, words))

    def test_investigation_file_command_uses_the_shared_list_report(self) -> None:
        keeper = fixture_member('producer', name='morty')
        owner = self.fixture.unit
        with (patch.object(staff, 'caller_identity', return_value=owner.current_session),
              redirect_stdout(io.StringIO()) as result):
            self.assertEqual(investigation.main(['file', '--title', 'Check the startup',
                '--question', 'Read the startup log.', '--waiting-why', 'A check is due.']), 0)
        report = investigation.load(owner.id, result.getvalue().strip())
        item, = the_list.records(keeper.id)
        self.assertEqual(report.list_destination, investigation.ListDestination(keeper.id, item.number))
        key = review_items.investigation_record_key(report)
        self.assertEqual(item.report, the_list.ListReport(the_list.ReportKind.INVESTIGATION, key, report.question))
        self.assertFalse(any(entry['key'] == key for entry in review_page.build_snapshot(self.root).entries))

    def test_investigation_file_reuses_the_native_id_after_interrupted_list_filing(self) -> None:
        keeper = fixture_member('producer', name='morty')
        owner = self.fixture.unit
        arguments = ['file', '--title', 'Check the startup', '--question', 'Read the startup log.',
                     '--waiting-why', 'A check is due.']
        with patch.object(staff, 'caller_identity', return_value=owner.current_session):
            with (patch.object(the_list, 'file_report', side_effect=OSError('The List write failed.')),
                  redirect_stdout(io.StringIO()) as failed, redirect_stderr(io.StringIO())):
                self.assertEqual(investigation.main(arguments), 1)
            self.assertEqual(failed.getvalue(), '')
            before, = investigation.records(owner.id)
            self.assertEqual(the_list.records(keeper.id), [])

            review_actions.recover_interrupted_list_reports()
            first, = the_list.records(keeper.id)
            with redirect_stdout(io.StringIO()) as retried:
                self.assertEqual(investigation.main(arguments), 0)
            self.assertEqual(retried.getvalue(), before.id+'\n')
            review_actions.recover_interrupted_list_reports()
            self.assertEqual(the_list.records(keeper.id), [first])
            self.assertEqual([record.id for record in investigation.records(owner.id)], [before.id])

            with redirect_stdout(io.StringIO()) as filed_again:
                self.assertEqual(investigation.main(arguments), 0)
            self.assertNotEqual(filed_again.getvalue(), retried.getvalue())
            self.assertEqual(len(the_list.records(keeper.id)), 2)

    def test_investigation_file_retries_titles_with_quotes_and_spaces(self) -> None:
        keeper = fixture_member('producer', name='morty')
        owner = self.fixture.unit
        cases = (('Check the "ready" status', True), ('  Check the pause  ', False),
                 ('  Check the "start" status  ', True))
        with patch.object(staff, 'caller_identity', return_value=owner.current_session):
            for title, recover in cases:
                with self.subTest(title=title, recover=recover):
                    previous = investigation.records(owner.id)
                    list_count = len(the_list.records(keeper.id))
                    arguments = ['file', '--title', title, '--question', 'Read the log.',
                                 '--waiting-why', '  A check is due.  ']
                    with (patch.object(the_list, 'file_report', side_effect=OSError('The List write failed.')),
                          redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                        self.assertEqual(investigation.main(arguments), 1)
                    before = investigation.records(owner.id)
                    self.assertEqual(len(before), len(previous)+1)
                    self.assertEqual(before[-1].title, title)
                    if recover:
                        review_actions.recover_interrupted_list_reports()
                    with redirect_stdout(io.StringIO()) as retried:
                        self.assertEqual(investigation.main(arguments), 0)
                    self.assertEqual(retried.getvalue(), before[-1].id+'\n')
                    self.assertEqual(len(investigation.records(owner.id)), len(before))
                    self.assertEqual(len(the_list.records(keeper.id)), list_count+1)

    def test_investigation_file_skips_an_unrelated_pending_record_with_a_damaged_body(self) -> None:
        keeper = fixture_member('producer', name='morty')
        owner = self.fixture.unit
        with investigation.file_for_list(investigation.InvestigationPurpose.LIST_INVESTIGATION):
            damaged = investigation.file(owner.id, '  Check the "old" log  ', 'Read the old log.',
                                         waiting_why='A check is due.')
        path = investigation.review_record_path(owner.id, damaged.id)
        contents = path.read_text().partition(record_store.section('Answer'))[0]
        _ = path.write_text(contents)

        with (patch.object(staff, 'caller_identity', return_value=owner.current_session),
              redirect_stdout(io.StringIO()) as filed, redirect_stderr(io.StringIO()) as errors):
            status = investigation.main(['file', '--title', '  Check the "new" log  ',
                                         '--question', 'Read the new log.', '--waiting-why', 'A check is due.'])
        self.assertEqual(status, 0, errors.getvalue())
        error, = errors.getvalue().splitlines()
        self.assertTrue(error.startswith(f'investigation: {path}:'), error)
        report = investigation.load(owner.id, filed.getvalue().strip())
        row, = the_list.records(keeper.id)
        self.assertEqual(report.list_destination, investigation.ListDestination(keeper.id, row.number))
        self.assertEqual(path.read_text(), contents)

    def test_investigation_file_skips_an_unrelated_pending_record_with_invalid_utf8(self) -> None:
        keeper = fixture_member('producer', name='morty')
        owner = self.fixture.unit
        with investigation.file_for_list(investigation.InvestigationPurpose.LIST_INVESTIGATION):
            damaged = investigation.file(owner.id, 'Old report', 'Read the old log.', waiting_why='A check is due.')
        path = investigation.review_record_path(owner.id, damaged.id)
        contents = path.read_bytes().replace(b'Read the old log.', b'Read the old log\xff')
        _ = path.write_bytes(contents)

        with (patch.object(staff, 'caller_identity', return_value=owner.current_session),
              redirect_stdout(io.StringIO()) as filed, redirect_stderr(io.StringIO()) as errors):
            status = investigation.main(['file', '--title', 'New report', '--question', 'Read the new log.',
                                         '--waiting-why', 'A check is due.'])
        self.assertEqual(status, 0, errors.getvalue())
        error, = errors.getvalue().splitlines()
        self.assertTrue(error.startswith(f'investigation: {path}:'), error)
        self.assertIn('utf-8', error)
        identity = filed.getvalue().strip()
        self.assertEqual(identity, f'I{int(damaged.id[1:])+1}')
        report = investigation.load(owner.id, identity)
        row, = the_list.records(keeper.id)
        self.assertEqual(report.list_destination, investigation.ListDestination(keeper.id, row.number))
        self.assertEqual(path.read_bytes(), contents)

        with (patch.object(staff, 'caller_identity', return_value=owner.current_session),
              redirect_stdout(io.StringIO()) as refused, redirect_stderr(io.StringIO())):
            status = investigation.main(['file', '--title', 'Old report', '--question', 'Read the old log.',
                                         '--waiting-why', 'A check is due.'])
        self.assertEqual(status, 1)
        self.assertEqual(refused.getvalue(), '')
        self.assertEqual(the_list.records(keeper.id), [row])
        self.assertEqual(path.read_bytes(), contents)

    def test_completed_bug_stays_in_its_store_and_out_of_review(self) -> None:
        producer = fixture_member('producer', name='builds')
        bug = investigation.file(producer.id, 'Check the build', 'Which step stopped?', waiting_why='builds checks the failure.',
                                 kind=investigation.InvestigationKind.BUG)
        with patch.object(the_list, 'file_exchanges', side_effect=OSError('The List save failed.')):
            _ = investigation.answer(bug.owner, bug.id, 'The test step.')
        key = f'investigation/{bug.owner}/{bug.id}'
        before = review_page.build_snapshot(self.root)
        request = console.NewsRequest(self.root)
        self.assertTrue(any(item.startswith(key+'/') for item in review_page.waiting_items(request, snapshot=before)))

        _ = investigation.mark_read(bug.owner, bug.id)
        with patch.object(investigation, 'read_review_record', wraps=investigation.read_review_record) as read:
            after = review_page.build_snapshot(self.root)
        read.assert_not_called()
        for show, _ in review_page.SHOWS:
            page = after.render(show=show)
            self.assertNotIn(key.encode(), page)
            self.assertNotIn(f'data-filter-value="staff:{producer.id}"'.encode(), page)
        waiting = review_page.waiting_items(console.NewsRequest(self.root), snapshot=after)
        self.assertFalse(any(item.startswith(key+'/') for item in waiting))
        self.assertEqual(investigation.load(bug.owner, bug.id).state, investigation.InvestigationState.READ)

    def test_open_read_skips_done_payloads_and_retains_full_selected_records(self) -> None:
        question_key = self.question()
        ready = self.investigation()
        _ = self.investigation(False)
        _ = investigation.mark_read(ready.owner, ready.id)
        delivery, _ = review_page.submit(self.root, self.body(question_key, 'answer', 'Use the first layout.'), self.status)
        self.deliver_review_message(delivery, self.status)
        _ = change_review.save_verdict(self.fixture.review_list, 'C1', 'works', self.fixture.production)
        all_items = review_items.read(self.root)
        members = staff.snapshot()
        expected_owners = {item.owner:review_page.owner_name(item, members) for item in all_items}
        expected_owners.update({item.source_unit:review_items.name(item.source_unit, members)
                                for item in all_items if item.type == review_items.ReviewType.INVESTIGATION})
        owners: dict[staff.StaffId, str] = {}
        with patch.object(review_items, 'change_item', wraps=review_items.change_item) as project_change, patch.object(
                review_items, 'question_item', wraps=review_items.question_item) as project_question, patch.object(
                investigation, 'read_review_record', wraps=investigation.read_review_record) as load_investigation:
            opened = review_items.read(self.root, show='open', owner_options=owners)
        self.assertEqual(opened, [item for item in all_items if item.state == review_items.ReviewState.OPEN])
        self.assertEqual(owners, expected_owners, 'The owner menu includes owners of omitted Done records.')
        self.assertTrue(project_change.called)
        self.assertFalse(any(cast(dict[str, str], call.args[1]).get('Verdict') for call in project_change.call_args_list),
                         'An Open read does not project a Done change.')
        self.assertFalse(project_question.called, 'An Open read does not project a closed question.')
        self.assertNotIn(ready.id, [cast(Path, call.args[0]).stem for call in load_investigation.call_args_list],
                         'An Open read does not load a read investigation.')
        for show in ('Open', 'Done', 'all'):
            with self.subTest(show=show):
                expected = all_items if show == 'all' else [item for item in all_items if item.state == show]
                self.assertEqual(review_items.read(self.root, show=show), expected)

    def test_native_writer_allocates_question_id_without_decoding_malformed_closed_registration(self) -> None:
        key = self.question()
        identities = update_registration.QuestionStaff.for_record(staff.snapshot())
        record = update_registration.question_records_for_document(self.fixture.production, identities)[0]
        _ = record.item.pop('review_id', None)
        lines = record.path.read_text().splitlines()
        _ = record.path.write_text('\n'.join('registration: '+json.dumps(record.item)
            if line.startswith('registration: ') else line for line in lines)+'\n')
        closed = record.path.with_name('question-closed.md')
        _ = closed.write_text('---\nrecord_type: question\nstatus: closed\n'
            +f'production_id: {record.owner}\nasked_by: {record.asker}\n'
            +'registration: {"review_id": "Q8", invalid}\n---\n')
        update_registration.write_question_record(record, assign_id=True)
        opened = review_items.read(self.root, show='open')
        self.assertEqual(next(item.id for item in opened if item.key == key), 'page-owner/Q9',
                         'F003: allocate after existing IDs without decoding a malformed closed registration.')

    def test_open_retains_reviewed_change_without_verdict(self) -> None:
        _ = change_review.save_verdict(self.fixture.review_list, 'C1', 'works', self.fixture.production)
        text = self.fixture.review_list.read_text()
        _ = self.fixture.review_list.write_text(text.replace('| works |', '|  |'))
        all_items = review_items.read(self.root)
        expected = next(item for item in all_items if item.key == 'page-owner/C1')
        self.assertEqual(expected.state, review_items.ReviewState.OPEN)
        opened = review_items.read(self.root, show='open')
        self.assertEqual(next((item for item in opened if item.key == expected.key), None), expected,
                         'F005: the Verdict decides the state in both Reviewed and Open.')

    def test_open_skips_investigation_that_disappears_after_enumeration(self) -> None:
        record = self.investigation()
        missing = investigation.folder(record.owner) / (record.id+'.md')
        original = Path.open

        def open_record(path: Path, *args: object, **kwargs: object) -> TextIO:
            if path == missing:
                raise FileNotFoundError(str(path))
            return cast(Callable[..., TextIO], original)(path, *args, **kwargs)

        with patch.object(Path, 'open', open_record):
            opened = review_items.read(self.root, show='open')
        self.assertNotIn('investigation/'+record.owner+'/'+record.id, [item.key for item in opened],
                         'F004: skip a record that disappears before state selection.')

    def test_proposal_read_scans_each_shared_folder_once(self) -> None:
        folder = self.fixture.temporary / 'shared-proposals'
        folder.mkdir()
        other_doc = self.fixture.temporary / 'other.md'
        _ = other_doc.write_text(f'# Other\n- **Proposals:** {folder}\n')
        other = fixture_member('showrunner', name='other', doc=staff.LiveDoc(local_machine(), other_doc))
        _ = self.fixture.production.write_text(self.fixture.production.read_text()+f'- **Proposals:** {folder}\n')
        paths: list[Path] = []
        for identity, owner, extra in (('P1', self.fixture.unit.id, ''), ('P2', other.id, ''),
                                       ('P3', other.id, 'unit: implementer\n')):
            path = folder / (identity+'.md')
            paths.append(path)
            _ = path.write_text('---\nstatus: open\ntags: [proposal]\nstage: backlog\n'
                +f'proposed_by: {owner}\nproposal_id: {identity}\ndate_created: 2026-11-01\n{extra}---\n')
        with patch.object(issue_store, 'frontmatter', wraps=issue_store.frontmatter) as read_header:
            opened = review_items.proposal_items(staff.snapshot(), (self.fixture.production, other_doc),
                                                states=review_items.selected_states('open'))
        self.assertEqual({item.id for item in opened}, {'page-owner/P1', 'other/P2'})
        calls = [call.args[0] for call in read_header.call_args_list]
        self.assertEqual({path:calls.count(path) for path in paths}, dict.fromkeys(paths, 1),
                         'Read each proposal header once when productions share a folder.')
        (self.fixture.temporary / 'extra').mkdir()
        variants = {
            'relative':Path(os.path.relpath(self.fixture.production)),
            'equivalent':self.fixture.temporary / 'extra' / '..' / self.fixture.production.name,
        }
        members = staff.snapshot()
        expected_queue = next_item.read_queue('proposal', self.fixture.production, record=members)
        for spelling, document in variants.items():
            with self.subTest(path=spelling):
                self.assertEqual(review_items.proposals(document, record=members), 1,
                                 'F006: an equivalent production path retains the proposal count.')
                self.assertEqual(next_item.read_queue('proposal', document, record=members), expected_queue,
                                 'F006: an equivalent production path retains the /next entry.')
                with patch.object(issue_store, 'frontmatter', wraps=issue_store.frontmatter) as read_header:
                    opened = review_items.proposal_items(members, (document, other_doc),
                                                        states=review_items.selected_states('open'))
                self.assertEqual({item.id for item in opened}, {'page-owner/P1', 'other/P2'})
                calls = [call.args[0] for call in read_header.call_args_list]
                self.assertEqual({path:calls.count(path) for path in paths}, dict.fromkeys(paths, 1),
                                 'F006: equivalent production paths keep one scan per shared folder.')

    def test_fast_reads_keep_count_queue_news_and_list_results(self) -> None:
        _ = self.question()
        ready = self.investigation()
        _ = self.investigation(False)
        _ = investigation.mark_read(ready.owner, ready.id)
        _ = change_review.save_verdict(self.fixture.review_list, 'C1', 'works', self.fixture.production)
        proposals = self.fixture.temporary / 'shared-proposals'
        proposals.mkdir()
        for name, fields in (('first', 'depends_on: [approved]'), ('approved', 'unit: implementer')):
            _ = (proposals / (name+'.md')).write_text('---\nstatus: open\ntags: [proposal]\nstage: backlog\n'
                +f'proposed_by: {self.fixture.unit.id}\nproposal_id: {name}\ndate_created: 2026-11-01\n{fields}\n---\n')
        ledger = self.fixture.temporary / 'shared-efficiency.md'
        _ = ledger.write_text('- 2026-11-01 tests: Open — proposed\n- 2026-11-02 tests: Done — accepted\n')
        _ = self.fixture.production.write_text(self.fixture.production.read_text()
            +f'- **Proposals:** {proposals}\n- **Efficiency ledger:** {ledger}\n')
        members = staff.snapshot()
        owner = next(row for row in members.rows if row.name == 'page-owner')
        identities = update_registration.QuestionStaff.for_record(members)
        original = review_items.read_kind

        def all_states(kind: review_items.ReviewType, scope: Path | staff.StaffRow,
                       record: staff.StaffSnapshot, *, below: bool = False, sources: list[Path] | None = None,
                       own: list[Issue] | None = None, show: str = 'all',
                       zone: ZoneInfo | None = None) -> list[review_items.ReviewItem]:
            _ = show
            return original(kind, scope, record, below=below, sources=sources, own=own, zone=zone)

        def results() -> tuple[footer_counts.FooterCounts, tuple[int, str], list[next_item.Queue], str]:
            counts = footer_counts.read_counts(self.fixture.production, record=members)
            questions = counts_row.question_counts(owner, user_zone(), identities, None)
            queues = [next_item.read_queue(kind, self.fixture.production)
                      for kind in ('question', 'change', 'investigation', 'proposal', 'efficiency')]
            return counts, questions, queues, list_page.counts_strip(members, staff_names.StaffNames(members))

        with patch.object(review_items, 'read_kind', side_effect=all_states):
            before = results()
        self.assertEqual(results(), before, 'Counts, /next and The List keep the same results.')
        now = datetime.now(user_zone())
        expected_news = frozenset(item.key+'/'+item.times.updated.isoformat() for item in review_items.read(self.root)
                                  if item.for_you(now))
        self.assertEqual(review_page.waiting_items(console.NewsRequest(self.root)), expected_news,
                         'Console news and the nav new mark keep the same records and revisions.')

        listing = self.fixture.review_list
        saved = listing.read_text()
        _ = listing.write_text(re.sub(r'\| C1 \|[^\n]*', lambda row: row[0].replace('works', '', 1), saved))
        unrelated = change_routing.delivery_path(self.root, listing, 'C97')
        for has_delivery in (False, True):
            with self.subTest(reviewed_without_verdict=True, has_delivery=has_delivery):
                if has_delivery:
                    _ = unrelated.write_text('{}')
                visible = {item.queue_key for item in review_items.read_open(self.root)
                           if item.type == review_items.ReviewType.CHANGE}
                self.assertIn('C1', visible)
                self.assertEqual({entry.key for entry in next_item.read_queue('change', self.fixture.production).entries},
                                 visible)
                self.assertEqual(footer_counts.change_counts(self.fixture.production, members), (len(visible), 0))

    def test_review_reads_each_store_that_the_page_shows_once(self) -> None:
        with patch.object(review_items, 'proposal_items', return_value=[]) as proposals, patch.object(
                review_items, 'efficiency_items', return_value=[]) as efficiency:
            _ = review_page.render(self.root)
        proposals.assert_called_once()
        efficiency.assert_called_once()

    def test_review_nav_replaces_the_three_pages_and_marks_only_for_you(self) -> None:
        _ = self.question()
        _ = self.investigation(False)
        pages = console.read_pages()
        self.assertEqual(next(page.icon for page in pages if page.identity == 'review'), 'inbox', 'F009: use the Review icon')
        self.assertIn('review',[page.identity for page in pages])
        self.assertFalse({'changes','questions','investigations'} & {page.identity for page in pages})
        request = console.NewsRequest(self.root)
        waiting = review_page.waiting_items(request)
        self.assertFalse(any(key.startswith('investigation/') for key in waiting))
        self.assertTrue(any(key.startswith('question/') for key in waiting))
        self.assertEqual(review_page.legacy_url('/console/changes.html','type=questions'),'/console/review.html?type=question')

    def test_each_type_has_the_approved_action_groups_and_later(self) -> None:
        proposal, efficiency = self.decisions()
        question = self.question()
        ready = self.legacy_investigation()
        expected = {question:('answer','feedback','question'),
                    'page-owner/C1':('works','not works','validate','feedback','question'),
                    f'investigation/{ready.owner}/{ready.id}':('read','feedback','question'),
                    proposal:('approve','decline','feedback','question'),
                    efficiency:('approve','decline','feedback','question')}
        token, initial = change_browser.browser_manifest(change_page.console_folder(self.root))
        for item in review_items.read(self.root):
            if item.key not in expected:
                continue
            with self.subTest(kind=item.type):
                self.assertEqual(item.actions,expected[item.key])
                form = review_page.form(item,change_page.ReviewBrowser(token,initial),4,'fixture-revision')
                self.assertIn('Closes the item',form)
                if item.type == review_items.ReviewType.CHANGE:
                    self.assertIn('<span>Sends to <span class="staff-name" data-color-text="role.showrunner"'
                                  + ' style="color:#a379fd">showrunner</span></span>', form)
                    self.assertIn('value="validate">Ask showrunner to check</button>', form)
                else:
                    self.assertIn('The item returns to you', form)
                self.assertIn('aria-haspopup="menu" aria-expanded="false" data-later-toggle>Later',form)
                for label in ('1 hour','Tomorrow','Next week','Pick a date…'):
                    self.assertIn('<span>'+label+'</span>',form)
                self.assertNotIn('Until delivered',form)
                self.assertNotIn('type="datetime-local"',form)
                self.assertIn('type="hidden" name="later"',form)
                self.assertIn('role="grid"',form)
                self.assertIn('role="combobox" aria-expanded="false" data-later-time',form)
                self.assertIn('value="later">Save',form)
                self.assertNotIn('value="skip"',form)

    def test_review_render_reads_card_motion_settings(self) -> None:
        before = review_page.build_snapshot(self.root)
        settings = self.fixture.temporary / "config/review_links.conf"
        _ = write_config_fixture(settings, "card_fade_out_ms=365\ncard_fade_in_ms=725\n")
        after = review_page.build_snapshot(self.root)
        self.assertNotEqual(after.version, before.version)
        self.assertIn(b'data-card-fade-out-ms="365" data-card-fade-in-ms="725"', after.render())
        self.assertIn(b'"cardFadeOutMs": 365, "cardFadeInMs": 725', after.index_response())

    def test_park_settings_validate_the_times_weekday_and_step(self) -> None:
        lines = ('# whole: 1..1440', 'later_soon_minutes=60', 'later_morning=09:30',
                 'later_week_day=Wednesday', 'later_week_time=10:35', '# whole: 1..60', 'park_time_step_minutes=15')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'review_links.conf'
            _ = path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
            self.assertEqual(record_table.park_settings(conf.read(path)),
                             {'soon': 60, 'morning': 570, 'weekDay': 3, 'weekTime': 635, 'step': 15})
            for bad in ('later_soon_minutes=0', 'later_morning=9:00', 'later_morning=24:00', 'later_week_day=Wed',
                        'later_week_time=9:00', 'park_time_step_minutes=61'):
                with self.subTest(bad=bad):
                    key = bad.split('=')[0]
                    _ = path.write_text('\n'.join(bad if line.startswith(key+'=') else line for line in lines)+'\n',
                                        encoding='utf-8')
                    with self.assertRaisesRegex(ValueError,key):
                        _ = record_table.park_settings(conf.read(path))

    def test_ask_question_and_one_owner_answer_command_for_each_type(self) -> None:
        decisions = self.decisions()
        question = self.question()
        ready = self.investigation()
        keys = (question,'page-owner/C1',f'investigation/{ready.owner}/{ready.id}',*decisions)
        for key in keys:
            with self.subTest(key=key):
                item = next(item for item in review_items.read(self.root) if item.key == key)
                delivery, result = review_page.submit(self.root,self.body(key,'question','Check the second case.'),self.status)
                expected_answer = 'question saved' if item.type == review_items.ReviewType.CHANGE else 'question sent'
                self.assertEqual(result, expected_answer)
                self.assertIsInstance(delivery,change_page.MessageDelivery)
                assert isinstance(delivery,change_page.MessageDelivery)
                self.assertIn(item.owner,delivery.recipients)
                self.assertIn('change_page.py answer-question --item '+key,delivery.message)
                change_page.deliver(delivery,self.status)
                pending = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(pending.state, review_items.ReviewState.WAITING)
                if item.type == review_items.ReviewType.CHANGE:
                    self.assertEqual(pending.actions, ())
                    self.assertNotIn('value="question"', review_page.build_snapshot(self.root).body(key).decode())
                args = ['answer-question','--item',key,'--text','The second case passes.']
                request = self.status.slots(delivery.page,delivery.row).question
                assert isinstance(request,change_page.OpenQuestion)
                args += ['--question',request.question_id]
                now = datetime.now(user_zone())
                until = now + timedelta(days=1)
                if item.type == review_items.ReviewType.CHANGE:
                    review_later.save(key, until)
                with patch.object(the_list, "_save", side_effect=OSError("The List save failed.")):
                    self.assertEqual(change_page.main(args),0)
                returned = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(returned.state, review_items.ReviewState.OPEN)
                self.assertEqual(sum('The second case passes.' in entry.words for entry in returned.log),1)
                self.assertEqual(sum('Check the second case.' in entry.words for entry in returned.log),1)
                if item.type == review_items.ReviewType.CHANGE:
                    self.assertTrue(returned.for_you(now), 'The answered change is visible in Open.')
                    # Mark read returns the item to Later.
                    self.assertEqual(review_later.times()[key], until)
                if key in decisions:
                    snapshot = review_page.build_snapshot(self.root)
                    self.assertTrue(snapshot.records[key].unread_answer)
                    request = self.card_body(snapshot, key, 'read')
                    _, result = review_page.submit(self.root, request, self.status, snapshot=snapshot)
                    self.assertEqual(result, 'marked read')
                    read = review_page.build_snapshot(self.root).records[key]
                    self.assertFalse(read.unread_answer)
                    self.assertTrue(read.for_you(now))

        with self.subTest(finding='F022'):
            members = staff.snapshot()
            parent = next(row for row in members.rows if row.name == 'page-owner')
            child_doc = self.fixture.temporary / 'child-production.md'
            _ = child_doc.write_text(self.fixture.production.read_text())
            child = fixture_member('showrunner', name='child-owner', parent=parent.id,
                                   doc=staff.LiveDoc(local_machine(), child_doc))
            identities = update_registration.QuestionStaff.for_record(staff.snapshot())
            scope = read_production(child, identities.record)
            update_registration.store_questions(scope, [update_registration.OutstandingItem(
                since='2026-11-12T12:00', text='Which layout should the page use?',
                question='Choose the layout.', asked_by=child.id)], identities)
            child_key = next(item.key for item in review_items.read(self.root)
                             if isinstance(item.source, review_items.QuestionSource) and item.owner == child.id)
            for key in (question, child_key):
                delivery, _ = review_page.submit(self.root,self.body(key,'question','Check this exact record.'),self.status)
                self.deliver_review_message(delivery,self.status)
            child_record = next(item.source.record for item in review_items.read(self.root)
                                if item.key == child_key and isinstance(item.source, review_items.QuestionSource))
            child_before = child_record.path.read_bytes()
            for key, answer in ((question, "The parent layout is ready."), (child_key, "The child layout is ready.")):
                item = next(item for item in review_items.read(self.root) if item.key == key)
                page = review_actions.delivery_page(item, self.status)
                pending = self.status.slots(page, page.rows[0]).question
                assert isinstance(pending, change_page.OpenQuestion)
                self.assertEqual(change_page.main(['answer-question','--item',key,'--question',pending.question_id,'--text',answer]),0,
                                 'F022: the exact item selects its record despite identical subtree text')
                returned = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(returned.state,review_items.ReviewState.OPEN)
                self.assertIn(answer, returned.log[-1].words)
                if key == question:
                    self.assertEqual(change_page.main(['answer-question','--item',key,'--question',pending.question_id,'--text','Repeat parent answer']),1,
                                     'F022: a repeated parent answer cannot select the child')
                    self.assertEqual(child_record.path.read_bytes(),child_before,'F022: only the selected record changes')
                    self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == child_key),
                                     review_items.ReviewState.WAITING)

    def test_waiting_cards_keep_the_for_you_action_groups(self) -> None:
        decisions = self.decisions()
        question = self.question()
        with patch("scripts.review_links.answer_sources.file_saved_investigation"):
            report = self.investigation()
        keys = (question, f'investigation/{report.owner}/{report.id}', *decisions)
        token, initial = change_browser.browser_manifest(change_page.console_folder(self.root))
        browser = change_page.ReviewBrowser(token, initial)
        words_rows = conf.read('review_links').whole('review_words_rows')
        for key in keys:
            opened = next(item for item in review_items.read(self.root) if item.key == key)
            delivery, _ = review_page.submit(self.root, self.body(key, 'question', 'Check the old case.'), self.status)
            self.deliver_review_message(delivery, self.status)
            waiting = next(item for item in review_items.read(self.root) if item.key == key)
            with self.subTest(kind=opened.type):
                self.assertEqual(waiting.state, review_items.ReviewState.WAITING)
                self.assertEqual(waiting.actions, opened.actions)
                self.assertEqual(review_page.form(waiting, browser, words_rows, 'fixture-revision'),
                                 review_page.form(opened, browser, words_rows, 'fixture-revision'))

    def waiting_actions(self, kind: review_items.ReviewType, actions: tuple[str, ...], wait: str = 'question') -> None:
        """Use a fresh record for each action and check the displaced answer."""
        for action in actions:
            with self.subTest(kind=kind, wait=wait, action=action):
                case = ReviewPageTests('runTest')
                case.setUp()
                try:
                    if kind == review_items.ReviewType.QUESTION:
                        key = case.question()
                    elif kind == review_items.ReviewType.INVESTIGATION:
                        report = case.investigation(wait != 'native')
                        key = f'investigation/{report.owner}/{report.id}'
                    else:
                        proposal, efficiency = case.decisions()
                        key = proposal if kind == review_items.ReviewType.PROPOSAL else efficiency
                        _ = write_config_fixture(case.fixture.temporary / 'config/nightly_review.conf',
                            'state_folder=nightly-review\n')
                    old_id = ''
                    if wait in ('question', 'validate'):
                        delivery, _ = review_page.submit(case.root, case.body(key, wait, 'Check the old case.'), case.status)
                        assert isinstance(delivery, change_page.MessageDelivery)
                        old = case.status.slots(delivery.page, delivery.row).question
                        old_id = old.question_id if isinstance(old, change_page.OpenQuestion) else ''
                        change_page.deliver(delivery, case.status)
                    elif wait == 'deferred':
                        item = next(item for item in review_items.read(case.root) if item.key == key)
                        assert isinstance(item.source, review_items.EfficiencySource)
                        _ = item.source.path.write_text(item.source.path.read_text().replace(' — proposed', ' — deferred'))
                    before = next(item for item in review_items.read(case.root) if item.key == key)
                    self.assertEqual(before.state, review_items.ReviewState.WAITING)
                    delivery, _ = review_page.submit(case.root, case.body(key, action, 'Use the new case.'), case.status)
                    self.deliver_review_message(delivery, case.status)
                    updated = next(item for item in review_items.read(case.root) if item.key == key)
                    expected = review_items.ReviewState.WAITING if action in ('question', 'validate') else review_items.ReviewState.DONE
                    self.assertEqual(updated.state, expected)
                    self.assertTrue(any('superseded' in event.words.casefold() for event in updated.log))
                    self.assertTrue(any('Use the new case.' in event.words for event in updated.log))
                    owner = staff.snapshot().by_id(updated.owner)
                    assert owner is not None
                    owner_log = read_production(owner, staff.snapshot()).log.read_text()
                    self.assertIn('superseded', owner_log.casefold())
                    if wait in ('question', 'native'):
                        record_before = updated
                        with self.assertRaises((ValueError, update_registration.RegistrationFailure)):
                            _ = change_page.answer_item(key, 'An old answer.', old_id, case.status)
                        after = next(item for item in review_items.read(case.root) if item.key == key)
                        self.assertEqual(after, record_before)
                    if action == 'question':
                        assert isinstance(delivery, change_page.MessageDelivery)
                        request = case.status.slots(delivery.page, delivery.row).question
                        self.assertIsInstance(request, change_page.OpenQuestion)
                        assert isinstance(request, change_page.OpenQuestion)
                        self.assertNotEqual(request.question_id, old_id)
                        self.assertIn('--question '+request.question_id, delivery.message)
                        _ = change_page.answer_item(key, 'The new case passes.', request.question_id, case.status)
                        self.assertEqual(next(item.state for item in review_items.read(case.root) if item.key == key),
                                         review_items.ReviewState.OPEN)
                finally:
                    case.doCleanups()

    def test_waiting_question_accepts_each_action(self) -> None:
        self.waiting_actions(review_items.ReviewType.QUESTION, ('answer', 'feedback', 'question'))

    def test_waiting_investigation_question_accepts_each_action(self) -> None:
        self.waiting_actions(review_items.ReviewType.INVESTIGATION, ('read', 'feedback', 'question'))

    def test_open_investigation_accepts_each_action(self) -> None:
        self.waiting_actions(review_items.ReviewType.INVESTIGATION, ('read', 'feedback', 'question'), 'native')

    def test_waiting_proposal_accepts_each_action(self) -> None:
        self.waiting_actions(review_items.ReviewType.PROPOSAL, review_items.DECISION_ACTIONS)

    def test_waiting_efficiency_accepts_each_action(self) -> None:
        self.waiting_actions(review_items.ReviewType.EFFICIENCY, review_items.DECISION_ACTIONS)

    def test_deferred_efficiency_accepts_each_action(self) -> None:
        self.waiting_actions(review_items.ReviewType.EFFICIENCY, review_items.DECISION_ACTIONS, 'deferred')

    def test_feedback_saves_each_type_and_uses_startup_delivery_recovery(self) -> None:
        decisions = self.decisions()
        question = self.question()
        ready = self.investigation()
        failed = investigation.file(ready.owner, 'Why did the report stop?', 'Read the report.',
                                    waiting_why='The owner must complete the investigation.')
        failed = investigation.answer(failed.owner, failed.id, 'The report is ready.')
        failed_key = f'investigation/{failed.owner}/{failed.id}'
        keys = (question,'page-owner/C1',f'investigation/{ready.owner}/{ready.id}',*decisions,failed_key)
        write = atomic_file.write_bytes_atomic

        def write_delivery(path: Path, content: bytes, *, mode: int | None = None) -> None:
            if path.name.startswith(change_page.DELIVERY_PREFIX):
                fields = cast(change_page.StoredDeliverySlots, json.loads(content))
                saved = fields.get('review_message')
                if saved is not None and saved.get('row') == failed.id and saved['saved'] and 'verdict' in fields:
                    raise OSError('fixture delivery save mark failed')
            write(path, content, mode=mode)

        for key in keys:
            with self.subTest(key=key), patch.object(atomic_file, 'write_bytes_atomic', new=write_delivery):
                item = next(item for item in review_items.read(self.root) if item.key == key)
                delivery,result = review_page.submit(self.root,self.body(key,'feedback','Use the second layout.'),self.status)
                self.assertEqual(result,'feedback sent')
                assert isinstance(delivery,change_page.MessageDelivery)
                self.assertIn(item.owner,delivery.recipients)
                expected = review_items.ReviewState.WAITING if item.type == review_items.ReviewType.CHANGE else review_items.ReviewState.DONE
                self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == key),expected)
        restarted = change_page.DeliveryStatus(self.root)
        with patch.object(atomic_file, 'write_bytes_atomic', new=write_delivery):
            change_page.recover_pending(restarted)
        self.assertEqual(sum('Use the second layout.' in message for _,_,message in self.fixture.messages),6,
                         'Each request sends one feedback message to its owner.')
        before = len(self.fixture.messages)
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(len(self.fixture.messages),before)

    def file_sent_question(self, recipient: staff.StaffId | None = None) -> str:
        document = self.fixture.production
        _ = document.write_text(document.read_text().replace(
            '# Demo\n', '# Demo\n\n> **Status: PRODUCTION — running.**\n', 1))
        owner = recipient if recipient is not None else next(
            row.id for row in staff.snapshot().rows if row.name == 'page-owner')
        kind = review_submissions.SubmissionKind.QUESTION
        request_id = f'r1-{"a" * 32}-{kind}-{owner}'
        filed = review_submissions.submit(review_submissions.NewItemRequest(
            request_id, kind, owner, 'Which layout should the page use?', review_submissions.NoEvidence()))
        return f'question/{owner}/{filed.path.name}'

    def file_response(self, owner: staff.StaffId) -> str:
        request = self.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner)
        saved = review_submissions.submit(request)
        with patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed.")):
            self.assertTrue(review_submissions.answer(saved.key, 'Use the wide layout.', saved.reply_token))
        return f'question/{owner}/{saved.path.name}'

    def test_sent_question_feedback_recovers_when_the_saved_mark_write_fails(self) -> None:
        key = self.file_sent_question()
        words = 'Use the wide layout for the next review.'
        write = atomic_file.write_bytes_atomic

        def write_delivery(path: Path, content: bytes, *, mode: int | None = None) -> None:
            if path.name.startswith(change_page.DELIVERY_PREFIX):
                fields = cast(change_page.StoredDeliverySlots, json.loads(content))
                saved = fields.get('user_answer')
                if saved is not None and saved['saved'] and 'verdict' in fields:
                    raise OSError('fixture delivery save mark failed')
            write(path, content, mode=mode)

        with patch.object(atomic_file, 'write_bytes_atomic', new=write_delivery):
            delivery, result = review_page.submit(self.root, self.body(key, 'feedback', words), self.status)
        self.assertEqual(result, 'feedback sent')
        assert isinstance(delivery, change_page.MessageDelivery)
        self.assertIsInstance(delivery.page, change_page.UserQuestionPage)
        self.assertEqual(self.fixture.messages, [])
        closed = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(closed.state, review_items.ReviewState.DONE)

        restarted = change_page.DeliveryStatus(self.root)
        change_page.recover_pending(restarted)
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual([recipient for recipient, _, message in self.fixture.messages if words in message], ['page-owner'])
        self.assertIsInstance(restarted.slots(delivery.page, delivery.row).verdict, change_page.NoDelivery)
        self.assertFalse(restarted.review_save_receipt(delivery.page, delivery.row).exists())

    def test_recovery_does_not_send_again_when_the_completion_write_fails(self) -> None:
        question = self.file_sent_question()
        record = self.investigation()
        for key in (question, f'investigation/{record.owner}/{record.id}'):
            with self.subTest(key=key):
                status = change_page.DeliveryStatus(self.root)
                write_slots = status.write_slots

                def fail_saved_mark(page: change_page.ReviewPage, row: str, slots: change_page.DeliverySlots) -> None:
                    if isinstance(page, (change_page.UserQuestionPage, change_page.ReviewMessagePage)) and page.saved['saved']:
                        raise OSError('fixture delivery save mark failed')
                    write_slots(page, row, slots)

                words = f'Use the wider layout for {key}.'
                with patch.object(status, 'write_slots', new=fail_saved_mark):
                    delivery, _ = review_page.submit(self.root, self.body(key, 'feedback', words), status)
                assert isinstance(delivery, change_page.MessageDelivery)
                receipt = status.review_save_receipt(delivery.page, delivery.row)
                self.assertTrue(receipt.exists())
                path = status.path(delivery.page, delivery.row)
                restarted = change_page.DeliveryStatus(self.root)
                write = atomic_file.write_bytes_atomic

                def fail_completion(target: Path, content: bytes, *, mode: int | None = None) -> None:
                    if target == path and 'verdict' not in json.loads(content):
                        raise OSError('fixture delivery completion failed')
                    write(target, content, mode=mode)

                self.fixture.messages.clear()
                with patch.object(atomic_file, 'write_bytes_atomic', new=fail_completion):
                    change_page.recover_pending(restarted)
                self.assertIsInstance(restarted.slots(delivery.page, delivery.row).verdict, change_page.DeliveryIntent)
                self.assertTrue(receipt.exists())
                self.assertEqual(sum(words in message for _, _, message in self.fixture.messages), 1)
                change_page.recover_pending(change_page.DeliveryStatus(self.root))
                self.assertEqual(sum(words in message for _, _, message in self.fixture.messages), 1)
                self.assertIsInstance(restarted.slots(delivery.page, delivery.row).verdict, change_page.NoDelivery)
                self.assertFalse(receipt.exists())

    def test_recovery_sends_after_the_dispatch_marker_sync_fails(self) -> None:
        question = self.file_sent_question()
        record = self.investigation()
        for key in (question, f'investigation/{record.owner}/{record.id}'):
            with self.subTest(key=key):
                words = f'Recover the send for {key}.'
                delivery, _ = review_page.submit(self.root, self.body(key, 'feedback', words), self.status)
                assert isinstance(delivery, change_page.MessageDelivery)
                mark = self.status.closeout_path(delivery.page, delivery.row)
                sync = atomic_file.sync_folder
                failed = False

                def fail_sync(folder: Path) -> None:
                    nonlocal failed
                    if folder == mark.parent and mark.exists() and not failed:
                        failed = True
                        raise OSError('fixture dispatch directory sync failed')
                    sync(folder)

                with patch.object(atomic_file, 'sync_folder', new=fail_sync):
                    change_page.recover_pending(change_page.DeliveryStatus(self.root))
                self.assertTrue(failed)
                self.assertFalse(any(words in message for _, _, message in self.fixture.messages))
                change_page.recover_pending(change_page.DeliveryStatus(self.root))
                change_page.recover_pending(change_page.DeliveryStatus(self.root))
                self.assertEqual(sum(words in message for _, _, message in self.fixture.messages), 1)

    def test_recovery_alerts_the_showrunner_after_a_failed_send_and_completion_write(self) -> None:
        producer = fixture_member("producer", name="bob")
        _ = write_config_fixture(conf.path("review_links"), "unrouted_alert_recipient=page-owner\n")
        question = self.file_sent_question(producer.id)
        open_record = investigation.file(producer.id, "Why did the gate stop?", "Read the gate log.",
                                         waiting_why="The producer checks the gate.")
        with patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed.")):
            record = investigation.answer(producer.id, open_record.id, "The repair is ready.")
        cases = ((question, 'feedback'), (f'investigation/{record.owner}/{record.id}', 'feedback'),
                 (self.file_response(producer.id), 'question'))
        for key, action in cases:
            with self.subTest(key=key):
                words = f'Keep the feedback for {key}.'
                snapshot = review_page.build_snapshot(self.root)
                body = self.card_body(snapshot, key, action, words)
                delivery, _ = review_page.submit(self.root, body, self.status, snapshot=snapshot)
                assert isinstance(delivery, change_page.MessageDelivery)
                path = self.status.path(delivery.page, delivery.row)
                original_message = change_page.outbound_review_message(delivery).text
                write = atomic_file.write_bytes_atomic
                send = change_page.send_to_staff
                original_sends = 0

                def fail_completion(target: Path, content: bytes, *, mode: int | None = None) -> None:
                    if target == path and 'verdict' not in json.loads(content):
                        raise OSError('fixture delivery completion failed')
                    write(target, content, mode=mode)

                def reject_feedback(recipient: staff.StaffId, sender: str, message: str, *, key: str = "") -> None:
                    nonlocal original_sends
                    if message == original_message:
                        original_sends += 1
                        raise RuntimeError('fixture recipient offline')
                    _ = send(recipient, sender, message, key=key)

                with patch.object(change_page, 'send_to_staff', new=reject_feedback):
                    with patch.object(atomic_file, 'write_bytes_atomic', new=fail_completion):
                        change_page.recover_pending(change_page.DeliveryStatus(self.root))
                    sends_before_recovery = original_sends
                    self.assertGreater(sends_before_recovery, 0)
                    self.assertFalse(any(words in message for _, _, message in self.fixture.messages))
                    change_page.recover_pending(change_page.DeliveryStatus(self.root))
                    change_page.recover_pending(change_page.DeliveryStatus(self.root))
                self.assertEqual(original_sends, sends_before_recovery)
                alerts = [(target, message) for target, _, message in self.fixture.messages if words in message]
                self.assertEqual(len(alerts), 1)
                target, message = alerts[0]
                self.assertEqual(target, "page-owner")
                self.assertIn('Last error: fixture recipient offline', message)
                self.assertIsInstance(self.status.slots(delivery.page, delivery.row).verdict, change_page.NoDelivery)

    def test_recovery_reuses_the_send_key_when_the_result_write_fails(self) -> None:
        sent: dict[str, str] = {}
        for case in ("investigation", "change", "response"):
            with self.subTest(case=case):
                if case == "investigation":
                    record = self.investigation()
                    key = f'investigation/{record.owner}/{record.id}'
                    action = "feedback"
                elif case == "change":
                    key, action = "page-owner/C1", "works"
                else:
                    key, action = self.file_response(self.ready_submission_recipient().id), "question"
                words = "Keep the saved layout."
                snapshot = review_page.build_snapshot(self.root) if case == "response" else None
                body = self.card_body(snapshot, key, action, words) if snapshot else self.body(key, action, words)
                delivery, _ = review_page.submit(self.root, body, self.status, snapshot=snapshot)
                assert isinstance(delivery, change_page.MessageDelivery)
                file = self.status.closeout_path(delivery.page, delivery.row)
                write = atomic_file.write_bytes_atomic
                seen: list[str] = []

                def relay(_address: str, _sender: str, message: str, *, key: str, recipient_key: str) -> None:
                    self.assertTrue(recipient_key)
                    seen.append(key)
                    if key not in sent:
                        sent[key] = message

                def stop(target: Path, content: bytes, *, mode: int | None = None) -> None:
                    if target == file and seen:
                        raise OSError("The server stopped before it saved the send result.")
                    write(target, content, mode=mode)

                with patch.object(change_page, "send_message", new=relay):
                    with patch.object(atomic_file, "write_bytes_atomic", new=stop):
                        change_page.recover_pending(change_page.DeliveryStatus(self.root))
                    if case in ("investigation", "response"):
                        verdict = self.status.slots(delivery.page, delivery.row).verdict
                        assert isinstance(verdict, change_page.DeliveryIntent)
                        self.assertEqual(verdict.attempt, delivery.attempt)
                    if case == "response":
                        current = review_page.build_snapshot(self.root)
                        self.assertEqual(current.records[key].state, review_items.ReviewState.WAITING)
                        with patch.object(reply_tokens, 'path', return_value=self.root / 'absent-reply-token.key'):
                            for source in (current, None):
                                with self.assertRaises(review_items.ReviewRecordChanged):
                                    _ = review_page.submit(self.root, body, self.status, snapshot=source)
                                replayed = self.status.slots(delivery.page, delivery.row).verdict
                                assert isinstance(replayed, change_page.DeliveryIntent)
                                self.assertEqual(replayed.attempt, delivery.attempt)
                    change_page.recover_pending(change_page.DeliveryStatus(self.root))
                send_key = change_page.review_delivery_key(delivery.page.review_list, delivery.row, delivery.attempt)
                self.assertEqual(seen, [send_key] * 2)
                if case == "investigation":
                    self.assertEqual(sum(words in message for message in sent.values()), 1)
                self.assertIsInstance(self.status.slots(delivery.page, delivery.row).verdict, change_page.NoDelivery)
        self.assertEqual(len(sent), 3)

    def test_validating_question_recovers_after_receipt_and_result_write_failures(self) -> None:
        words = 'Keep the saved layout.'
        write = atomic_file.write_bytes_atomic

        def fail_result(target: Path, content: bytes, *, mode: int | None = None) -> None:
            if target == path and self.fixture.messages:
                raise OSError('fixture send result failed')
            write(target, content, mode=mode)

        _ = change_review.append_rows(self.fixture.review_list, pictured_changes([
            change_review.Visible('Retry the Question log', 'Ask the validator.', 'The question arrives.'),
        ]), datetime.now(user_zone()).date(), '', self.fixture.production, record=staff.snapshot())
        cases = (('C1', 'closeout'), ('C2', 'slots'), ('C3', 'receipt and slots'), ('C4', 'question log'))
        write_text = atomic_file.write_text_atomic
        for row, failed_write in cases:
            with self.subTest(write=failed_write):
                self.fixture.messages.clear()
                key = 'page-owner/'+row
                check, _ = review_page.submit(self.root, self.body(key, 'validate'), self.status)
                assert isinstance(check, change_page.MessageDelivery)
                change_page.deliver(check, self.status)
                self.fixture.messages.clear()
                snapshot = review_page.build_snapshot(self.root)
                body = self.card_body(snapshot, key, 'question', words)
                if failed_write == 'question log':
                    with (patch.object(change_review, 'record_review_log', side_effect=OSError('fixture log write failed')),
                          self.assertRaisesRegex(OSError, 'fixture log write failed')):
                        _ = review_page.submit(self.root, body, self.status, snapshot=snapshot)
                    pending = self.status.slots(check.page, row).question
                    assert isinstance(pending, change_page.OpenQuestion)
                    self.assertIsInstance(pending.delivery, change_page.DeliveryIntent)
                    change_page.recover_pending(self.status)
                    change_page.recover_pending(self.status)
                    self.assertEqual(len(self.fixture.messages), 1)
                    self.assertIn(words, self.fixture.messages[0][2])
                    self.assertEqual(self.status.slots(check.page, row).question,
                                     replace(pending, delivery=change_page.NoDelivery()))
                    continue
                delivery, _ = review_page.submit(self.root, body, self.status, snapshot=snapshot)
                assert isinstance(delivery, change_page.MessageDelivery)
                path = (self.status.closeout_path(delivery.page, row) if failed_write == 'closeout'
                        else self.status.path(delivery.page, row))
                pending = self.status.slots(delivery.page, row).question
                assert isinstance(pending, change_page.OpenQuestion)
                assert isinstance(pending.delivery, change_page.DeliveryIntent)
                receipt = self.status.review_save_receipt(delivery.page, row, pending.delivery)

                def fail_receipt(target: Path, content: str, *, new_file_mode: int | None = None) -> None:
                    if failed_write == 'receipt and slots' and target == receipt:
                        raise OSError('fixture acceptance receipt write failed')
                    write_text(target, content, new_file_mode=new_file_mode)

                with (patch.object(atomic_file, 'write_bytes_atomic', new=fail_result),
                      patch.object(atomic_file, 'write_text_atomic', new=fail_receipt)):
                    change_page.recover_pending(change_page.DeliveryStatus(self.root))
                self.assertEqual(len(self.fixture.messages), 1)
                self.assertEqual(self.status.slots(delivery.page, row).question, pending)
                self.assertEqual(receipt.exists(), failed_write != 'receipt and slots')
                change_page.recover_pending(change_page.DeliveryStatus(self.root))
                change_page.recover_pending(change_page.DeliveryStatus(self.root))
                self.assertEqual(len(self.fixture.messages), 1)
                recovered = self.status.slots(delivery.page, row).question
                self.assertEqual(recovered, replace(pending, delivery=change_page.NoDelivery()))

    def test_later_refuses_an_expired_deadline_before_writing(self) -> None:
        snapshot = review_page.build_snapshot(self.root)
        key = 'page-owner/C1'
        listing = self.fixture.review_list.read_bytes()
        times_before = review_later.times()
        form = self.card_body(snapshot, key, 'later', 'Try this expired time.')
        for value in ('2026-10-31T09:00', '2026-10-31T09:00:00-04:00'):
            with self.subTest(value=value):
                body = form+'&'+urlencode({'later': value})
                with self.assertRaisesRegex(ValueError, r'^Choose a future parking date\.$'):
                    _ = review_page.submit(self.root, body, self.status, snapshot=snapshot)
                self.assertEqual(self.fixture.review_list.read_bytes(), listing)
                self.assertEqual(review_later.times(), times_before)

    def test_review_projects_legacy_and_positioned_answers(self) -> None:
        _ = self.enterContext(patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed.")))
        now = datetime.now(user_zone())
        before = now - timedelta(hours=2)
        read_at = now - timedelta(hours=1)
        after = now - timedelta(minutes=30)
        until = now + timedelta(days=1)
        expected: list[tuple[str, tuple[str, ...], question_path.Queue, bool]] = []
        question = 'Does the layout fit?'
        repeated = 'The layout fits.'
        old_reply = 'The earlier result passed.'
        new_reply = 'The current result passed.'
        for case, positions, log_id, stamps, later, answers, queue, unread in (
            ('first reply unlogged', {}, 'second', (before, after), True, (old_reply, new_reply),
             question_path.Queue.NEEDS_ME, True),
            ('saved position wins', {'1': 1}, 'first', (before, after), True, (repeated, repeated),
             question_path.Queue.NEEDS_ME, True),
            ('read after reply', {}, 'second', (before, before), True, (old_reply, new_reply),
             question_path.Queue.LATER, False),
            ('read at the reply time', {}, '', (before, read_at), False, (old_reply, new_reply),
             question_path.Queue.NEEDS_ME, False),
            ('unnamed log copy', {}, '', (before, after), False, (repeated, repeated),
             question_path.Queue.NEEDS_ME, True),
        ):
            key = self.question(case)
            path = self.question_path(key)
            record, = update_registration.selected_question_records([path], (), None)
            record.item['followups'] = [
                update_registration.FollowupExchange(followup=question, reply=words,
                                                     question_id=identifier, replied_at=stamp.isoformat())
                for identifier, words, stamp in zip(('first', 'second'), answers, stamps, strict=True)
            ]
            if positions:
                record.item['reply_log_positions'] = positions
            read_words = 'Later until ' + until.isoformat() if later else 'Marked read'
            record.item['review_log'] = review_events.encode((
                review_events.ReviewLog(read_at, 'nate', read_words),
                review_events.ReviewLog(stamps[-1], record.asker, 'Answer: ' + answers[-1], log_id),
            ))
            update_registration.write_question_record(record)
            if later:
                review_later.save(key, until)
            shown = (*answers, answers[-1]) if not positions and not log_id else answers
            expected.append((key, shown, queue, unread))

        def legacy_question(title: str, replies: tuple[str, ...], events: tuple[review_events.ReviewLog, ...],
                            positions: dict[str, int]) -> str:
            key = self.question(title)
            record, = update_registration.selected_question_records([self.question_path(key)], (), None)
            record.item['followups'] = [
                update_registration.FollowupExchange(followup=question, reply=reply) for reply in replies]
            if positions:
                record.item['reply_log_positions'] = positions
            record.item['review_log'] = review_events.encode(events)
            update_registration.write_question_record(record)
            review_later.save(key, until)
            return key

        parked = review_events.ReviewLog(read_at, 'nate', 'Later until ' + until.isoformat())
        owner_answer = review_events.ReviewLog(after, 'page-owner', 'Answer: ' + new_reply)
        old_answer = review_events.ReviewLog(before, 'page-owner', 'Answer: ' + old_reply)
        for title, replies, events, positions, shown, queue, unread in (
            ('undated native reply', (new_reply,), (parked,), {}, (new_reply,), question_path.Queue.LATER, False),
            ('unnamed answer after Later', (new_reply,), (parked, owner_answer), {}, (new_reply, new_reply),
             question_path.Queue.NEEDS_ME, True),
            ('log-only Question', (), (parked, old_answer, owner_answer), {}, (new_reply, old_reply),
             question_path.Queue.NEEDS_ME, True),
            ('old reply with a positioned answer', (old_reply, new_reply,), (old_answer, owner_answer, parked),
             {'1': 1}, (old_reply, new_reply, old_reply), question_path.Queue.NEEDS_ME, True),
        ):
            key = legacy_question(title, replies, events, positions)
            expected.append((key, shown, queue, unread))

        orphan = legacy_question('unowned Question', (), (parked, owner_answer), {})
        members = staff.snapshot()
        orphan_owner = next(member for member in members.rows if member.name == 'page-owner')
        without_owner = replace(members, rows=tuple(member for member in members.rows if member.id != orphan_owner.id))
        expected.append((orphan, (new_reply,), question_path.Queue.NEEDS_ME, True))

        extra_changes = [
            change_review.Visible(title, 'Read the log.', 'The result is present.')
            for title in ('Delayed answer log', 'Named answer without a saved time', 'Repeated Change feedback',
                          'Answer before the new handoff', 'Answer before the current question',
                          'Answer while a new question is open', 'Log written after a new handoff')]
        _ = change_review.append_rows(self.fixture.review_list, pictured_changes(extra_changes), now.date(), '',
                                      self.fixture.production, record=staff.snapshot())
        page = self.fixture.page

        class EventClock(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> Self:
                return cls.fromtimestamp(event.time.timestamp(), tz)

        for row, validating, answer_type, saved_time, named, read in (
            ('C1', False, change_page.AnsweredQuestion, after, False, False),
            ('C2', True, change_page.AnsweredQuestion, after, False, False),
            ('C3', False, change_page.AnsweredQuestion, before, False, True),
            ('C4', False, change_page.AnsweredQuestion, before, True, True),
            ('C5', False, change_page.AnsweredQuestion, None, True, False),
            ('C6', False, change_page.CompletedFeedback, after, False, False),
        ):
            key = 'page-owner/' + row
            if validating:
                event = review_events.ReviewLog(read_at, 'nate', 'validate')
                with patch.object(change_review, 'datetime', EventClock):
                    delivery, _ = review_page.submit(self.root, self.body(key, 'validate'), self.status)
                self.deliver_review_message(delivery, self.status)
            previous = new_reply if row == 'C6' else old_reply
            events = [review_events.ReviewLog(before, 'page-owner', 'Answer: ' + previous, 'older')]
            if read:
                events.append(replace(parked, words='Marked read: current') if row == 'C4' else parked)
                review_later.save(key, until)
            elif row in ('C1', 'C6'):
                review_later.save(key, until)
            if named:
                events.append(replace(owner_answer, feedback_request_id=question_path.answer_log_id('current')))
            for event in events:
                with patch.object(change_review, 'datetime', EventClock):
                    change_review.record_review_log(page.review_list, row, event.words, page.production,
                                                    feedback_request_id=event.feedback_request_id)
            with self.status.locked():
                slots = self.status.slots(page, row)
                self.status.write_slots(page, row, replace(slots,
                    question=answer_type('current', question, new_reply)))
            if saved_time is not None:
                path = self.status.path(page, row)
                fields = cast(dict[str, object], json.loads(path.read_text()))
                fields['answered_at'] = saved_time.isoformat()
                if validating:
                    fields['handed_at'] = read_at.isoformat()
                _ = path.write_text(json.dumps(fields))
            shown = (previous, new_reply) if validating else (new_reply, previous)
            expected.append((key, shown, question_path.Queue.LATER if read else question_path.Queue.NEEDS_ME, not read))

        for row, reply_time, current_request, expected_answers in (
            ('C7', before, change_page.NoQuestion(), (old_reply,)),
            ('C8', read_at + timedelta(minutes=15), change_page.AnsweredQuestion('current', question, new_reply),
             (old_reply, new_reply)),
            ('C9', read_at + timedelta(minutes=15), change_page.OpenQuestion('current', question, change_page.NoDelivery()),
             (old_reply,)),
        ):
            key = 'page-owner/' + row
            event = review_events.ReviewLog(read_at, 'nate', 'validate')
            with patch.object(change_review, 'datetime', EventClock):
                delivery, _ = review_page.submit(self.root, self.body(key, 'validate'), self.status)
            self.deliver_review_message(delivery, self.status)
            events = [replace(old_answer, time=reply_time, feedback_request_id='older')]
            if row != 'C7':
                events.append(review_events.ReviewLog(after, 'nate', 'question: ' + question, 'current'))
            for event in events:
                with patch.object(change_review, 'datetime', EventClock):
                    change_review.record_review_log(page.review_list, row, event.words, page.production,
                                                    feedback_request_id=event.feedback_request_id)
            with self.status.locked():
                slots = self.status.slots(page, row)
                self.status.write_slots(page, row, replace(slots, question=current_request))
            path = self.status.path(page, row)
            fields = cast(dict[str, object], json.loads(path.read_text()))
            fields['handed_at'] = read_at.isoformat()
            _ = path.write_text(json.dumps(fields))
            expected.append((key, expected_answers, question_path.Queue.VALIDATING, False))

        key = 'page-owner/C10'
        _ = self.status.ask(page, 'C10', question)
        request = self.status.slots(page, 'C10').question
        assert isinstance(request, change_page.OpenQuestion)
        save_log = change_review.record_review_log

        def write_delayed_log(path: Path, row: str, words: str, production: Path, who: str = 'nate', *,
                              feedback_request_id: str = '', **saved_time: datetime) -> None:
            nonlocal event
            if row == 'C10' and words == 'Answer: ' + new_reply:
                event = review_events.ReviewLog(read_at, 'nate', 'validate')
                with (patch.object(change_review, 'datetime', EventClock),
                      patch.object(time, 'time', return_value=read_at.timestamp())):
                    delivery, _ = review_page.submit(self.root, self.body(key, 'validate'), self.status)
                    self.deliver_review_message(delivery, self.status)
                event = replace(owner_answer, time=after)
            with patch.object(change_review, 'datetime', EventClock):
                save_log(path, row, words, production, who, feedback_request_id=feedback_request_id, **saved_time)

        event = replace(owner_answer, time=before)
        with (patch.object(change_page, 'datetime', EventClock),
              patch.object(change_review, 'record_review_log', new=write_delayed_log)):
            self.assertTrue(change_page.answer_item(key, new_reply, request.question_id, self.status))
        expected.append((key, (new_reply,), question_path.Queue.VALIDATING, False))

        for kind, legacy in ((investigation.InvestigationKind.INVESTIGATION, False),
                             (investigation.InvestigationKind.INVESTIGATION, True),
                             (investigation.InvestigationKind.BUG, False)):
            report = investigation.file(self.fixture.unit.id, 'Why did the gate stop?', 'Read the gate log.',
                                        waiting_why='The owner has the log.', kind=kind)
            key = f'investigation/{report.owner}/{report.id}'
            review_later.save(key, until)
            path = investigation.folder(report.owner) / (report.id + '.md')
            previous_log = next(line for line in path.read_text().splitlines(keepends=True)
                                if line.startswith('review_log: '))
            _ = investigation.answer(report.owner, report.id, new_reply)
            if legacy:
                _ = path.write_text(''.join(previous_log if line.startswith('review_log: ') else line
                                            for line in path.read_text().splitlines(keepends=True)))
            # Main shows both the native answer and the unnamed log answer.
            expected.append((key, (new_reply,) if legacy else (new_reply, new_reply), question_path.Queue.NEEDS_ME, True))

        proposal, efficiency = self.decisions()
        proposal_path = Path(proposal.removeprefix('proposal/'))
        _ = proposal_issue.record_review(proposal_path, 'Answer: ' + new_reply, review_events.KeepMark(), now.date())
        review_later.save(proposal, until)
        expected.append((proposal, (new_reply,), question_path.Queue.NEEDS_ME, True))

        document = self.fixture.production
        _ = document.write_text(document.read_text().replace(
            '# Demo\n', '# Demo\n\n> **Status: PRODUCTION — running.**\n', 1))
        legacy_response = self.file_response(orphan_owner.id)
        cached_response = self.file_response(orphan_owner.id)
        for key, response_case in ((cached_response, 'positioned'),
                                   (self.file_response(orphan_owner.id), 'named exchange'),
                                   (legacy_response, 'legacy with a named log')):
            if response_case != 'positioned':
                record, = update_registration.selected_question_records([self.question_path(key)], (), None)
                del record.item['reply_log_positions']
                del record.item['answered_at']
                assert 'review_log' in record.item and 'followups' in record.item
                events = review_events.decode(record.item['review_log'])
                record.item['review_log'] = review_events.encode(
                    (*events[:-1], replace(events[-1], feedback_request_id=question_path.answer_log_id('current'))))
                if response_case == 'named exchange':
                    record.item['followups'][-1]['question_id'] = 'current'
                update_registration.write_question_record(record)
            if key in (legacy_response, cached_response):
                review_later.save(key, until)
            expected.append((key, ('Use the wide layout.',), question_path.Queue.NEEDS_ME, True))

        ledger = Path(efficiency.removeprefix('efficiency/').rsplit('/', 1)[0])
        _ = ledger.write_text(''.join(f'- 2026-11-01 tests: Check {number} — proposed\n' for number in range(6)))
        stored: dict[str, object] = {}
        for index in range(6):
            key = f'efficiency/{ledger}/{index}'
            later = index % 2 == 1
            events = (
                review_events.ReviewLog(before.replace(tzinfo=None), 'owner', 'Answer: ' + new_reply),
                *((review_events.ReviewLog(read_at.replace(tzinfo=None), 'nate', 'Marked read'),) if later else ()),
            )
            stored[f'E{index+1}'] = {'log': review_events.rows(events)}
            if later:
                review_later.save(key, until)
            expected.append((key, (new_reply,), question_path.Queue.LATER if later else question_path.Queue.NEEDS_ME,
                             not later))
        _ = efficiency_reviews.path(ledger).write_text(json.dumps(stored))

        snapshot = review_page.build_snapshot(self.root)
        late_event, = [entry for entry in snapshot.records['page-owner/C10'].log
                       if entry.feedback_request_id == question_path.answer_log_id(request.question_id)]
        self.assertEqual(late_event.time, before)
        with patch.object(staff, 'snapshot', return_value=without_owner):
            orphan_snapshot = review_page.build_snapshot(self.root)
        self.assertIsInstance(orphan_snapshot.records[orphan].source, review_items.UnownedQuestionSource)
        answers = re.compile('|'.join(re.escape(word) for word in (old_reply, new_reply, repeated, 'Use the wide layout.')))
        entries = {entry['key']: entry for entry in unpack_index(review_page.index_json(snapshot.entries))}
        entries[orphan] = next(entry for entry in unpack_index(review_page.index_json(orphan_snapshot.entries))
                               if entry['key'] == orphan)
        for key, words, queue, unread in expected:
            page_snapshot = orphan_snapshot if key == orphan else snapshot
            item = page_snapshot.records[key]
            with self.subTest(record=item.title, key=key):
                self.assertEqual(tuple(answers.findall(page_snapshot.body(key).decode())), words)
                self.assertIs(item.queue_at(now), queue)
                self.assertEqual(bool(entries[key].get('unreadAnswer')), unread)

        for key, submission_snapshot in ((orphan, orphan_snapshot), (legacy_response, None), (cached_response, None)):
            record_type = 'Question' if key == orphan else 'Response'
            with self.subTest(record=f'Mark read on an unowned {record_type}', key=key), patch.object(
                    staff, 'snapshot', return_value=without_owner):
                card = orphan_snapshot.body(key).decode()
                self.assertIn('value="read">Mark read</button>', card)
                if record_type == 'Response':
                    parser = test_change_page.SavedAnswerForm()
                    parser.feed(card)
                    body = urlencode(parser.fields)
                else:
                    body = self.card_body(orphan_snapshot, key, 'read')
                if key == cached_response:
                    cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
                    self.addCleanup(cache.close)
                    saved = cache.save(body, self.status)
                    delivery, answer = saved.delivery, saved.answer
                else:
                    delivery, answer = review_page.submit(self.root, body, self.status, snapshot=submission_snapshot)
                if record_type == 'Response':
                    self.assertIn('<input type="hidden" name="feedback" value="">', card)
                self.assertIsInstance(delivery, change_page.NoMessageDelivery)
                self.assertEqual(answer, 'marked read')
                updated = review_page.build_snapshot(self.root).records[key]
                self.assertIs(updated.queue_at(now), question_path.Queue.LATER)
                self.assertFalse(updated.unread_answer)

        with self.subTest(count='six legacy Efficiency rows'):
            zone_path = self.fixture.temporary / 'user_zone.conf'
            _ = zone_path.write_text('zone=America/New_York\n')
            with (patch.dict(os.environ), patch.object(user_zone_config, 'CONF', str(zone_path)),
                  patch.object(conf_reads, 'read_text', wraps=conf_reads.read_text) as reads):
                _ = os.environ.pop('USER_ZONE', None)
                self.assertEqual(review_items.efficiency(document, record=snapshot.members), 3)
            self.assertEqual(sum(call.args[0] == str(zone_path) for call in reads.call_args_list), 1)

    def test_an_answer_returns_a_later_investigation_until_mark_read(self) -> None:
        _ = self.enterContext(patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed.")))
        for legacy_answer in (False, True):
            with self.subTest(legacy_answer=legacy_answer):
                self.check_later_investigation_answer(legacy_answer)

    def check_later_investigation_answer(self, legacy_answer: bool) -> None:
        record = self.investigation(False)
        key = f'investigation/{record.owner}/{record.id}'
        now = datetime.now(user_zone())
        until = now + timedelta(days=1)
        item = review_page.build_snapshot(self.root).records[key]
        _ = review_actions.later(item, until.isoformat(), '')
        before = review_page.build_snapshot(self.root)
        self.assertFalse(before.records[key].for_you(now))

        path = investigation.folder(record.owner) / (record.id + '.md')
        earlier_log = next(line for line in path.read_text().splitlines() if line.startswith('review_log: '))
        _ = investigation.answer(record.owner, record.id, 'The gate log has the answer.')
        if legacy_answer:
            lines = path.read_text().splitlines(keepends=True)
            _ = path.write_text(''.join(earlier_log + '\n' if line.startswith('review_log: ') else line for line in lines))
        zone_path = self.fixture.temporary / 'user_zone.conf'
        _ = zone_path.write_text('zone=America/New_York\n')
        with patch.dict(os.environ), patch.object(user_zone_config, 'CONF', str(zone_path)):
            _ = os.environ.pop('USER_ZONE', None)
            answered = review_page.build_snapshot(self.root)
            with patch.object(conf_reads, 'read_text', wraps=conf_reads.read_text) as reads:
                self.assertTrue(answered.records[key].for_you(now))
                entries = unpack_index(review_page.index_json(answered.entries))
                self.assertTrue(answered.records[key].unread_answer)
            self.assertEqual([call for call in reads.call_args_list if call.args[0] == str(zone_path)], [])
        self.assertTrue(next(entry for entry in entries if entry['key'] == key).get('unreadAnswer'))
        self.assertIn(b'value="read"', answered.body(key))
        self.assertEqual(review_later.times()[key], until)
        if shutil.which('firefox'):
            proof = """<script>
const row=document.querySelector('[data-list-item][data-key="'+KEY+'"]');
const marked=row?.querySelector('[data-new-answer]')?.textContent==='New answer';
void fetch('/results',{method:'POST',body:JSON.stringify(marked ? [] : ['The Open row needs a New answer mark.'])});
</script>""".replace('KEY', json.dumps(key))
            markup = answered.render().replace(b'</body>', proof.encode() + b'</body>')
            self.assertEqual(firefox_probe_results(markup), [[]])

        delivery, result = review_page.submit(self.root, self.card_body(answered, key, 'read'), self.status, snapshot=answered)
        self.assertIsInstance(delivery, change_page.NoMessageDelivery)
        self.assertEqual(result, 'marked read')
        current = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertFalse(current.unread_answer)
        self.assertTrue(current.hidden_until(now))
        self.assertEqual(review_later.times()[key], until)

    def test_later_deadlines_during_the_first_dst_hour(self) -> None:
        zone = ZoneInfo('America/Los_Angeles')
        now = datetime(2026, 11, 1, 1, 15, tzinfo=zone)
        first = now.replace(minute=45)
        second = now.replace(minute=30, fold=1)
        self.check_later_deadlines(now, ((first, True), (second, True), (second.replace(minute=0), True)), first)

    def test_later_deadlines_during_the_second_dst_hour(self) -> None:
        zone = ZoneInfo('America/Los_Angeles')
        now = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
        first = now.replace(minute=45, fold=0)
        self.check_later_deadlines(now, ((first, False), (now, False)), None)

    def check_later_deadlines(self, now: datetime, cases: tuple[tuple[datetime, bool], ...],
                              earliest: datetime | None) -> None:
        key = self.question()
        snapshot = review_page.build_snapshot(self.root)
        item = snapshot.records[key]
        owner = next(member for member in snapshot.members.rows if member.id == item.owner)
        identities = update_registration.QuestionStaff.for_record(snapshot.members)
        items = [replace(item, times=replace(item.times, later=deadline)) for deadline, _ in cases]
        for candidate, (deadline, active) in zip(items, cases, strict=True):
            with self.subTest(deadline=deadline.isoformat()):
                self.assertEqual(candidate.hidden_until(now), active)
        expected = '' if earliest is None else str(int(earliest.timestamp()))
        stored = {str(i): deadline for i, (deadline, _) in enumerate(cases)}
        clock = Mock(now=Mock(return_value=now))
        with (patch.object(review_items, 'read_kind', return_value=items),
              patch.object(review_items, 'datetime', clock),
              self.subTest(reader='Question counts')):
            self.assertEqual(review_items.question_counts(owner, snapshot.zone, identities, None),
                             (sum(not active for _, active in cases), expected))
        with (patch.object(review_later, 'times', return_value=stored),
              patch.object(review_later, 'datetime', clock),
              self.subTest(reader='Status deadline')):
            self.assertEqual(review_later.deadline(), expected)

    def test_later_preserves_both_instants_of_a_repeated_wall_time(self) -> None:
        key = self.question()
        zone = ZoneInfo('America/Los_Angeles')
        before_repeat = datetime(2026, 11, 1, 8, 30, tzinfo=ZoneInfo('UTC'))
        clock = Mock(wraps=datetime, now=Mock(return_value=before_repeat))
        with (patch.dict(os.environ, {'USER_ZONE': zone.key}),
              patch.object(review_actions, 'datetime', clock)):
            for hour, offset in ((8, '-07:00'), (9, '-08:00')):
                with self.subTest(hour=hour):
                    item = review_page.build_snapshot(self.root).records[key]
                    selected = datetime(2026, 11, 1, hour, 45, tzinfo=ZoneInfo('UTC'))
                    _ = review_actions.later(item, selected.isoformat(), '')
                    saved = review_later.times()[key]
                    self.assertEqual(saved.isoformat(), '2026-11-01T01:45:00' + offset)
                    self.assertEqual(saved.timestamp(), selected.timestamp())

    def test_later_hides_each_type_from_the_shared_queue_and_then_returns(self) -> None:
        _ = change_review.append_rows(self.fixture.review_list,
            pictured_changes([change_review.Visible('Unrouted change', 'Read the log.', 'The result is present.')]),
            datetime.now(user_zone()).date(), '', self.fixture.production,
            producers=[staff.StaffId('missing-producer')], record=staff.snapshot())
        decisions = self.decisions()
        question = self.question()
        ready = self.legacy_investigation()
        keys = (question,'page-owner/C1',f'investigation/{ready.owner}/{ready.id}',*decisions)
        until = datetime.now(user_zone()).replace(second=0,microsecond=0)+timedelta(days=1)
        for key in keys:
            with self.subTest(key=key):
                item = next(item for item in review_items.read(self.root) if item.key == key)
                if key == 'page-owner/C1':
                    delivery, _ = review_page.submit(self.root, self.body(key, 'question', 'Did the check pass?'), self.status)
                    assert isinstance(delivery, change_page.MessageDelivery)
                    change_page.deliver(delivery, self.status)
                    request = self.status.slots(delivery.page, delivery.row).question
                    assert isinstance(request, change_page.OpenQuestion)
                    with patch.object(the_list, 'file_exchanges', side_effect=OSError('The List save failed.')):
                        self.assertTrue(change_page.answer_item(key, 'The check passed.', request.question_id, self.status))
                    item = review_page.build_snapshot(self.root).records[key]
                    self.assertTrue(item.unread_answer)
                body = self.body(key,'later','Read this tomorrow.')+'&'+urlencode({'later':until.isoformat()})
                if key == question:
                    with (patch.object(review_later, 'write_text_atomic', side_effect=OSError('fixture deadline write failed')),
                          self.assertRaisesRegex(OSError, 'fixture deadline write failed')):
                        _ = review_page.submit(self.root, body, self.status)
                    current = next(current for current in review_items.read(self.root) if current.key == key)
                    self.assertEqual(current.log, item.log)
                    self.assertNotIn(key, review_later.times())
                _,result = review_page.submit(self.root,body,self.status)
                self.assertIn('later until',result)
                hidden = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(hidden.state,item.state)
                self.assertFalse(hidden.for_you(datetime.now(user_zone())))
                self.assertTrue(hidden.for_you(until+timedelta(seconds=1)))
                self.assertTrue(any('Read this tomorrow.' in entry.words for entry in hidden.log))
                _,entries = review_items.next_queue(item.type,self.fixture.production,False,staff.snapshot())
                self.assertNotIn(item.queue_key,[entry.key for entry in entries])
                self.assertEqual(review_later.times()[key],until)
        for action, prior_logged_answer in (('read', False), ('later', False), ('read', True), ('later', True)):
            with self.subTest(legacy_question_reply=True, action=action, prior_logged_answer=prior_logged_answer):
                key = self.question(f'Review the legacy reply with {action}?')
                path = self.question_path(key)
                if prior_logged_answer:
                    record, = update_registration.selected_question_records([path], (), None)
                    asked = 'Does the latest layout fit?'
                    replied = 'The full layout fits.'
                    earlier = datetime.now(user_zone()).replace(tzinfo=None) - timedelta(minutes=1)
                    record.item['followups'] = [update_registration.FollowupExchange(
                        followup=asked, reply=replied, question_id='legacy-layout')]
                    record.item['reply_log_positions'] = {'0': 1}
                    record.item['review_log'] = review_events.encode((
                        review_events.ReviewLog(earlier, 'nate', 'question: ' + asked, 'legacy-layout'),
                        review_events.ReviewLog(earlier, record.asker, 'Answer: ' + replied),
                    ))
                    update_registration.write_question_record(record)
                item = review_page.build_snapshot(self.root).records[key]
                _ = review_actions.later(item, until.isoformat(), '')
                record, = update_registration.selected_question_records([path], (), None)
                record.item['answered_at'] = datetime.now(user_zone()).isoformat()
                record.item.setdefault('followups', []).append(update_registration.FollowupExchange(
                    followup='Does the latest layout fit?', reply='The full layout fits.'))
                update_registration.write_question_record(record)
                answered = review_page.build_snapshot(self.root)
                self.assertIn(key, {item.key for item in review_items.read_open(self.root)})
                self.assertTrue(next(entry for entry in answered.entries if entry['key'] == key).get('unreadAnswer'))
                self.assertIn(b'The full layout fits.', answered.body(key))
                words = [event.words for event in answered.records[key].log]
                self.assertEqual(words.count('The full layout fits.'), 1)
                self.assertEqual(words.count('Does the latest layout fit?'), 1)
                body = self.card_body(answered, key, action, 'Keep this note.' if action == 'later' else '')
                if action == 'later':
                    body += '&'+urlencode({'later': (until+timedelta(days=1)).isoformat()})
                _ = review_page.submit(self.root, body, self.status, snapshot=answered)
                current = review_page.build_snapshot(self.root).records[key]
                self.assertFalse(current.unread_answer)
                self.assertTrue(current.hidden_until(datetime.now(user_zone())))
                record, = update_registration.selected_question_records([path], (), None)
                record.item['answered_at'] = datetime.now(user_zone()).isoformat()
                record.item.setdefault('followups', []).append(update_registration.FollowupExchange(
                    followup='Did the test keep the control visible?', reply='The control remains visible.'))
                update_registration.write_question_record(record)
                current = review_page.build_snapshot(self.root).records[key]
                self.assertTrue(current.unread_answer)
                if prior_logged_answer:
                    record, = update_registration.selected_question_records([path], (), None)
                    record.item['followup'] = update_registration.PendingFollowup(
                        text='Does the new layout fit?', sent=datetime.now(user_zone()).strftime('%Y-%m-%dT%H:%M'))
                    update_registration.record_followup_reply(record.item, 'The full layout fits.', record.asker)
                    update_registration.write_question_record(record)
                    current = review_page.build_snapshot(self.root).records[key]
                    self.assertTrue(current.unread_answer)
                    words = [event.words for event in current.log]
                    self.assertEqual(words.count('Answer: The full layout fits.'), 2)
                    self.assertEqual(words.count('The full layout fits.'), 1)

        class AfterLater(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> Self:
                return cls.fromtimestamp((until+timedelta(seconds=1)).timestamp(),tz)
        with patch.object(review_items,'datetime',AfterLater):
            for key in keys:
                item = next(item for item in review_items.read(self.root) if item.key == key)
                _,entries = review_items.next_queue(item.type,self.fixture.production,False,staff.snapshot())
                self.assertIn(item.queue_key,[entry.key for entry in entries])

        with self.subTest(finding='F026', case='unavailable production'):
            key = 'page-owner/C2'
            item = next(item for item in review_items.read(self.root) if item.key == key)
            assert isinstance(item.source, review_items.ChangeSource)
            assignment = replace(item.source.assignment, production=change_routing.UnavailableProduction(self.fixture.review_list))
            unavailable = replace(item, source=replace(item.source, assignment=assignment))
            before = review_later.path().read_bytes()
            body = self.body(key, 'later', 'Keep these words.')+'&'+urlencode({'later':until.isoformat()})
            with patch.object(review_items, 'read', return_value=[unavailable]), \
                    patch.object(change_review, 'record_review_log') as log:
                with self.assertRaisesRegex(question_record.AnswerRefused, '^the production document is not available$'):
                    _ = review_page.submit(self.root, body, self.status)
                log.assert_not_called()
            self.assertNotIn(key, review_later.times())
            self.assertEqual(review_later.path().read_bytes(), before)

        with self.subTest(finding='F026', case='log write failure'):
            key = 'page-owner/C3'
            item = next(item for item in review_items.read(self.root) if item.key == key)
            assert isinstance(item.source, review_items.ChangeSource)
            self.assertIsInstance(item.source.assignment.production, Path)
            before = review_later.path().read_bytes()
            listing = self.fixture.review_list.read_bytes()
            body = self.body(key, 'later', 'Keep these words after the failure.')+'&'+urlencode({'later':until.isoformat()})
            with patch.object(change_review, 'record_review_log', side_effect=OSError('fixture log write failed')) as log:
                with self.assertRaisesRegex(OSError, 'fixture log write failed'):
                    _ = review_page.submit(self.root, body, self.status)
                log.assert_called_once()
            self.assertEqual(review_later.path().read_bytes(), before)
            self.assertNotIn(key, review_later.times())
            self.assertEqual(self.fixture.review_list.read_bytes(), listing)
            visible = next(item for item in review_items.read(self.root) if item.key == key)
            self.assertTrue(visible.for_you(datetime.now(user_zone())))

        with self.subTest(finding='F026', case='unrouted owner with valid production'):
            key = 'page-owner/C4'
            item = next(item for item in review_items.read(self.root) if item.key == key)
            assert isinstance(item.source, review_items.ChangeSource)
            self.assertIsInstance(item.source.assignment.owner, change_routing.UnroutedChange)
            self.assertEqual(item.source.assignment.production, self.fixture.production)
            body = self.body(key, 'later', 'Read the unrouted change tomorrow.')+'&'+urlencode({'later':until.isoformat()})
            _, result = review_page.submit(self.root, body, self.status)
            self.assertIn('later until', result)
            saved = next(item for item in review_items.read(self.root) if item.key == key)
            self.assertTrue(any(entry.words == 'Later until '+until.isoformat()+': Read the unrouted change tomorrow.'
                                for entry in saved.log))
            self.assertEqual(review_later.times()[key], until)

        with self.subTest(value='delivered'):
            key = 'page-owner/C2'
            before = review_later.path().read_bytes()
            body = self.body(key, 'later')+'&'+urlencode({'later':'delivered'})
            with self.assertRaisesRegex(question_record.AnswerRefused, r'^Review Later requires a time\.$'):
                _ = review_page.submit(self.root, body, self.status)
            self.assertEqual(review_later.path().read_bytes(), before)

    def test_decision_cards_list_current_assignees_in_role_colors(self) -> None:
        proposal, efficiency = self.decisions()
        runner = fixture_member('showrunner', name='air', doc=staff.DocUnfilled())
        worker = fixture_member('worker', name='worker', parent=self.fixture.unit.id)
        wrapped = fixture_member('showrunner', name='wrapped',
                                 doc=staff.WrappedDoc(local_machine(), self.fixture.temporary / 'wrapped.md'))
        snapshot = review_page.build_snapshot(self.root)
        expected = [
            (runner.id, f' data-send-receiver="{runner.name}" style="color:#a379fd"', runner.name),
            (self.fixture.unit.id, f' data-send-receiver="{self.fixture.unit.name}" style="color:#ffaf02"',
             self.fixture.unit.name),
            (self.fixture.showrunner_id, ' data-send-receiver="page-owner" style="color:#a379fd"', 'page-owner'),
        ]
        for key in (proposal, efficiency):
            with self.subTest(key=key):
                body = snapshot.body(key).decode()
                self.assertIn('>Approve</button><label class="action-select">Assign', body)
                self.assertEqual(body.count('name="assignee"'), 1)
                options = body.partition('<select name="assignee">')[2].partition('</select>')[0]
                self.assertEqual(re.findall(r'<option value="([^"]*)"([^>]*)>([^<]+)</option>', options),
                                 [('', ' style="color:#a379fd"', 'page-owner decides'), *expected])
                for member in (worker, wrapped):
                    self.assertNotIn(member.id, options)

    def test_response_action_heading_colors_the_producer_name(self) -> None:
        doc = self.fixture.temporary / 'producer.md'
        _ = doc.write_text('# Producer\n')
        producer = fixture_member('producer', name='fixture-producer', doc=staff.LiveDoc(local_machine(), doc))
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        self.addCleanup(cache.close)
        created = cache.create_submission(self.submission_request(new_item_dialog.SubmissionKind.QUESTION, producer.id))
        with patch.object(the_list, 'file_exchanges', side_effect=OSError('The List save failed.')):
            self.assertTrue(review_submissions.answer(created.saved.key, 'The test passed.', created.saved.reply_token))
        snapshot = review_page.build_snapshot(self.root)
        self.assertIn(created.key, {row['key'] for row in snapshot.entries})
        card = snapshot.body(created.key).decode()
        self.assertRegex(card, r'Sends to <span[^>]*data-color-text="role.producer"'
                         + r' style="color:#90fe42">fixture-producer</span>')

    def test_explicit_approval_delivers_and_records_the_assignee(self) -> None:
        proposal, efficiency = self.decisions()
        path = Path(proposal.removeprefix('proposal/'))
        second_path = path.with_name('P2.md')
        _ = second_path.write_text(path.read_text().replace('P1', 'P2'))
        second_proposal = 'proposal/'+str(second_path)
        runner = fixture_member('showrunner', name='destination')
        unit = fixture_member('unit', name='implementation', parent=runner.id)
        for key, assignee in ((proposal, unit), (second_proposal, runner), (efficiency, runner)):
            with self.subTest(key=key):
                snapshot = review_page.build_snapshot(self.root)
                request = self.card_body(snapshot, key, 'approve')+'&'+urlencode({'assignee': assignee.id})
                with patch.object(proposal_issue, 'write_issue', wraps=proposal_issue.write_issue) as write:
                    delivery, answer = review_page.submit(self.root, request, self.status, snapshot=snapshot)
                expected = 'approved, assigned to '+assignee.name
                self.assertEqual(answer, expected)
                assert isinstance(delivery, change_page.MessageDelivery)
                self.assertEqual(delivery.recipients, (assignee.id,))
                saved = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(saved.state, review_items.ReviewState.DONE)
                self.assertEqual(saved.log[-1].words, expected)
                if key == proposal:
                    write.assert_called_once()
                    assert isinstance(saved.source, review_items.ProposalSource)
                    issue = saved.source.record
                    self.assertEqual(issue.value('unit'), unit.id)
                    self.assertEqual(issue.value('approved_in'), runner.id)
                    self.assertEqual(issue.value('approved_by'), 'user')
                    self.assertEqual([entry.words for entry in proposal_issue.review_log(issue)], [expected])
                    self.assertIn('The user approved the proposal and assigned it to you.', delivery.message)
                    self.assertIn('The approval is recorded.', delivery.message)
                    self.assertNotIn('proposal_issue.py approve', delivery.message)
                else:
                    self.assertIn('The user assigned this item to you.', delivery.message)
                    if key == second_proposal:
                        assert isinstance(saved.source, review_items.ProposalSource)
                        self.assertEqual(saved.source.record.value('unit'), '')
                        self.assertIn('proposal_issue.py approve', delivery.message)
                        self.assertIn(' --by user --unit <staff id>', delivery.message)
                    else:
                        self.assertIn('candidates.py outcome E1 accepted', delivery.message)
                self.fixture.messages.clear()
                change_page.deliver(delivery, self.status)
                self.assertEqual([target for target, _, _ in self.fixture.messages], [assignee.name])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_approve_sends_the_assignment_after_refresh(self) -> None:
        proposal, _ = self.decisions()
        source = browser_markup(self.root)
        setup = r"""<script>
window.refreshMarkup=MARKUP;
window.posts=[];
const nativeFetch=fetch;
window.fetch=async(url,options={})=>{
 if(options.method!=='POST') return {ok:true,text:async()=>window.refreshMarkup};
 if(url==='/results') return nativeFetch(url,options);
 if(String(url).endsWith('/undo')) return Response.json({});
 window.posts.push(new URLSearchParams(options.body));
 return Response.json({hold:'a'.repeat(24),seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
};
</script>""".replace('MARKUP', json.dumps(source).replace('</', '<\\/'))
        page = source.replace('<script>', setup+'<script>', 1)
        probe = r"""<script>
(async()=>{
 const failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 const row=()=>[...document.querySelectorAll('[data-list-item]')].find(node=>node.dataset.key===KEY);
 let revision=0;
 window.transformFixtureBody=(text,key)=>{
  if(key!==KEY) return text;
  const panel=document.createElement('template');
  panel.innerHTML=text;
  panel.content.querySelector('[name="revision"]').value='fixture-'+revision;
  if(revision===2 && __REMOVE_OPTION__){
   const option=[...panel.content.querySelectorAll('[name="assignee"] option')]
    .find(option=>option.value===ASSIGNEE);
   option.remove();
  }
  return panel.innerHTML;
 };
 try{
  row().querySelector('.list-toggle').click();
  const original=await readyReviewCard(row());
  const assignee=original.elements.assignee;
  check(assignee.value==='' && assignee.selectedOptions[0].textContent==='page-owner decides',
        'Assign starts with the card owner.');
  assignee.value=ASSIGNEE;
  assignee.dispatchEvent(new Event('change',{bubbles:true}));
  const refresh=async()=>{
   revision++;
   dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
   const end=performance.now()+8000;
   while(recordDetails(row())?.querySelector('[name="revision"]')?.value!=='fixture-'+revision){
    if(performance.now()>end) throw new Error('The refreshed form did not load the assignment.');
    await new Promise(resolve=>requestAnimationFrame(resolve));
   }
   return readyReviewCard(row());
  };
  const form=await refresh();
  check(form!==original && form.elements.assignee.value===ASSIGNEE,'Refresh keeps the assignment.');
  const name=form.elements.assignee.selectedOptions[0].textContent;
  const refreshed=await refresh();
  const unavailable=refreshed.elements.assignee.selectedOptions[0];
  check(refreshed!==form && unavailable.value===ASSIGNEE &&
        unavailable.textContent===(__REMOVE_OPTION__ ? name+' — not available' : name),
        'Refresh keeps the assignee selected.');
  const approve=refreshed.querySelector('[value="approve"]');
  refreshed.requestSubmit(approve);
  check(window.posts.length===1 && window.posts[0].get('outcome')==='approve' &&
        window.posts[0].get('assignee')===ASSIGNEE,'Approve sends the selected assignee ID.');
  const until=performance.now()+8000,host=window.top.document;
  while(!host.querySelector('[data-send-undo]')){
   if(performance.now()>until) throw new Error('The Approve toast did not show.');
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
  const toast=host.querySelector('[data-send-undo]');
  const expected=__REMOVE_OPTION__ ? 'Sending.' : 'Sent to '+name+'.';
  check(toast.querySelector('span').textContent===expected,'The toast uses the current selected assignee.');
  if(!__REMOVE_OPTION__){
   const receiver=approve.getAttribute('aria-label')?.replace(/^Approve, /,'');
   check(toast.querySelector('span').textContent==='Sent to '+receiver+'.',
         'The Approve button and toast name the same receiver.');
  }
  toast.querySelector('button').click();
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>""".replace('KEY', json.dumps(proposal)).replace('ASSIGNEE', json.dumps(self.fixture.unit.id))
        for removed in (False, True):
            with self.subTest(removed=removed):
                self.assertEqual(firefox_probe_results((page+probe.replace('__REMOVE_OPTION__', json.dumps(removed))).encode()), [[]])

    def test_approve_and_decline_close_proposals_and_efficiencies(self) -> None:
        _ = write_config_fixture(self.fixture.temporary / 'config/nightly_review.conf', 'state_folder=nightly-review\n')
        proposal, efficiency = self.decisions()
        folder = self.fixture.temporary / 'decision-proposals'
        ledger = self.fixture.temporary / 'decision-efficiency.md'
        _ = (folder / 'P2.md').write_text((folder / 'P1.md').read_text().replace('P1', 'P2'))
        _ = ledger.write_text(ledger.read_text()+'- 2026-11-02 tests: Slower checks — proposed\n')
        items = {item.id:item for item in review_items.read(self.root)}
        self.assertEqual(items['page-owner/P1'].key, proposal)
        self.assertEqual(items['page-owner/E1'].key, efficiency)
        body = review_page.build_snapshot(self.root).body(proposal).decode()
        self.assertIn('Run fewer checks.', body, 'The open card loads the issue text.')
        self.assertNotIn('proposal_id', body, 'The issue front matter stays off the card.')
        efficiency_body = review_page.build_snapshot(self.root).body(efficiency).decode()
        self.assertIn('<button data-send-receiver="" name="outcome" value="decline">Decline</button>', efficiency_body)
        report = self.legacy_investigation()
        report_body = review_page.build_snapshot(self.root).body(f'investigation/{report.owner}/{report.id}').decode()
        self.assertIn('<button data-send-receiver="" name="outcome" value="read"', report_body)
        commands = {proposal:'proposal_issue.py approve', efficiency:"candidates.py outcome E1 accepted --applied '<repo> <sha>'"}
        for key, command in commands.items():
            with self.subTest(key=key, action='approve'):
                request = self.body(key, 'approve', 'Use it.')+'&assignee='
                delivery, result = review_page.submit(self.root, request, self.status)
                self.assertEqual(result, 'approved, sent to page-owner')
                assert isinstance(delivery, change_page.MessageDelivery)
                self.assertIn('Use it.', delivery.message)
                self.assertIn(command, delivery.message)
                self.fixture.messages.clear()
                change_page.deliver(delivery, self.status)
                self.assertEqual([target for target, _, _ in self.fixture.messages], ['page-owner'])
                approved = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(approved.state, review_items.ReviewState.DONE, 'Approve makes the item Done at once.')
                self.assertEqual(approved.log[-1].words, 'Approve: Use it.')
                with self.assertRaisesRegex(question_record.AnswerRefused, 'choose an action'):
                    _ = review_page.submit(self.root, self.body(key, 'decline'), self.status)
        with self.subTest(action='decline', kind='proposal'):
            delivery, result = review_page.submit(self.root, self.body(items['page-owner/P2'].key, 'decline', 'Not now.'),
                                                  self.status)
            self.assertEqual(result, 'declined')
            assert isinstance(delivery, change_page.MessageDelivery)
            self.assertIn('/close_issue', delivery.message)
            self.assertIn('style_answer: "declined"', (folder / 'P2.md').read_text())
            declined = next(item for item in review_items.read(self.root) if item.id == 'page-owner/P2')
            self.assertEqual(declined.state, review_items.ReviewState.DONE)
        with self.subTest(action='decline', kind='efficiency'):
            delivery, result = review_page.submit(self.root, self.body(items['page-owner/E2'].key, 'decline', 'Too slow.'),
                                                  self.status)
            self.assertEqual(result, 'declined')
            self.assertIsInstance(delivery, change_page.NoMessageDelivery)
            self.assertRegex(ledger.read_text().splitlines()[1], ' — declined', 'Decline writes the ledger outcome at once.')
            declined = next(item for item in review_items.read(self.root) if item.id == 'page-owner/E2')
            self.assertEqual(declined.state, review_items.ReviewState.DONE)
            stored = efficiency_reviews.path(ledger).read_text()
            self.assertEqual(set(efficiency_reviews.read(ledger)), {'E1', 'E2'}, 'The review file is keyed by the E<n> id.')
            self.assertNotIn('Slower checks', stored, 'The review file never copies the ledger line.')

        with self.subTest(action='decline', kind='efficiency', summary='settings unavailable'):
            nightly_ledger = ledger.with_name('ledger.md')
            _ = nightly_ledger.write_text('- 2026-11-03 tests: Slow checks — proposed\n'
                                          +'- 2026-11-04 tests: Faster checks — proposed\n')
            doc = self.fixture.production
            _ = doc.write_text(doc.read_text().replace(str(ledger), str(nightly_ledger)))
            item = next(item for item in review_items.read(self.root) if item.id == 'page-owner/E1')
            with (patch.object(candidates, 'state_root', return_value=nightly_ledger.parent),
                  patch.object(candidates, 'table', side_effect=conf.Unusable('fixture title_characters unavailable')) as summary):
                delivery, result = review_page.submit(self.root, self.body(item.key, 'decline', 'Too slow.'), self.status)
            summary.assert_called_once()
            self.assertEqual(result, 'declined')
            self.assertIsInstance(delivery, change_page.NoMessageDelivery)
            self.assertTrue(nightly_ledger.read_text().splitlines()[0].endswith(' — declined'))
            declined = next(record for record in review_items.read(self.root) if record.key == item.key)
            self.assertEqual(declined.state, review_items.ReviewState.DONE)
            self.assertEqual(declined.log[-1].words, 'Decline: Too slow.')

    def test_question_answer_words_remain_after_the_answer_archive_expires(self) -> None:
        key = self.question()
        delivery,_ = review_page.submit(self.root,self.body(key,'answer','Keep these exact answer words.'),self.status)
        self.deliver_review_message(delivery,self.status)
        future = datetime.now(user_zone())+timedelta(days=3)
        _ = update_registration.answered_items(self.fixture.temporary/'outstanding.json',future)
        item = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(item.state,review_items.ReviewState.DONE)
        self.assertIn('Keep these exact answer words.',item.log[-1].words)

    def test_legacy_links_keep_filters(self) -> None:
        self.assertEqual(review_page.legacy_url('/console/changes.html', 'type=questions&owner=page-owner&show=Waiting&q=layout&sort=Age&dir=desc'),
            '/console/review.html?type=question&owner=page-owner&show=Waiting&q=layout&sort=Age&dir=desc')

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_legacy_investigations_bookmark_shows_bugs_and_investigations(self) -> None:
        _ = self.investigation(False)
        _ = investigation.file(self.fixture.unit.id, 'Check the build', 'Which step stopped?',
                               waiting_why='The owner checks the build.', kind=investigation.InvestigationKind.BUG)
        redirect = review_page.legacy_url('/console/investigations.html', 'show=all')
        page = browser_markup(self.root, show='all')
        setup = '<script>history.replaceState(null,"",'+json.dumps(redirect)+');</script>'
        probe = r"""<script>(async()=>{
 const failures=[];
 const mounted=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')].map(row=>row.dataset.type).sort().join(',');
 const combined=document.querySelector('[data-filter-row="type"] [data-filter-value="investigations"]');
 if(mounted()!=='bug,investigation') failures.push('The bookmark must show both report types.');
 if(combined?.hidden || combined?.getAttribute('aria-pressed')!=='true')
  failures.push('Select the combined filter on the legacy bookmark.');
 document.querySelector('[data-filter-row="type"] [data-filter-value="investigation"]').click();
 if(mounted()!=='investigation') failures.push('The Investigation filter must show only Investigations.');
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        markup = page.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>', 1)
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_legacy_bug_filter_link_selects_all_and_keeps_id_search(self) -> None:
        _ = self.investigation(False)
        _ = investigation.file(self.fixture.unit.id, 'Check the build', 'Which step stopped?',
                               waiting_why='The owner checks the build.', kind=investigation.InvestigationKind.BUG)
        page = browser_markup(self.root, show='all')
        probe = r"""<script>(async()=>{
 const failures=[];
 const type=document.querySelector('[data-filter-row="type"]');
 if(type.querySelector('[data-filter-value="bug"]')) failures.push('Review has no Bugs chip.');
 if(type.querySelector('[aria-pressed="true"]')?.dataset.filterValue!=='all')
  failures.push('The Bug link selects All types.');
 const visible=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')];
 const bug=visible().find(row=>row.dataset.type==='bug');
 if(!bug?.querySelector('.review-icon.bug')) failures.push('The legacy Bug row has its icon.');
 if(!visible().some(row=>row.dataset.type==='investigation')) failures.push('All includes Investigations.');
 if(bug){
  const search=document.querySelector('#list-search');
  search.value=bug.dataset.number;
  search.dispatchEvent(new Event('input',{bubbles:true}));
  await new Promise(resolve=>requestAnimationFrame(resolve));
  if(visible().length!==1 || visible()[0].dataset.key!==bug.dataset.key)
   failures.push('An ID search finds the legacy Bug row.');
 }
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        for address in ('?type=bug&show=all', '#type=bug&show=all'):
            with self.subTest(address=address):
                setup = '<script>history.replaceState(null,"",'+json.dumps(address)+');</script>'
                markup = page.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>', 1)
                self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    def new_item_filter_page(self, kind: new_item_dialog.SubmissionKind, *, matches_filters: bool = False) -> bytes:
        owner = self.ready_submission_recipient()
        _ = self.question('Keep this question')
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        before = cache.get()
        request = self.submission_request(kind, owner.id)
        if matches_filters:
            request = replace(request, words='Keep this question too.')
        created = cache.create_submission(request)
        fresh = cache.get()
        values: dict[str, object] = {
            'owner': owner.id, 'key': created.key, 'id': created.saved.id, 'kind': kind,
            'words': request.words, 'matches': matches_filters,
            'body': fresh.body(created.key).decode(), 'snapshot': cast(object, json.loads(fresh.index_response())),
        }
        receipt: dict[str, object] = {'id': created.saved.id, 'key': created.key, 'version': fresh.version}
        if kind in (new_item_dialog.SubmissionKind.INVESTIGATION, new_item_dialog.SubmissionKind.BUG):
            row = review_actions.submission_report_row(created.saved)
            assert isinstance(row, the_list.ListItem)
            assert isinstance(row.report, the_list.ListReport)
            self.assertEqual((row.report.kind, row.report.evidence_key, row.report.words), (
                the_list.ReportKind.BUG if kind == new_item_dialog.SubmissionKind.BUG
                else the_list.ReportKind.INVESTIGATION, created.key, request.words))
            assert not isinstance(row.owner, the_list.UnassignedOwner)
            receipt.update(list_number=row.number, list_owner=review_items.name(row.owner, created.members),
                           list_producer=review_items.name(row.producer, created.members))
        values['receipt'] = receipt
        setup = r'''<script>
const fixture=FIXTURE, nativeFetch=fetch;
let sent=false;
const selection={owner:fixture.owner,type:fixture.matches?fixture.kind:'question',
 show:fixture.matches?'all':'open',q:'keep this question'};
history.replaceState(null,'','#'+new URLSearchParams(selection));
window.fetch=async(url,options={})=>{
 const route=new URL(url,location.href);
 if(route.pathname==='/console/review/new-item'){
  if(options.method==='POST'){
   if(JSON.parse(options.body).page_capture) return Response.json({});
   sent=true;
   return Response.json(fixture.receipt);
  }
  return Response.json({token:'fixture',page_owner:fixture.owner,
   recipients:[{id:fixture.owner,name:'page-owner',proposal:true}]});
 }
 if(sent && route.searchParams.has('body')) return new Response(fixture.body,{
  headers:{'X-Review-Version':fixture.snapshot.version}
 });
 if(sent && (route.searchParams.has('index') || route.searchParams.has('version')))
  return Response.json(fixture.snapshot);
 return nativeFetch(url,options);
};
</script>'''.replace('FIXTURE', json.dumps(values).replace('</', '<\\/'))
        probe = r'''<script>(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const review=document.querySelector('[data-review]'), dialog=document.querySelector('#review-new-item');
 const params=()=>new URLSearchParams(location.hash.slice(1));
 try {
  const previous=history.length, previousHash=location.hash;
  check(params().get('owner')===fixture.owner, 'The original owner filter is active.');
  const recipients=dialog.querySelector('[data-recipients]');
  const ready=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{
    const recipient=recipients.querySelector('input');
    if(recipient){observer.disconnect();resolve(recipient);}
   });
   observer.observe(recipients,{childList:true});
  });
  document.querySelector('[data-owner-new]').click();
  if(!dialog.open) throw new Error('The owner button must open the dialog.');
  dialog.querySelector('[name="include_page"]').click();
  dialog.querySelector('[name="kind"][value="'+fixture.kind+'"]').click();
  (await ready).click();
  const words=dialog.querySelector('[name="words"]');
  words.value=fixture.words;
  words.dispatchEvent(new Event('input',{bubbles:true}));
  const refreshed=new Promise(resolve=>review.addEventListener('review-refreshed',resolve,{once:true}));
  const created=new Promise(resolve=>document.addEventListener('review-item-created',resolve,{once:true}));
  dialog.querySelector('[type="submit"]').click();
  const event=await created;
  check(!dialog.open && event.detail.id===fixture.id,'Send closes the dialog and reports the saved ID.');
  const receipt=fixture.receipt;
  if(receipt.list_number){
   const text='List item '+receipt.list_number+'. Owner '+receipt.list_owner+'. Producer '+receipt.list_producer+'.';
   check(window.top.document.querySelector('[data-report-receipt]')?.textContent===text,
         'Send shows the saved List number, owner, and producer.');
  }
  await refreshed;
  const row=review.querySelector('[data-key="'+CSS.escape(fixture.key)+'"]');
  check(Object.entries(selection).every(([key,value])=>params().get(key)===value),
        'Send keeps every filter in the address.');
  check(review.querySelector('[data-filter-row="show"] [data-filter-value][aria-pressed="true"]').dataset.filterValue===selection.show &&
   review.querySelector('[data-filter-row="type"] [data-filter-value][aria-pressed="true"]').dataset.filterValue===selection.type &&
   filterControl.ownerId(review.querySelector('[data-filter-row="owner"] [data-filter-value][aria-pressed="true"]'))===selection.owner &&
   review.querySelector('#list-search').value===selection.q,'Send keeps each selected control.');
  if(fixture.matches){
   check(row && recordIsOpen(row),'The new item opens when it matches the filters.');
   check(history.length===previous+1,'Opening the item adds one history entry.');
   if(history.length===previous+1){
    const restored=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
    history.back();
    await restored;
    check(location.hash===previousHash && !params().has('item') &&
     !recordIsOpen(review.querySelector('[data-key="'+CSS.escape(fixture.key)+'"]')),
     'Back restores the previous filters and closes the new item.');
   }
  }else{
   check(!row && history.length===previous,'Send keeps a hidden item outside the view.');
   const notice=review.querySelector('[data-review-notice]');
   if(receipt.list_number)
    check(notice.hidden,'Review leaves the List report outside its filters.');
   else
    check(notice.textContent==='The current filters hide '+fixture.id+'.',
          'Review explains why the saved item is hidden.');
  }
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        markup = page_frame.with_owner(before.render(), 'review', owner, staff.StaffSnapshot((owner,)),
                                       new_item_dialog.render)
        return markup.replace(b'<head>', b'<head>'+setup.encode(), 1).replace(b'</body>', probe.encode()+b'</body>', 1)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_new_items_keep_every_review_filter(self) -> None:
        for kind in new_item_dialog.SubmissionKind:
            with self.subTest(kind=kind):
                self.assertEqual(firefox_probe_results(self.new_item_filter_page(kind)), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_new_item_opens_when_it_matches_every_review_filter(self) -> None:
        page = self.new_item_filter_page(new_item_dialog.SubmissionKind.QUESTION, matches_filters=True)
        self.assertEqual(firefox_probe_results(page), [[]])

    def test_the_reader_counts_only_for_you_across_all_five_stores(self) -> None:
        _ = self.legacy_investigation()
        _ = self.investigation(False)
        proposals = self.fixture.temporary / 'issues'
        proposals.mkdir()
        _ = (proposals / 'proposal.md').write_text('---\nstatus: open\ntags: [proposal]\nstage: backlog\n'
            +f'proposed_by: {self.fixture.unit.id}\nproposal_id: P1\ndate_created: 2026-11-01\n---\n# Smaller checks\n')
        ledger = self.fixture.temporary / 'efficiency.md'
        _ = ledger.write_text('- 2026-11-01 tests: Faster checks — proposed\n'
            +'- 2026-11-02 tests: A deferred change — deferred\n')
        doc = self.fixture.production
        _ = doc.write_text(doc.read_text()+f'- **Proposals:** {proposals}\n- **Efficiency ledger:** {ledger}\n')
        _ = self.question()
        identities = update_registration.QuestionStaff.for_record(staff.snapshot())
        owner = next(row for row in identities.record.rows if row.name == 'page-owner')
        reminder = update_registration.outstanding_file(owner.id, identities=identities)
        reminder.parent.mkdir(parents=True, exist_ok=True)
        _ = reminder.write_text(json.dumps([{'since':'2026-11-01T12:00', 'text':'Remind the owner to check the gate'}]))
        items = review_items.read(self.root)
        self.assertFalse(any(item.title == 'Remind the owner to check the gate' for item in items), 'F011: reminders are not user questions')
        count, _ = review_items.question_counts(owner, user_zone(), identities, None)
        self.assertEqual(count, 1, 'F011: count the question without counting the reminder')
        self.assertEqual({item.type.value for item in items},
                         {'question', 'change', 'investigation', 'proposal', 'efficiency'})
        self.assertEqual(review_items.efficiency(doc),1)
        deferred = next(item for item in items if 'deferred change' in item.title)
        self.assertEqual(deferred.state,review_items.ReviewState.WAITING)
        self.assertEqual(review_items.proposals(doc),1)
        self.assertEqual(review_items.ready_investigations(self.fixture.unit.id),1)
        news = review_page.waiting_items(console.NewsRequest(self.root))
        with self.subTest(finding='F017'):
            self.assertTrue(any(key.startswith('proposal/') for key in news), 'F017: every shown type marks Review as new')
            self.assertTrue(any(key.startswith('efficiency/') for key in news), 'F017: every shown type marks Review as new')
        for fields in ('unit: implementer', 'stage: active', 'style_answer: declined'):
            with self.subTest(finding='F016', fields=fields):
                _ = (proposals / 'approved.md').write_text('---\nstatus: open\ntags: [proposal]\n'
                    +f'proposed_by: {self.fixture.unit.id}\nproposal_id: P2\ndate_created: 2026-11-01\n{fields}\n---\n')
                approved = next(item for item in review_items.read(self.root) if item.id == 'page-owner/P2')
                self.assertEqual(approved.state, review_items.ReviewState.DONE, 'F016: approval finishes the Review item')
                self.assertEqual(review_items.proposals(doc), 1)
                self.assertEqual(len(review_items.next_queue('proposal', doc, False, staff.snapshot())[1]), 1)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_title_fallback_checks_rendered_text_and_proposal_loads(self) -> None:
        key = self.question()
        question = next(item for item in review_items.read(self.root) if item.key == key)
        _ = self.investigation()
        _ = self.decisions()
        records = review_items.read(self.root)
        assert isinstance(question.source, review_items.QuestionSource)
        question_source = question.source

        def question_case(name: str, title: str, ask: str, context: str = '') -> review_items.ReviewItem:
            fields = question_source.record.item.copy()
            fields['text'] = title
            fields['parts'] = update_registration.QuestionParts(
                ask=ask, context=context, items=[], choices=[], recommend='')
            source = replace(question_source, record=replace(question_source.record, item=fields))
            return replace(question, key='question/'+name, title=title, source=source,
                           log=(replace(question.log[0], words=title),))

        equal = question_case('equal', 'W'*100, 'W'*100)
        code = question_case('code', 'rust', 'Read the code.')
        assert isinstance(code.source, review_items.QuestionSource)
        assert 'parts' in code.source.record.item
        code.source.record.item['parts']['items'] = ['```rust\nlet x = 1;\n```']
        formatted = question_case('formatted', 'Use wide layout', 'Use **wide** layout')
        investigation_item = next(item for item in records if isinstance(item.source, review_items.InvestigationSource))
        assert isinstance(investigation_item.source, review_items.InvestigationSource)
        investigation_source = investigation_item.source
        same_title = 'Why did the gate stop?'
        same = replace(investigation_item, key='investigation/equal', title=same_title,
                       source=replace(investigation_source, record=replace(investigation_source.record, question=same_title)))
        distinct = replace(investigation_item, key='investigation/distinct', title='Gate report')
        paragraphs = replace(investigation_item, key='investigation/paragraphs',
                             title='Which check failed? Which repair works?',
                             source=replace(investigation_source, record=replace(
                                 investigation_source.record, question='Which check failed?\n\nWhich repair works?')))
        spaced = replace(investigation_item, key='investigation/spaced', title='Run a b',
                         source=replace(investigation_source, record=replace(
                             investigation_source.record, question='Run ab')))
        change = next(item for item in records if isinstance(item.source, review_items.ChangeSource))
        assert isinstance(change.source, review_items.ChangeSource)
        image_title = 'Check the blue marker'
        image = replace(change, key='change/image', title=image_title,
                        source=replace(change.source, row={**change.source.row, 'Change': image_title, 'Image': 'fixture-image'}))
        proposal = next(item for item in records if isinstance(item.source, review_items.ProposalSource))
        proposals = [replace(proposal, key='proposal/'+name, title=title)
                     for name, title in (('with-title', 'P1'), ('without-title', 'Other proposal'),
                                         ('empty', 'Empty proposal'), ('failure', 'Failed proposal'))]
        cases = [equal, code, formatted, same, distinct, paragraphs, spaced, image, *proposals]
        with patch.object(review_items, 'read', return_value=cases), patch.object(
                change_review, 'image_path', return_value='/fixture-image.svg'):
            snapshot = review_page.build_snapshot(self.root)
        source = snapshot.render().decode()
        cards = {key:snapshot.body(key).decode() for key in snapshot.records}
        self.assertTrue(all('<section data-title-fallback hidden>' in card for card in cards.values()))
        setup = r'''<script>
const nativeFetch=fetch,nativeTimeout=setTimeout,cards=CARDS;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.bodyRequests=[];window.pendingBodies=new Map();
const version=()=>document.querySelector('[data-review]').dataset.version;
const response=text=>({ok:true,headers:new Headers({'X-Review-Version':version()}),text:async()=>text});
window.fetch=async(url)=>{
 const key=new URL(url,location.href).searchParams.get('body');
 if(!key) return {ok:true,json:async()=>({version:version()})};
 window.bodyRequests.push(key);
 if(!key.startsWith('proposal/')) return response(cards[key]);
 return new Promise(resolve=>window.pendingBodies.set(key,body=>{
  if(body===null){resolve({ok:false,status:503,headers:new Headers(),text:async()=>''});return;}
  const panel=document.createElement('div');panel.innerHTML=cards[key];
  panel.querySelector('[data-proposal-body]').innerHTML=body;
  resolve(response(panel.innerHTML));
 }));
};
</script>'''.replace('CARDS', json.dumps(cards).replace('</', '<\\/'))
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>nativeTimeout(resolve,80));
 const row=key=>[...document.querySelectorAll('[data-list-item]')].find(row=>row.dataset.key===key);
 const panel=key=>recordDetails(row(key)).querySelector('.review-panel');
 const fallback=key=>panel(key).querySelector('[data-title-fallback]');
 const open=key=>{const button=row(key).querySelector('.list-toggle');if(button.getAttribute('aria-expanded')!=='true') button.click();};
 const visibleText=key=>{
  const copy=panel(key).cloneNode(true);
  copy.querySelectorAll('[hidden],.list-log,form,.review-source').forEach(node=>node.remove());
  return copy.textContent.replace(/\s+/g,' ').trim();
 };
 const refresh=async()=>{
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();await flush();await flush();
 };
 try{
  await flush();
  for(const [key,hidden,title] of [
   ['question/equal',true,'W'.repeat(100)],['question/code',false,'rust'],
   ['question/formatted',true,'Use wide layout'],
   ['investigation/equal',true,'Why did the gate stop?'],['investigation/distinct',false,'Gate report'],
   ['change/image',false,'Check the blue marker']]){
   open(key);await flush();
   check(fallback(key).hidden===hidden,key+': fallback follows rendered text');
   check(visibleText(key).split(title).length-1===1,key+': one visible title');
  }
  check(panel('question/code').querySelector('code').textContent.includes('let x = 1;'),'the code block renders');
  check(row('question/equal').querySelector('.review-title').textContent==='W'.repeat(100),'the title limit preserves a full title');
  check(panel('question/formatted').querySelector('strong').textContent==='wide','the ask formatting renders');
  check(panel('change/image').querySelector('img').alt.includes('Check the blue marker'),'the image alt contains the title');
  open('investigation/paragraphs');await flush();
  const paragraphs=[...panel('investigation/paragraphs').querySelectorAll('[data-review-body] p')];
  check(paragraphs.some(p=>p.textContent==='Which check failed?'),'the first question paragraph renders');
  check(paragraphs.some(p=>p.textContent==='Which repair works?'),'the second question paragraph renders');
  check(fallback('investigation/paragraphs').hidden,'paragraph boundaries preserve the title');
  open('investigation/spaced');await flush();
  check([...panel('investigation/spaced').querySelectorAll('[data-review-body] p')]
   .some(p=>p.textContent==='Run ab'),'the body keeps the joined word');
  check(!fallback('investigation/spaced').hidden,'different word spacing keeps the fallback visible');
  const outcomes=[
   ['proposal/with-title','<h2>P1</h2><p>Use fewer checks.</p>',true],
   ['proposal/without-title','<p>Use fewer checks.</p>',false],
   ['proposal/empty','',false],['proposal/failure',null,false]];
  for(const [key,body,hidden] of outcomes){
   open(key);
   check(recordDetails(row(key)).textContent.includes('Loading the item'),key+': loading text');
   check(row(key).querySelector('.review-title').textContent.length>0,key+': the title remains visible while loading');
   const resolve=window.pendingBodies.get(key);
   check(typeof resolve==='function',key+': a body request starts');
   resolve(body);
   await flush();await flush();
   if(body===null) check(document.querySelector('[data-record-error]:not([hidden])')?.textContent.includes('503'),
    key+': the error appears outside the body');
   else{
    check(fallback(key).hidden===hidden,key+': fallback follows the loaded body');
    check(visibleText(key).split(row(key).querySelector('.review-title').textContent).length-1===1,key+': one title after loading');
   }
  }
  for(const [key,body,hidden] of outcomes){
   open('question/equal');
   const count=()=>window.bodyRequests.filter(requested=>requested===key).length;
   const before=count();
   await refresh();open(key);await flush();
   if(body===null){
    check(count()===before+1,key+': refresh retries the failed load');
    check(row(key).querySelector('.review-title').textContent.length>0,key+': the title shows during the retry');
    window.pendingBodies.get(key)(null);await flush();
   }else{
    check(count()===before,key+': refresh uses the cached body');
    check(panel(key).querySelector('[data-proposal-body]').innerHTML===body,key+': cache replaces the loading text');
   }
   if(body!==null) check(fallback(key).hidden===hidden,key+': fallback follows the refreshed body');
  }
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        body = source.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>').encode()
        results: list[list[str]] = []
        complete = threading.Event()

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                if self.path == '/complete':
                    _ = complete.wait(20)
                elif self.path == '/fixture-image.svg':
                    _ = self.wfile.write(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
                else:
                    _ = self.wfile.write(body if self.path == '/page' else
                        b'<iframe src="/page" style="width:100vw;height:100vh"></iframe><img hidden src="/complete">')

            def do_POST(self) -> None:
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
                with tempfile.TemporaryDirectory(dir=self.fixture.temporary) as profile:
                    done = run_process_group(['firefox', '--headless', '--no-remote', '--profile', profile,
                        '--window-size', '390,900', '--screenshot', str(self.fixture.temporary/'review-titles.png'),
                        f'http://127.0.0.1:{server.server_port}/'], timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)
        self.assertEqual(results, [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_owner_button_is_the_only_title_action(self) -> None:
        owner = replace(self.ready_submission_recipient(), name='Review and console delivery')
        markup = page_frame.with_owner(review_page.build_snapshot(self.root).page, 'review', owner,
                                       staff.StaffSnapshot((owner,)), new_item_dialog.render)
        probe = b'''<script>
(async()=>{
 const failures=[];
 try{
  const title=document.querySelector('h1.page-title'), row=title.closest('.page-heading');
  const actions=document.querySelector('[data-page-actions]');
  const name=document.querySelector('.page-owner-name'), owner=document.querySelector('[data-owner-new]');
  const bounds=element=>element.getBoundingClientRect(), center=box=>(box.top+box.bottom)/2;
  await document.fonts.ready;
  for(const width of [1280,390,320]){
   frameElement.style.width=width+'px';
   await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
   const t=bounds(title), n=bounds(name), o=bounds(owner), r=bounds(row);
   const beside=Math.abs(center(t)-center(o))<=1;
   const atRight=Math.abs(o.right-r.right)<=1;
   if(innerWidth!==width || row.querySelector('button') || actions.querySelectorAll('button').length!==1 ||
      !actions.contains(owner) ||
      Math.abs(center(n)-center(o))>1 || o.left<n.right+4 ||
      n.left<r.left-1 || !atRight ||
      (beside ? n.left<t.right+4 : width===1280 || o.bottom+4>t.top)){
    failures.push('The owner button is the only title action and fits at '+width+' px.');
   }
   if(width===320 && Math.abs(t.height-parseFloat(getComputedStyle(title).lineHeight))>1){
    failures.push('The Review title fits on one line at 320 px.');
   }

  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(markup.replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_clears_the_review_save_notice(self) -> None:
        fixture = self.fixture
        _ = change_review.append_rows(fixture.review_list, pictured_changes([
            change_review.Visible(f'Change {number}', 'Read the diff', 'Check the result')
            for number in range(4, 9)
        ]), datetime.now(user_zone()).date(), '', fixture.production, record=staff.snapshot())
        self.assertEqual(save_notice_failures(browser_markup(self.root).encode(), 'works'), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_card_actions_fade_and_slide(self) -> None:
        source = browser_markup(self.root)
        setup = r"""<script>
const realFetch=fetch,posts=[];
window.setInterval=()=>0;
window.fetch=(url,options)=>options?.method==='POST'
 ? (posts.push(new URLSearchParams(options.body).get('outcome')),new Promise(()=>{}))
 : realFetch(url,options);
if(REDUCED) window.matchMedia=()=>({matches:true});
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const top=row=>row.getBoundingClientRect().top;
 try{
  frameElement.style.width='1280px';frameElement.style.height='820px';
  const [row,next]=document.querySelectorAll('[data-list-item][data-type="change"]');
  row.querySelector('.list-toggle').click();
  const form=await readyReviewCard(row);
  const first=top(row),start=top(next);
  const action=ACTION;
  form.elements.feedback.value='Review these words.';
  if(action==='later'){
   form.querySelector('[data-later-toggle]').click();
   form.querySelector('[data-later-preset="soon"]').click();
  }else form.requestSubmit(form.querySelector('button[value="'+action+'"]'));
  if(action==='question' || action==='feedback'){
   check(posts[0]===action && row.isConnected && recordIsOpen(row),'The action posts and keeps the row open for the reply.');
   await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
   return;
  }
  check(posts[0]===action && !row.isConnected,'The action posts and removes the record.');
  const motion=()=>document.querySelector('[data-record-motion]');
  if(REDUCED){
   check(!motion() && Math.abs(top(next)-first)<2,'Reduced motion moves the next card at once.');
  }else{
   const clip=motion()?.querySelector('.record-motion-clip'),exit=clip?.getAnimations()[0];
   if(!exit) throw new Error('The shared exit animation is missing.');
   check(motion().querySelector('[data-detail]:not([hidden])'),'The expanded row fades with its summary.');
   check(exit.effect.getTiming().duration===Number(document.querySelector('[data-review]').dataset.cardFadeOutMs),
    'The card uses the configured fade time.');
   exit.pause();exit.currentTime=0;
   const initialOpacity=Number(getComputedStyle(clip).opacity);
   check(Math.abs(top(next)-start)<2,'The next card starts in its original position.');
   exit.currentTime=exit.effect.getTiming().duration/2;
   const opacity=Number(getComputedStyle(clip).opacity);
   check(opacity>0 && opacity<initialOpacity,'The motion copy fades before it disappears.');
   check(top(next)<start && top(next)>first,'The next card moves up during the fade.');
   const submitter=form.querySelector('button[value="'+action+'"]');
   form.dispatchEvent(new SubmitEvent('submit',{cancelable:true,submitter}));
   check(posts.length===1,'A second submit during the fade sends no POST.');
   exit.finish();await exit.finished;
   check(!motion() && Math.abs(top(next)-first)<2,'The next card fills the gap when the copy leaves.');
  }
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        cases = [(action, False) for action in ('works', 'not works', 'validate', 'question', 'feedback', 'later')]
        for action, reduced in [*cases, ('later', True)]:
            with self.subTest(action=action, reduced_motion=reduced):
                markup = source.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>')
                markup = markup.replace('REDUCED', json.dumps(reduced)).replace('ACTION', json.dumps(action))
                self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_departing_card_keeps_its_place_in_the_scrolled_window(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        records = [replace(change, key=f'change-{number}', id=f'C{number}') for number in range(1, 181)]
        with patch.object(review_items, 'read', return_value=records):
            source = browser_markup(self.root)
        self.check_card_motion(source, r"""
 document.querySelector('[data-sort="0"]').click();
 scrollTo(0,4800);await frame();
 const row=rows().find(node=>top(node)>120 && top(node)<420);
 if(!row) throw new Error('A card must be in the viewport.');
 const panel=motionKey(row),next=row.nextElementSibling;
 const before=top(row);
 row.querySelector('.list-toggle').click();
 const form=await readyReviewCard(row);
 await frame();
 const following=top(next);
 check(near(top(row),before) && following>before+recordDetails(row).getBoundingClientRect().height,
  'The row opens below its summary and leaves the summary in place.');
 send(form);
 const clip=copy(panel);
 check(clip && near(top(clip),before),'The summary copy starts at the original position.');
 check(next.isConnected && near(top(next),following),'The visible next card keeps its position.');
 const exit=clip.getAnimations()[0];
 halfway(exit);dispatchEvent(new Event('scroll'));await frame();
 check(near(top(clip),before),'The shrinking copy keeps its place after a redraw.');
 check(next.isConnected && top(next)<following,'The next card stays mounted while it moves up.');
 const saveTop=scrollY,height=clip.getBoundingClientRect().height,key=row.dataset.key;
 let redraws=0;
 const observer=new MutationObserver(records=>redraws+=records.length);
 observer.observe(document.querySelector('[data-filter-row="owner"] [data-filter-value="all"] b'),{childList:true});
 scrollBy(0,innerHeight+50);await frame();
 check(scrollY-saveTop>=innerHeight,'The page scrolls one viewport during the save.');
 check(copy(panel)===clip && near(clip.getBoundingClientRect().height,height),
  'A scroll during the save keeps the same fade at the same height.');
 scrollTo(0,saveTop);await frame();
 check(redraws===0 && !rows().some(row=>row.dataset.key===key),
  'The pending card stays out of the window during the fade.');
 exit.finish();await exit.finished;await frame();
 check(!copy(panel),'The summary copy disappears when the fade ends.');
 check(redraws===1,'The deferred redraw runs once after the fade.');
 observer.disconnect();
""")

    def last_owner_card_markup(self) -> str:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        other = fixture_member('showrunner', name='builds')
        origin = change.source
        assert isinstance(origin, review_items.ChangeSource)
        change = replace(change, source=replace(origin, row={**origin.row, 'Image': ''}),
                         log=tuple(replace(entry, who='page-owner') for entry in change.log))
        records = [
            replace(change, source_unit=change.owner, title='Review the console layout'),
            replace(change, key='builds/C1', id='builds/C1', owner=other.id, source_unit=other.id, title='Check the build'),
        ]
        with patch.object(review_items, 'read', return_value=records):
            return browser_markup(self.root)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_later_keeps_the_last_owner_through_the_fade(self) -> None:
        source = self.last_owner_card_markup()
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.check_card_motion(source, r"""
 const owner=document.querySelector('[data-owner-name="page-owner"]');
 const selected=()=>document.querySelector('[data-filter-row="owner"] [data-filter-value][aria-pressed="true"]');
 document.querySelector('[data-sort="3"]').click();
 owner.click();
 const [row]=rows(),panel=motionKey(row);
 const form=await openCard(row);
 form.querySelector('[data-later-toggle]').click();
 form.querySelector('[data-later-preset="soon"]').click();
 const clip=copy(panel),exit=clip.getAnimations()[0];
 halfway(exit);
 const position=top(clip);
 posts.get(row.dataset.key)({headers:new Headers(),ok:true,json:async()=>({answer:'later',state:'Open',later:Date.now()+3600000})});
 await frame();
 check(selected()===owner && !owner.hidden,'The last owner stays selected and visible during the fade.');
 check(copy(panel)===clip && near(top(clip),position) && !rows().length,
  'The fading card keeps its position and the other owner’s card stays out of view.');
 if(CANCEL) document.querySelector('[data-sort="3"]').click();
 else exit.finish();
 await frame();
 check(!copy(panel) && selected()===owner && !owner.hidden,
  'The Owner filter stays selected after the fade.');
 check(!rows().length && posts.size===1,'Later hides the last card in this view.');
 check(postCount===1,'Later sends exactly one POST.');
""".replace('CANCEL', json.dumps(cancel)))

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_card_submits_keep_every_filter(self) -> None:
        _ = self.question('Keep this question')
        source = browser_markup(self.root)
        for kind, outcome, state in (
            ('question', 'answer', 'Done'),
            ('change', 'works', 'Done'),
            ('change', 'not works', 'Done'),
            ('change', 'feedback', 'Validating'),
            ('change', 'question', 'Validating'),
            ('change', 'validate', 'Validating'),
        ):
            with self.subTest(outcome=outcome):
                settings = json.dumps({'kind': kind, 'outcome': outcome, 'state': state})
                self.check_card_motion(source, 'const action='+settings+r""";
 const initial=rows().find(row=>row.dataset.type===action.kind);
 const filters={type:action.kind,show:'open',owner:initial.dataset.owner,q:initial.dataset.number};
 history.replaceState(null,'','#'+new URLSearchParams(filters));
 dispatchEvent(new HashChangeEvent('hashchange'));
 const row=rows().find(row=>row.dataset.key===initial.dataset.key);
 const form=await openCard(row);
 form.elements.feedback.value='Save these words.';
 form.requestSubmit(form.querySelector('[value="'+action.outcome+'"]'));
 const clip=copy(motionKey(row));
 const motion=clip?.getAnimations()[0];
 const staysOpen=['feedback','question'].includes(action.outcome);
 if(!posts.has(row.dataset.key) || (staysOpen ? !recordIsOpen(row) : !motion))
  throw new Error('The save did not start with the expected row state.');
 posts.get(row.dataset.key)({headers:new Headers(),ok:true,json:async()=>({answer:'saved',state:action.state,later:0})});
 motion?.finish();await frame();
 if(action.state==='Done') check(!rows().some(item=>item.dataset.key===row.dataset.key),
  'The completed item leaves Review.');
 const place=new URLSearchParams(location.hash.slice(1));
 check(Object.entries(filters).every(([key,value])=>place.get(key)===value),
  'The card submit keeps every filter in the address.');
 check(document.querySelector('[data-filter-row="type"] [data-filter-value][aria-pressed="true"]').dataset.filterValue===filters.type &&
  document.querySelector('[data-filter-row="show"] [data-filter-value][aria-pressed="true"]').dataset.filterValue===filters.show &&
  filterControl.ownerId(document.querySelector('[data-filter-row="owner"] [data-filter-value][aria-pressed="true"]'))===filters.owner &&
  document.querySelector('#list-search').value===filters.q,'The card submit keeps each selected control.');
""")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_undo_restores_later_and_validate_cards(self) -> None:
        probe = '<script>' + UNDO_ACTION_SCRIPT + r"""
(async()=>{
 const {realFetch,posts,until,undo}=fixtureUndo();
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 try{
  frameElement.style.width='1280px';frameElement.style.height='820px';
  const visible=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')];
  const key=visible().find(item=>item.dataset.type==='change').dataset.key;
  const row=()=>visible().find(item=>item.dataset.key===key);
  row().querySelector('.list-toggle').click();
  const initial=()=>visible().map(item=>item.dataset.key).join(',');
  const order=initial(),state=row().dataset.state;
  for(const action of ['later','validate']){
   const form=await readyReviewCard(row());
   form.elements.feedback.value='Keep these words.';
   if(action==='later'){
    form.querySelector('[data-later-toggle]').click();
    form.querySelector('[data-later-preset="soon"]').click();
   }else form.querySelector('[value="validate"]').click();
   check(!row(),action+' hides the card at once.');
   await undo();
   await until(()=>row() && recordIsOpen(row()) &&
    recordDetails(row()).querySelector('[value="validate"]:enabled'),action+' did not restore the open card.');
   check(initial()===order && row().dataset.state===state,action+' restores the card order and state.');
   check((await readyReviewCard(row())).elements.feedback.value==='Keep these words.',action+' restores the words.');
   await until(()=>!document.querySelector('[data-record-motion]'),'The return animation did not finish.');
  }
  check(posts.map(post=>post.get('outcome')).join(',')==='later,validate','Each action creates one hold.');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        markup = browser_markup(self.root).replace('</body>', probe+'</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_scroll_keeps_held_later_and_validate_cards_hidden(self) -> None:
        _ = write_config_fixture(self.fixture.temporary / "config/review_links.conf", "undo_seconds=30\n")
        probe = '<script>' + UNDO_ACTION_SCRIPT + r"""
(async()=>{
 const {realFetch,posts,until,undo}=fixtureUndo(),failures=[];
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 try{
  frameElement.style.width='1280px';frameElement.style.height='820px';
  const spacer=document.createElement('div');spacer.style.height='400vh';document.body.append(spacer);
  const key=document.querySelector('[data-list-item][data-type="change"]').dataset.key;
  const row=()=>document.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]:not([hidden])');
  row().querySelector('.list-toggle').click();
  for(const action of ['later','validate']){
   const form=await readyReviewCard(row());
   if(action==='later'){
    form.querySelector('[data-later-toggle]').click();
    form.querySelector('[data-later-preset="soon"]').click();
   }else form.querySelector('[value=validate]').click();
   const top=scrollY;
   await until(()=>window.top.document.querySelector('[data-send-undo] button'),'Undo did not show.');
   scrollTo(0,top+innerHeight*2);await frame();
   if(scrollY-top<innerHeight) failures.push('The page did not scroll far enough.');
   await until(()=>!document.querySelector('[data-record-motion]'),'The departure animation did not finish.');
   scrollTo(0,top);await frame();
   if(row()) failures.push(action+' must stay hidden while held.');
   await undo();
   await until(()=>row() && recordIsOpen(row()),'Undo did not restore the card.');
   await until(()=>!document.querySelector('[data-record-motion]'),'The return animation did not finish.');
  }
  if(posts.map(post=>post.get('outcome')).join()!=='later,validate') failures.push('Each action creates one hold.');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        markup = browser_markup(self.root).replace('</body>', probe+'</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_undo_keeps_the_card_words_and_a_second_send_posts_again(self) -> None:
        keeper = fixture_member('producer', name='morty')
        producer = self.fixture.unit
        key = 'page-owner/C1'
        item = the_list.add(keeper.id, 'Check the changed layout', owner=self.fixture.showrunner_id,
                            source='nate', changes={'producer': producer.id})
        linked = the_list.link(keeper.id, item.id, (key,), source='nate')
        self.assertNotEqual(linked.producer, keeper.id)
        setup = r"""<script>
const realFetch=fetch,posts=[];
window.setInterval=()=>0;
window.fetch=async(url,options)=>{
 if(options?.method!=='POST') return realFetch(url,options);
 if(String(url).endsWith('/undo')) return Response.json({outcome:'undone'});
 posts.push(new URLSearchParams(options.body));
 if(posts.length===1)
  return Response.json({hold:'a'.repeat(24),seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
 return Response.json({answer:'works',state:'Done',later:0});
};
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const host=window.top.document,words='Keep these words.';
 let sentTo='';
 const until=async(test,label)=>{
  const end=performance.now()+8000;
  while(!test()){
   if(performance.now()>end) throw new Error(label);
   await new Promise(resolve=>setTimeout(resolve,20));
  }
 };
 try{
  frameElement.style.width='1280px';frameElement.style.height='820px';
  const key=document.querySelector('[data-list-item][data-type="change"]').dataset.key;
  const row=()=>document.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');
  row().querySelector('.list-toggle').click();
  let form=await readyReviewCard(row());
  const version=document.querySelector('[data-review]').dataset.version;
  form.elements.feedback.value=words;
  const works=form.querySelector('[value="works"]');
  form.requestSubmit(works);
  await until(()=>host.querySelector('[data-send-undo] button'),'The Undo toast did not show.');
  sentTo=host.querySelector('[data-send-undo] span').textContent;
  check(sentTo==='Sent to page-owner and builder-unit.',
        'The toast names every receiver of the verdict with words.');
  const receiver=works.getAttribute('aria-label')?.replace(/^Works, /,'');
  check(host.querySelector('[data-send-undo] span').textContent==='Sent to '+receiver+'.',
        'The Works button and toast name the same receivers.');
  check(!row()?.getClientRects().length,'The verdict card is gone when the Undo toast shows.');
  host.querySelector('[data-send-undo] button').click();
  const cardForm=()=>row()?.isConnected && !row().hidden ? recordDetails(row())?.querySelector('[data-review-form]') : null;
  const undone=()=>cardForm()?.querySelector('[data-send-status]').textContent==='Undone. Edit and send again.';
  await until(()=>undone() || (row()?.isConnected && !row().hidden && !cardForm()),'Undo did not return the item.');
  if(!cardForm()) row().querySelector('.list-toggle').click();
  await until(undone,'The card did not say that the send was undone.');
  form=cardForm();
  check(form.elements.feedback.value===words,'After Undo the card keeps the words.');
  await until(()=>row()?.dataset.bodyVersion && row().dataset.bodyVersion!==version,
   'The restored card did not receive the new snapshot.');
  check(cardForm()===form,'A refresh with the same body keeps the restored form.');
  await until(()=>document.activeElement===form.elements.feedback,'Undo did not focus the feedback field.');
  await until(()=>!form.querySelector('[value="works"]').disabled,'The card did not enable Send after Undo.');
  // The row comes back with a short fade, and a Send during that fade does nothing, so Send again until it posts.
  let next=0;
  await until(()=>{
   if(posts.length<2 && performance.now()>next){
    next=performance.now()+250;cardForm()?.requestSubmit(cardForm().querySelector('[value="works"]'));
   }
   return posts.length===2;
  },'The second Send did not post.');
  check(posts.every(post=>post.get('outcome')==='works' && post.get('feedback')===words),
   'The second Send posts the same words again.');
 }catch(error){failures.push(String(error));}
 failures.push(JSON.stringify([sentTo,posts.at(-1)?.toString() || '']));
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        markup = browser_markup(self.root).replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>')
        results, = firefox_probe_results(markup.encode())
        self.assertEqual(results[:-1], [])
        toast, body = cast(tuple[str, str], json.loads(results[-1]))
        snapshot = review_page.build_snapshot(self.root)
        delivery, _ = review_page.submit(self.root, body, self.status, snapshot=snapshot)
        self.deliver_review_message(delivery, self.status)
        receivers = [recipient for recipient, _, message in self.fixture.messages if 'Keep these words.' in message]
        self.assertIn(producer.name, receivers)
        self.assertCountEqual(receivers, toast.removeprefix('Sent to ').removesuffix('.').split(' and '))

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_held_review_question_and_feedback_hide_the_row(self) -> None:
        probe = '<script>' + UNDO_ACTION_SCRIPT + r"""
(async()=>{
 const {realFetch,posts,until}=fixtureUndo(),failures=[];
 const check=(ok,label)=>{if(!ok) failures.push(label);};
 const heldFetch=window.fetch,host=window.top.document;
 window.fetch=(url,options)=>String(url).startsWith('/send-hold/') && options?.method!=='POST'
  ? Promise.resolve(Response.json({reason:'The Review send failed.'},{status:503})) : heldFetch(url,options);
 try{
  frameElement.style.width='1280px';frameElement.style.height='820px';
  const key=document.querySelector('[data-list-item][data-type="change"]').dataset.key;
  const rows=()=>[...document.querySelectorAll('[data-list-item]')].filter(item=>item.getClientRects().length);
  const row=()=>rows().find(item=>item.dataset.key===key);
  const before=rows().map(item=>item.dataset.key).join();
  row().querySelector('.list-toggle').click();
  const words='Keep the Review draft.';
  for(const outcome of ['question','feedback']){
   const form=await readyReviewCard(row());
   form.elements.feedback.value=words;
   form.requestSubmit(form.querySelector('[value="'+outcome+'"]'));
   const toast=()=>host.querySelector('[data-send-undo]:has(button)');
   await until(toast,'The Undo toast did not show.');
   const motion=document.querySelector('[data-record-motion-key="'+CSS.escape(key)+'"]');
   check(!row() && !motion?.getClientRects().length,'The row and its animation leave when the toast appears.');
   if(outcome==='question') toast().querySelector('button').click();
   else toast().getAnimations()[0].finish();
   const message=outcome==='question' ? 'Undone. Edit and send again.' : 'Not saved: The Review send failed.';
   const restored=()=>row() && recordDetails(row()).querySelector('[data-review-form]');
   await until(()=>restored()?.querySelector('[data-send-status]').textContent===message,'The card did not report the result.');
   if(outcome==='question')
    await until(()=>document.activeElement===restored()?.elements.feedback,'Undo did not focus the Review draft.');
   check(recordIsOpen(row()) && restored().elements.feedback.value===words &&
    rows().map(item=>item.dataset.key).join()===before,'Undo and failure restore the open row, its order and its draft.');
  }
  check(posts.map(post=>post.get('outcome')).join()==='question,feedback','Each Review send creates one hold.');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        markup = browser_markup(self.root).replace('</body>', probe+'</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_refused_card_reverses_above_the_next_departing_card(self) -> None:
        self.check_card_motion(browser_markup(self.root), r"""
 const [first,second,next]=rows(),firstPanel=motionKey(first),secondPanel=motionKey(second);
 const firstForm=await openCard(first);send(firstForm);
 const firstClip=copy(firstPanel);halfway(firstClip.getAnimations()[0]);
 const secondForm=await openCard(second);send(secondForm);
 const secondClip=copy(secondPanel);halfway(secondClip.getAnimations()[0]);
 const height=firstClip.getBoundingClientRect().height,opacity=Number(getComputedStyle(firstClip).opacity);
 const following=top(next);
 posts.get(first.dataset.key)({headers:new Headers(),ok:false,json:async()=>({reason:'Try again.'})});
 await frame();
 const restored=copy(firstPanel);
 check(restored && restored!==firstClip,'The refused card reverses its fade.');
 check(restored?.getAnimations()[0]?.effect.getTiming().duration===
   Number(document.querySelector('[data-review]').dataset.cardFadeInMs),'The return uses the configured duration.');
 check(restored && top(restored)<top(secondClip),'The first card returns above the departing second card.');
 check(restored && near(restored.getBoundingClientRect().height,height) &&
   near(Number(getComputedStyle(restored).opacity),opacity,0.01),'The reverse starts at the current height and opacity.');
 check(near(top(next),following),'The next card does not jump when the fade reverses.');
""")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_link_to_a_returning_card_waits_for_both_fades(self) -> None:
        self.check_card_motion(browser_markup(self.root), r"""
 const [first,second]=rows(),firstPanel=motionKey(first),secondPanel=motionKey(second);
 const firstKey=first.dataset.key;
 send(await openCard(first));
 const firstExit=copy(firstPanel).getAnimations()[0];
 halfway(firstExit);
 send(await openCard(second));
 const secondClip=copy(secondPanel),secondExit=secondClip.getAnimations()[0];
 halfway(secondExit);
 posts.get(firstKey)({headers:new Headers(),ok:false,json:async()=>({reason:'Try again.'})});
 await frame();
 const returning=copy(firstPanel),returnFade=returning.getAnimations()[0];
 check(returnFade!==firstExit,'The refused card starts its return fade.');
 const jumped=()=>rows().find(row=>row.dataset.key===firstKey)?.querySelector('[data-record-highlight]');
 const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
 location.hash=new URLSearchParams({jump:firstKey});
 await changed;await frame();
 check(copy(firstPanel)===returning && copy(secondPanel)===secondClip && !jumped(),
  'The link waits while both cards fade.');
 returnFade.finish();await returnFade.finished;await frame();
 check(!copy(firstPanel) && copy(secondPanel)===secondClip && !jumped(),
  'The link waits for the second card after the first card returns.');
 if(secondClip.isConnected) secondExit.finish();
 await frame();
 check(!copy(secondPanel) && jumped(),'The link opens the first card after both fades end.');
""")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_saved_card_keeps_its_place_when_sorted_by_state(self) -> None:
        self.check_card_motion(browser_markup(self.root), r"""
 document.querySelector('[data-sort="4"]').click();
 const [first,row,next]=rows(),panel=motionKey(row);
 check([first,row,next].every(node=>node.dataset.state==='Open'),'Three open cards are sorted by State.');
 send(await openCard(row));
 const clip=copy(panel),exit=clip.getAnimations()[0];
 halfway(exit);
 const position=top(clip),following=top(next);
 check(top(first)<position && position<following,'The departing card starts between its neighbors.');
 posts.get(row.dataset.key)({headers:new Headers(),ok:true,json:async()=>({answer:'works',state:'Done',later:0})});
 await frame();
 check(copy(panel)===clip && near(top(clip),position),'The saved card keeps its position during the fade.');
 check(near(top(next),following),'The next card keeps its position when the save completes.');
 exit.finish();await exit.finished;await frame();
 check(!copy(panel) && top(next)<following,'The next card fills the gap when the fade ends.');
""")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_completed_card_leaves_after_remount_during_save(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        records = [replace(change, key=f'change-{number}', id=f'C{number}') for number in range(1, 181)]
        with patch.object(review_items, 'read', return_value=records):
            source = browser_markup(self.root)
        self.check_card_motion(source, r"""
 document.querySelector('[data-sort="0"]').click();
 const row=rows()[1],key=row.dataset.key,panel=motionKey(row);
 const current=()=>rows().find(node=>node.dataset.key===key);
 send(await openCard(row));
 check(posts.has(key) && !row.isConnected,'The pending save removes the original row.');
 const firstExit=copy(panel)?.getAnimations()[0];
 if(!firstExit) throw new Error('The original row must animate when it leaves.');
 scrollTo({top:document.documentElement.scrollHeight,behavior:'instant'});await frame();
 check(scrollY>innerHeight && !current() && copy(panel),'Scrolling away preserves the first fade.');
 firstExit.finish();await firstExit.finished;await frame();
 check(!current() && !copy(panel),'The first fade ends while the row is outside the window.');
 scrollTo({top:0,behavior:'instant'});await frame();
 const remounted=current();
 if(!remounted || remounted===row) throw new Error('The pending row must mount again.');
 const form=await readyReviewCard(remounted);
 check(form.dataset.sending==='true','The remounted form shows the pending save.');
 posts.get(key)({headers:new Headers(),ok:true,json:async()=>({answer:'works',state:'Done',later:0})});
 await frame();
 check(!current(),'The completed row leaves the table.');
 const exit=copy(panel)?.getAnimations()[0];
 if(!exit) throw new Error('The remounted row must animate when it leaves.');
 exit.finish();await exit.finished;await frame();
 check(!current() && !document.querySelector('[data-record-motion]'),'The animation leaves no row or spacer.');
""")

    def check_card_motion(self, source: str, steps: str) -> None:
        setup = r"""<script>
const realFetch=fetch,posts=new Map(),animate=Element.prototype.animate;
let postCount=0;
window.setInterval=()=>0;
window.fetch=(url,options)=>{
 if(options?.method!=='POST') return realFetch(url,options);
 postCount++;
 return new Promise(resolve=>posts.set(new URLSearchParams(options.body).get('row'),resolve));
};
Element.prototype.animate=function(...args){
 const motion=animate.apply(this,args);
 if(this.classList.contains('record-motion-clip')){motion.pause();motion.currentTime=0;}
 return motion;
};
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const rows=()=>[...document.querySelectorAll('[data-list-item]')];
 const top=node=>node.getBoundingClientRect().top;
 const halfway=animation=>{animation.currentTime=animation.effect.getTiming().duration/2;};
 const near=(a,b,tolerance=2)=>Math.abs(a-b)<tolerance;
 const motionKey=row=>row.dataset.key;
 const copy=key=>document.querySelector('[data-record-motion-key="'+CSS.escape(key)+'"] .record-motion-clip');
 const openCard=async row=>{row.querySelector('.list-toggle').click();return readyReviewCard(row);};
 const send=form=>form.requestSubmit(form.querySelector('button[value="works"]'));
 try{
  frameElement.style.width='1280px';frameElement.style.height='820px';
  document.documentElement.style.overflowAnchor='none';
  await frame();
""" + steps + r"""
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        markup = source.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_reload_keeps_a_saved_verdict_and_the_selected_show(self) -> None:
        self.check_review_address_cases('copied-legacy-refresh', 'copied-legacy-link', 'copied-legacy-row', 'verdict', 'show')

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_reload_keeps_marked_review_links(self) -> None:
        self.check_review_address_cases('restored', 'copied', 'copied-row', 'appearance')

    def check_review_address_cases(self, *cases: str) -> None:
        setup = r"""<script>
const sendProbe=fetch;
window.setInterval=()=>0;
window.fetch=(url,options={})=>{
 if(options.method==='POST') return Promise.resolve(Response.json({answer:'works'}));
 return sendProbe(url,options);
};
const reloaded=Boolean(sessionStorage.getItem('review-reload'));
const copied=CASE.startsWith('copied');
const copiedRow=CASE.endsWith('-row');
const legacyCopy=CASE.startsWith('copied-legacy');
const lateCollision=CASE==='copied-legacy-refresh';
if(!reloaded){
 const place=copiedRow ? {show:'all'} : {item:INITIAL_ITEM};
 if(CASE==='verdict' || CASE==='appearance') place['item-kind']='id';
 history.replaceState(null,'','#'+new URLSearchParams(place));
}
if((copied || CASE==='appearance') && reloaded) history.replaceState(null,'');
const initialHistory=history.state;
</script>"""
        count_requests = r"""<script>
let arrivingReviewRow;
const removeSavedRow=(CASE==='restored' || copied) && reloaded;
if(CASE==='appearance' || removeSavedRow){
 const node=document.querySelector('[data-review-index]'),index=JSON.parse(node.textContent);
 const key=removeSavedRow ? 'page-owner/C1' : 'page-owner/C2';
 const [removed]=index.r.splice(index.r.findIndex(entry=>entry[0]===key),1);
 if(!removeSavedRow){arrivingReviewRow=removed;arrivingReviewRow[1]='builds/C2';}
 if(lateCollision){
  [arrivingReviewRow]=index.r.splice(index.r.findIndex(entry=>entry[0]==='page-owner/C2'),1);
 }
 node.textContent=JSON.stringify(index);
}
const initialBodyRequests=[],fetchBody=fetch;
window.fetch=(url,options)=>{
 const key=new URL(url,location.href).searchParams.get('body');
 if(key) initialBodyRequests.push(key);
 return fetchBody(url,options);
};
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],key='page-owner/C1',otherKey='page-owner/C2';
 const check=(condition,message)=>{if(!condition) failures.push(message);};
 const content=document.querySelector('[data-review]');
 const row=(item=key)=>content.querySelector('[data-key="'+item+'"]');
 const expanded=item=>row(item)?.querySelector('.list-toggle').getAttribute('aria-expanded')==='true';
 const notice=content.querySelector('[data-missing-item]');
 const addressItem=()=>new URLSearchParams(location.hash.slice(1)).get('item');
 const fetchCard=fetch;
 window.fetch=(url,options)=>new URL(url,location.href).search==='?version=1'
  ? Promise.resolve(Response.json({version:content.dataset.version,facts:pageRefreshFacts()}))
  : fetchCard(url,options);
 const follow=async params=>{
  const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams(params);
  await changed;
 };
 const refreshIndex=async update=>{
  const index=JSON.parse(content.querySelector('[data-review-index]').textContent);
  update(index);
  const facts=pageRefreshFacts(),version='confirmed';
  window.fetch=(url,options)=>{
   const params=new URL(url,location.href).searchParams;
   if(params.has('index')) return Promise.resolve(Response.json({index,version,facts}));
   if(params.has('version') && !params.has('body')) return Promise.resolve(Response.json({version,facts}));
   return fetchCard(url,options);
  };
  const refreshed=new Promise(resolve=>content.addEventListener('review-refreshed',resolve,{once:true}));
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await refreshed;
 };
 try{
  if(CASE==='appearance'){
   check(!notice.hidden && addressItem()==='builds/C2' && !row(otherKey),
    'Review shows the notice while the shown ID is missing.');
   if(!reloaded && !failures.length){
    sessionStorage.setItem('review-reload','1');location.reload();return;
   }
   check(initialHistory===null,'The missing shown ID loads without browser history state.');
   await refreshIndex(index=>index.r.push(arrivingReviewRow));
   await readyReviewCard(row(otherKey));
   check(expanded(otherKey) && notice.hidden && addressItem()===otherKey,
    'A refresh resolves the missing shown ID and opens its card.');
  }else if(!reloaded){
   if(CASE==='verdict'){
    const form=await readyReviewCard(row());
    check(expanded(key) && notice.hidden,
     'A link opens the Change by its shown ID with different letter case.');
    check(addressItem()===key,'The address uses the row key after the shown ID resolves.');
    check(initialBodyRequests.length===1 && initialBodyRequests[0]===key,
     'The first card request uses the row key and loads the body once.');
    const saved=new Promise(resolve=>{
     const observer=new MutationObserver(()=>{
      if(!form.dataset.sending){observer.disconnect();resolve();}
     });
     observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
    });
    form.requestSubmit(form.querySelector('[value="works"]'));
    await saved;
    history.replaceState(null,'','#item=page-owner%2FC1');
   }else if(CASE==='restored' || copied){
    if(copiedRow) row().querySelector('.list-toggle').click();
    check(expanded(key) && notice.hidden && history.state?.recordTable?.opened[0]===key,
     'Review saves the key of the open card.');
    if(legacyCopy){
     const place=new URLSearchParams(location.hash.slice(1));
     place.delete('item-kind');
     history.replaceState(history.state,'','#'+place);
    }
   }else content.querySelector('[data-filter-row="show"] [data-filter-value="open"]').click();
   if(!failures.length){
    sessionStorage.setItem('review-reload','1');
    location.reload();return;
   }
  }else if(CASE==='verdict'){
   const savedKey='review-saved-verdicts:'+location.pathname;
   check(!row() && !notice.hidden && sessionStorage.getItem(savedKey),
    'A reload hides the completed Change while the snapshot still contains it.');
   await refreshIndex(index=>{index.r=index.r.filter(entry=>entry[0]!==key);});
   check(!row() && !sessionStorage.getItem(savedKey),
    'The new snapshot confirms the removal and clears the saved verdict marker.');
   await follow({item:'BUILDS/c2','item-kind':'id'});
   check(expanded(otherKey) && notice.hidden && addressItem()===otherKey,
    'A new link resolves the shown ID and clears the missing item notice.');
   await follow({item:'unknown/C1'});
   check(!notice.hidden && notice.textContent==='This review is no longer on the Review page.',
    'An unknown ID shows the missing item notice.');
   await follow({show:'all','filter-id':'builds/C2'});
   const shown=[...content.querySelectorAll('[data-list-item]:not([hidden])')];
   check(shown.length===1 && shown[0].dataset.key===otherKey && !expanded(otherKey),
    'A filter link shows the row with the matching ID and keeps its card closed.');
  }else if(CASE==='restored' || copied){
   if(copied) check(initialHistory===null,'The copied address loads without browser history state.');
   if(lateCollision){
    check(!row() && !row(otherKey) && !notice.hidden,'Review shows the missing notice before the other row arrives.');
    await refreshIndex(index=>index.r.push(arrivingReviewRow));
   }
   check(!row() && row(otherKey) && !expanded(otherKey) && !notice.hidden && addressItem()===key,
    'A missing saved key does not open a card with the same shown ID.');
   if(legacyCopy) check(new URLSearchParams(location.hash.slice(1)).get('item-kind')==='key',
    'The old address keeps its value as a stored key.');
   check(initialBodyRequests.length===0,'Review does not request a card for the missing saved key.');
  }else{
   check(content.dataset.show==='open' && !row(),'A reload restores Open when the address names a Done Change.');
  }
 }catch(error){failures.push(String(error));}
 await sendProbe('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        for case in cases:
            with self.subTest(case=case):
                if case == 'show':
                    _ = change_review.record_verdict(self.fixture.review_list, 'C1', 'works',
                                                     datetime.now(user_zone()).date(), self.fixture.production)
                shown_ids = {'page-owner/C1': 'builds/C1', 'page-owner/C2': 'builds/C2'} if case == 'verdict' else {}
                if case == 'restored' or case.startswith('copied'):
                    shown_ids = {'page-owner/C2': 'page-owner/C1'}
                if shown_ids:
                    records = [replace(item, id=shown_ids[item.key]) if item.key in shown_ids else item
                               for item in review_items.read(self.root)]
                    with patch.object(review_items, 'read', return_value=records):
                        page = browser_markup(self.root)
                else:
                    page = browser_markup(self.root)
                item = {'verdict': 'BUILDS/c1', 'appearance': 'builds/C2'}.get(case, 'page-owner/C1')
                page = page.replace('<head>', '<head>'+setup.replace('INITIAL_ITEM', json.dumps(item))
                                    .replace('CASE', json.dumps(case)), 1)
                request_script = '<script>'+review_page.CARD_REQUEST_SCRIPT
                page = page.replace(request_script, count_requests.replace('CASE', json.dumps(case))+request_script, 1)
                page = page.replace('</body>', probe.replace('CASE', json.dumps(case))+'</body>', 1)
                self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_copied_review_filter_keeps_its_resolved_key(self) -> None:
        shown_ids = {'page-owner/C1': 'builds/C1', 'page-owner/C2': 'page-owner/C1'}
        records = [replace(item, id=shown_ids.get(item.key, item.id)) for item in review_items.read(self.root)]
        with patch.object(review_items, 'read', return_value=records):
            page = browser_markup(self.root)
        setup = r"""<script>
window.setInterval=()=>0;
const copiedPlace=sessionStorage.getItem('review-filter-place');
history.replaceState(null,'',copiedPlace || '#show=all&filter-id=builds%2FC1');
const initialHistory=history.state;
</script>"""
        before_index = r"""<script>
if(copiedPlace){
 const node=document.querySelector('[data-review-index]'),data=JSON.parse(node.textContent);
 data.r=data.r.filter(row=>row[0]!=='page-owner/C1');
 node.textContent=JSON.stringify(data);
}
</script>"""
        probe = r"""<script>
const failures=[];
const shown=[...document.querySelectorAll('[data-list-item]:not([hidden])')].map(row=>row.dataset.key);
if(!copiedPlace && shown.join()==='page-owner/C1'){
 sessionStorage.setItem('review-filter-place',location.hash);
 location.reload();
}else{
 if(!copiedPlace) failures.push('The filter selects the row by its displayed ID.');
 if(initialHistory!==null) failures.push('The copied filter loads without browser history state.');
 if(shown.length) failures.push('The stored filter key cannot select a row with the same displayed ID.');
 void fetch('/results',{method:'POST',body:JSON.stringify(failures)});
}
</script>"""
        request_script = '<script>'+review_page.CARD_REQUEST_SCRIPT
        page = page.replace('<head>', '<head>'+setup, 1).replace(request_script, before_index+request_script, 1)
        page = page.replace('</body>', probe+'</body>', 1)
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_links_retry_only_marked_displayed_ids(self) -> None:
        records = [replace(item, id='builds/C2') if item.key == 'page-owner/C2' else item
                   for item in review_items.read(self.root)]
        with patch.object(review_items, 'read', return_value=records):
            page = browser_markup(self.root)
        setup = r"""<script>
window.setInterval=()=>0;
history.replaceState(HISTORY,'','#'+new URLSearchParams({owner:'',show:'all',q:'',sort:'',dir:'asc',...LINK}));
</script>"""
        before_index = r"""<script>
const node=document.querySelector('[data-review-index]'),fullIndex=JSON.parse(node.textContent);
const indexWithoutTarget={...fullIndex,r:fullIndex.r.filter(row=>row[0]!=='page-owner/C2')};
node.textContent=JSON.stringify(indexWithoutTarget);
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],key='page-owner/C2',review=document.querySelector('[data-review]');
 const check=(condition,message)=>{if(!condition) failures.push(message);};
 const row=()=>review.querySelector('[data-key="'+key+'"]');
 const address=()=>new URLSearchParams(location.hash.slice(1));
 const nativeFetch=fetch,facts=pageRefreshFacts(),link=LINK;
 const refresh=async(index,version)=>{
  window.fetch=(url,options)=>{
   const params=new URL(url,location.href).searchParams;
   if(params.has('index')) return Promise.resolve(Response.json({index,version,facts}));
   if(params.has('version') && !params.has('body')) return Promise.resolve(Response.json({version,facts}));
   return nativeFetch(url,options);
  };
  const refreshed=new Promise(resolve=>review.addEventListener('review-refreshed',resolve,{once:true}));
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await refreshed;
 };
 try{
  check(!row(),'The displayed ID is unresolved at startup.');
  await refresh(indexWithoutTarget,'missing');
  const stored=link['item-kind']!=='id' && !link['filter-id'];
  const retained=link.item ? (address().get('item-kind') || 'key')===(stored ? 'key' : 'id') &&
   address().get('item')==='builds/C2' : address().get(stored ? 'filter-key' : 'filter-id')==='builds/C2';
  check(!row() && retained,'A refresh keeps the address while the row is absent.');
  await refresh(fullIndex,'confirmed');
  const open=row()?.querySelector('.list-toggle').getAttribute('aria-expanded');
  if(stored && link.item){
   check(open==='false' && address().get('item')==='builds/C2' &&
    !review.querySelector('[data-missing-item]').hidden,'The older card address keeps the missing item notice.');
  }else if(stored){
   check(!row() && address().get('filter-key')==='builds/C2' &&
    !review.querySelector('[data-missing-keys]').hidden,'The older filter keeps the missing key notice.');
  }else if(link.item){
   check(open==='true' && address().get('item')===key && address().get('item-kind')==='key',
    'A refresh resolves the marked item link and saves the key.');
  }else{
   check(open==='false' && address().get('filter-key')===key && !address().has('filter-id'),
    'A refresh resolves the marked filter link and saves the key.');
  }
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        request_script = '<script>'+review_page.CARD_REQUEST_SCRIPT
        old_link = {'item': 'builds/C2', 'linked': '1'}
        old_history: dict[str, object] = {'recordTable': {'opened': [], 'linkedItem': 'builds/C2', 'pendingItem': True}}
        for link, history in ((old_link, None), (old_link, old_history), ({'key': 'builds/C2'}, None),
                              ({**old_link, 'item-kind': 'id'}, None), ({'filter-id': 'builds/C2'}, None)):
            with self.subTest(link=link, history=history):
                script = setup.replace('HISTORY', json.dumps(history)).replace('LINK', json.dumps(link))
                markup = page.replace('<head>', '<head>'+script, 1)
                markup = markup.replace(request_script, before_index+request_script, 1)
                markup = markup.replace('</body>', probe.replace('LINK', json.dumps(link))+'</body>', 1)
                self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_back_during_a_save_keeps_the_link_for_a_change_and_question(self) -> None:
        _ = self.question()
        setup = r"""<script>
const sendProbe=fetch;
let finishSave,savedState;
window.setInterval=()=>0;
window.fetch=(url,options={})=>{
 if(options.method!=='POST') return sendProbe(url,options);
 return new Promise(resolve=>{finishSave=()=>resolve(Response.json({answer:'saved',state:savedState}));});
};
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[];
 const content=document.querySelector('[data-review]');
 const row=key=>content.querySelector('[data-key="'+key+'"]');
 const fetchCard=fetch;
 let snapshot;
 window.fetch=(url,options)=>{
  const params=new URL(url,location.href).searchParams;
  if(params.has('index') && snapshot) return Promise.resolve(Response.json(snapshot));
  if(params.has('version') && !params.has('body'))
   return Promise.resolve(Response.json({version:snapshot?.version ?? content.dataset.version,facts:pageRefreshFacts()}));
  return fetchCard(url,options);
 };
 const afterHash=()=>new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
 const follow=async key=>{
  const changed=afterHash();
  location.hash=new URLSearchParams({item:key});
  await changed;
 };
 try{
  const question=content.querySelector('[data-type="question"][data-list-item]').dataset.key;
  for(const [key,action] of [['page-owner/C1','works'],[question,'answer'],['page-owner/C3','validate']]){
   await follow(key);
   const form=await readyReviewCard(row(key));
   form.elements.feedback.value='The saved words.';
   const previous={href:location.href,state:history.state};
   const saved=new Promise(resolve=>{
    const observer=new MutationObserver(()=>{
     if(!form.dataset.sending){observer.disconnect();resolve();}
    });
    observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
   });
   form.requestSubmit(form.querySelector('[value="'+action+'"]'));
   await follow('page-owner/C2');
   const oldURL=location.href;
   history.replaceState(previous.state,'',previous.href);
   dispatchEvent(new PopStateEvent('popstate',{state:previous.state}));
   dispatchEvent(new HashChangeEvent('hashchange',{oldURL,newURL:previous.href}));
   savedState=action==='validate' ? 'Validating' : 'Done';
   const refreshed=new Promise(resolve=>content.addEventListener('review-refreshed',resolve,{once:true}));
   if(action==='validate'){
    const index=JSON.parse(content.querySelector('[data-review-index]').textContent);
    snapshot={index,version:'handoff-publication-pending',facts:pageRefreshFacts()};
   }
   finishSave();await saved;
   await refreshed;
   const target=row(key),item=new URLSearchParams(location.hash.slice(1)).get('item');
   if(action==='validate'){
    if(item!==key || content.dataset.version!==snapshot.version || target)
     failures.push('The deferred link keeps the Validating marker when a new snapshot still shows the old Change.');
    continue;
   }
   if(item!==key || target || content.querySelector('[data-missing-item]').hidden)
    failures.push('Back keeps the link and reports the completed item as unavailable: '+key+'.');
  }
 }catch(error){failures.push(String(error));}
 await sendProbe('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        page = browser_markup(self.root).replace('<head>', '<head>'+setup, 1)
        page = page.replace('</body>', probe+'</body>', 1)
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_back_and_forward_defer_the_link_to_its_own_pending_save(self) -> None:
        _ = self.question()
        setup = r"""<script>
const sendProbe=fetch,finishSave=new Map();
history.scrollRestoration='manual';
window.setInterval=()=>0;
window.fetch=(url,options={})=>{
 if(options.method!=='POST') return sendProbe(url,options);
 const key=new URLSearchParams(options.body).get('row');
 return new Promise(resolve=>finishSave.set(key,resolve));
};
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[];
 const content=document.querySelector('[data-review]');
 const row=key=>content.querySelector('[data-key="'+key+'"]');
 const fetchCard=fetch;
 window.fetch=(url,options)=>new URL(url,location.href).search==='?version=1'
  ? Promise.resolve(Response.json({version:content.dataset.version,facts:pageRefreshFacts()}))
  : fetchCard(url,options);
 const afterHash=step=>new Promise((resolve,reject)=>{
  const timeout=setTimeout(()=>reject(new Error('The history entry did not change during '+step+'.')),5000);
  addEventListener('hashchange',()=>{clearTimeout(timeout);resolve();},{once:true});
 });
 const item=()=>new URLSearchParams(location.hash.slice(1)).get('item');
 async function start(key,action){
  const changed=afterHash('link to '+key);location.hash=new URLSearchParams({item:key});await changed;
  const form=await readyReviewCard(row(key));
  form.elements.feedback.value='The saved words.';
  form.requestSubmit(form.querySelector('[value="'+action+'"]'));
  return form;
 }
 async function settle(key,form,response=Response.json({answer:'saved',state:'Done'})){
  finishSave.get(key)(response);
  while(form.dataset.sending) await new Promise(resolve=>requestAnimationFrame(resolve));
 }
 const afterFades=async()=>{
  const fades=[...content.querySelectorAll('.record-motion-clip')].flatMap(node=>node.getAnimations());
  await Promise.allSettled(fades.map(animation=>animation.finished));
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 };
 try{
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  frameElement.style.width='1280px';frameElement.style.height='820px';
  await new Promise(resolve=>requestAnimationFrame(resolve));
  const change='page-owner/C1',question=content.querySelector('[data-type="question"][data-list-item]').dataset.key;
  const changeForm=await start(change,'works'),questionForm=await start(question,'answer');
  const panel=recordFromDetail(questionForm).dataset.key;
  const clip=content.querySelector('[data-record-motion-key="'+CSS.escape(panel)+'"] .record-motion-clip')
   ?.closest('.record-motion-clip');
  const exit=clip?.getAnimations()[0];
  if(!exit) throw new Error('The Question must fade during the save.');
  exit.pause();exit.currentTime=exit.effect.getTiming().duration/2;
  const back=afterHash('Back');history.back();await back;
  if(item()!==change) failures.push('Back restores the pending Change link.');
  if(REFUSE_QUESTION){
   await settle(change,changeForm);
   if(!clip.isConnected || row(change)) throw new Error('The Change link must wait for the Question fade.');
   let returning;
   const animate=Element.prototype.animate;
   Element.prototype.animate=function(keyframes,options){
    const motion=animate.call(this,keyframes,options);
    if(this.classList.contains('record-motion-clip') && keyframes.at(-1).opacity===1){
     motion.pause();motion.currentTime=motion.effect.getTiming().duration/2;returning={clip:this,motion};
    }
    return motion;
   };
   await settle(question,questionForm,Response.json({reason:'Try again.'},{status:503}));
   Element.prototype.animate=animate;
   if(!returning) throw new Error('The refused Question must start a return fade.');
   if(!returning.clip.isConnected || row(change))
    failures.push('F007: The deferred Change link waits for the Question return fade.');
   if(returning.clip.isConnected) returning.motion.finish();
  }else{
   const forward=afterHash('Forward');history.forward();await forward;
   if(item()!==question) failures.push('Forward restores the pending Question link.');
   await settle(change,changeForm);
   if(!questionForm.dataset.sending || item()!==question)
    failures.push('The Change save leaves the Question link pending.');
   await settle(question,questionForm);
   if(!clip.isConnected || row(question))
    failures.push('The Question link waits for the fade after the save. Fade mounted: '+clip.isConnected+'.');
   exit.finish();
  }
  await afterFades();
  const linked=REFUSE_QUESTION ? change : question,target=row(linked);
   if(item()!==linked || target || content.querySelector('[data-missing-item]').hidden)
    failures.push('The restored link reports the completed item as unavailable after the fades.');
 }catch(error){failures.push(String(error));}
 await sendProbe('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        page = browser_markup(self.root).replace('<head>', '<head>'+setup, 1)
        for refuse_question in (False, True):
            with self.subTest(refuse_question=refuse_question):
                script = probe.replace('REFUSE_QUESTION', json.dumps(refuse_question))
                self.assertEqual(firefox_probe_results(page.replace('</body>', script+'</body>', 1).encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_console_reload_preserves_show_and_an_explicit_link_opens_the_card(self) -> None:
        _ = change_review.record_verdict(self.fixture.review_list, 'C1', 'works',
                                         datetime.now(user_zone()).date(), self.fixture.production)
        review = browser_markup(self.root)
        pages = (console.ConsolePage('review', 'Review', 'inbox', 'Review',
                                     console.AvailablePage('/console/review.html')),)
        shell = console.render(pages, console.UnavailablePage()).decode()
        shell = shell.replace('</head>', '''<script>
const sendProbe=fetch;
window.fetch=(url,options)=>String(url)==='/results' ? sendProbe(url,options) :
 Promise.resolve(Response.json({available:true,count:0,entries:[],new:[]}));
</script></head>''', 1)
        probe = r"""<script>
(async()=>{
 const failures=[],stage=sessionStorage.getItem('console-reload');
 const frame=document.querySelector('iframe'),open='page-owner/C2',done='page-owner/C1';
 const explicit='/console/review.html?show=all&item='+encodeURIComponent(open);
 const row=key=>frame.contentDocument.querySelector('[data-key="'+key+'"]');
 const show=()=>frame.contentDocument.querySelector('[data-review]')?.dataset.show;
 const until=async (predicate,label)=>{
  const end=performance.now()+8000;
  while(!predicate()){
   if(performance.now()>end) throw new Error('Timed out waiting for '+label+'.');
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 try{
  if(!stage){
   sessionStorage.setItem('console-reload','linked');
   location.search=new URLSearchParams({'page':'review','at.review':explicit});return;
  }
  await until(()=>frame.contentWindow.history.state?.recordTable,'Review to load');
  if(stage==='linked'){
   if(show()!=='all' || !frame.contentWindow.recordIsOpen(row(open)) || row(done))
    failures.push('An explicit link opens the active Change under All and leaves the completed Change hidden.');
   frame.contentDocument.querySelector('[data-filter-row="show"] [data-filter-value="open"]').click();
   await until(()=>new URLSearchParams(location.search).get('at.review')==='/console/review.html'+
    frame.contentWindow.location.hash,'Console to record Open');
   sessionStorage.setItem('console-reload','reloaded');location.reload();return;
  }
  if(show()!=='open' || row(done)) failures.push('A Console reload keeps Open and hides the completed Change.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        shell = shell.replace('</body>', probe+'</body>', 1)
        review_html, shell_html = (json.dumps(page).replace('</', '<\\/') for page in (review, shell))
        router = f'<script>document.write(location.pathname.endsWith("review.html") ? {review_html} : {shell_html});</script>'
        self.assertEqual(firefox_probe_results(router.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_links_keep_completed_changes_and_questions_hidden(self) -> None:
        _ = self.question()
        setup = r"""<script>
const nativeFetch=fetch;
let finishVerdict;
history.replaceState(null,'','#show=all');
window.setInterval=()=>0;
window.fetch=(url,options={})=>{
 if(options.method!=='POST') return nativeFetch(url,options);
 const fields=new URLSearchParams(options.body);
 if(fields.get('row').startsWith('question/') && fields.get('outcome')==='answer')
  return Promise.resolve(Response.json({answer:'answered',state:'Done'}));
 if(!['page-owner/C1','page-owner/C2'].includes(fields.get('row')) || fields.get('outcome')!=='works')
  throw new Error('The form did not submit a verdict for the expected Change.');
 const result=()=>Response.json({answer:'works',state:'Done'});
 if(fields.get('row')==='page-owner/C1') return new Promise(resolve=>{finishVerdict=()=>resolve(result());});
 return Promise.resolve(result());
};
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 const change=key=>document.querySelector('[data-key="'+key+'"]');
 const read=fetch;
 window.fetch=(url,options)=>new URL(url,location.href).search==='?version=1'
  ? Promise.resolve(Response.json({version:document.querySelector('[data-review]').dataset.version,facts:pageRefreshFacts()}))
  : read(url,options);
 const review=document.querySelector('[data-review]');
 const selectShow=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]').click();
 const selectedOwner=()=>filterControl.ownerId(document.querySelector('[data-filter-row="owner"] [data-filter-value][aria-pressed="true"]'));
 const follow=async(params,key='page-owner/C1')=>{
  const link=document.createElement('a');
  link.href='#'+new URLSearchParams({item:key,...params});
  link.textContent='Open review item';
  document.body.append(link);
  const navigated=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  link.click();
  await navigated;
  link.remove();
  return change(key);
 };
 const savedForm=form=>new Promise(resolve=>{
  const observer=new MutationObserver(()=>{
   if(form.dataset.sending) return;
   observer.disconnect();resolve();
  });
  observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
 });
 const expanded=(row,state)=>row?.dataset.state===state && !row.hidden && recordIsOpen(row);
 try{
  const owner=change('page-owner/C1').dataset.owner;
  document.querySelector('[data-filter-row="owner"] [data-filter-value="'+CSS.escape(staffChipValue(owner))+'"]').click();
  selectShow('open');
  if(selectedOwner()!==owner) document.querySelector('[data-filter-row="owner"] [data-filter-value="'+CSS.escape(staffChipValue(owner))+'"]').click();
  check(expanded(await follow({}),'Open') && review.dataset.show==='open' && selectedOwner()===owner,
   'F003: A bare link preserves the matching Show and Owner filters.');
  const jump=document.createElement('a');
  jump.dataset.recordJump='';
  review.append(jump);
  jump.href='#item=page-owner%2FC99';jump.click();
  const missing=document.querySelector('[data-record-link-notice]'),missingHash=location.hash;
  jump.href='#item=page-owner%2FC2';jump.click();
  jump.remove();
  check(expanded(change('page-owner/C2'),'Open') && missing.hidden,'A valid review link clears the notice.');
  const restored=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  history.back();await restored;
  check(location.hash===missingHash && expanded(change('page-owner/C1'),'Open') &&
   !expanded(change('page-owner/C2'),'Open') && !missing.hidden &&
   missing.textContent==='This review is no longer on the Review page.',
   'Back restores the open review and the notice for the missing review.');
  change('page-owner/C1').querySelector('.list-toggle').click();

  for(const key of ['page-owner/C2','page-owner/C1']){
   const row=change(key);
   check(row && !row.hidden,key+' is visible before the save.');
   row.querySelector('.list-toggle').click();
   const form=await readyReviewCard(row);
   const saved=savedForm(form);
   form.requestSubmit(form.querySelector('button[value="works"]'));
   if(!form.dataset.sending) throw new Error('The save did not start for '+key+'.');
   if(key==='page-owner/C1'){
    const clip=document.querySelector('[data-record-motion-key="'+
     CSS.escape(row.dataset.key)+'"]')?.querySelector('.record-motion-clip');
    const exit=clip?.getAnimations()[0];
    if(!exit) throw new Error('The Change must fade during the save.');
    const midpoint=exit.effect.getTiming().duration/2;
    exit.pause();exit.currentTime=midpoint;
    await follow({show:'all',owner});
    finishVerdict();
    await saved;
    check(clip.isConnected && exit.currentTime===midpoint && !change(key),
     'The link waits for the fade after the save.');
    if(clip.isConnected){
     exit.finish();await exit.finished;
     await new Promise(resolve=>requestAnimationFrame(resolve));
    }
   }
   await saved;
   check(!form.querySelector('[data-send-status]').textContent,key+' saves without action status.');
   check(!change(key),'The save removes '+key+'.');
   if(key==='page-owner/C2'){
    const fades=[...review.querySelectorAll('.record-motion-clip')].flatMap(node=>node.getAnimations());
    await Promise.allSettled(fades.map(animation=>animation.finished));
   }
  }
  check(!change('page-owner/C1') && selectedOwner()===owner,
   'A link during a save keeps the completed Change hidden and preserves the owner.');
  check(!(await follow({show:'all'})) && review.dataset.show==='all',
   'A same-tab link cannot reopen the completed Change under All.');
  selectShow('open');
  check(!(await follow({show:'open'})) && review.dataset.show==='open',
   'A same-tab link cannot reopen the completed Change under Open.');
  check(!change('page-owner/C2'),'The other completed Change stays hidden.');

  const question=document.querySelector('[data-type="question"][data-list-item]');
  question.querySelector('.list-toggle').click();
  const answerForm=await readyReviewCard(question),answered=savedForm(answerForm);
  answerForm.elements.feedback.value='Use the wide layout.';
  answerForm.requestSubmit(answerForm.querySelector('[value="answer"]'));
  if(!answerForm.dataset.sending) throw new Error('The Question save did not start.');
  await answered;
  selectShow('all');
  check(!(await follow({},question.dataset.key)) && review.dataset.show==='all',
   'A link cannot reopen the answered Question.');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        markup = browser_markup(self.root, show='all')
        page = markup.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>', 1)
        with self.subTest(navigation='same tab'):
            self.assertEqual(firefox_probe_results(page.encode()), [[]])

        fixture = self.fixture
        _ = change_review.record_verdict(fixture.review_list, 'C1', 'works', datetime.now(user_zone()).date(),
                                         fixture.production)
        markup = browser_markup(self.root)
        initial_link = '<script>history.replaceState(null, "", "#show=open&item=page-owner%2FC1");</script>'
        initial_probe = r"""<script>
const review=document.querySelector('[data-review]'),row=review.querySelector('[data-key="page-owner/C1"]');
const hidden=!row && !review.querySelector('[data-missing-item]').hidden;
void fetch('/results',{method:'POST',body:JSON.stringify(hidden && review.dataset.show==='open' ? [] :
 ['F004: A fresh link reports that the completed Change is unavailable.'])});
</script>"""
        page = markup.replace('<head>', '<head>'+initial_link, 1).replace('</body>', initial_probe+'</body>', 1)
        with self.subTest(navigation='fresh load'):
            self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_filters_single_card_refresh_drafts_and_saved_row_removal(self) -> None:
        question_key = self.question()
        question_item = next(item for item in review_items.read(self.root) if item.key == question_key)
        assert isinstance(question_item.source, review_items.QuestionSource)
        question_item.source.record.item['parts'] = update_registration.QuestionParts(
            ask='Which layout should the page use?',context='Choose a layout.',items=[],
            recommend='',choices=['Compact layout','Wide layout'])
        update_registration.write_question_record(question_item.source.record, assign_id=True)
        filed = self.investigation()
        _ = self.investigation(False)
        for _ in range(3):
            done = self.investigation()
            _ = investigation.mark_read(done.owner, done.id)
        filed_key = review_items.investigation_record_key(filed)
        confirmed_answers = [
            exchange.answer.text
            for entry in list_page.records()
            for exchange in the_list.confirmed_exchanges(
                filed_key, investigation.ListDestination(entry.keeper, entry.item.number), entry.item.filed_exchanges)
            if isinstance(exchange.answer, list_exchange.ExchangeAnswer)
        ]
        self.assertEqual(confirmed_answers, [filed.answer])
        source = browser_markup(self.root)
        setup = r"""<script>
const nativeFetch=fetch, nativeTimeout=setTimeout, original=MARKUP;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.refreshMarkup=original;
let heldBody;
window.transformFixtureBody=async(text,key)=>{
 if(heldBody?.key===key) await heldBody.ready;
 const panel=document.createElement('div');panel.innerHTML=text;
 if(key.startsWith('question/')){
  const form=panel.querySelector('form');form.elements.token.value+='-refreshed';
 }
 return panel.innerHTML;
};
window.refreshRequests=[];
window.posts=[];
let nextPost;
window.fetch=async(_url,options={})=>{
 if(options.method==='POST'){
  window.posts.push(new URLSearchParams(options.body));
  if(nextPost){const post=nextPost;nextPost=null;return post;}
  return {headers:new Headers(),ok:true,json:async()=>({answer:'works'})};
 }
 window.refreshRequests.push(_url);return {ok:true,text:async()=>window.refreshMarkup};
};
</script>""".replace('MARKUP',json.dumps(source).replace('</','<\\/'))
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>nativeTimeout(resolve,60));
 const rows=()=>[...document.querySelectorAll('[data-list-item]')];
 const shown=()=>rows().filter(row=>!row.hidden);
 const holdPost=()=>{
  let finish;nextPost=new Promise(resolve=>{finish=resolve;});
  return {save:()=>finish({headers:new Headers(),ok:true,json:async()=>({answer:'works'})}),
   refuse:reason=>finish({headers:new Headers(),ok:false,json:async()=>({reason})})};
 };
 const visibleStatus=text=>[...document.querySelectorAll('[data-send-status],[data-hidden-action-status]')]
   .some(node=>node.textContent.includes(text) && node.getClientRects().length);
 const leavingNow=row=>!row.isConnected && Boolean(document.querySelector('[data-record-motion]'));
 const selectedOwner=(doc=document)=>doc.querySelector('[data-filter-row="owner"] [data-filter-value][aria-pressed="true"]');
 const choose=(id,value,event='change')=>{
  if(id==='list-owner'){document.querySelector('[data-filter-row="owner"] [data-filter-value="'+(value ? CSS.escape(staffChipValue(value)) : 'all')+'"]').click();return;}
  if(id==='list-show'){document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]').click();return;}
  const field=document.getElementById(id);field.value=value;field.dispatchEvent(new Event(event,{bubbles:true}));
 };
 const showValue=(doc=document)=>doc.querySelector('[data-filter-row="show"] [data-filter-value][aria-pressed="true"]').dataset.filterValue;
 WAIT_FOR_BROWSER_STATE
 const readyReviewCard=async row=>{
  const form=()=>recordDetails(row).querySelector('[data-review-form]');
  await until(form,'the open card '+row.dataset.key);
  return form();
 };
 try{
  await flush();
  check(shown().length===4 && shown().every(row=>row.dataset.state==='Open'),'Open includes only Open records');
  check(document.querySelector('[data-filter-row="show"] [data-filter-value="open"] b').textContent==='4',
    'F007: Open counts only Open records');
  check(!document.querySelector('[data-filter-row="show"] [data-filter-value="Validating"]'),
    'The Show row has no Validating chip.');
  check(document.querySelector('[data-filter-row="type"] [data-filter-value="question"]').hidden,
   'The legacy Question filter stays hidden under All.');
  choose('list-search','Q1','input');check(shown().length===1,'Search finds the legacy Question by ID.');
  choose('list-search','','input');
  const question=rows().find(row=>row.dataset.type==='question');question.querySelector('.list-toggle').click();await readyReviewCard(question);
  const title=question.querySelector('.review-title'),titleHeight=title.getBoundingClientRect().height;
  title.textContent='A long review title '.repeat(20);
  check(title.getBoundingClientRect().height===titleHeight && title.scrollWidth>title.clientWidth,
    'a long row title stays on one line and hides its overflow');
  const change=rows().find(row=>row.dataset.type==='change');change.querySelector('.list-toggle').click();await readyReviewCard(change);
  check(!recordIsOpen(question),'only one card opens');
  question.querySelector('.list-toggle').click();await readyReviewCard(question);
  const draftLater=recordDetails(question).querySelector('[name="later"]');draftLater.value='2098-12-31T16:30';
  draftLater.dispatchEvent(new Event('change',{bubbles:true}));
  const picked=recordDetails(question).querySelector('input[name=choice]');picked.click();
  const draft=recordDetails(question).querySelector('textarea');draft.value='Keep this draft';draft.dispatchEvent(new Event('input'));
  draft.focus();draft.setSelectionRange(2,7,'backward');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  const currentQuestion=()=>rows().find(row=>row.dataset.type==='question');
  await until(()=>recordDetails(currentQuestion())?.querySelector('[name="token"]')?.value.endsWith('-refreshed'),
   'the revised question');
  let fresh=currentQuestion();
  const refreshedDraft=recordDetails(fresh).querySelector('textarea');
  check(recordIsOpen(fresh) && refreshedDraft.value==='Keep this draft','Refresh keeps the card and draft.');
  check(document.activeElement===refreshedDraft && refreshedDraft.selectionStart===2 &&
    refreshedDraft.selectionEnd===7 && refreshedDraft.selectionDirection==='backward','refresh keeps focus and selection');
  check(recordDetails(fresh).querySelector('[name="later"]').value==='2098-12-31T16:30','F018: refresh keeps the picked Later date and time');
  check(recordDetails(fresh).querySelector('input[name=choice]:checked')?.value===picked.value,'F018: refresh keeps the selected choice');
  check(recordDetails(fresh).querySelector('form').elements.row.value===fresh.dataset.key &&
    recordDetails(fresh).querySelector('form').elements.token.value.endsWith('-refreshed'),'F018: refresh supplies the row and token');
  const [initialTarget,other]=rows().filter(row=>row.dataset.type==='change');
  const afterRefresh=()=>new Promise((resolve,reject)=>{
   const review=document.querySelector('[data-review]');
   const refreshed=()=>{clearTimeout(timeout);resolve();};
   const timeout=nativeTimeout(()=>{
    review.removeEventListener('review-refreshed',refreshed);
    reject(new Error('Review did not refresh after the refusal.'));
   },HANG_GUARD_MS);
   review.addEventListener('review-refreshed',refreshed,{once:true});
  });
  const liveRow=key=>rows().find(row=>row.dataset.key===key);
  other.querySelector('.list-toggle').click();
  let otherForm=await readyReviewCard(other);
  let rejected=holdPost();
  otherForm.requestSubmit(otherForm.querySelector('[value="works"]'));
  check(leavingNow(other),'The row starts to leave before the response.');
  let releaseBody;
  heldBody={key:other.dataset.key,ready:new Promise(resolve=>{releaseBody=resolve;})};
  let refreshed=afterRefresh();rejected.refuse('Fixture older refusal');await refreshed;
  const returnedOther=liveRow(other.dataset.key);
  otherForm=await readyReviewCard(returnedOther);
  const retainedWorks=otherForm.querySelector('[value="works"]');
  check(retainedWorks.disabled && !otherForm.elements.feedback.disabled,
   'During refresh the retained form disables actions and keeps the draft editable.');
  const postCount=posts.length;
  retainedWorks.click();
  check(posts.length===postCount,'Clicking the disabled action sends no request.');
  releaseBody();heldBody=null;
  const works=()=>recordDetails(returnedOther).querySelector('[value="works"]:enabled:not([aria-disabled="true"])');
  await until(works,'the actions in the returned card');
  check(visibleStatus('Not saved: Fixture older refusal'),'A refused row shows the reason.');
  rejected=holdPost();works().click();
  refreshed=afterRefresh();rejected.refuse('Fixture newer refusal');await refreshed;
  check(!visibleStatus('Not saved: Fixture older refusal'),'A later refusal replaces the previous reason.');
  const targetKey=initialTarget.dataset.key;
  liveRow(targetKey).querySelector('.list-toggle').click();
  await until(()=>recordDetails(liveRow(targetKey))?.querySelector('[data-review-form]'),
   'the target Change body');
  const target=liveRow(targetKey);
  const form=recordDetails(target).querySelector('form'),savedResponse=holdPost();
  form.requestSubmit(form.querySelector('[value="works"]'));
  check(leavingNow(target),'A save starts the departure before the response.');
  savedResponse.save();
  await flush();
  check(!form.querySelector('[data-send-status]').textContent,'The saved row shows no action status.');
  const status=document.querySelector('[data-hidden-action-status]');
  check(status.hidden && !status.textContent,'The hidden row leaves no action status.');
  check(!visibleStatus('Not saved: Fixture newer refusal'),'saving another card clears the failure notice');
  await new Promise(resolve=>nativeTimeout(resolve,950));
  check(target.hidden,'the saved row leaves the view');
  check(status.hidden && !status.textContent,'The animation leaves no action status.');
  check(document.querySelector('[data-filter-row="show"] [data-filter-value="open"] b').textContent==='3','the count excludes the saved row');
  check(rows().every(row=>!recordIsOpen(row)),'no next card opens');
  choose('list-show','all');
  const allTarget=rows().find(row=>row.dataset.type==='change' && row!==target);allTarget.querySelector('.list-toggle').click();await readyReviewCard(allTarget);
  const allForm=recordDetails(allTarget).querySelector('form');allForm.requestSubmit(allForm.querySelector('[value="works"]'));
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  check(allTarget.hidden,'F010: the saved row leaves All');
  location.hash=new URLSearchParams({item:target.dataset.key});await flush();
  const reopened=liveRow(target.dataset.key);
  check(!reopened, 'A same-tab link keeps the completed Change hidden.');
  check(!liveRow(allTarget.dataset.key),'F010: the unlinked saved Change stays hidden in All.');
  const reloaded=document.createElement('iframe');reloaded.src='/console/review.html#'+new URLSearchParams({item:target.dataset.key});
  const reloadComplete=new Promise(resolve=>reloaded.addEventListener('load',resolve,{once:true}));document.body.append(reloaded);await reloadComplete;
  const linkedTarget=reloaded.contentDocument.querySelector('[data-key="'+target.dataset.key+'"]');
  check(linkedTarget.dataset.state==='Open' && !linkedTarget.hidden && recordIsOpen(linkedTarget),
   'The fixture still shows the Change as Open on a fresh load.');
  check(showValue(reloaded.contentDocument)==='open' && filterControl.ownerId(selectedOwner(reloaded.contentDocument))==='' &&
    reloaded.contentDocument.getElementById('list-search').value==='','A bare link keeps the default filters for an Open Change.');
  reloaded.remove();
  const retained={item:fresh.dataset.key,type:'question',owner:fresh.dataset.owner,show:'open',q:'Q1',sort:'Age',dir:'desc'};
  location.hash=new URLSearchParams(retained);await flush();
  fresh=rows().find(row=>row.dataset.type==='question');
  check(filterControl.ownerId(selectedOwner())===retained.owner && showValue()==='open' &&
    document.getElementById('list-search').value==='Q1' && document.querySelector('[data-filter-row="type"] [data-filter-value="question"]').getAttribute('aria-pressed')==='true',
    'F014: restore the filters with an open row');
  check(!fresh.hidden && recordIsOpen(fresh),'F014: restore the open row');
  check(document.querySelector('[data-sort="5"]').parentElement.getAttribute('aria-sort')==='descending','F014: restore the sort');

  const legacy=document.createElement('iframe');
  legacy.src='/console/changes.html?type=questions#'+new URLSearchParams({showrunner:'page-owner',show:'Waiting',q:'layout',sort:'Age',dir:'desc'});
  const loaded=new Promise(resolve=>legacy.addEventListener('load',resolve,{once:true}));document.body.append(legacy);await loaded;
  await flush();await flush();
  const restored=legacy.contentDocument;
  check(filterControl.ownerId(selectedOwner(restored))===fresh.dataset.owner &&
    selectedOwner(restored).textContent==='page-owner 1' && showValue(restored)==='open' &&
    restored.getElementById('list-search').value==='layout' && restored.querySelector('[data-filter-row="type"] [data-filter-value="question"]').getAttribute('aria-pressed')==='true',
    'F012: a legacy link selects Open and retains the other filters.');
  check(restored.querySelector('[data-sort="5"]').parentElement.getAttribute('aria-sort')==='descending','F012: the legacy link keeps its sort');
  legacy.remove();

  check(document.documentElement.scrollWidth<=innerWidth,'no horizontal overflow');
  if(innerWidth<960) check(getComputedStyle(shown()[0]).display==='block','phone rows are cards');
  await readyReviewCard(fresh);
  const laterToggle=recordDetails(fresh).querySelector('[data-later-toggle]'),laterMenu=recordDetails(fresh).querySelector('[data-later-menu]');
  const menuNow=Date.now;Date.now=()=>Date.parse('2026-11-23T04:53:00Z');
  laterToggle.click();
  Date.now=menuNow;
  const menuItems=()=>[...laterMenu.querySelectorAll('[role=menuitem]')].filter(item=>!item.hidden);
  const presetTime=name=>laterMenu.querySelector('[data-later-preset="'+name+'"] small').textContent;
  check(!laterMenu.hidden && laterToggle.getAttribute('aria-expanded')==='true' && document.activeElement===menuItems()[0],
    'Later opens its menu with the first preset in focus');
  check(presetTime('soon')==='' && presetTime('morning')==='09:00' &&
    presetTime('week')==='Mon 09:00','Later shows relative choices without dates');
  menuItems()[0].dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true}));
  check(document.activeElement===menuItems().at(-1),'ArrowUp moves from the first item to the last');
  menuItems().at(-1).dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));
  check(document.activeElement===menuItems()[0],'ArrowDown moves from the last item to the first');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();
  check(fresh.isConnected && !laterMenu.hidden,'an open Later menu holds the page refresh');
  const chooserDraft=recordDetails(fresh).querySelector('[name="later"]');chooserDraft.value='';
  chooserDraft.dispatchEvent(new Event('input',{bubbles:true}));
  const chooserNow=Date.now;Date.now=()=>Date.parse('2026-11-30T15:00:00Z');
  laterMenu.querySelector('[data-later-choose]').click();
  const calendar=laterMenu.querySelector('[role=grid]'),timeField=laterMenu.querySelector('[data-later-time]');
  check(!laterMenu.querySelector('[data-later-custom]').hidden && document.activeElement===calendar?.querySelector('[tabindex="0"]'),
    'Pick a date shows the calendar with the selected day in focus');
  const times=document.getElementById(timeField.getAttribute('aria-controls'));
  check([...times.children].slice(0,3).map(option=>option.textContent).join()==='10:15,10:30,10:45',
    'The time list starts at the next quarter hour');
  const chooserChecks=['the grid opens on tomorrow in the user zone','arrows move the selected day across months',
    'a past day cannot be picked','Save is disabled for a time before now','the summary shows the selected wall time',
    'the chooser fits within the menu and viewport'];
  let submitter='';const watch=event=>{submitter=event.submitter.value;event.preventDefault();event.stopImmediatePropagation();};
  recordDetails(fresh).querySelector('form').addEventListener('submit',watch,{capture:true});
  if(calendar){
   const wall=()=>recordDetails(fresh).querySelector('[name="later"]').value;
   const selected=()=>calendar.querySelector('[tabindex="0"]');
   check(wall()==='2026-12-01T09:00' && timeField.value==='09:00',chooserChecks[0]);
   selected().dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true,cancelable:true}));
   check(wall()==='2026-11-30T09:00' && laterMenu.querySelector('[data-later-month]').textContent==='November 2026',chooserChecks[1]);
   const past=calendar.querySelector('[data-date="2026-11-29"]');past.click();
   check(past.disabled && wall()==='2026-11-30T09:00',chooserChecks[2]);
   check(laterMenu.querySelector('[value="later"]').disabled,chooserChecks[3]);
   selected().dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));
   selected().dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true,cancelable:true}));
   check(wall()==='2026-12-08T09:00','ArrowDown moves the selected day by one week');
   selected().dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true,cancelable:true}));
   timeField.value='10:15';timeField.dispatchEvent(new Event('input',{bubbles:true}));
   check(wall()==='2026-12-01T10:15' && laterMenu.querySelector('[data-later-summary]').textContent==='Hides until Tue 1 Dec 10:15',chooserChecks[4]);
   const box=laterMenu.getBoundingClientRect(),grid=calendar.getBoundingClientRect();
   check(box.left>=0 && box.right<=innerWidth && grid.left>=box.left && grid.right<=box.right &&
     laterMenu.scrollWidth<=laterMenu.clientWidth,chooserChecks[5]);
   selected().dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
  }else{
   chooserChecks.forEach(label=>check(false,label));
   check(false,'ArrowDown moves the selected day by one week');
   document.activeElement.value='2099-01-01T09:00';
   document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
  }
  recordDetails(fresh).querySelector('form').removeEventListener('submit',watch,{capture:true});
  check(submitter==='later','Enter in the chooser saves it as Later');
  document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  check(laterMenu.hidden && laterMenu.querySelector('[data-later-custom]').hidden && document.activeElement===laterToggle,
    'Escape closes the menu and returns focus to Later');
  laterToggle.click();
  // The Review card is a modal dialog, so the focus goes to a field in the card outside the Later menu.
  const outside=[...recordDetails(fresh).querySelectorAll('textarea,input[type=text]')]
   .find(field=>!laterMenu.contains(field) && field.getClientRects().length),previous=document.activeElement;
  outside.focus();
  // Firefox screenshot mode moves focus without sending focus events.
  if(!document.hasFocus()) previous.dispatchEvent(new FocusEvent('focusout',{bubbles:true,relatedTarget:outside}));
  check(laterMenu.hidden && document.activeElement===outside && !dateTimePickerOpen(fresh),
    'Focus outside Later closes the menu and releases the refresh hold');
  Date.now=()=>Date.parse('2026-11-23T04:59:00Z');
  laterToggle.click();
  const week=laterMenu.querySelector('[data-later-preset="week"]'),laterPosts=window.posts.length;
  check(presetTime('week')==='Mon 09:00','The relative choice shows Monday at 09:00 before midnight');
  Date.now=()=>Date.parse('2026-11-23T05:01:00Z');
  const laterResponse=holdPost();
  week.click();
  check(laterMenu.hidden && window.posts.length===laterPosts+1,'One click after midnight submits Later');
  Date.now=chooserNow;
  check(leavingNow(fresh),'Later starts the departure before the response');
  check(document.querySelector('[data-filter-row="show"] [data-filter-value="open"] b').textContent==='0' &&
    filterControl.ownerId(selectedOwner())===retained.owner,'Later updates the count and keeps the owner while the request runs');
  laterResponse.save();
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  check(window.posts.at(-1).get('outcome')==='later' && window.posts.at(-1).get('later')==='2026-11-30T14:00:00.000Z',
    'The first click sends the deadline for the new day');
  check(fresh.hidden,'Later removes the question from the page');
  check(document.querySelector('[data-filter-row="show"] [data-filter-value="open"] b').textContent==='0','Later removes the question from the filtered count');
  const nativeNow=Date.now;Date.now=()=>new Date('2099-01-01T09:00').getTime()+1000;
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  let returned=rows().find(row=>row.dataset.type==='question');
  check(!returned.hidden && returned.dataset.state==='Open','Later returns the question with its original state');
  Date.now=nativeNow;
  returned.querySelector('.list-toggle').click();await readyReviewCard(returned);
  const answerForm=recordDetails(returned).querySelector('form');
  check(answerForm.elements.feedback.value==='' && answerForm.elements.later.value==='',
    'F081: a successful save and unmount discard the submitted draft.');
  answerForm.elements.later.value='';
  answerForm.requestSubmit(answerForm.querySelector('[value="later"]'));await flush();
  answerForm.requestSubmit(answerForm.querySelector('[value="answer"]'));await flush();
  check(!answerForm.querySelector('[data-send-status]').textContent,
    'F023: Answer leaves no action status after an empty Later.');
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  choose('list-show','open');choose('list-search','','input');choose('list-owner','');
  document.querySelector('[data-filter-row="type"] [data-filter-value="all"]').click();
  const pending=rows().find(row=>row.dataset.number==='page-owner/c3'),pendingKey=pending.dataset.key;
  pending.querySelector('.list-toggle').click();await readyReviewCard(pending);
  const pendingForm=recordDetails(pending).querySelector('form');pendingForm.elements.feedback.value='Wait for a new answer.';
  const initialCount=Number(document.querySelector('[data-filter-row="show"] [data-filter-value="open"] b').textContent);
  const questionResponse=holdPost();
  pendingForm.requestSubmit(pendingForm.querySelector('[value="question"]'));
  check(window.posts.at(-1).get('outcome')==='question' && recordIsOpen(pending) &&
    Number(document.querySelector('[data-filter-row="show"] [data-filter-value="open"] b').textContent)===initialCount-1,
    'Ask question keeps the row open and updates the count before the response');
  questionResponse.save();
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  const sent=liveRow(pendingKey);
  if(recordIsOpen(sent)) sent.querySelector('.list-toggle').click();
  const revision=pending.dataset.updated;
  for(const [state,updated,revealed] of [
    ['Open',revision,false],['Validating',revision,false],
    ['Validating','2099-02-01T09:00:00+00:00',false],['Open','2099-02-01T10:00:00+00:00',true]]){
   const update=new DOMParser().parseFromString(original,'text/html');
   const payload=update.querySelector('[data-review-index]'),entries=JSON.parse(payload.textContent);
   const entry=entries.r.find(entry=>entry[0]===pendingKey);entry[6]=['Open','Waiting','Validating'].indexOf(state);entry[8]=updated;
   payload.textContent=JSON.stringify(entries);
   window.refreshMarkup=update.documentElement.outerHTML;
   dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
   const shownPending=rows().find(row=>row.dataset.key===pendingKey);
   check(Boolean(shownPending && !shownPending.hidden)===revealed,'F021: state and revision control return for '+state+' '+updated);
   check(Number(document.querySelector('[data-filter-row="show"] [data-filter-value="open"] b').textContent)===initialCount-(revealed?0:1),
     'F021: only the new Open response restores the count');
  }
  const beforeShowRequests=window.refreshRequests.length;
  choose('list-show','all');await flush();await flush();
  check(document.querySelector('[data-filter-row="show"] [data-filter-value="all"] b').textContent==='2' &&
    rows().every(row=>row.dataset.state!=='Done'),
    'All excludes completed records, saved verdicts and the saved answer.');
  check(window.refreshRequests.length===beforeShowRequests,
    'the Show chips select rows without a request');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(new URL(window.refreshRequests.at(-1),location.href).searchParams.has('version') &&
    showValue()==='all','the refresh keeps the selected Show view');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        self_root = self.root
        probe = (probe.replace('WAIT_FOR_BROWSER_STATE', WAIT_FOR_BROWSER_STATE)
                 .replace('HANG_GUARD_MS', str(TEST_HANG_GUARD_SECS * 1000)))
        body = source.replace('<head>','<head>'+setup,1).replace('</body>',probe+'</body>').encode()
        for width in (1440,390):
            with self.subTest(width=width):
                results: list[list[str]] = []
                complete = threading.Event()
                class Probe(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        if self.path.startswith('/console/changes.html'):
                            self.send_response(303)
                            self.send_header('Location', review_page.legacy_url('/console/changes.html', self.path.partition('?')[2]))
                            self.end_headers()
                            return
                        self.send_response(200)
                        self.send_header('Content-Type','text/html; charset=utf-8')
                        self.end_headers()
                        if self.path == '/complete':
                            if not complete.wait(TEST_HANG_GUARD_SECS):
                                raise TimeoutError('The browser did not report the Review filter results.')
                        else:
                            _ = self.wfile.write(review_page.render(self_root, show=parse_qs(self.path.partition('?')[2]).get('show', ['open'])[0])
                                if self.path.startswith('/console/review.html') else
                                body if self.path == '/page' else b'<iframe src="/page" style="border:0;width:100vw;height:100vh"></iframe><img hidden src="/complete">')
                    def do_POST(self) -> None:
                        results.append(cast(list[str],json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                        complete.set()
                        self.send_response(204)
                        self.end_headers()
                    @override
                    def log_message(self, format: str, *args: object) -> None:
                        pass
                with ThreadingHTTPServer(('127.0.0.1',0),Probe) as server:
                    thread=threading.Thread(target=server.serve_forever,daemon=True)
                    thread.start()
                    try:
                        with tempfile.TemporaryDirectory(dir=self.fixture.temporary) as profile:
                            done=run_process_group(['firefox','--headless','--no-remote','--profile',profile,
                                '--window-size',f'{width},900','--screenshot',str(self.fixture.temporary/f'review-{width}.png'),
                                f'http://127.0.0.1:{server.server_port}/'],timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(done.returncode,0,done.stderr)
                    finally:
                        server.shutdown()
                        thread.join(TEST_HANG_GUARD_SECS)
                self.assertEqual(results,[[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_later_focus_moves_to_the_calendar_when_previous_month_disables(self) -> None:
        _ = self.question()
        with patch.dict(os.environ, {'USER_ZONE': 'UTC'}):
            source = browser_markup(self.root)
        setup = r"""<script>
window.setInterval=()=>0;
Date.now=()=>Date.parse('2026-11-30T15:00:00Z');
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 try{
  const row=document.querySelector('[data-type="question"]');
  row.querySelector('.list-toggle').click();
  const form=await readyReviewCard(row);
  form.querySelector('[data-later-toggle]').click();
  form.querySelector('[data-later-choose]').click();
  const previous=form.querySelector('[data-later-month-step="-1"]');
  const grid=form.querySelector('[data-later-grid]');
  previous.focus();
  check(document.activeElement===previous,'Previous month has focus in December');
  previous.click();
  const day=grid.querySelector('button:not(:disabled)[tabindex="0"]');
  check(previous.disabled && day?.dataset.date==='2026-11-30','November has an enabled day');
  check(document.activeElement===day,'Focus moves from Previous month to the enabled day');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        markup = source.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_later_time_displays_and_saves_24_hour_time(self) -> None:
        _ = self.question()
        with patch.dict(os.environ, {'USER_ZONE': 'UTC'}):
            source = browser_markup(self.root)
        setup = r"""<script>
const nativeFetch=fetch,posts=[];
window.setInterval=()=>0;
Date.now=()=>Date.parse('2026-11-30T15:00:00Z');
window.fetch=async(_url,options={})=>{
 if(options.method==='POST') posts.push(new URLSearchParams(options.body));
 return {ok:true,json:async()=>({answer:'later'})};
};
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 try{
  const row=document.querySelector('[data-type="question"]');
  row.querySelector('.list-toggle').click();await readyReviewCard(row);
  const form=recordDetails(row).querySelector('form');
  const field=form.querySelector('[data-later-time]'),save=form.querySelector('[value="later"]');
  form.elements.later.value='2026-12-01T17:30';
  form.querySelector('[data-later-toggle]').click();form.querySelector('[data-later-choose]').click();
  check(field.type==='text' && field.value==='17:30',
   'The 17:30 draft shows 17:30 without AM or PM (input type: '+field.type+')');
  const key=name=>field.dispatchEvent(new KeyboardEvent('keydown',{key:name,bubbles:true,cancelable:true}));
  const type=value=>{field.value=value;field.dispatchEvent(new Event('input',{bubbles:true}));};
  const times=form.querySelector('[data-later-times]'),menu=form.querySelector('[data-later-menu]');
  check(field.getAttribute('aria-controls')===times.id && times.hidden && !field.form,
   'Time controls the suggestion list and has no implicit form action');
  field.focus();
  key('ArrowDown');
  for(const name of ['ArrowLeft','ArrowRight','Home','End','Tab']){
   check(key(name) && field.value==='17:30' && form.elements.later.value==='2026-12-01T17:30',
    'The page leaves '+name+' to the browser and keeps the draft');
  }
  key('Escape');
  check(times.hidden && !menu.hidden && !posts.length,'Escape closes only the suggestion list');
  key('ArrowDown');key('Enter');
  check(times.hidden && !menu.hidden && field.value==='15:15' && !posts.length,
   'Enter accepts the active suggestion without saving the draft');
  for(const value of ['9:00','0900','09:00']){
   field.focus();type(value);
   check(field.getAttribute('aria-invalid')==='false' && !save.disabled &&
    form.elements.later.value==='2026-12-01T09:00','The draft accepts '+value);
   field.dispatchEvent(new FocusEvent('blur'));
   check(field.value==='09:00','Blur shows 09:00 for '+value);
  }
  field.focus();
  for(const value of ['','24:00','12:60','9:00 AM','17:30:00']){
   type(value);key('Enter');
   check(field.getAttribute('aria-invalid')==='true' && save.disabled && !form.elements.later.value && !posts.length,
    'An invalid time disables Save: '+JSON.stringify(value));
  }
  const error=document.createElement('span');error.style.color='var(--error)';form.append(error);
  const errorColor=getComputedStyle(error).color,style=getComputedStyle(field);
  check(style.color===errorColor && style.borderTopColor===errorColor,'An invalid time uses the console error color');
  type('1730');
  save.focus();
  if(!document.hasFocus()) field.dispatchEvent(new FocusEvent('blur',{relatedTarget:save}));
  check(field.value==='17:30' && field.getAttribute('aria-invalid')==='false' && !save.disabled && !posts.length,
   'Moving from Time to Save formats 17:30 and enables Save');
  field.focus();key('Enter');
  check(posts.length===1 && posts[0].get('later')==='2026-12-01T17:30:00.000Z',
   'Enter in Time sends T17:30 in UTC when the list is closed');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        markup = source.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_later_retains_drafts_and_resolves_daylight_saving_times(self) -> None:
        _ = self.question()
        with patch.dict(os.environ, {'USER_ZONE': 'America/New_York'}):
            source = browser_markup(self.root)
        setup = r"""<script>
const nativeFetch=fetch,nativeTimeout=setTimeout;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.posts=[];
window.fetch=async(_url,options={})=>{
 if(options.method==='POST'){
  window.posts.push(new URLSearchParams(options.body));return {ok:true,json:async()=>({answer:'later'})};
 }
 return {ok:true,text:async()=>MARKUP};
};
Date.now=()=>Date.parse(SCENARIO==='gap' ? '2027-03-13T15:00:00Z' :
 SCENARIO==='repeat' ? '2027-11-06T14:00:00Z' : '2026-11-30T15:00:00Z');
</script>""".replace('MARKUP', json.dumps(source).replace('</', '<\\/'))
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>nativeTimeout(resolve,60));
 const form=()=>recordDetails(document.querySelector('[data-type="question"]')).querySelector('[data-review-form]');
 const open=()=>{form().querySelector('[data-later-toggle]').click();form().querySelector('[data-later-choose]').click();};
 const close=()=>form().querySelector('[data-later-grid] [tabindex="0"]')
  .dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 const time=value=>{const field=form().querySelector('[data-later-time]');field.value=value;field.dispatchEvent(new Event('input',{bubbles:true}));};
 const selected=()=>form().querySelector('[role=grid] [aria-selected="true"]')?.dataset.date;
 try{
  const question=document.querySelector('[data-type="question"]');question.querySelector('.list-toggle').click();await readyReviewCard(question);
  check(document.querySelector('[data-review]').dataset.zone==='America/New_York','the fixture sets America/New_York');
  if(SCENARIO==='draft'){
   open();
   form().querySelector('[role=grid] [tabindex="0"]').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));
   time('10:15');close();open();
   check(form().elements.later.value==='2026-12-02T10:15' && selected()==='2026-12-02' &&
    form().querySelector('[data-later-time]').value==='10:15','F004: Escape and reopen retain the selected day and time');
   time('11:45');
   const restored=form().elements.later.value;
   close();
   dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
   await readyReviewCard(document.querySelector('[data-type="question"]'));
   check(form().elements.later.value===restored,'F004: refresh restores the Later draft');
   open();
   check(form().elements.later.value===restored && selected()===restored.slice(0,10) &&
    form().querySelector('[data-later-time]').value==='11:45','F004: opening the chooser retains the restored draft');
  }else{
   const gap=SCENARIO==='gap',clock=gap ? '02:30' : '01:30';
   const expected=gap ? '2027-03-14T07:30:00.000Z' : '2027-11-07T05:30:00.000Z';
   const content=document.querySelector('[data-review]'),settings=JSON.parse(content.dataset.later);
   settings.morning=gap ? 150 : 90;content.dataset.later=JSON.stringify(settings);
   form().querySelector('[data-later-toggle]').click();
   const preset=form().querySelector('[data-later-preset="morning"]');
   const at=parkPresetTime(preset.dataset.laterPreset,Date.now(),content.dataset.zone,settings);
   check(new Date(at).toISOString()===expected,'F005: '+SCENARIO+' preset resolves at activation');
   form().querySelector('[data-later-choose]').click();time(clock);
   check(form().querySelector('[data-later-summary]').textContent===
    (gap ? 'Hides until Sun 14 Mar 03:30' : 'Hides until Sun 7 Nov 01:30'),
    'F005: '+SCENARIO+' summary shows the saved instant');
   form().requestSubmit(form().querySelector('[value="later"]'));await flush();
   check(window.posts.at(-1)?.get('later')===expected,'F005: '+SCENARIO+' Save sends the compatible instant');
  }
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        for scenario in ('draft', 'gap', 'repeat'):
            with self.subTest(scenario=scenario):
                page = source.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>')
                body = page.replace('SCENARIO', json.dumps(scenario)).encode()
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        if self.path == '/complete':
                            _ = complete.wait(20)
                        else:
                            _ = self.wfile.write(body if self.path == '/page' else
                                b'<iframe src="/page" style="width:100vw;height:100vh"></iframe><img hidden src="/complete">')

                    def do_POST(self) -> None:
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
                        with tempfile.TemporaryDirectory(dir=self.fixture.temporary) as profile:
                            done = run_process_group(['firefox', '--headless', '--no-remote', '--profile', profile,
                                '--window-size', '1440,900', '--screenshot', str(self.fixture.temporary/'later-time.png'),
                                f'http://127.0.0.1:{server.server_port}/'], timeout=30)
                        self.assertEqual(done.returncode, 0, done.stderr)
                    finally:
                        server.shutdown()
                        thread.join(TEST_HANG_GUARD_SECS)
                self.assertEqual(results, [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_later_chooser_validates_restored_drafts(self) -> None:
        _ = self.question()
        with patch.dict(os.environ, {'USER_ZONE': 'UTC'}):
            source = browser_markup(self.root)
        setup = r"""<script>
const nativeFetch=fetch;
window.setInterval=()=>0;
Date.now=()=>Date.parse('2026-11-30T15:00:00Z');
window.fetch=async()=>({headers:new Headers(),ok:true,text:async()=>MARKUP});
</script>""".replace('MARKUP', json.dumps(source).replace('</', '<\\/'))
        probe = r"""<script>
(async()=>{
 const failures=[],errors=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const row=()=>document.querySelector('[data-type="question"]');
 addEventListener('error',event=>errors.push(event.error?.name || event.message));
 try{
  row().querySelector('.list-toggle').click();await readyReviewCard(row());
  const fallback='2026-12-01T09:00';
  for(const [draft,expected] of [
   ['2026-12-08T7',fallback],['2026-12-08T24:00',fallback],['2026-12-08T23:60',fallback],
   ['2026-12-08T',fallback],['2027-02-29T09:00',fallback],['2026-12-08T1730','2026-12-08T17:30']
  ]){
   const original=row(),version=original.dataset.bodyVersion;
   const wall=recordDetails(original).querySelector('[name="later"]');
   wall.value=draft;wall.dispatchEvent(new Event('input',{bubbles:true}));
   dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
   const end=performance.now()+8000;
   while(row().dataset.bodyLoaded!=='true' || row().dataset.bodyVersion===version){
    if(performance.now()>end) throw new Error('The card did not refresh.');
    await new Promise(resolve=>requestAnimationFrame(resolve));
   }
   const current=row(),form=await readyReviewCard(current);
   check(form===wall.form,'Refresh keeps the Later form.');
   check(current!==original && form.elements.later.value===draft,'Refresh restores '+draft);
   const toggle=form.querySelector('[data-later-toggle]'),chooser=form.querySelector('[data-later-custom]');
   toggle.click();form.querySelector('[data-later-choose]').click();
   const selected=chooser.querySelector('[aria-selected="true"]')?.dataset.date;
   const field=chooser.querySelector('[data-later-time]'),save=chooser.querySelector('[value="later"]');
   check(!errors.length,'Opening '+draft+' raises '+errors.join(', '));errors.length=0;
   check(!chooser.hidden && selected===expected.slice(0,10) && field.value===expected.slice(11) &&
    form.elements.later.value===expected && !save.disabled,'Opening '+draft+' selects '+expected);
   toggle.click();
  }
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        markup = source.replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_review_draws_the_same_body_as_the_single_card(self) -> None:
        key = 'page-owner/C1'
        snapshot = review_page.build_snapshot(self.root)
        single = snapshot.body(key, view=review_page.ReviewBodyView.CARD).decode()
        probe = r"""<script>
(async()=>{
 const failures=[];
 try{
  const row=document.querySelector('[data-key="page-owner/C1"]');
  row.querySelector('.list-toggle').click();
  const form=await readyReviewCard(row);
  const template=new DOMParser().parseFromString(SINGLE,'text/html').querySelector('[data-card-body]');
  const rendered=recordDetails(row).cloneNode(true);
  // The picker adds the suggestion list ID when it binds the form.
  rendered.querySelector('[data-later-time]').removeAttribute('aria-controls');
  rendered.querySelector('[data-later-times]').removeAttribute('id');
  for(const selector of ['[data-review-body]','.list-log','.review-actions']){
   if(rendered.querySelector(selector).innerHTML!==template.content.querySelector(selector).innerHTML)
    failures.push('Review and the single card draw different '+selector+'.');
  }
  if(!form.elements.revision.value) failures.push('The Review action has no revision.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>""".replace("SINGLE", json.dumps(single).replace('</', '<\\/'))
        markup = browser_markup(self.root).replace('</body>', probe + '</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_uses_the_shared_status_with_the_real_loader(self) -> None:
        _ = self.question()
        _ = self.investigation()
        assert_status_states(self, browser_markup(self.root),15)


if __name__ == '__main__':
    _ = unittest.main()
