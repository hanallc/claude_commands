"""Check the shared review records, signed actions and table behavior."""
from __future__ import annotations
from scripts.lint.memory_test_support import write_config_fixture

from scripts.production.change_review_test_support import pictured_changes

import shutil
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Self, TextIO, cast
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import atomic_file, conf, urgency
from scripts.lib.user_zone import user_zone
import unittest
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from scripts.agents.remote import local_machine
from scripts.nightly_review import candidates
from scripts.production.production_doc import read_production
from scripts.production.issues import Issue
from scripts.production import issues as issue_store
from scripts.production.staff_test_support import fixture_member
from scripts.production import change_review, change_routing, footer_counts, investigation, next_item, proposal_issue, staff, update_registration
from scripts.review_links import change_browser, change_page, console, page_frame, record_table, review_items, review_page
from scripts.review_links import question_record
from scripts.review_links import list_page, staff_names, test_change_page
from scripts.review_links import efficiency_reviews, new_item_dialog, review_actions, review_events, review_later, review_submissions
from scripts.review_links.test_accounts_page import firefox_probe_results
from scripts.review_links.test_page_frame import assert_status_states
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
        entries.append(entry)
    return entries



BODY_READY_SCRIPT = r"""async function readyReviewCard(row){
 if(!row) throw new Error('The summary row is missing.');
 const end=performance.now()+8000;
 while(!row.querySelector('[data-review-form]')){
  if(performance.now()>end) throw new Error('The card body did not load: '+row.dataset.key);
  await new Promise(resolve=>requestAnimationFrame(resolve));
 }
 return row.querySelector('[data-review-form]');
}
"""

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
            self.assertIn(f'data-owner-name="{html.escape(name)}" aria-pressed="false">{markup} <b>', owner_menu)
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

    def card_details(self, item: review_items.ReviewItem) -> str:
        snapshot = review_page.build_snapshot(self.root)
        assets: dict[str, str] = {}
        review_page.retain_asset(item, assets)
        inputs = review_page.ReviewBodyInputs(snapshot.browser, snapshot.members, snapshot.words_rows,
            'In 1 hour', assets, snapshot.zone, snapshot.names)
        return review_page.details(item, inputs)

    @override
    def setUp(self) -> None:
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
            "review_words_rows=4\nlater_soon_minutes=60\nlater_evening=18:00\n"
            + "later_evening_until=17:00\nlater_morning=09:00\nlater_week_day=Monday\nlater_week_time=09:00\n")
        self.root = self.fixture.root
        _ = change_page.console_folder(self.root)
        self.status = change_page.DeliveryStatus(self.root)
        document = self.fixture.production.read_text()
        _ = self.fixture.production.write_text(document+'- **Merge branch:** main\n- **Log:** progress.log\n')

    def question(self, title: str = 'Which layout should the page use?') -> str:
        return self.fixture.file_user_question(title)

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

    def test_sent_rows_keep_native_types_and_return_to_open(self) -> None:
        owner = self.ready_submission_recipient()
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
                self.assertEqual(entries[created.key].get('waitingWhy'), f'{recipient.name} answers this')
                self.assertNotIn(created.saved.reply_token.encode(), snapshot.body(created.key))
                self.assertNotIn(created.saved.key.encode(), snapshot.index_response())
                self.assertNotIn(created.key, initial.records)
        snapshot = review_page.build_snapshot(self.root)
        self.assertIn(b'data-type-chip="bug"', snapshot.page)
        self.assertEqual([item.key for item in review_items.read(
            self.root, kinds=(review_items.ReviewType.BUG,))], [published[-1].key])
        self.assertIn(proposals, snapshot.source_stamps)
        for created in published:
            with self.subTest(key=created.key):
                self.assertEqual(snapshot.records[created.key], cache.get().records[created.key])
                self.assertNotIn(created.key, {item.key for item in review_items.read_open(self.root)})
                self.assertTrue(change_page.answer_item(created.saved.key, 'The check passed.', created.saved.reply_token, self.status))
        open_keys = {item.key for item in review_items.read_open(self.root)}
        self.assertTrue({item.key for item in published} <= open_keys)
        self.assertEqual(review_items.investigations(self.fixture.production, staff.snapshot()), 2)
        reopened = review_page.build_snapshot(self.root)
        self.assertEqual([reopened.records[created.key].type for created in published],
                         [review_items.ReviewType.RESPONSE, *[case[2] for case in cases[1:]]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_old_review_tab_refreshes_a_response_as_a_question(self) -> None:
        owner = self.ready_submission_recipient()
        request = self.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id)
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        created = cache.create_submission(request)
        before = cache.get().render(show='all').decode()
        self.assertTrue(review_submissions.answer(created.saved.key, 'Use the wide layout.', created.saved.reply_token))
        fresh = review_page.build_snapshot(self.root)
        self.assertEqual(fresh.records[created.key].type, review_items.ReviewType.RESPONSE)

        source = subprocess.check_output(
            ['git', 'show', 'eb6e3239d:scripts/review_links/review_page.py'],
            cwd=Path(__file__).resolve().parents[2], text=True)
        start = '(reviewPlace => {'
        shared_script = review_page.SCRIPT.split(start, 1)[0]
        old_script = start + source.split("r'''"+start, 1)[1].rsplit("''')", 1)[0]
        markup = before.replace(review_page.SCRIPT, shared_script+old_script, 1)
        markup = re.sub(r'<button id="review-type-response"[^>]*>.*?</button>', '', markup, count=1)
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
 const questions=review.querySelector('[data-type-chip="question"]');
 questions.click();
 review.querySelector('[data-show-chip="all"]').click();
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
    def test_response_card_hides_validating_and_keeps_the_filters_on_submit(self) -> None:
        owner = self.ready_submission_recipient()
        request = replace(self.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id),
                          words='Which layout should I use?')
        saved = review_submissions.submit(request)
        self.assertTrue(review_submissions.answer(saved.key, 'Use the wide layout.', saved.reply_token))
        source = browser_markup(self.root)
        for action, state in (('read', 'Done'), ('question', 'Waiting')):
            with self.subTest(action=action):
                self.check_card_motion(source, 'const action='+json.dumps(action)+',state='+json.dumps(state)+r""";
 const chip=(name,value)=>document.querySelector('[data-'+name+'-chip="'+value+'"]');
 const selected=name=>document.querySelector('[data-'+name+'-chip][aria-pressed="true"]');
 chip('show','Validating').click();
 const responses=chip('type','response');responses.click();
 check(responses.previousElementSibling===chip('type','question') &&
  responses.nextElementSibling===chip('type','change') && responses.textContent.trim()==='Responses 1',
  'Responses has a count between Questions and Changes.');
 check(chip('show','Validating').hidden && !chip('show','Done') && selected('show')===chip('show','open'),
  'Responses uses Open when Validating was selected.');
 const row=rows().find(item=>item.dataset.type==='response');
 chip('owner',row.dataset.owner).click();
 chip('show','all').click();
 const words=document.querySelector('#list-search');
 words.value='which layout';words.dispatchEvent(new Event('input',{bubbles:true}));
 document.querySelector('[data-sort="3"]').click();
 const place=()=>{const params=new URLSearchParams(location.hash.slice(1));params.delete('item');return params.toString();};
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
 check(form.elements.feedback.placeholder==='Type your words for Ask a follow-up.',
  'The placeholder says which action sends the words.');
 const panel=form.closest('[data-list-item]'),id=panelId(panel);
 if(action==='question') form.elements.feedback.value='Can I use it on a phone?';
 form.requestSubmit(form.querySelector('[value="'+action+'"]'));
 const motion=copy(id)?.getAnimations()[0];
 if(!motion || !posts.has(row.dataset.key)) throw new Error('The Response save must start the fade.');
 posts.get(row.dataset.key)({ok:true,json:async()=>({answer:'saved',state,type:action==='read'?'response':'question',later:0})});
 motion.finish();await frame();
 check(postCount===1 && !rows().some(item=>item.dataset.type==='response'),
  'The Response leaves the view after one submit.');
 check(place()===before && selected('owner').dataset.ownerChip===row.dataset.owner &&
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
        self.assertIn(created.key, {item.key for item in review_items.read_open(self.root)})

    def test_bug_card_shows_captured_page_state_and_screenshot(self) -> None:
        import base64

        from scripts.review_links.test_review_submissions import PNG

        owner = self.ready_submission_recipient()
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(self.root, start_worker=False)
        evidence = new_item_dialog.PageDataEvidence("Review", (("Page", "Console"),))
        state = new_item_dialog.BugPageState(
            "http://127.0.0.1/console/review.html#owner=builds", new_item_dialog.PageViewport(1280, 800, True),
            2.4, ("Could not load brain.",), (), "Type All · State Open · Owner <builds>", True)
        request = self.submission_request(new_item_dialog.SubmissionKind.BUG, owner.id)
        created = cache.create_submission(replace(request, page="review", evidence=evidence, page_state=state))
        self.assertNotIn(b"Screenshot of Review", cache.get().body(created.key))
        self.assertTrue(cache.attach_page_screenshot(created.saved, base64.b64decode(PNG)))
        snapshot = review_page.build_snapshot(self.root)
        self.assertEqual(snapshot.records[created.key].state, review_items.ReviewState.WAITING)
        card = snapshot.body(created.key).decode()
        self.assertIn('<h4>Page state</h4>', card)
        self.assertIn('<dt>Page</dt><dd>Review</dd>', card)
        self.assertIn('<dt>Filters</dt><dd>Type All · State Open · Owner &lt;builds&gt;</dd>', card)
        self.assertIn('<dt>Render time</dt><dd>2.40 s</dd><dt>Errors</dt><dd>1 error line</dd>', card)
        self.assertIn(f'id={review_submissions.PAGE_SCREENSHOT.id}" loading="lazy" alt="Screenshot of Review"', card)
        self.assertIn('<figcaption>Screenshot of Review</figcaption>', card)

    def test_moved_reports_display_the_original_source_unit(self) -> None:
        owner = self.ready_submission_recipient()
        unit = self.fixture.unit
        reports: list[investigation.Investigation] = []
        for kind in investigation.InvestigationKind:
            request = self.submission_request(new_item_dialog.SubmissionKind(kind.value), unit.id)
            origin = new_item_dialog.ReviewSubmission(
                request.request_id, 'a'*64, request.evidence, (), new_item_dialog.new_reply_token())
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
                detail = snapshot.body(key).decode().split('data-review-body>', 1)[1]
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
        detail = review_page.build_snapshot(self.root).body(key).decode()
        self.assertIn('<h3>Item</h3><p>'+title+'</p>', detail)
        self.assertEqual(detail.count(title), 1)
        self.assertEqual(detail.count(ask), 1)
        self.assertIn('Choose the width.', detail)

    def test_investigation_shows_its_question_title_once_in_details(self) -> None:
        title = 'Why did the gate stop?'
        filed = investigation.file(self.fixture.unit.id, title, title, waiting_why='The owner must complete the investigation.')
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
        folder = self.question_path(key).parent
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
        with patch.object(review_page, 'user_zone', return_value=ZoneInfo('UTC')):
            original = review_page.build_snapshot(self.root)
        filed = original.records[proposal].times.filed
        with patch.object(review_page, 'user_zone', side_effect=ValueError('Invalid live zone')):
            self.assertIn(filed.astimezone(ZoneInfo('UTC')).strftime('%b %d %H:%M').encode(), original.body(proposal))
        with patch.object(review_page, 'user_zone', return_value=ZoneInfo('Asia/Tokyo')):
            updated = review_page.build_snapshot(self.root)
        self.assertNotEqual(updated.version, original.version)
        self.assertIn(filed.astimezone(ZoneInfo('Asia/Tokyo')).strftime('%b %d %H:%M').encode(), updated.body(proposal))

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
        self.assertNotRegex(shell, r'<form[^>]*\bdata-review-form\b')
        self.assertRegex(shell, r'<dialog id="review-new-item"[^>]*>\s*<form>')
        self.assertEqual(len(re.findall(r'<form\b', shell)), 1)
        self.assertNotIn('<tbody data-list-item', shell.split('<script>')[0], 'The browser mounts summaries from the index.')
        self.assertIn('data-review-index', shell)
        self.assertIn('Which layout gives each review card enough space for its controls?', card)
        self.assertIn('data-review-form', card)
        revision = review_page.record_revision(snapshot.records[key], snapshot.assets)
        self.assertRegex(revision, r'^[0-9a-f]{16}$')
        self.assertIn(f'name="revision" value="{revision}"', card)
        with self.assertRaises(LookupError):
            _ = snapshot.body('missing/key')

    def test_snapshot_changes_outside_requests_and_retains_the_current_version(self) -> None:
        cache = review_page.ReviewSnapshotCache(self.root)
        self.addCleanup(cache.close)
        previous = cache.get()
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
        during_retry: list[bool] = []

        def rebuild(root: Path) -> review_page.ReviewSnapshot:
            if read.is_set():
                during_retry.append(key in cache.get().records)
                retry.set()
                return original(root)
            read.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise TimeoutError('The test did not release the old read.')
            return previous

        with patch.object(review_page, 'build_snapshot', side_effect=rebuild):
            cache.start()
            cache.invalidate()
            try:
                self.assertTrue(read.wait(TEST_HANG_GUARD_SECS))
                saved = cache.save(self.card_body(previous, key, 'answer', 'Use the first layout.'), self.status)
                self.assertEqual(cache.get().version, saved.version)
                self.assertNotIn(key, cache.get().records)
            finally:
                release.set()
            self.assertTrue(retry.wait(TEST_HANG_GUARD_SECS))
            cache.synchronize()
        self.assertEqual(during_retry, [False])
        self.assertNotIn(key, cache.get().records)

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
        text = path.read_text()
        line = next(line for line in text.splitlines() if line.startswith('registration: '))
        fields = cast(dict[str, object], json.loads(line.removeprefix('registration: ')))
        _ = fields.pop('review_id', None)
        legacy = text.replace(line, 'registration: '+json.dumps(fields))
        _ = path.write_text(legacy)
        (investigation.state_root() / 'record-ids' / 'question.water').unlink()
        page = review_page.render(self.root).decode()
        index = page.split('<script type="application/json" data-review-index>', 1)[1].split('</script>', 1)[0]
        self.assertEqual(next(entry['id'] for entry in unpack_index(index) if entry['key']==key), 'page-owner/'+path.stem)
        self.assertEqual(path.read_text(), legacy)
        record, = update_registration.selected_question_records([path], (), None)
        update_registration.write_question_record(record, assign_id=True)
        page = review_page.render(self.root).decode()
        index = page.split('<script type="application/json" data-review-index>', 1)[1].split('</script>', 1)[0]
        self.assertEqual(next(entry['id'] for entry in unpack_index(index) if entry['key']==key), 'page-owner/Q1')
        self.assertIn('"review_id": "Q1"', path.read_text())

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
 const until=async (predicate,label='the expected browser state')=>{
  const end=performance.now()+8000;
  while(!predicate()){if(performance.now()>end) throw new Error('Timed out waiting for '+label+'.');await frame();}
 };
 const row=key=>document.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');
 const form=key=>row(key)?.querySelector('[data-review-form]');
 const open=async key=>{
  if(new URLSearchParams(location.hash.slice(1)).get('item')===key){scrollTo(0,0);await frame();}
  else location.hash=new URLSearchParams({item:key});
  await until(()=>form(key),'the open card '+key);await frame();
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
  await open(DRAFT_KEY);
  const draft=form(DRAFT_KEY),choice=row(DRAFT_KEY).querySelector('input[name=choice]');
  choice.checked=true;choice.dispatchEvent(new Event('change'));
  draft.elements.feedback.value='Keep these draft words.';draft.elements.feedback.dispatchEvent(new Event('input'));
  draft.elements.later.value='2030-12-01T18:30';draft.elements.later.dispatchEvent(new Event('input'));
  const choiceValue=choice.value,first=[...document.querySelectorAll('[data-list-item]')].map(node=>node.dataset.key);
  draft.elements.feedback.blur();
  scrollTo(0,document.documentElement.scrollHeight);await until(()=>!row(DRAFT_KEY),'the unfocused draft to leave the window');
  check(document.querySelectorAll('[data-list-item]').length<=100 &&
    [...document.querySelectorAll('[data-list-item]')].some(node=>!first.includes(node.dataset.key)),
    'Scrolling loads distant summaries and removes old summaries.');
  check(!row(DRAFT_KEY),'The open card leaves the mounted window during distant scrolling.');
  await open(DRAFT_KEY);
  check(form(DRAFT_KEY).elements.feedback.value==='Keep these draft words.' &&
   form(DRAFT_KEY).elements.later.value==='2030-12-01T18:30' &&
   row(DRAFT_KEY).querySelector('input[name=choice]:checked')?.value===choiceValue,
   'Feedback, choice and Later survive removal and mounting by key.');
  const feedback=form(DRAFT_KEY).elements.feedback;
  feedback.focus();
  scrollTo(0,document.documentElement.scrollHeight);await frame();await frame();
  check(document.activeElement===feedback && feedback.isConnected,'F013: scrolling keeps the focused row mounted.');
  await open(DRAFT_KEY);
  row(DRAFT_KEY).querySelector('.list-toggle').focus();
  const oldVersion=document.querySelector('[data-review]').dataset.version;
  await fetch('/advance');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await until(()=>document.querySelector('[data-review]').dataset.version!==oldVersion && form(DRAFT_KEY),
   'a refreshed version and the open draft');
  check(document.activeElement===row(DRAFT_KEY).querySelector('.list-toggle'),'F007: refresh restores focus to the row toggle.');
  check(form(DRAFT_KEY).elements.feedback.value==='Keep these draft words.' &&
   form(DRAFT_KEY).elements.later.value==='2030-12-01T18:30' &&
   row(DRAFT_KEY).querySelector('input[name=choice]:checked')?.value===choiceValue,
   'The open card and all editable values survive a new snapshot.');
  check(form(DRAFT_KEY).elements.token.value===EXPECTED_TOKEN,
   'The refreshed body supplies the current row token.');
  check(form(DRAFT_KEY).elements.revision.value===EXPECTED_REVISION,
   'F024: the refreshed body supplies the current record revision.');
  await fetch('/reset-counts');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await frame();await frame();
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
    posts++;await delayedSave;return new Response(JSON.stringify({reason:'Retry the save.'}),{status:400});
   }
   if(new URL(url,location.href).searchParams.get('version')==='1'){
    if(holdVersion){
     holdVersion=false;
     await new Promise(resolve=>{releaseVersion=resolve;});
    }
    if(timeoutVersion){
     timeoutVersion=false;stalled=true;
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
  scrollTo(0,document.documentElement.scrollHeight);await until(()=>!row('page-owner/C2'),'the delayed row to unmount');
  scrollTo(0,top);await until(()=>row('page-owner/C2'),'the delayed row to mount');releaseBody();
  const ready=async key=>{
   const end=performance.now()+1500;
   while(!form(key) && performance.now()<end) await frame();
   return form(key);
  };
  check(await ready('page-owner/C2'),'F003: the delayed response fills the remounted row.');
  if(!form('page-owner/C2')){
   scrollTo(0,document.documentElement.scrollHeight);await until(()=>!row('page-owner/C2'),'the delayed row to unmount again');
   scrollTo(0,top);await until(()=>form('page-owner/C2'),'the delayed form to mount');
  }
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
  await until(()=>!row('page-owner/C2'),'the saving row to unmount');
  scrollTo(0,top);await until(()=>form('page-owner/C2'),'the saving row to mount');
  const replacement=form('page-owner/C2');
  const laterButton=replacement.querySelector('button[value=later]');
  check(replacement.elements.later.value==='2030-12-03T18:30',
   'F019: the form mounts with the Later time edited during the save.');
  replacement.elements.later.value='2030-12-02T18:30';
  replacement.requestSubmit(laterButton);
  check(laterButton.disabled && posts===1,'F002: a remounted form cannot submit a second save.');
  releaseSave();
  await until(()=>!replacement.dataset.sending,'the failed save to finish');
  check(replacement.elements.feedback.value==='Edit during save.',
   'F019: a failed save keeps text edited before the form remounted.');
  const previousVersion=document.querySelector('[data-review]').dataset.version;
  await fetch('/advance-again');
  location.hash=new URLSearchParams({item:'page-owner/C4'});
  check(await ready('page-owner/C4'),'The newer card body loads.');
  check(document.querySelector('[data-review]').dataset.version!==previousVersion &&
   row('page-owner/C4').dataset.title==='a newer summary','F004: the body version refreshes the summary.');
  location.hash=new URLSearchParams({show:'open',item:'page-owner/C4'});
  await until(()=>document.querySelector('[data-review]').dataset.show==='open' && form('page-owner/C4'),'the Open form');
  conflict='/resolve-conflict';
  const stale=form('page-owner/C4');
  const conflictVersion=document.querySelector('[data-review]').dataset.version;
  stale.requestSubmit(stale.querySelector('button[value=works]'));
  await until(()=>!row('page-owner/C4') && !stale.dataset.sending &&
   document.querySelector('[data-review]').dataset.version!==conflictVersion,
   'the refreshed view without the completed row');
  const conflictNotice=document.querySelector('[data-review-notice]');
  check(document.querySelector('[data-review]').dataset.show==='open' &&
   !conflictNotice.hidden && conflictNotice.textContent.includes('This item is already Done.'),
   'F015: HTTP 409 keeps the reason when the completed item leaves Review.');
  await open(DRAFT_KEY);
  conflict='/revise-conflict';
  const oldQuestion=form(DRAFT_KEY),questionRevision=oldQuestion.elements.revision.value;
  check(row(DRAFT_KEY).querySelector('input[name=choice]:checked')?.value===choiceValue,
   'The question has a selected choice before the save.');
  oldQuestion.requestSubmit(oldQuestion.querySelector('button[value=answer]'));
  await until(()=>form(DRAFT_KEY)?.elements.revision.value===REVISED_QUESTION &&
   !form(DRAFT_KEY).querySelector('button[value=answer]').disabled,'the revised question form');
  const newQuestion=form(DRAFT_KEY);
  check(newQuestion.elements.revision.value!==questionRevision,'Revision recovery loads a different revision.');
  check(newQuestion.elements.feedback.value==='Keep these draft words.','Revision recovery keeps the feedback.');
  check(newQuestion.elements.later.value==='2030-12-01T18:30','Revision recovery keeps the Later time.');
  check(row(DRAFT_KEY).querySelector('input[name=choice]:checked')?.value===choiceValue,
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
  await until(()=>!form(DRAFT_KEY).querySelector('button[value=answer]').disabled,'the reload deadline');
  check(document.querySelector('[data-page-status]').dataset.state==='Failed' &&
   document.querySelector('[data-review]').dataset.requestTimeoutSeconds==='3',
   'The configured recovery deadline releases the stale revision.');
  check(form(DRAFT_KEY).elements.feedback.value==='Keep these newer words.',
   'A timed-out reload keeps the current words.');
  const remountDraft=async()=>{
   const previous=form(DRAFT_KEY);
   document.activeElement.blur();
   scrollTo(0,document.documentElement.scrollHeight);
   await until(()=>!row(DRAFT_KEY),'the unfocused question to leave the window');
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
   await until(()=>status.textContent.includes('This item changed.'),'the first recovery to finish');
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
  const notice=document.querySelector('[data-review-notice]');
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
        probe = (probe.replace('DRAFT_KEY', json.dumps(key)).replace('EXPECTED_TOKEN', json.dumps(expected_token))
                 .replace('EXPECTED_REVISION', json.dumps(expected_revision))
                 .replace('REVISED_QUESTION', json.dumps(review_page.record_revision(revised.records[key], revised.assets)))
                 .replace('REVISED_AGAIN', json.dumps(review_page.record_revision(revised_again.records[key], revised_again.assets))))
        counts = {'version':0,'index':0}
        spy = """<script>window.coldErrors=[];
addEventListener('error',event=>window.coldErrors.push(event.message));
addEventListener('unhandledrejection',event=>window.coldErrors.push(String(event.reason)));
const measuredFetch=fetch;window.fetch=async(url,options)=>{
 if(new URL(url,location.href).searchParams.get('body')==='page-owner/C399' && window.coldBodyMounts===undefined){
  window.coldBodyMounts=document.querySelectorAll('[data-list-item]').length;
  const response=measuredFetch(url,options);
  await new Promise(resolve=>{window.releaseColdBody=resolve;});
  return response;
 }
 return measuredFetch(url,options);
};</script>"""
        markup = (initial.render(show='all').decode().replace('<head>','<head>'+spy,1)
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
                    result=subprocess.run(['firefox','--headless','--no-remote','--profile',profile,
                        '--window-size','1440,900','--screenshot',str(self.fixture.temporary/'window.png'),
                        f'http://127.0.0.1:{server.server_port}/#item=page-owner%2FC399'],
                        capture_output=True,text=True,timeout=45)
                self.assertEqual(result.returncode,0,result.stderr)
            finally:
                server.shutdown()
                thread.join(5)
        self.assertEqual(results,[[]])
        self.assertEqual(set(requested_bodies), {'page-owner/C399', key, 'page-owner/C2', 'page-owner/C4', 'page-owner/C6'},
                         'Only opened keys request a body.')

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_tab_reaches_adjacent_rows_after_a_distant_scroll(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        records = [replace(change, key=f'page-owner/C{number}', id=f'page-owner/C{number}')
                   for number in range(1, 401)]
        with patch.object(review_items, 'read', return_value=records):
            page = self.fixture.temporary / 'tab-review.html'
            _ = page.write_bytes(review_page.build_snapshot(self.root).render())
        with self.firefox_commands(page) as command:
            for initial, expected, modifier in ((1, 2, ''), (2, 1, '\ue008')):
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
 if(row(400)) done();
 else requestAnimationFrame(waitForBottom);
}
requestAnimationFrame(focusRow);
""")
                    keys = ([('keyDown', modifier)] if modifier else []) + [
                        ('keyDown', '\ue004'), ('keyUp', '\ue004')] + ([('keyUp', modifier)] if modifier else [])
                    _ = command('WebDriver:PerformActions', actions=[{'type':'key', 'id':'keyboard',
                        'actions':[{'type':kind, 'value':key} for kind,key in keys]}])
                    focused = command('WebDriver:ExecuteScript', args=[], script=
                        'return document.activeElement.closest("[data-list-item]")?.dataset.key;')
                    self.assertEqual(focused, {'value': f'page-owner/C{expected}'})

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_selected_chips_keep_text_contrast_on_hover_and_press(self) -> None:
        review = review_page.build_snapshot(self.root).render().decode()
        list_chip = record_table.chip_row('Show', [record_table.Chip('open', 'Open', 3)], 'open')
        page = self.fixture.temporary / 'chip-contrast.html'
        _ = page.write_text(review.replace('<div data-review ', list_chip + '<div data-review ', 1))
        with self.firefox_commands(page) as command:
            for theme in ('dark', 'light'):
                for selector in ('#review-show-open', '#filter-show-open'):
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
                               'about:blank'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as browser:
            try:
                with self.marionette_commands(browser) as command:
                    _ = command('WebDriver:Navigate', url=page.as_uri())
                    yield command
            finally:
                browser.kill()

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
        ready = self.investigation()
        waiting = self.investigation(False)
        items = review_items.read(self.root)
        self.assertEqual({item.type for item in items},{review_items.ReviewType.QUESTION,review_items.ReviewType.CHANGE,review_items.ReviewType.INVESTIGATION})
        question = next(item for item in items if item.key == key)
        self.assertRegex(question.id,r'^page-owner/Q[1-9][0-9]*$')
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
        original = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(original.id, 'page-owner/Q1', 'F013: show the native ID on the first read')
        self.assertIsInstance(original.source,review_items.QuestionSource)
        assert isinstance(original.source,review_items.QuestionSource)
        record = original.source.record
        update_registration.write_question_record(replace(record,status='closed'))
        _ = self.question('Second question?')
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
        change_page.deliver(delivery,self.status)
        self.assertTrue(any('Use the first layout.' in message for _,_,message in self.fixture.messages))
        self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == key),review_items.ReviewState.DONE)
        ready = self.investigation()
        key = f'investigation/{ready.owner}/{ready.id}'
        _,answer = review_page.submit(self.root,self.body(key,'read'),self.status)
        self.assertEqual(answer,'marked read')
        self.assertEqual(investigation.load(ready.owner,ready.id).state,investigation.InvestigationState.READ)

    def test_review_show_row_has_open_validating_and_all(self) -> None:
        markup = review_page.render(self.root).decode()
        self.assertEqual(re.findall(r'data-show-chip="([^"]+)"', markup), ['open', 'Validating', 'all'])

    def test_validating_change_has_a_card_and_uses_only_a_saved_handoff_time(self) -> None:
        key = 'page-owner/C1'
        until = datetime.now(user_zone()) + timedelta(days=1)
        review_later.save(key, until)
        with patch.object(time, 'time', return_value=1794902400):
            delivery, _ = review_page.submit(self.root, self.body(key, 'validate'), self.status)
        change_page.deliver(delivery, self.status)
        with self.subTest(later='cleared by hand-off'):
            self.assertNotIn(key, review_later.times())
        review_later.save(key, until)
        snapshot = review_page.build_snapshot(self.root)
        entry = next(item for item in snapshot.entries if item['key'] == key)
        self.assertEqual(entry['state'], review_items.ReviewState.VALIDATING)
        self.assertEqual(entry['hiddenUntil'], 0)
        self.assertEqual(entry['ownerName'], 'page-owner')
        self.assertEqual(entry.get('handoff', ('', ''))[1], 'Nov 17 03:00 EST')
        card = snapshot.body(key).decode()
        for text in ('How to check', 'Proof', 'Log · newest first', 'Your words', 'Sends to page-owner',
                     'Ask a question', 'data-validating-age datetime="2026-11-17T03:00:00-05:00"'):
            self.assertIn(text, card)
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
        self.assertIn('Sends to page-owner', current.body(key).decode())
        fields = self.card_body(current, key, 'question', question)
        sent = cache.save(fields, self.status)
        self.assertEqual(sent.answer, 'question saved')
        with patch.object(change_page, 'send_message', side_effect=RuntimeError('The recipient is offline.')):
            change_page.deliver(sent.delivery, self.status)
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
        self.assertIn('Question for page-owner', pending_card)
        self.assertIn(question, pending_card)
        self.assertNotIn('Answer from page-owner', pending_card)
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
                for text in ('Question for page-owner', 'Answer from page-owner', question, words):
                    self.assertIn(text, card)
                    self.assertEqual(card.count(text), 1)
                self.assertEqual([event.words for event in snapshot.records[key].log][-3:],
                                 ['Asked page-owner to validate', 'Asked a question about validation',
                                  'Answered the question'])
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
        change_page.deliver(pending.delivery, self.status)
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
        self.assertIn('Sends to page-owner', snapshot.body(key).decode())
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
        review_later.save(key, now + timedelta(days=1))
        self.assertTrue(change_page.answer_item(key, 'The spacing is adjusted.', request.question_id, self.status))
        returned = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(returned.state, review_items.ReviewState.OPEN)
        with self.subTest(finding='F007'):
            self.assertTrue(returned.for_you(now), 'The returned feedback is visible in Open.')
            self.assertNotIn(key, review_later.times())
        with self.subTest(finding='F008'):
            self.assertEqual([entry.words.rstrip() for entry in returned.log[1:]],
                             ['feedback: Adjust the spacing.', 'Answer: The spacing is adjusted.'])

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
        change_page.deliver(delivery,self.status)
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
        _ = self.investigation()
        source = review_page.render(self.root).decode()
        self.assertIn(record_table.STYLE,source)
        self.assertIn(record_table.SCRIPT,source)
        for label in review_page.HEADERS:
            self.assertIn(label+'<span data-sort-mark',source)
        for kind in ('question','change','investigation'):
            self.assertIn(f'data-type-chip="{kind}"',source)
        cards = ''.join(review_page.build_snapshot(self.root).body(item.key).decode() for item in review_items.read(self.root))
        for action in ('Answer','Works','Does not work','Ask showrunner to check','Ask question','Send feedback','Mark read'):
            self.assertIn('>'+action+'</button>',cards)
        self.assertIn('data-later-toggle>Later <span',cards)
        self.assertIn('createPageRefresh(refresh,()=>content.dataset.refreshSeconds)',source)
        self.assertIn('class="lucide lucide-microscope"',source)
        self.assertIn('data-refresh-seconds="15.0"',source)

    def test_open_state_replaces_for_you_without_changing_the_user_totals(self) -> None:
        _ = self.decisions()
        _ = self.question()
        _ = self.investigation()
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
                         (('open', 'Open'), ('Validating', 'Validating'), ('all', 'All')))
        for spelling in ('For you', 'for-you', 'for you'):
            with self.subTest(spelling=spelling):
                markup = review_page.render(self.root, show=spelling).decode()
                self.assertIn('data-show="open"', markup)
                self.assertNotIn('data-state="For you"', markup)
                self.assertNotIn('data-show-chip="For you"', markup)
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
                for label, container in (
                    ('Type', 'class="review-types"'),
                    ('State', 'class="review-types review-shows" id="list-show"'),
                    ('Owner', 'class="review-types" id="list-owner"'),
                ):
                    self.assertIn('<div class="record-filter-row">'
                                  + f'<span aria-hidden="true">{label}</span>'
                                  + f'<div {container} role="group" aria-label="{label}">', source)
                group = source.partition('<div class="review-types review-shows"')[2].partition('</div>')[0]
                self.assertTrue(group, 'Show has a second chip row')
                self.assertNotIn('<select id="list-show"', source)
                self.assertNotIn('<label for="list-show">', source)
                self.assertEqual(group.count('aria-pressed="true"'), 1)
                self.assertIn(f'data-show-chip="{show}" aria-pressed="true"', group)
                for value, label in review_page.SHOWS:
                    self.assertIn(f'type="button" data-show-chip="{value}"', group)
                    self.assertIn('>'+label+' <b', group)

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
        ready = self.investigation()
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
        expected = {'open':open_keys, 'Validating':validating_keys, 'doneKey':done_key,
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
 if(options?.method==='POST') return {ok:true,json:async()=>({answer:'saved'})};
 window.refreshRequests.push(url);
 const show=new URL(url,location.href).searchParams.get('show') || 'open';
 return {ok:true,text:async()=>showPages[show]};
};
</script>""".replace('PAGES', json.dumps(pages).replace('</', '<\\/'))
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>nativeTimeout(resolve,80));
 const chip=(value,doc=document)=>doc.querySelector('[data-show-chip="'+value+'"]');
 const pressed=(doc=document)=>[...doc.querySelectorAll('[data-show-chip][aria-pressed="true"]')];
 const selected=(value,doc=document)=>pressed(doc).length===1 && pressed(doc)[0].dataset.showChip===value;
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
  check([...document.querySelectorAll('[data-show-chip]')].map(button=>button.dataset.showChip).join('|')===
    'open|Validating|all','Show chips have the required order');
  check(selected('open'),'exactly one Open chip starts pressed');
  check(exactKeys('open'),'F020: Open mounts the exact Open keys');
  check(noMountedKey(EXPECTED.doneKey),'F064: Open does not mount the Done investigation');
  check(noMountedKey(EXPECTED.waitingInvestigation),'F064: Open does not mount the Waiting investigation');
  check(noMountedState('Done'),'F064: the initial Open view mounts no Done rows');
  check(noMountedKey(EXPECTED.Validating[0]),'Open must not mount the active check.');
  check(count('open')?.textContent==='6' && count('Validating')?.textContent==='1' &&
    !chip('Done') && count('all')?.textContent==='9',
    'F001: Open shows complete counts, including the active check in Validating');
  check(chip('Validating') && getComputedStyle(chip('Validating')).opacity==='1',
    'A nonzero Validating chip has full opacity.');
  const search=document.querySelector('#list-search');
  search.value='no matching title';search.dispatchEvent(new Event('input'));
  check(count('Validating')?.textContent==='0' && chip('Validating') &&
    Number(getComputedStyle(chip('Validating')).opacity)<1,'A zero Validating chip is dim.');
  search.value='';search.dispatchEvent(new Event('input'));
  if(chip('Validating')){
   chip('Validating').click();await flush();await flush();
   const visible=[...document.querySelectorAll('[data-list-item]:not([hidden])')];
   check(selected('Validating') && visible.length===1 && visible[0].dataset.state==='Validating' &&
    visible[0].dataset.key==='page-owner/C1','Validating shows only the active check.');
   check(document.querySelector('[data-owner-chip=""] b')?.textContent==='1' &&
    document.querySelector('[data-type-chip=""] b')?.textContent==='1' &&
    document.querySelector('[data-type-chip="change"] b')?.textContent==='1',
    'Owner and Type count the active check.');
   const row=visible[0],toggle=row?.querySelector('.list-toggle');
   check(row?.dataset.parked==='false' && toggle?.tagName==='BUTTON',
    'The active check can open without a Later state.');
   const handoff=row?.querySelector('[data-label="Item"] .review-handoff');
   check(handoff?.textContent==='With page-owner · Validating for 1 h 20 m',
    'The row shows the validator and elapsed time.');
   toggle?.click();
   const form=await readyReviewCard(row);
   check(!row.querySelector('[data-detail]').hidden && form.textContent.includes('Sends to page-owner') &&
    form.querySelector('[value="question"]')?.textContent==='Ask a question' &&
    row.querySelector('[data-detail] .review-handoff')?.textContent===handoff?.textContent,
    'Open the card with the same elapsed time and send label.');
   reviewClock+=60000;updateAges();
   check(handoff?.textContent==='With page-owner · Validating for 1 h 21 m' &&
    row.querySelector('[data-detail] .review-handoff')?.textContent===handoff?.textContent,
    'The row and card update without a server read.');
   toggle?.click();
   reviewClock-=60000;
   const owner=handoff?.querySelector('span');
   check(owner && getComputedStyle(owner).whiteSpace==='nowrap','The showrunner name stays together.');
   if(innerWidth>960) check(handoff &&
    handoff.getBoundingClientRect().height<=parseFloat(getComputedStyle(handoff).lineHeight)+1,
    'The hand-off text fits on one desktop line.');
   check(/^\d+ (d|h|min)$/.test(visible[0]?.querySelector('[data-label="Age"]')?.textContent.trim() || ''),
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
    document.querySelector('[data-type-chip="investigation"] b').textContent==='2' &&
    document.querySelector('[data-owner-chip=""] b').textContent==='9',
    'All excludes the completed investigation from the rows, Type counts and Owner counts.');
  check(document.activeElement===chip('all'),'F002: the pressed Show chip keeps focus after the load');
  const ordinary=document.querySelector('[data-key="'+EXPECTED.waitingChange+'"]');
  ordinary?.querySelector('button.list-toggle')?.click();
  const ordinaryForm=await readyReviewCard(ordinary);
  check(ordinary?.dataset.state==='Waiting' && !ordinary.hidden && !ordinaryForm.querySelector('[value="question"]') &&
   !ordinary.querySelector('.review-handoff'), 'An ordinary question stays in Waiting without validation controls.');
  ordinary?.querySelector('button.list-toggle')?.click();
  chip('Validating')?.focus();chip('Validating')?.click();await flush();await flush();
  check(exactKeys('Validating'),'F020: Validating mounts the exact validation keys');
  check(count('Validating')?.textContent==='1','F001: Validating retains its complete count after the load');
  check(document.activeElement===chip('Validating'),'F002: the pressed Show chip keeps focus after the load');
  chip('all')?.focus();chip('all')?.click();await flush();await flush();
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(document.activeElement===chip('all'),'F002: the Show chip keeps focus after refresh');
  const typeChip=()=>document.querySelector('[data-type-chip="change"]');
  typeChip().focus();typeChip().click();
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(document.activeElement===typeChip(),'F002: the type chip keeps focus after refresh');
  document.querySelector('[data-type-chip=""]').click();
  chip('Validating')?.click();await flush();await flush();
  check(selected('Validating') && new URLSearchParams(location.hash.slice(1)).get('show')==='Validating',
    'A State chip changes the pressed state and address.');
  check(exactKeys('Validating'),'Validating shows the active check.');
  check(['open','Validating','all'].map(value=>count(value)?.textContent).join('|')==='6|1|9',
    'State counts exclude the completed investigation.');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(selected('Validating') && new URL(window.refreshRequests.at(-1),location.href).searchParams.has('version'),
    'refresh checks the version and retains the pressed Show view');
  const reload=document.createElement('iframe');reload.src='/reload'+location.hash;
  const loaded=new Promise(resolve=>reload.addEventListener('load',resolve,{once:true}));document.body.append(reload);await loaded;
  await flush();await flush();
  check(selected('Validating',reload.contentDocument) && reload.contentDocument.querySelector('[data-review]').dataset.show==='Validating',
    'a reload keeps the shared Show address');reload.remove();
  for(const value of ['all','open']){
   chip(value)?.click();await flush();await flush();
   check(selected(value),'exactly one chip stays pressed for '+value);
   check(exactKeys(value),'F020: the selected view mounts its exact keys for '+value);
   check(noMountedKey(EXPECTED.doneKey) && noMountedState('Done'),'Done stays hidden under '+value+'.');
   if(value==='open') check(decisions().length===2 && decisions().every(row=>!row.hidden),
    'Open retains the two decision rows after a Show load');
  }
  chip('all')?.click();await flush();await flush();
  check(['open','Validating','all'].map(value=>count(value)?.textContent).join('|')==='6|1|9' &&
    [...document.querySelectorAll('[data-show-chip] b')].every(number=>!number.hidden),
    'All supplies the count for every Show chip');
  const groups=[...document.querySelectorAll('.review-types')],buttons=[...document.querySelectorAll('[data-show-chip]')];
  const labels=groups.map(group=>group.previousElementSibling);
  check(labels.map(label=>label.textContent).join('|')==='Type|State|Owner' &&
    groups.every((group,index)=>group.getAttribute('aria-label')===labels[index].textContent),
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
  const ownerChip=(value,doc=document)=>doc.querySelector('[data-owner-chip="'+value+'"]');
  const ownerChips=()=>[...document.querySelectorAll('[data-owner-chip]:not([hidden])')];
  const selectedOwner=(doc=document)=>doc.querySelector('[data-owner-chip][aria-pressed="true"]');
  const chooseOwner=value=>ownerChip(value).click();
  const chooseType=value=>document.querySelector('[data-type-chip="'+value+'"]').click();
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
  chooseType('question');
  check(ownerChips().length===2 && ownerChips()[0].textContent==='All 1' &&
    ownerChips().some(button=>button.textContent==='page-owner 1') &&
    ownerChip(builder).hidden,
    'F007: Owner lists only owners with the selected type and words in any Show.');
  check(selectedOwner().dataset.ownerChip==='' && !new URLSearchParams(location.hash.slice(1)).get('owner') &&
    ['open','Validating','all'].map(value=>count(value)?.textContent).join('|')==='1|0|1',
    'F007: an owner without the selected type resets to All with matching counts.');
  check([...document.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1,
    'F007: the question appears after the owner resets.');
  chooseType('investigation');
  check(ownerChips().some(button=>button.dataset.ownerChip===builder) && ownerChips()[0].textContent==='All 1',
    'a type change restores owners and counts');
  chooseOwner(builder);chip('all').click();await flush();await flush();
  check(ownerChips()[0].textContent==='All 2' && selectedOwner().dataset.ownerChip===builder,
    'Show limits Owner counts and retains an owner with matching loaded rows');
  chip('open').click();await flush();await flush();
  chooseType('question');chooseOwner(ownerChips().find(button=>button.dataset.ownerName==='page-owner')?.dataset.ownerChip);
  chip('Validating').click();await flush();await flush();
  check(ownerChips().length===2 && ownerChips()[0].textContent==='All 0' && selectedOwner().dataset.ownerChip!=='' &&
    ownerChips()[1].textContent==='page-owner 0',
    'Show retains the selected owner with a zero count.');
  chip('open').click();await flush();await flush();
  check(ownerChips().length===2 && ownerChips()[0].textContent==='All 1','Show restores matching owners');
  chooseType('investigation');chooseOwner(builder);
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(selectedOwner().dataset.ownerChip===builder && ownerChips()[0].textContent==='All 1' &&
    ownerChips().every(button=>button.querySelector('b').textContent!=='0'),'refresh retains the matching owner list and selection');
  const legacy=document.createElement('iframe');
  legacy.src='/reload#'+new URLSearchParams({owner:builder,type:'question',show:'open'});
  const legacyLoaded=new Promise(resolve=>legacy.addEventListener('load',resolve,{once:true}));document.body.append(legacy);
  await legacyLoaded;await flush();await flush();
  check(selectedOwner(legacy.contentDocument).dataset.ownerChip==='' &&
    [...legacy.contentDocument.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1 &&
    !new URLSearchParams(legacy.contentWindow.location.hash.slice(1)).get('owner'),
    'F007: a legacy owner without the selected type resets to All.');legacy.remove();
  chooseType('question');
  const navigated=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams({owner:'builder-unit',type:'investigation',show:'open'});
  await navigated;await flush();await flush();
  check(selectedOwner().dataset.ownerChip===builder && [...document.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1,
    'F004: hash navigation resolves an owner name whose chip was hidden by Questions');
  const translated=document.createElement('iframe');
  translated.src='/reload#'+new URLSearchParams({legacy:'investigation',owner:'no-investigations',type:'investigation',show:'not-read'});
  const translatedLoaded=new Promise(resolve=>translated.addEventListener('load',resolve,{once:true}));
  document.body.append(translated);await translatedLoaded;await flush();await flush();
  const translatedPage=translated.contentDocument,translatedAddress=new URLSearchParams(translated.contentWindow.location.hash.slice(1));
  check(selectedOwner(translatedPage).dataset.ownerChip==='' &&
    selectedOwner(translatedPage).textContent==='All 1' &&
    !translatedAddress.get('owner') && !translatedAddress.has('source-owner') &&
    [...translatedPage.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1 &&
    count('open',translatedPage)?.textContent==='1',
    'F003: a legacy investigation reset clears both owner constraints and shows the All rows');
  translated.remove();
  chooseType('question');chooseOwner(ownerChips().find(button=>button.dataset.ownerName==='page-owner')?.dataset.ownerChip);
  const questionOwner=selectedOwner().dataset.ownerChip;
  const hasEmptyOwnerSelection=()=>ownerChips().length===2 && ownerChips()[0].textContent==='All 0' &&
   selectedOwner().dataset.ownerChip===questionOwner && selectedOwner().textContent==='page-owner 0';
  const question=document.querySelector('[data-type="question"]');question.querySelector('.list-toggle').click();await readyReviewCard(question);
  const form=question.querySelector('[data-review-form]');form.elements.feedback.value='Use this layout.';
  form.querySelector('[value="answer"]').click();await new Promise(resolve=>nativeTimeout(resolve,1100));
  check(hasEmptyOwnerSelection() && !question.isConnected && count('all').textContent==='0',
    'A save removes the completed question from the Owner and State counts.');
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
                            done = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', profile,
                                '--window-size', f'{width},900', '--screenshot', str(self.fixture.temporary/'show-chips.png'),
                                f'http://127.0.0.1:{server.server_port}/'], capture_output=True, text=True, timeout=30)
                        self.assertEqual(done.returncode, 0, done.stderr)
                    finally:
                        server.shutdown()
                        thread.join(TEST_HANG_GUARD_SECS)
                self.assertEqual(results, [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_filter_counts_share_the_complete_index(self) -> None:
        _ = self.question()
        _ = self.investigation(False)
        _ = review_page.submit(self.root, self.body('page-owner/C1', 'works'), self.status)
        pages = {show: browser_markup(self.root, show=show) for show, _ in review_page.SHOWS}
        setup = r"""<script>
const nativeFetch=fetch,nativeTimeout=setTimeout;
window.setInterval=()=>0;window.clearInterval=()=>{};
const showPages=PAGES;
window.fetch=async(url,options)=>{
 if(options?.method==='POST'){window.didSave=true;return {ok:true,json:async()=>({answer:'saved'})};}
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
 const show=value=>document.querySelector('[data-show-chip="'+value+'"]');
 const type=value=>document.querySelector('[data-type-chip="'+value+'"]');
 const counts=selector=>[...document.querySelectorAll(selector+' b')].map(node=>Number(node.textContent)).join('|');
 const search=value=>{const field=document.querySelector('#list-search');field.value=value;field.dispatchEvent(new Event('input'));};
 try{
  await flush();
  check(!document.querySelector('[data-list-count]'),'The Show chips replace the right count text.');
  check(counts('[data-show-chip]')==='3|0|4','The index counts states outside the rendered view.');
  const ownerChip=value=>document.querySelector('[data-owner-chip="'+value+'"]');
  const selectedOwner=()=>document.querySelector('[data-owner-chip][aria-pressed="true"]');
  const builder=document.querySelector('[data-type="change"]').dataset.sourceOwner;
  const chooseOwner=value=>ownerChip(value).click();
  show('all').click();await flush();await flush();
  chooseOwner(builder);
  check(counts('[data-show-chip]')==='0|0|1','Show counts use the selected owner.');
  check(type('change').querySelector('b').textContent==='0' && type('investigation').querySelector('b').textContent==='1',
   'Type counts use the selected owner.');
  type('change').click();
  check(selectedOwner().dataset.ownerChip==='' && counts('[data-show-chip]')==='2|0|2',
   'F007: an owner without the selected type resets to All.');
  type('investigation').click();
  chooseOwner(builder);show('open').click();await flush();await flush();
  check(selectedOwner().dataset.ownerChip===builder && counts('[data-show-chip]')==='0|0|1' &&
   [...document.querySelectorAll('[data-list-item]')].every(row=>row.hidden),
   'F007: the user case keeps an owner with rows in another Show.');
  show('all').click();await flush();await flush();
  check(counts('[data-show-chip]')==='0|0|1','Show counts combine the type and owner.');
  search('gate');
  check(counts('[data-show-chip]')==='0|0|1' && ownerChip('').textContent==='All 1',
   'All counts combine the words, owner, type and Show filters.');
  search('');chooseOwner('');type('').click();show('open').click();await flush();await flush();
  type('change').click();
  check(counts('[data-show-chip]')==='2|0|2','Show counts use the selected type.');
  search('Numbered');
  check(counts('[data-show-chip]')==='1|0|1','Show counts use the words filter.');
  check(type('change').querySelector('b').textContent==='1' && type('question').querySelector('b').textContent==='0',
   'Type counts use the words filter and Show.');
  check(selectedOwner().textContent==='All 1',
   'Owner counts use the type, Show and words.');
  search('no matching title');
  check(counts('[data-show-chip]')==='0|0|0' && [...document.querySelectorAll('[data-type-chip] b')].every(n=>n.textContent==='0') &&
   selectedOwner().textContent==='All 0',
   'An empty filtered view has zero in every applicable count.');
  search('');show('all').click();await flush();await flush();
  check(type('change').querySelector('b').textContent==='2','Type counts include the selected Show.');
  type('question').click();
  const row=document.querySelector('[data-list-item][data-type="question"]');
  row.querySelector('.list-toggle').click();await readyReviewCard(row);
  const form=row.querySelector('form');form.elements.feedback.value='Use the first layout.';
  form.requestSubmit(form.querySelector('[value="answer"]'));
  await new Promise(resolve=>nativeTimeout(resolve,1100));
  check(counts('[data-show-chip]')==='0|0|0','A save removes the completed entry from all State counts.');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();await flush();
  check(counts('[data-show-chip]')==='0|0|0','A refresh retains the saved state from the server index.');
  type('change').click();
  check(counts('[data-show-chip]')==='1|1|2','A refresh replaces an unsaved entry in the index.');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/result',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
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
                            done = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', profile,
                                '--window-size', f'{width},900', '--screenshot', str(self.fixture.temporary/'show-chips.png'),
                                f'http://127.0.0.1:{server.server_port}/'], capture_output=True, text=True, timeout=30)
                        self.assertEqual(done.returncode, 0, done.stderr)
                    finally:
                        server.shutdown()
                        thread.join(TEST_HANG_GUARD_SECS)
                self.assertEqual(results, [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_owner_counts_follow_the_type_and_show_chips(self) -> None:
        _ = self.decisions()
        _ = self.question()
        _ = self.investigation()
        _ = investigation.file(self.fixture.unit.id, 'Check the build', 'Which step stopped?',
                               waiting_why='The owner checks the build.', kind=investigation.InvestigationKind.BUG)
        owner = self.ready_submission_recipient()
        sent = review_submissions.submit(self.submission_request(new_item_dialog.SubmissionKind.QUESTION, owner.id))
        self.assertTrue(review_submissions.answer(sent.key, 'Use the wide layout.', sent.reply_token))
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

        show_states = {'open': (state_type.OPEN,), 'Validating': (state_type.VALIDATING,),
                       'all': review_items.selected_states('active')}
        expected: dict[str, dict[str, dict[str, int]]] = {}
        for show, selected_states in show_states.items():
            expected[show] = {}
            for kind in ('', *review_items.REVIEW_TYPES):
                counts = {str(owner): sum(item.owner == owner and item.state in selected_states and
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
 const chip=(group,value)=>document.querySelector('[data-'+group+'-chip="'+value+'"]');
 const selected=group=>document.querySelector('[data-'+group+'-chip][aria-pressed="true"]')
  ?.getAttribute('data-'+group+'-chip');
 const count=owner=>chip('owner',owner)?.querySelector('b')?.textContent;
 const counts=()=>Object.keys(expected.open['']).map(count).join('|');
 const checkCounts=(show,type)=>{
  const wanted=expected[show][type];
  const visible=[...document.querySelectorAll('[data-owner-chip]:not([hidden])')];
  check(visible.length===Object.keys(wanted).length && visible.every(button=>Object.hasOwn(wanted,button.dataset.ownerChip)),
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
  changeShow('Validating');checkCounts('Validating','change');
  check(counts()!==allStates,'Show alone changes the Owner counts for Changes.');
  for(const [show,types] of Object.entries(expected)){
   if(show==='Validating' && selected('type')==='response') chip('type','').click();
   if(selected('show')!==show) changeShow(show);
   for(const type of Object.keys(types)){
    if(show==='Validating' && type==='response') continue;
    chip('type',type).click();
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
        self.assertIn('data-show="open"', done)
        self.assertNotIn('data-show-chip="Done"', done)
        all_items = review_page.render(self.root, show='all').decode()
        self.assertNotIn(done_key,{entry['key'] for entry in unpack_index(all_items.split('data-review-index>',1)[1].split('</script>',1)[0])})
        self.assertIn(waiting_key,{entry['key'] for entry in unpack_index(all_items.split('data-review-index>',1)[1].split('</script>',1)[0])})
        for show in ('open', 'Validating'):
            with self.subTest(show=show):
                source = review_page.render(self.root, show=show).decode()
                self.assertIn(f'data-show="{show}"', source)
                owner_menu = source.partition('id="list-owner"')[2].partition('</div>')[0]
                self.assertIn(f'data-owner-chip="{waiting.owner}"', owner_menu,
                              'F002: the unit Owner option remains when its Show view has no rows')

    def test_completed_bug_stays_in_its_store_and_out_of_review(self) -> None:
        producer = fixture_member('producer', name='builds')
        bug = investigation.file(producer.id, 'Check the build', 'Which step stopped?', waiting_why='builds checks the failure.',
                                 kind=investigation.InvestigationKind.BUG)
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
            self.assertNotIn(f'data-owner-chip="{producer.id}"'.encode(), page)
        waiting = review_page.waiting_items(console.NewsRequest(self.root), snapshot=after)
        self.assertFalse(any(item.startswith(key+'/') for item in waiting))
        self.assertEqual(investigation.load(bug.owner, bug.id).state, investigation.InvestigationState.READ)

    def test_open_read_skips_done_payloads_and_retains_full_selected_records(self) -> None:
        question_key = self.question()
        ready = self.investigation()
        _ = self.investigation(False)
        _ = investigation.mark_read(ready.owner, ready.id)
        delivery, _ = review_page.submit(self.root, self.body(question_key, 'answer', 'Use the first layout.'), self.status)
        change_page.deliver(delivery, self.status)
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
                       own: list[Issue] | None = None, show: str = 'all') -> list[review_items.ReviewItem]:
            _ = show
            return original(kind, scope, record, below=below, sources=sources, own=own)

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
        ready = self.investigation()
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
                heading = 'Sends to showrunner' if item.type == review_items.ReviewType.CHANGE else 'The item returns to you'
                self.assertIn(heading,form)
                self.assertIn('aria-haspopup="menu" aria-expanded="false" data-later-toggle>Later',form)
                for label in ('In 1 hour','This evening','Tomorrow morning','Next week','Choose a time…'):
                    self.assertIn('<span>'+label+'</span>',form)
                self.assertNotIn('type="datetime-local"',form)
                self.assertIn('type="hidden" name="later"',form)
                self.assertIn('role="grid"',form)
                self.assertIn('<label class="later-time">Time<input type="text" inputmode="numeric" autocomplete="off" data-later-time></label>',form)
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

    def test_later_presets_read_their_settings(self) -> None:
        for minutes, label in ((1,'In 1 minute'),(90,'In 90 minutes'),(60,'In 1 hour'),(120,'In 2 hours')):
            with self.subTest(minutes=minutes):
                self.assertEqual(review_page.soon_label(minutes),label)
        lines = ('# whole: 1..1440','later_soon_minutes=60','later_evening=18:00','later_evening_until=17:00','later_morning=09:00',
                 'later_week_day=Monday','later_week_time=09:30')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'review_links.conf'
            _ = path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
            self.assertEqual(review_page.later_settings(conf.read(path)),
                             {'soon':60,'evening':1080,'eveningUntil':1020,'morning':540,'weekDay':1,'weekTime':570})
            for bad in ('later_week_day=Mon','later_morning=9:00'):
                with self.subTest(bad=bad):
                    key = bad.split('=')[0]
                    _ = path.write_text('\n'.join(line for line in lines if not line.startswith(key+'='))+'\n'+bad+'\n',encoding='utf-8')
                    with self.assertRaisesRegex(ValueError,key):
                        _ = review_page.later_settings(conf.read(path))

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
                if item.type == review_items.ReviewType.CHANGE:
                    review_later.save(key, now + timedelta(days=1))
                self.assertEqual(change_page.main(args),0)
                returned = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(returned.state, review_items.ReviewState.OPEN)
                self.assertEqual(sum('The second case passes.' in entry.words for entry in returned.log),1)
                self.assertEqual(sum('Check the second case.' in entry.words for entry in returned.log),1)
                if item.type == review_items.ReviewType.CHANGE:
                    self.assertTrue(returned.for_you(now), 'The answered change is visible in Open.')
                    self.assertNotIn(key, review_later.times())

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
                change_page.deliver(delivery,self.status)
            child_record = next(item.source.record for item in review_items.read(self.root)
                                if item.key == child_key and isinstance(item.source, review_items.QuestionSource))
            child_before = child_record.path.read_bytes()
            for key in (question, child_key):
                item = next(item for item in review_items.read(self.root) if item.key == key)
                page = review_actions.delivery_page(item, self.status)
                pending = self.status.slots(page, page.rows[0]).question
                assert isinstance(pending, change_page.OpenQuestion)
                self.assertEqual(change_page.main(['answer-question','--item',key,'--question',pending.question_id,'--text','Answer for '+key]),0,
                                 'F022: the exact item selects its record despite identical subtree text')
                returned = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(returned.state,review_items.ReviewState.OPEN)
                self.assertIn('Answer for '+key, returned.log[-1].words)
                if key == question:
                    self.assertEqual(change_page.main(['answer-question','--item',key,'--question',pending.question_id,'--text','Repeat parent answer']),1,
                                     'F022: a repeated parent answer cannot select the child')
                    self.assertEqual(child_record.path.read_bytes(),child_before,'F022: only the selected record changes')
                    self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == child_key),
                                     review_items.ReviewState.WAITING)

    def test_waiting_cards_keep_the_for_you_action_groups(self) -> None:
        decisions = self.decisions()
        question = self.question()
        report = self.investigation()
        keys = (question, f'investigation/{report.owner}/{report.id}', *decisions)
        token, initial = change_browser.browser_manifest(change_page.console_folder(self.root))
        browser = change_page.ReviewBrowser(token, initial)
        words_rows = conf.read('review_links').whole('review_words_rows')
        for key in keys:
            opened = next(item for item in review_items.read(self.root) if item.key == key)
            delivery, _ = review_page.submit(self.root, self.body(key, 'question', 'Check the old case.'), self.status)
            change_page.deliver(delivery, self.status)
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
                    change_page.deliver(delivery, case.status)
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
                original_message = delivery.message
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

        cases = (('C1', 'closeout'), ('C2', 'slots'), ('C3', 'receipt and slots'))
        write_text = atomic_file.write_text_atomic
        for row, failed_write in cases:
            with self.subTest(write=failed_write):
                self.fixture.messages.clear()
                key = 'page-owner/'+row
                check, _ = review_page.submit(self.root, self.body(key, 'validate'), self.status)
                change_page.deliver(check, self.status)
                self.fixture.messages.clear()
                snapshot = review_page.build_snapshot(self.root)
                body = self.card_body(snapshot, key, 'question', words)
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

    def test_later_hides_each_type_from_the_shared_queue_and_then_returns(self) -> None:
        _ = change_review.append_rows(self.fixture.review_list,
            pictured_changes([change_review.Visible('Unrouted change', 'Read the log.', 'The result is present.')]),
            datetime.now(user_zone()).date(), '', self.fixture.production,
            producers=[staff.StaffId('missing-producer')], record=staff.snapshot())
        decisions = self.decisions()
        question = self.question()
        ready = self.investigation()
        keys = (question,'page-owner/C1',f'investigation/{ready.owner}/{ready.id}',*decisions)
        until = datetime.now(user_zone()).replace(second=0,microsecond=0)+timedelta(days=1)
        for key in keys:
            with self.subTest(key=key):
                item = next(item for item in review_items.read(self.root) if item.key == key)
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
        commands = {proposal:'proposal_issue.py approve', efficiency:"candidates.py outcome E1 accepted --applied '<repo> <sha>'"}
        for key, command in commands.items():
            with self.subTest(key=key, action='approve'):
                delivery, result = review_page.submit(self.root, self.body(key, 'approve', 'Use it.'), self.status)
                self.assertEqual(result, 'approved, sent to page-owner')
                assert isinstance(delivery, change_page.MessageDelivery)
                self.assertIn('Use it.', delivery.message)
                self.assertIn(command, delivery.message)
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
        change_page.deliver(delivery,self.status)
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
 const combined=document.querySelector('[data-type-chip="investigations"]');
 if(mounted()!=='bug,investigation') failures.push('The bookmark must show both report types.');
 if(combined?.hidden || combined?.getAttribute('aria-pressed')!=='true')
  failures.push('Select the combined filter on the legacy bookmark.');
 document.querySelector('[data-type-chip="investigation"]').click();
 if(mounted()!=='investigation') failures.push('The Investigation filter must show only Investigations.');
 document.querySelector('[data-type-chip="bug"]').click();
 if(mounted()!=='bug') failures.push('The Bug filter must show only Bugs.');
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
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
        values = {
            'owner': owner.id, 'key': created.key, 'id': created.saved.id, 'kind': kind,
            'words': request.words, 'matches': matches_filters,
            'body': fresh.body(created.key).decode(), 'snapshot': cast(object, json.loads(fresh.index_response())),
        }
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
   sent=true;
   return Response.json({id:fixture.id,key:fixture.key,version:fixture.snapshot.version});
  }
  return Response.json({token:'fixture',recipients:[{id:fixture.owner,name:'page-owner',proposal:true}]});
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
  document.querySelector('[data-review-new]').click();
  if(!dialog.open) throw new Error('New must open the dialog.');
  dialog.querySelector('[name="kind"][value="'+fixture.kind+'"]').click();
  (await ready).click();
  const words=dialog.querySelector('[name="words"]');
  words.value=fixture.words;
  words.dispatchEvent(new Event('input',{bubbles:true}));
  const refreshed=new Promise(resolve=>review.addEventListener('review-refreshed',resolve,{once:true}));
  dialog.querySelector('[type="submit"]').click();
  await refreshed;
  const row=review.querySelector('[data-key="'+CSS.escape(fixture.key)+'"]');
  check(Object.entries(selection).every(([key,value])=>params().get(key)===value),
        'Send keeps every filter in the address.');
  check(review.querySelector('[data-show-chip][aria-pressed="true"]').dataset.showChip===selection.show &&
   review.querySelector('[data-type-chip][aria-pressed="true"]').dataset.typeChip===selection.type &&
   review.querySelector('[data-owner-chip][aria-pressed="true"]').dataset.ownerChip===selection.owner &&
   review.querySelector('#list-search').value===selection.q,'Send keeps each selected control.');
  if(fixture.matches){
   check(row && !row.querySelector('[data-detail]').hidden,'The new item opens when it matches the filters.');
   check(history.length===previous+1,'Opening the item adds one history entry.');
   if(history.length===previous+1){
    const restored=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
    history.back();
    await restored;
    check(location.hash===previousHash && !params().has('item') &&
     review.querySelector('[data-key="'+CSS.escape(fixture.key)+'"] [data-detail]').hidden,
     'Back restores the previous filters and closes the new item.');
   }
  }else{
   check(!row && history.length===previous,'Send keeps a hidden item outside the view.');
   check(review.querySelector('[data-review-notice]').textContent==='Saved '+fixture.id+'.',
         'A hidden item has a save confirmation.');
  }
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        markup = before.render().decode().replace('<head>', '<head>'+setup, 1).replace('</body>', probe+'</body>', 1)
        return markup.encode()

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
        _ = self.investigation()
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
 const panel=key=>row(key).querySelector('.review-panel');
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
   check(row(key).querySelector('[data-detail]').textContent.includes('Loading the item'),key+': loading text');
   check(row(key).querySelector('.review-title').textContent.length>0,key+': the title remains visible while loading');
   const resolve=window.pendingBodies.get(key);
   check(typeof resolve==='function',key+': a body request starts');
   resolve(body);
   await flush();await flush();
   if(body===null) check(row(key).querySelector('[data-detail]').textContent.includes('not available'),key+': failure stays visible');
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
                    done = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', profile,
                        '--window-size', '390,900', '--screenshot', str(self.fixture.temporary/'review-titles.png'),
                        f'http://127.0.0.1:{server.server_port}/'], capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)
        self.assertEqual(results, [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_new_button_aligns_with_the_review_title_at_desktop_width(self) -> None:
        markup = review_page.build_snapshot(self.root).page.decode()
        probe = r'''<script>
(async()=>{
 const failures=[];
 frameElement.style.width='1280px';
 await document.fonts.ready;
 const group=document.querySelector('[role="group"][aria-label="Type"]');
 const button=document.querySelector('[data-review-new]');
 const heading=document.querySelector('h1.page-title'),label=document.createRange();
 label.selectNodeContents(heading);
 const bounds=button.getBoundingClientRect(),title=heading.getBoundingClientRect();
 const table=document.querySelector('.list-table').getBoundingClientRect();
 if(innerWidth!==1280 || bounds.width===0 || bounds.left<label.getBoundingClientRect().right ||
    Math.abs((bounds.top+bounds.bottom)-(title.top+title.bottom))>2 || Math.abs(bounds.right-table.right)>1){
  failures.push('The New button must align with the title at the right edge at 1280 px.');
 }
 if(group.contains(button)) failures.push('The Type chips must exclude the New button.');
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>'''
        markup = markup.replace('</body>', probe + '</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_owner_and_new_share_the_title_line(self) -> None:
        owner = replace(self.ready_submission_recipient(), name='Review and console delivery')
        markup = page_frame.with_owner(review_page.build_snapshot(self.root).page, 'review', owner,
                                       staff.StaffSnapshot((owner,)), new_item_dialog.render)
        probe = b'''<script>
(async()=>{
 const failures=[];
 try{
  const title=document.querySelector('h1.page-title'), row=title.closest('.page-heading');
  const name=document.querySelector('.page-owner-name'), owner=document.querySelector('[data-owner-new]');
  const button=document.querySelector('[data-review-new]');
  const bounds=element=>element.getBoundingClientRect(), center=box=>(box.top+box.bottom)/2;
  await document.fonts.ready;
  for(const width of [1280,390,320]){
   frameElement.style.width=width+'px';
   const t=bounds(title), n=bounds(name), o=bounds(owner), b=bounds(button), r=bounds(row);
   const beside=Math.abs(center(t)-center(b))<=1;
   if(innerWidth!==width || !row.contains(owner) || !row.contains(button) ||
      Math.abs(center(o)-center(b))>1 || Math.abs(center(n)-center(b))>1 ||
      Math.abs((o.left-n.right)-(b.left-o.right))>1 || o.left<n.right+4 ||
      n.left<r.left-1 || Math.abs(b.right-r.right)>1 ||
      (beside ? n.left<t.right+4 : width===1280 || b.top<t.bottom+4)){
    failures.push('The title and both controls must fit at '+width+' px.');
   }
   if(width===320 && Math.abs(t.height-parseFloat(getComputedStyle(title).lineHeight))>1){
    failures.push('The Review title fits on one line at 320 px.');
   }
   if(width===320 && (button.innerText.trim()!=='+' || button.getAttribute('aria-label')!=='New item' ||
                     button.title!=='New item')){
    failures.push('The narrow New item button shows only + and keeps its name.');
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
  check(posts[0]===action && !row.isConnected,'The action posts and removes the record.');
  const motion=()=>document.querySelector('[data-record-motion]');
  if(REDUCED){
   check(!motion() && Math.abs(top(next)-first)<2,'Reduced motion moves the next card at once.');
  }else{
   const clip=motion()?.querySelector('.record-motion-clip'),exit=clip?.getAnimations()[0];
   if(!exit) throw new Error('The shared exit animation is missing.');
   check(motion().querySelector('[data-detail]:not([hidden])'),'The motion copy includes the open card.');
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
 const panel=panelId(row),next=row.nextElementSibling;
 row.querySelector('.list-toggle').click();
 const form=await readyReviewCard(row);
 row.querySelector('[data-detail]>td').style.height='1600px';
 await frame();
 scrollBy(0,top(next)-540);await frame();
 const before=top(row),following=top(next);
 check(scrollY>0 && before<0 && following>0 && following<innerHeight,'The tall card and next card share the scrolled viewport.');
 send(form);
 const clip=copy(panel);
 check(clip && near(top(clip),before),'The tall copy starts at the original position.');
 check(next.isConnected && near(top(next),following),'The visible next card keeps its position.');
 const exit=clip.getAnimations()[0];
 halfway(exit);dispatchEvent(new Event('scroll'));await frame();
 check(near(top(clip),before),'The shrinking copy keeps its place after a redraw.');
 check(next.isConnected && top(next)<following,'The next card stays mounted while it moves up.');
 const saveTop=scrollY,height=clip.getBoundingClientRect().height,key=row.dataset.key;
 let redraws=0;
 const observer=new MutationObserver(records=>redraws+=records.length);
 observer.observe(document.querySelector('[data-owner-chip=""] b'),{childList:true});
 scrollBy(0,innerHeight+50);await frame();
 check(scrollY-saveTop>=innerHeight,'The page scrolls one viewport during the save.');
 check(copy(panel)===clip && near(clip.getBoundingClientRect().height,height),
  'A scroll during the save keeps the same fade at the same height.');
 scrollTo(0,saveTop);await frame();
 check(redraws===0 && !rows().some(row=>row.dataset.key===key),
  'The pending card stays out of the window during the fade.');
 exit.finish();await exit.finished;await frame();
 check(!copy(panel),'The tall copy disappears when the fade ends.');
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
 const selected=()=>document.querySelector('[data-owner-chip][aria-pressed="true"]');
 document.querySelector('[data-sort="3"]').click();
 owner.click();
 const [row]=rows(),panel=panelId(row);
 const form=await openCard(row);
 form.querySelector('[data-later-toggle]').click();
 form.querySelector('[data-later-preset="soon"]').click();
 const clip=copy(panel),exit=clip.getAnimations()[0];
 halfway(exit);
 const position=top(clip);
 posts.get(row.dataset.key)({ok:true,json:async()=>({answer:'later',state:'Open',later:Date.now()+3600000})});
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
 const clip=copy(panelId(row));
 const motion=clip?.getAnimations()[0];
 if(!motion || !posts.has(row.dataset.key)) throw new Error('The save must start the fade.');
 posts.get(row.dataset.key)({ok:true,json:async()=>({answer:'saved',state:action.state,later:0})});
 motion.finish();await frame();
 if(action.state==='Done') check(!rows().some(item=>item.dataset.key===row.dataset.key),
  'The completed item leaves Review.');
 const place=new URLSearchParams(location.hash.slice(1));
 check(Object.entries(filters).every(([key,value])=>place.get(key)===value),
  'The card submit keeps every filter in the address.');
 check(document.querySelector('[data-type-chip][aria-pressed="true"]').dataset.typeChip===filters.type &&
  document.querySelector('[data-show-chip][aria-pressed="true"]').dataset.showChip===filters.show &&
  document.querySelector('[data-owner-chip][aria-pressed="true"]').dataset.ownerChip===filters.owner &&
  document.querySelector('#list-search').value===filters.q,'The card submit keeps each selected control.');
""")

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_refused_card_reverses_above_the_next_departing_card(self) -> None:
        self.check_card_motion(browser_markup(self.root), r"""
 const [first,second,next]=rows(),firstPanel=panelId(first),secondPanel=panelId(second);
 const firstForm=await openCard(first);send(firstForm);
 const firstClip=copy(firstPanel);halfway(firstClip.getAnimations()[0]);
 const secondForm=await openCard(second);send(secondForm);
 const secondClip=copy(secondPanel);halfway(secondClip.getAnimations()[0]);
 const height=firstClip.getBoundingClientRect().height,opacity=Number(getComputedStyle(firstClip).opacity);
 const following=top(next);
 posts.get(first.dataset.key)({ok:false,json:async()=>({reason:'Try again.'})});
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
 const [first,second]=rows(),firstPanel=panelId(first),secondPanel=panelId(second);
 const firstKey=first.dataset.key;
 send(await openCard(first));
 const firstExit=copy(firstPanel).getAnimations()[0];
 halfway(firstExit);
 send(await openCard(second));
 const secondClip=copy(secondPanel),secondExit=secondClip.getAnimations()[0];
 halfway(secondExit);
 posts.get(firstKey)({ok:false,json:async()=>({reason:'Try again.'})});
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
 const [first,row,next]=rows(),panel=panelId(row);
 check([first,row,next].every(node=>node.dataset.state==='Open'),'Three open cards are sorted by State.');
 send(await openCard(row));
 const clip=copy(panel),exit=clip.getAnimations()[0];
 halfway(exit);
 const position=top(clip),following=top(next);
 check(top(first)<position && position<following,'The departing card starts between its neighbors.');
 posts.get(row.dataset.key)({ok:true,json:async()=>({answer:'works',state:'Done',later:0})});
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
 const row=rows()[1],key=row.dataset.key,panel=panelId(row);
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
 posts.get(key)({ok:true,json:async()=>({answer:'works',state:'Done',later:0})});
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
 const panelId=row=>row.querySelector('.list-toggle').getAttribute('aria-controls');
 const copy=panel=>document.querySelector('.record-motion-clip [aria-controls="'+CSS.escape(panel)+'"]')?.closest('.record-motion-clip');
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
        setup = r"""<script>
const sendProbe=fetch;
window.setInterval=()=>0;
window.fetch=(url,options={})=>{
 if(options.method==='POST') return Promise.resolve(Response.json({answer:'works'}));
 return sendProbe(url,options);
};
if(!sessionStorage.getItem('review-reload')) history.replaceState(null,'','#item=page-owner%2FC1');
</script>"""
        probe = r"""<script>
(async()=>{
 const failures=[],key='page-owner/C1',check=(condition,message)=>{if(!condition) failures.push(message);};
 const content=document.querySelector('[data-review]');
 const row=()=>content.querySelector('[data-key="'+key+'"]');
 const fetchCard=fetch;
 window.fetch=(url,options)=>new URL(url,location.href).search==='?version=1'
  ? Promise.resolve(Response.json({version:content.dataset.version,facts:pageRefreshFacts()}))
  : fetchCard(url,options);
 try{
  if(!sessionStorage.getItem('review-reload')){
   if(CASE==='verdict'){
    const form=await readyReviewCard(row());
    const saved=new Promise(resolve=>{
     const observer=new MutationObserver(()=>{
      if(!form.dataset.sending){observer.disconnect();resolve();}
     });
     observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
    });
    form.requestSubmit(form.querySelector('[value="works"]'));
    await saved;
    history.replaceState(null,'','#item=page-owner%2FC1');
   }else content.querySelector('[data-show-chip="open"]').click();
   sessionStorage.setItem('review-reload','1');
   location.reload();return;
  }
  if(CASE==='verdict'){
   const savedKey='review-saved-verdicts:'+location.pathname;
   check(!row() && !content.querySelector('[data-missing-item]').hidden && sessionStorage.getItem(savedKey),
    'A reload hides the completed Change while the snapshot still contains it.');
   const index=JSON.parse(content.querySelector('[data-review-index]').textContent);
   index.r=index.r.filter(entry=>entry[0]!==key);
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
   check(!row() && !sessionStorage.getItem(savedKey),
    'The new snapshot confirms the removal and clears the saved verdict marker.');
  }else{
   check(content.dataset.show==='open' && !row(),'A reload restores Open when the address names a Done Change.');
  }
 }catch(error){failures.push(String(error));}
 await sendProbe('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        for case in ('verdict', 'show'):
            with self.subTest(case=case):
                if case == 'show':
                    _ = change_review.record_verdict(self.fixture.review_list, 'C1', 'works',
                                                     datetime.now(user_zone()).date(), self.fixture.production)
                page = browser_markup(self.root).replace('<head>', '<head>'+setup, 1)
                page = page.replace('</body>', probe.replace('CASE', json.dumps(case))+'</body>', 1)
                self.assertEqual(firefox_probe_results(page.encode()), [[]])

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
  const panel=questionForm.closest('[data-list-item]').querySelector('.list-toggle').getAttribute('aria-controls');
  const clip=content.querySelector('.record-motion-clip [aria-controls="'+CSS.escape(panel)+'"]')
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
   if(show()!=='all' || row(open)?.querySelector('[data-detail]').hidden!==false || row(done))
    failures.push('An explicit link opens the active Change under All and leaves the completed Change hidden.');
   frame.contentDocument.querySelector('[data-show-chip="open"]').click();
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
 const selectShow=value=>document.querySelector('[data-show-chip="'+value+'"]').click();
 const selectedOwner=()=>document.querySelector('[data-owner-chip][aria-pressed="true"]').dataset.ownerChip;
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
 const expanded=(row,state)=>row?.dataset.state===state && !row.hidden && !row.querySelector('[data-detail]').hidden;
 try{
  const owner=change('page-owner/C1').dataset.owner;
  document.querySelector('[data-owner-chip="'+CSS.escape(owner)+'"]').click();
  selectShow('open');
  if(selectedOwner()!==owner) document.querySelector('[data-owner-chip="'+CSS.escape(owner)+'"]').click();
  check(expanded(await follow({}),'Open') && review.dataset.show==='open' && selectedOwner()===owner,
   'F003: A bare link preserves the matching Show and Owner filters.');
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
    const clip=document.querySelector('.record-motion-clip [aria-controls="'+
     CSS.escape(row.querySelector('.list-toggle').getAttribute('aria-controls'))+'"]')?.closest('.record-motion-clip');
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
   check(form.querySelector('[data-send-status]').textContent==='Saved: works',key+' saved.');
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
        update_registration.write_question_record(question_item.source.record)
        _ = self.investigation()
        _ = self.investigation(False)
        for _ in range(3):
            done = self.investigation()
            _ = investigation.mark_read(done.owner, done.id)
        source = browser_markup(self.root)
        setup = r"""<script>
const nativeFetch=fetch, nativeTimeout=setTimeout, original=MARKUP;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.refreshMarkup=original;
window.transformFixtureBody=(text,key)=>{
 const panel=document.createElement('div');panel.innerHTML=text;
 if(key.startsWith('question/')){
  const form=panel.querySelector('form');form.elements.token.value+='-refreshed';form.elements.row.value+='-refreshed';
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
  return {ok:true,json:async()=>({answer:'works'})};
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
  return {save:()=>finish({ok:true,json:async()=>({answer:'works'})}),
   refuse:reason=>finish({ok:false,json:async()=>({reason})})};
 };
 const visibleStatus=text=>[...document.querySelectorAll('[data-send-status],[data-hidden-action-status]')]
   .some(node=>node.textContent.includes(text) && node.getClientRects().length);
 const leavingNow=row=>!row.isConnected && Boolean(document.querySelector('[data-record-motion]'));
 const selectedOwner=(doc=document)=>doc.querySelector('[data-owner-chip][aria-pressed="true"]');
 const choose=(id,value,event='change')=>{
  if(id==='list-owner'){document.querySelector('[data-owner-chip="'+value+'"]').click();return;}
  if(id==='list-show'){document.querySelector('[data-show-chip="'+value+'"]').click();return;}
  const field=document.getElementById(id);field.value=value;field.dispatchEvent(new Event(event,{bubbles:true}));
 };
 const showValue=(doc=document)=>doc.querySelector('[data-show-chip][aria-pressed="true"]').dataset.showChip;
 try{
  await flush();
  check(shown().length===5 && shown().every(row=>row.dataset.state==='Open'),'Open includes only Open records');
  check(document.querySelector('[data-show-chip="open"] b').textContent==='5',
    'F007: Open counts only Open records');
  check(document.querySelector('[data-show-chip="Validating"]'),
    'The Show row includes Validating.');
  document.querySelector('[data-type-chip="question"]').click();
  choose('list-search','Q1','input');check(shown().length===1,'type and ID filters');
  choose('list-search','','input');document.querySelector('[data-type-chip=""]').click();
  const question=rows().find(row=>row.dataset.type==='question');question.querySelector('.list-toggle').click();await readyReviewCard(question);
  const title=question.querySelector('.review-title'),titleHeight=title.getBoundingClientRect().height;
  title.textContent='A long review title '.repeat(20);
  check(title.getBoundingClientRect().height===titleHeight && title.scrollWidth>title.clientWidth,
    'a long row title stays on one line and hides its overflow');
  const change=rows().find(row=>row.dataset.type==='change');change.querySelector('.list-toggle').click();await readyReviewCard(change);
  check(question.querySelector('[data-detail]').hidden,'only one card opens');
  question.querySelector('.list-toggle').click();await readyReviewCard(question);
  const draftLater=question.querySelector('[name="later"]');draftLater.value='2098-12-31T16:30';
  draftLater.dispatchEvent(new Event('change',{bubbles:true}));
  const picked=question.querySelector('input[name=choice]');picked.click();
  const draft=question.querySelector('textarea');draft.value='Keep this draft';draft.dispatchEvent(new Event('input'));
  draft.focus();draft.setSelectionRange(2,7,'backward');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  let fresh=rows().find(row=>row.dataset.type==='question');
  check(!fresh.querySelector('[data-detail]').hidden && fresh.querySelector('textarea').value==='Keep this draft','refresh keeps the card and draft');
  const refreshedDraft=fresh.querySelector('textarea');
  check(document.activeElement===refreshedDraft && refreshedDraft.selectionStart===2 &&
    refreshedDraft.selectionEnd===7 && refreshedDraft.selectionDirection==='backward','refresh keeps focus and selection');
  check(fresh.querySelector('[name="later"]').value==='2098-12-31T16:30','F018: refresh keeps the picked Later date and time');
  check(fresh.querySelector('input[name=choice]:checked')?.value===picked.value,'F018: refresh keeps the selected choice');
  check(fresh.querySelector('form').elements.row.value===fresh.dataset.key+'-refreshed' &&
    fresh.querySelector('form').elements.token.value.endsWith('-refreshed'),'F018: refresh supplies the row and token');
  const [initialTarget,other]=rows().filter(row=>row.dataset.type==='change');
  const afterRefresh=()=>new Promise(resolve=>
   document.querySelector('[data-review]').addEventListener('review-refreshed',resolve,{once:true}));
  const liveRow=key=>rows().find(row=>row.dataset.key===key);
  other.querySelector('.list-toggle').click();
  let otherForm=await readyReviewCard(other);
  let rejected=holdPost();
  otherForm.requestSubmit(otherForm.querySelector('[value="works"]'));
  check(leavingNow(other),'The row starts to leave before the response.');
  let refreshed=afterRefresh();rejected.refuse('Fixture older refusal');await refreshed;
  const returnedOther=liveRow(other.dataset.key);
  otherForm=await readyReviewCard(returnedOther);
  check(visibleStatus('Not saved: Fixture older refusal'),'A refused row shows the reason.');
  rejected=holdPost();otherForm.requestSubmit(otherForm.querySelector('[value="works"]'));
  refreshed=afterRefresh();rejected.refuse('Fixture newer refusal');await refreshed;
  check(!visibleStatus('Not saved: Fixture older refusal'),'A later refusal replaces the previous reason.');
  const target=liveRow(initialTarget.dataset.key);target.querySelector('.list-toggle').click();await readyReviewCard(target);
  const form=target.querySelector('form'),savedResponse=holdPost();
  form.requestSubmit(form.querySelector('[value="works"]'));
  check(leavingNow(target),'A save starts the departure before the response.');
  savedResponse.save();
  await flush();check(form.querySelector('[data-send-status]').textContent==='Saved: works','the row shows the saved result');
  check(!visibleStatus('Saved: works'),'a successful save adds no notice above the table');
  check(!visibleStatus('Not saved: Fixture newer refusal'),'saving another card clears the failure notice');
  await new Promise(resolve=>nativeTimeout(resolve,950));
  check(target.hidden,'the saved row leaves the view');
  check(!visibleStatus('Saved: works'),'no save notice appears after the animation');
  check(document.querySelector('[data-show-chip="open"] b').textContent==='4','the count excludes the saved row');
  check(rows().every(row=>row.querySelector('[data-detail]').hidden),'no next card opens');
  choose('list-show','all');
  const allTarget=rows().find(row=>row.dataset.type==='change' && row!==target);allTarget.querySelector('.list-toggle').click();await readyReviewCard(allTarget);
  const allForm=allTarget.querySelector('form');allForm.requestSubmit(allForm.querySelector('[value="works"]'));
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  check(allTarget.hidden,'F010: the saved row leaves All');
  location.hash=new URLSearchParams({item:target.dataset.key});await flush();
  const reopened=liveRow(target.dataset.key);
  check(!reopened, 'A same-tab link keeps the completed Change hidden.');
  check(!liveRow(allTarget.dataset.key),'F010: the unlinked saved Change stays hidden in All.');
  const reloaded=document.createElement('iframe');reloaded.src='/console/review.html#'+new URLSearchParams({item:target.dataset.key});
  const reloadComplete=new Promise(resolve=>reloaded.addEventListener('load',resolve,{once:true}));document.body.append(reloaded);await reloadComplete;
  const linkedTarget=reloaded.contentDocument.querySelector('[data-key="'+target.dataset.key+'"]');
  check(linkedTarget.dataset.state==='Open' && !linkedTarget.hidden && !linkedTarget.querySelector('[data-detail]').hidden,
   'The fixture still shows the Change as Open on a fresh load.');
  check(showValue(reloaded.contentDocument)==='open' && selectedOwner(reloaded.contentDocument).dataset.ownerChip==='' &&
    reloaded.contentDocument.getElementById('list-search').value==='','A bare link keeps the default filters for an Open Change.');
  reloaded.remove();
  const retained={item:fresh.dataset.key,type:'question',owner:fresh.dataset.owner,show:'open',q:'Q1',sort:'Age',dir:'desc'};
  location.hash=new URLSearchParams(retained);await flush();
  fresh=rows().find(row=>row.dataset.type==='question');
  check(selectedOwner().dataset.ownerChip===retained.owner && showValue()==='open' &&
    document.getElementById('list-search').value==='Q1' && document.querySelector('[data-type-chip="question"]').getAttribute('aria-pressed')==='true',
    'F014: restore the filters with an open row');
  check(!fresh.hidden && !fresh.querySelector('[data-detail]').hidden,'F014: restore the open row');
  check(document.querySelector('[data-sort="5"]').parentElement.getAttribute('aria-sort')==='descending','F014: restore the sort');

  const legacy=document.createElement('iframe');
  legacy.src='/console/changes.html?type=questions#'+new URLSearchParams({showrunner:'page-owner',show:'Waiting',q:'layout',sort:'Age',dir:'desc'});
  const loaded=new Promise(resolve=>legacy.addEventListener('load',resolve,{once:true}));document.body.append(legacy);await loaded;
  await flush();await flush();
  const restored=legacy.contentDocument;
  check(selectedOwner(restored).dataset.ownerChip===fresh.dataset.owner &&
    selectedOwner(restored).textContent==='page-owner 1' && showValue(restored)==='all' &&
    restored.getElementById('list-search').value==='layout' && restored.querySelector('[data-type-chip="question"]').getAttribute('aria-pressed')==='true',
    'F012: a legacy link selects All and retains the other filters.');
  check(restored.querySelector('[data-sort="5"]').parentElement.getAttribute('aria-sort')==='descending','F012: the legacy link keeps its sort');
  legacy.remove();

  check(document.documentElement.scrollWidth<=innerWidth,'no horizontal overflow');
  if(innerWidth<960) check(getComputedStyle(shown()[0]).display==='block','phone rows are cards');
  await readyReviewCard(fresh);
  const laterToggle=fresh.querySelector('[data-later-toggle]'),laterMenu=fresh.querySelector('[data-later-menu]');
  laterToggle.click();
  const menuItems=()=>[...laterMenu.querySelectorAll('[role=menuitem]')].filter(item=>!item.hidden);
  const presetTime=name=>laterMenu.querySelector('[data-later-preset="'+name+'"] small').textContent;
  check(!laterMenu.hidden && laterToggle.getAttribute('aria-expanded')==='true' && document.activeElement===menuItems()[0],
    'Later opens its menu with the first preset in focus');
  check(/^[A-Z][a-z]{2} [0-9]{2}:[0-9]{2}$/.test(presetTime('soon')) && /^[A-Z][a-z]{2} 09:00$/.test(presetTime('morning')) &&
    /^Mon 09:00$/.test(presetTime('week')),'each preset shows its resolved time in the user zone');
  menuItems()[0].dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true}));
  check(document.activeElement===menuItems().at(-1),'ArrowUp moves from the first item to the last');
  menuItems().at(-1).dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));
  check(document.activeElement===menuItems()[0],'ArrowDown moves from the last item to the first');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();
  check(fresh.isConnected && !laterMenu.hidden,'an open Later menu holds the page refresh');
  const chooserDraft=fresh.querySelector('[name="later"]');chooserDraft.value='';
  chooserDraft.dispatchEvent(new Event('input',{bubbles:true}));
  const chooserNow=Date.now;Date.now=()=>Date.parse('2026-11-30T15:00:00Z');
  laterMenu.querySelector('[data-later-choose]').click();
  const calendar=laterMenu.querySelector('[role=grid]'),timeField=laterMenu.querySelector('[data-later-time]');
  check(!laterMenu.querySelector('[data-later-custom]').hidden && document.activeElement===calendar?.querySelector('[tabindex="0"]'),
    'Choose a time shows the calendar with the selected day in focus');
  const chooserChecks=['the grid opens on tomorrow in the user zone','arrows move the selected day across months',
    'a past day cannot be picked','Save is disabled for a time before now','the summary shows the selected wall time',
    'the chooser fits within the menu and viewport'];
  let submitter='';const watch=event=>{submitter=event.submitter.value;event.preventDefault();event.stopImmediatePropagation();};
  fresh.querySelector('form').addEventListener('submit',watch,{capture:true});
  if(calendar){
   const wall=()=>fresh.querySelector('[name="later"]').value;
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
  fresh.querySelector('form').removeEventListener('submit',watch,{capture:true});
  check(submitter==='later','Enter in the chooser saves it as Later');
  document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  check(laterMenu.hidden && laterMenu.querySelector('[data-later-custom]').hidden && document.activeElement===laterToggle,
    'Escape closes the menu and returns focus to Later');
  Date.now=chooserNow;
  laterToggle.click();const week=laterMenu.querySelector('[data-later-preset="week"]'),weekAt=Number(week.dataset.at);
  const laterResponse=holdPost();
  week.click();
  check(leavingNow(fresh),'Later starts the departure before the response');
  check(document.querySelector('[data-show-chip="open"] b').textContent==='0' &&
    selectedOwner().dataset.ownerChip===retained.owner,'Later updates the count and keeps the owner while the request runs');
  laterResponse.save();
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  check(window.posts.at(-1).get('outcome')==='later' && window.posts.at(-1).get('later')===new Date(weekAt).toISOString(),
    'a preset sends its time at once');
  check(fresh.hidden,'Later removes the question from the page');
  check(document.querySelector('[data-show-chip="open"] b').textContent==='0','Later removes the question from the filtered count');
  const nativeNow=Date.now;Date.now=()=>new Date('2099-01-01T09:00').getTime()+1000;
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  let returned=rows().find(row=>row.dataset.type==='question');
  check(!returned.hidden && returned.dataset.state==='Open','Later returns the question with its original state');
  Date.now=nativeNow;
  returned.querySelector('.list-toggle').click();await readyReviewCard(returned);
  const answerForm=returned.querySelector('form');
  check(answerForm.elements.feedback.value==='' && answerForm.elements.later.value==='',
    'F081: a successful save and unmount discard the submitted draft.');
  answerForm.elements.later.value='';
  answerForm.requestSubmit(answerForm.querySelector('[value="later"]'));await flush();
  answerForm.requestSubmit(answerForm.querySelector('[value="answer"]'));await flush();
  check(answerForm.querySelector('[data-send-status]').textContent==='Saved: works',
    'F023: an empty Later does not block Answer');
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  choose('list-show','open');choose('list-search','','input');choose('list-owner','');
  document.querySelector('[data-type-chip=""]').click();
  const pending=rows().find(row=>row.dataset.number==='page-owner/c3'),pendingKey=pending.dataset.key;
  pending.querySelector('.list-toggle').click();await readyReviewCard(pending);
  const pendingForm=pending.querySelector('form');pendingForm.elements.feedback.value='Wait for a new answer.';
  const initialCount=Number(document.querySelector('[data-show-chip="open"] b').textContent);
  const questionResponse=holdPost();
  pendingForm.requestSubmit(pendingForm.querySelector('[value="question"]'));
  check(window.posts.at(-1).get('outcome')==='question' && leavingNow(pending) &&
    Number(document.querySelector('[data-show-chip="open"] b').textContent)===initialCount-1,
    'Ask question starts the departure and updates the count before the response');
  questionResponse.save();
  await new Promise(resolve=>nativeTimeout(resolve,1000));
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
   check(Number(document.querySelector('[data-show-chip="open"] b').textContent)===initialCount-(revealed?0:1),
     'F021: only the new Open response restores the count');
  }
  const beforeShowRequests=window.refreshRequests.length;
  choose('list-show','all');await flush();await flush();
  check(document.querySelector('[data-show-chip="all"] b').textContent==='3' &&
    rows().every(row=>row.dataset.state!=='Done'),
    'All excludes completed records, saved verdicts and the saved answer.');
  check(window.refreshRequests.length===beforeShowRequests,
    'the Show chips select rows without a request');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(new URL(window.refreshRequests.at(-1),location.href).searchParams.has('version') &&
    showValue()==='all','the refresh keeps the selected State view');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        self_root = self.root
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
                            _ = complete.wait(20)
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
                            done=subprocess.run(['firefox','--headless','--no-remote','--profile',profile,
                                '--window-size',f'{width},900','--screenshot',str(self.fixture.temporary/f'review-{width}.png'),
                                f'http://127.0.0.1:{server.server_port}/'],capture_output=True,text=True,timeout=30)
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
  const form=row.querySelector('form');
  const field=form.querySelector('[data-later-time]'),save=form.querySelector('[value="later"]');
  form.elements.later.value='2026-12-01T17:30';
  form.querySelector('[data-later-toggle]').click();form.querySelector('[data-later-choose]').click();
  check(field.type==='text' && field.value==='17:30',
   'The 17:30 draft shows 17:30 without AM or PM (input type: '+field.type+')');
  const key=name=>field.dispatchEvent(new KeyboardEvent('keydown',{key:name,bubbles:true,cancelable:true}));
  const type=value=>{field.value=value;field.dispatchEvent(new Event('input',{bubbles:true}));};
  field.focus();key('ArrowUp');
  check(field.value==='17:45' && form.elements.later.value==='2026-12-01T17:45','ArrowUp adds 15 minutes');
  key('ArrowDown');check(field.value==='17:30','ArrowDown subtracts 15 minutes');
  type('23:45');key('ArrowUp');
  check(field.value==='00:00' && form.elements.later.value==='2026-12-01T00:00','ArrowUp wraps at midnight');
  key('ArrowDown');
  check(field.value==='23:45' && form.elements.later.value==='2026-12-01T23:45','ArrowDown wraps at midnight');
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
    'An invalid time blocks Save and Enter: '+JSON.stringify(value));
  }
  const error=document.createElement('span');error.style.color='var(--error)';form.append(error);
  const errorColor=getComputedStyle(error).color,style=getComputedStyle(field);
  check(style.color===errorColor && style.borderTopColor===errorColor,'An invalid time uses the console error color');
  type('1730');
  let submitted=false;
  form.addEventListener('submit',event=>{
   event.preventDefault();event.stopImmediatePropagation();submitted=event.submitter===save;
  },{capture:true,once:true});
  key('Enter');
  check(field.value==='17:30' && field.getAttribute('aria-invalid')==='false' && !save.disabled && submitted,
   'Enter shows 17:30 and submits Save');
  save.click();
  check(posts.at(-1)?.get('later')==='2026-12-01T17:30:00.000Z','Save sends T17:30 in UTC');
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
 const form=()=>document.querySelector('[data-type="question"] form');
 const open=()=>{form().querySelector('[data-later-toggle]').click();form().querySelector('[data-later-choose]').click();};
 const close=()=>form().querySelector('[data-later-time]').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
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
   check(new Date(Number(preset.dataset.at)).toISOString()===expected,'F005: '+SCENARIO+' preset uses compatible resolution');
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
                            done = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', profile,
                                '--window-size', '1440,900', '--screenshot', str(self.fixture.temporary/'later-time.png'),
                                f'http://127.0.0.1:{server.server_port}/'], capture_output=True, text=True, timeout=30)
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
window.fetch=async()=>({ok:true,text:async()=>MARKUP});
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
   const original=row(),wall=original.querySelector('[name="later"]');
   wall.value=draft;wall.dispatchEvent(new Event('input',{bubbles:true}));
   dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
   await new Promise(resolve=>setTimeout(resolve,60));
   const current=row(),form=await readyReviewCard(current);
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
        expected = single.split('<template data-card-body>', 1)[1].split('</template>', 1)[0]
        probe = r"""<script>
(async()=>{
 const failures=[];
 try{
  const row=document.querySelector('[data-key="page-owner/C1"]');
  row.querySelector('.list-toggle').click();
  const form=await readyReviewCard(row);
  const template=document.createElement('template');
  template.innerHTML=EXPECTED;
  for(const selector of ['[data-review-body]','.list-log','.review-actions']){
   if(row.querySelector(selector).innerHTML!==template.content.querySelector(selector).innerHTML)
    failures.push('Review and the single card draw different '+selector+'.');
  }
  if(!form.elements.revision.value) failures.push('The Review action has no revision.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>""".replace("EXPECTED", json.dumps(expected).replace('</', '<\\/'))
        markup = browser_markup(self.root).replace('</body>', probe + '</body>')
        self.assertEqual(firefox_probe_results(markup.encode()), [[]])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_review_uses_the_shared_status_with_the_real_loader(self) -> None:
        _ = self.question()
        _ = self.investigation()
        assert_status_states(self, browser_markup(self.root),15)


if __name__ == '__main__':
    _ = unittest.main()
