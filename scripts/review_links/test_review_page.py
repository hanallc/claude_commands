"""Check the shared review records, signed actions and table behavior."""
from __future__ import annotations

import shutil
import json
import os
import re
import selectors
import socket
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Self, TextIO, cast
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import conf, urgency
from scripts.lib.user_zone import user_zone
import unittest
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from scripts.agents.remote import local_machine
from scripts.production.production_doc import read_production
from scripts.production.issues import Issue
from scripts.production import issues as issue_store
from scripts.production.staff_test_support import fixture_member
from scripts.production import change_review, change_routing, footer_counts, investigation, next_item, staff, update_registration
from scripts.review_links import change_browser, change_page, console, record_table, review_items, review_page
from scripts.review_links import list_page, test_change_page
from scripts.review_links import efficiency_reviews, review_actions, review_later
from scripts.review_links.test_page_frame import assert_status_states
from scripts.statusline import counts_row


def unpack_index(payload: str) -> list[review_page.ReviewIndexEntry]:
    """Decode the compact wire fields for exact index assertions."""
    packed = cast(dict[str,list[list[str | int | float]]],json.loads(payload))
    names = cast(list[list[str]],packed['n'])
    entries: list[review_page.ReviewIndexEntry] = []
    for row in packed['r']:
        key, item_id, kind, owner_ref, source_ref, title, state, filed, updated, hidden_until, urgency = row
        owner, source = names[int(owner_ref)], names[int(source_ref)]
        entries.append(review_page.ReviewIndexEntry(key=str(key), id=str(item_id or key),
            type=tuple(review_items.ReviewType)[int(kind)], owner=owner[0], sourceOwner=source[0],
            ownerName=owner[1], sourceName=source[1], title=str(title), state=tuple(review_items.ReviewState)[int(state)],
            filed=int(filed), updated=str(updated), hiddenUntil=float(hidden_until), urgency=str(urgency or 'normal')))
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

    @override
    def setUp(self) -> None:
        self.fixture = test_change_page.ChangePageTests('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.set_up_review(changes=3)
        self.configure_review()

    def configure_review(self) -> None:
        config = self.fixture.temporary / 'config'
        _ = shutil.copyfile(Path(__file__).resolve().parents[2]/'config/console.conf',config/'console.conf')
        self.root = self.fixture.root
        _ = change_page.console_folder(self.root)
        self.status = change_page.DeliveryStatus(self.root)
        document = self.fixture.production.read_text()
        _ = self.fixture.production.write_text(document+'- **Merge branch:** main\n- **Log:** progress.log\n')

    def question(self, title: str = 'Which layout should the page use?') -> str:
        return self.fixture.file_user_question(title)

    def investigation(self, ready: bool = True) -> investigation.Investigation:
        item = investigation.file(self.fixture.unit.id, 'Why did the gate stop?', 'Read the gate log.')
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
            _ = change_review.append_rows(target, [change_review.Visible(label, 'Check it', 'Proof')],
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

    def test_change_projection_isolates_an_unreadable_production_path(self) -> None:
        doc = self.fixture.temporary / 'unreadable.md'
        listing = change_review.list_file('unreadable')
        _ = doc.write_text(f'# Unreadable\n- **Change review:** `{listing}`\n')
        runner = fixture_member('showrunner', name='unreadable', doc=staff.LiveDoc(local_machine(), doc))
        _ = change_review.append_rows(listing, [change_review.Visible('Another Change', 'Check it', 'Proof')],
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
            self.assertEqual(snapshot.records[efficiency].state, review_items.ReviewState.DONE)
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
            done = snapshot.body('page-owner/C1').decode()
        self.assertNotIn('<form', shell.split('<script>')[0], 'The shell contains no card bodies or forms.')
        self.assertNotIn('<tbody data-list-item', shell.split('<script>')[0], 'The browser mounts summaries from the index.')
        self.assertIn('data-review-index', shell)
        self.assertIn('Which layout gives each review card enough space for its controls?', card)
        self.assertIn('data-review-form', card)
        revision = review_page.record_revision(snapshot.records[key], snapshot.assets)
        self.assertRegex(revision, r'^[0-9a-f]{16}$')
        self.assertIn(f'name="revision" value="{revision}"', card)
        self.assertIn('How to check', done, 'A Done key loads its body directly.')
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
        during_retry: list[review_items.ReviewState] = []

        def rebuild(root: Path) -> review_page.ReviewSnapshot:
            if read.is_set():
                during_retry.append(cache.get().records[key].state)
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
                self.assertEqual(cache.get().records[key].state, review_items.ReviewState.DONE)
            finally:
                release.set()
            self.assertTrue(retry.wait(TEST_HANG_GUARD_SECS))
            cache.synchronize()
        self.assertEqual(during_retry, [review_items.ReviewState.DONE])
        self.assertEqual(cache.get().records[key].state, review_items.ReviewState.DONE)

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
        page = review_page.render(self.root).decode()
        index = page.split('<script type="application/json" data-review-index>', 1)[1].split('</script>', 1)[0]
        self.assertEqual(next(entry['id'] for entry in unpack_index(index) if entry['key']==key), 'page-owner/'+path.stem)
        self.assertEqual(path.read_text(), legacy)
        record, = update_registration.selected_question_records([path], (), None)
        update_registration.write_question_record(record)
        page = review_page.render(self.root).decode()
        index = page.split('<script type="application/json" data-review-index>', 1)[1].split('</script>', 1)[0]
        self.assertEqual(next(entry['id'] for entry in unpack_index(index) if entry['key']==key), 'page-owner/Q1')
        self.assertIn('"review_id": "Q1"', path.read_text())

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_windowed_rows_keep_open_cards_and_drafts_across_scroll_and_refresh(self) -> None:
        key = self.question()
        records = review_items.read(self.root)
        question = next(item for item in records if item.key == key)
        assert isinstance(question.source,review_items.QuestionSource)
        question.source.record.item['parts']=update_registration.QuestionParts(
            ask='Which layout?',context='Keep the draft.',items=[],recommend='',choices=['Compact','Wide'])
        change = next(item for item in records if item.type == review_items.ReviewType.CHANGE)
        records = [question]+[replace(change,key=f'page-owner/C{number}',id=f'page-owner/C{number}',
            title=f'Review the completed change {number}',
            state=review_items.ReviewState.DONE if number%2 else review_items.ReviewState.OPEN)
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
  await until(()=>form('page-owner/C399'),'the cold Done card');
  check(window.coldBodyMounts===0,'The cold card request starts before the first summary mounts.');
  check(row('page-owner/C399').dataset.state==='Done' &&
   row('page-owner/C399').querySelector('.list-toggle').getAttribute('aria-expanded')==='true',
   'A Done item opens directly from its key.');
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
  let releaseBody,releaseSave,posts=0,conflict='';
  const delayedBody=new Promise(resolve=>{releaseBody=resolve;});
  const delayedSave=new Promise(resolve=>{releaseSave=resolve;});
  window.fetch=async(url,options)=>{
   if(options?.method==='POST' && url===location.pathname){
    if(conflict){
     await originalFetch(conflict);
     const reason=conflict==='/remove-conflict' ? 'This item is no longer on the list.' : 'This item is already Done.';
     return new Response(JSON.stringify({reason}),{status:409});
    }
    posts++;await delayedSave;return new Response(JSON.stringify({reason:'Retry the save.'}),{status:400});
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
  scrollTo(0,document.documentElement.scrollHeight);await until(()=>!row('page-owner/C2'),'the saving row to unmount');
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
  stale.requestSubmit(stale.querySelector('button[value=works]'));
  await until(()=>row('page-owner/C4')?.dataset.state==='Done' && form('page-owner/C4'),'the Done conflict form');
  check(document.querySelector('[data-review]').dataset.show==='Done' &&
   form('page-owner/C4').querySelector('[data-send-status]').textContent.includes('This item is already Done.'),
   'F015: HTTP 409 refreshes the index and reveals the Done card with the reason.');
  conflict='/remove-conflict';
  const removed=form('page-owner/C4');
  removed.elements.later.value='2030-12-02T18:30';
  removed.requestSubmit(removed.querySelector('button[value=later]'));
  await until(()=>!row('page-owner/C4'),'the removed conflict row');
  const notice=document.querySelector('[data-review-notice]');
  check(notice && !notice.hidden && notice.textContent.includes('This item is no longer on the list.'),
   'F029: the page retains the conflict reason when refresh removes the card.');
 }catch(error){failures.push(String(error), ...window.coldErrors);}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>"""
        probe = (probe.replace('DRAFT_KEY', json.dumps(key)).replace('EXPECTED_TOKEN', json.dumps(expected_token))
                 .replace('EXPECTED_REVISION', json.dumps(expected_revision)))
        results: list[list[str]] = []
        requested_bodies: list[str] = []
        complete = threading.Event()
        current = [initial]
        with patch.object(review_items,'read',return_value=[
                replace(item,title='A newer summary') if item.key=='page-owner/C4' else item
                for item in changed]):
            newer = review_page.build_snapshot(self.root)
        with patch.object(review_items, 'read', return_value=[
                replace(item, state=review_items.ReviewState.DONE, actions=()) if item.key=='page-owner/C4' else item
                for item in newer.records.values()]):
            accepted = review_page.build_snapshot(self.root)
        with patch.object(review_items, 'read', return_value=[
                item for item in accepted.records.values() if item.key != 'page-owner/C4']):
            deleted = review_page.build_snapshot(self.root)
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
        self.assertEqual(set(requested_bodies),{'page-owner/C399',key,'page-owner/C2','page-owner/C4'},
                         'Only opened keys request a body.')

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_tab_reaches_adjacent_rows_after_a_distant_scroll(self) -> None:
        change = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        records = [replace(change, key=f'page-owner/C{number}', id=f'page-owner/C{number}')
                   for number in range(1, 401)]
        with patch.object(review_items, 'read', return_value=records):
            page = self.fixture.temporary / 'tab-review.html'
            _ = page.write_bytes(review_page.build_snapshot(self.root).render())
        profile = self.fixture.temporary / 'tab-profile'
        profile.mkdir()
        _ = (profile/'user.js').write_text('user_pref("marionette.port", 0);\n')
        with subprocess.Popen(['firefox', '--headless', '--no-remote', '--profile', str(profile), '--marionette',
                               'about:blank'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as browser:
            try:
                self.check_firefox_tab_order(browser, page)
            finally:
                browser.kill()

    def check_firefox_tab_order(self, browser: subprocess.Popen[bytes], page: Path) -> None:
        """Send real Tab keys through Firefox's Marionette connection."""
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
            _ = command('WebDriver:Navigate', url=page.as_uri())
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

    def test_showrunner_check_waits_and_feedback_closes_the_change(self) -> None:
        key = 'page-owner/C1'
        delivery,_ = review_page.submit(self.root,self.body(key,'validate'),self.status)
        change_page.deliver(delivery,self.status)
        self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == key),review_items.ReviewState.WAITING)
        assert isinstance(delivery, change_page.MessageDelivery)
        _ = change_review.save_verdict(self.fixture.review_list, 'C1', 'not works: the spacing differs',
                                       self.fixture.production, validation_id=delivery.attempt)
        returned = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(returned.state, review_items.ReviewState.OPEN)
        self.assertIn('the spacing differs', returned.log[-1].words)
        _,answer = review_page.submit(self.root,self.body('page-owner/C2','feedback','Adjust the spacing.'),self.status)
        self.assertEqual(answer,'feedback sent')
        self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == 'page-owner/C2'),review_items.ReviewState.DONE)

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
        self.assertIn('>Later</button>',cards)
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
        self.assertEqual({item.type for item in actionable}, set(review_items.ReviewType))
        self.assertEqual({item.state.value for item in actionable}, {'Open'})
        self.assertEqual(tuple(state.value for state in review_items.ReviewState), ('Open', 'Waiting', 'Done'))
        self.assertTrue(all(item.actions for item in actionable))
        self.assertEqual(review_items.read(self.root, show='open'), actionable)
        self.assertEqual(review_page.SHOWS,
                         (('open', 'Open'), ('Waiting', 'Waiting'), ('Done', 'Done'), ('all', 'All')))
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
        self.assertEqual(len(entries), len(records))
        self.assertEqual({entry['state'] for entry in entries}, {'Open', 'Waiting', 'Done'})
        self.assertIn(f'investigation/{waiting.owner}/{waiting.id}', {entry['key'] for entry in entries})
        self.assertEqual(set(entries[0]), {'key', 'id', 'type', 'owner', 'sourceOwner', 'ownerName',
                                        'title', 'state', 'filed', 'updated', 'hiddenUntil', 'sourceName', 'urgency'})
        self.assertEqual([entry['updated'] for entry in entries], [item.times.updated.isoformat() for item in records])
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
        _ = review_page.submit(self.root, self.body('page-owner/C1', 'validate'), self.status)
        waiting_keys = ['page-owner/C1', f'investigation/{waiting.owner}/{waiting.id}']
        done_keys = [f'investigation/{finished.owner}/{finished.id}']
        open_keys = ['page-owner/C2', 'page-owner/C3', question, f'investigation/{ready.owner}/{ready.id}', *proposals]
        expected = {'open':open_keys, 'Waiting':waiting_keys, 'Done':done_keys, 'all':open_keys+waiting_keys+done_keys}
        pages = {show: browser_markup(self.root, show=show) for show, _ in review_page.SHOWS}
        setup = r"""<script>
const nativeFetch=fetch,nativeTimeout=setTimeout;
window.setInterval=()=>0;window.clearInterval=()=>{};
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
    'open|Waiting|Done|all','Show chips have the required order');
  check(selected('open'),'exactly one Open chip starts pressed');
  check(exactKeys('open'),'F020: Open mounts the exact Open keys');
  check(noMountedKey(EXPECTED.Done[0]),'F064: Open does not mount the Done investigation');
  check(noMountedKey(EXPECTED.Waiting[1]),'F064: Open does not mount the Waiting investigation');
  check(noMountedState('Done'),'F064: the initial Open view mounts no Done rows');
  check(count('open')?.textContent==='6' && count('Waiting')?.textContent==='2' &&
    count('Done')?.textContent==='1' && count('all')?.textContent==='9',
    'F001: Open shows complete counts, including the active check in Waiting');
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
  chip('Waiting')?.focus();chip('Waiting')?.click();await flush();await flush();
  check(exactKeys('Waiting'),'F020: Waiting mounts the exact Waiting keys');
  check(count('Waiting')?.textContent==='2','F001: Waiting retains its complete count after the load');
  check(document.activeElement===chip('Waiting'),'F002: the pressed Show chip keeps focus after the load');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(document.activeElement===chip('Waiting'),'F002: the Show chip keeps focus after refresh');
  const typeChip=()=>document.querySelector('[data-type-chip="change"]');
  typeChip().focus();typeChip().click();
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(document.activeElement===typeChip(),'F002: the type chip keeps focus after refresh');
  document.querySelector('[data-type-chip=""]').click();
  chip('Done')?.click();await flush();await flush();
  check(selected('Done') && new URLSearchParams(location.hash.slice(1)).get('show')==='Done',
    'a Show chip changes the pressed state and address');
  check(document.querySelector('[data-review]').dataset.show==='Done' &&
    [...document.querySelectorAll('[data-list-item]')].every(row=>row.dataset.state==='Done'),
    'a Show chip loads only the selected server rows');
  check(exactKeys('Done'),'F020: Done mounts the exact Done keys');
  check(noMountedKey(EXPECTED.Waiting[1]),'F064: Done does not mount the Waiting investigation');
  check(noMountedState('Open'),'F064: Done mounts no Open rows');
  check(noMountedState('Waiting'),'F064: Done mounts no Waiting rows');
  check(count('Done')?.textContent==='1' && count('open')?.textContent==='6' && count('Waiting')?.textContent==='2' &&
    [...document.querySelectorAll('[data-show-chip] b')].every(number=>!number.hidden),
    'F001: Done retains all complete Show counts');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(selected('Done') && new URL(window.refreshRequests.at(-1),location.href).searchParams.has('version'),
    'refresh checks the version and retains the pressed Show view');
  const reload=document.createElement('iframe');reload.src='/reload'+location.hash;
  const loaded=new Promise(resolve=>reload.addEventListener('load',resolve,{once:true}));document.body.append(reload);await loaded;
  await flush();await flush();
  check(selected('Done',reload.contentDocument) && reload.contentDocument.querySelector('[data-review]').dataset.show==='Done',
    'a reload keeps the shared Show address');reload.remove();
  for(const value of ['Waiting','all','open']){
   chip(value)?.click();await flush();await flush();
   check(selected(value),'exactly one chip stays pressed for '+value);
   check(exactKeys(value),'F020: the selected view mounts its exact keys for '+value);
   if(value!=='all') check(noMountedState('Done'),'F064: changing to '+value+' mounts no Done rows');
   if(value==='open') check(decisions().length===2 && decisions().every(row=>!row.hidden),
    'Open retains the two decision rows after a Show load');
  }
  chip('all')?.click();await flush();await flush();
  check(['open','Waiting','Done','all'].map(value=>count(value)?.textContent).join('|')==='6|2|1|9' &&
    [...document.querySelectorAll('[data-show-chip] b')].every(number=>!number.hidden),
    'All supplies the count for every Show chip');
  const groups=[...document.querySelectorAll('.review-types')],buttons=[...document.querySelectorAll('[data-show-chip]')];
  check(groups.length===2 && groups[1].getBoundingClientRect().top>=groups[0].getBoundingClientRect().bottom,
    'Show chips appear below the type chips');
  check(buttons.length===4 && buttons.every(button=>button.type==='button' && button.tabIndex===0),
    'Show chips use native keyboard buttons in tab order');
  chip('Waiting')?.focus();
  check(document.activeElement===chip('Waiting'),'a Show chip accepts keyboard focus');
  check(document.documentElement.scrollWidth<=innerWidth && groups.every(group=>
    getComputedStyle(group).gap==='8px' && [...group.querySelectorAll('button')].every(button=>{
     const box=button.getBoundingClientRect();return box.left>=0 && box.right<=innerWidth;
    })),'both chip rows wrap with even spacing and no clipping');

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
  const menu=()=>document.querySelector('#list-owner');
  const options=()=>[...menu().options];
  const chooseOwner=value=>{menu().value=value;menu().dispatchEvent(new Event('change'));};
  const chooseType=value=>document.querySelector('[data-type-chip="'+value+'"]').click();
  const builder=document.querySelector('[data-type="investigation"]').dataset.sourceOwner;
  chip('open').click();await flush();await flush();
  chooseOwner(builder);chooseType('question');
  check(options().length===2 && options()[0].textContent==='All (1)' &&
    options().some(option=>option.textContent==='page-owner (1)') &&
    !options().some(option=>option.value===builder),
    'F007: Owner lists only owners with the selected type and words in any Show.');
  check(menu().value==='' && !new URLSearchParams(location.hash.slice(1)).get('owner') &&
    ['open','Waiting','Done','all'].map(value=>count(value)?.textContent).join('|')==='1|0|0|1',
    'F007: an owner without the selected type resets to All with matching counts.');
  check([...document.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1,
    'F007: the question appears after the owner resets.');
  chooseType('investigation');
  check(options().some(option=>option.value===builder) && options()[0].textContent==='All (1)',
    'a type change restores owners and counts');
  chooseOwner(builder);chip('Done').click();await flush();await flush();
  check(options()[0].textContent==='All (1)' && menu().value===builder,
    'Show limits Owner counts and retains an owner with matching loaded rows');
  chip('open').click();await flush();await flush();
  chooseType('question');chooseOwner(options().find(option=>option.dataset.ownerName==='page-owner')?.value);
  chip('Waiting').click();await flush();await flush();
  check(options().length===2 && options()[0].textContent==='All (0)' && menu().value!=='' &&
    options()[1].textContent==='page-owner (0)',
    'Show retains the selected owner with a zero count.');
  chip('open').click();await flush();await flush();
  check(options().length===2 && options()[0].textContent==='All (1)','Show restores matching owners');
  chooseType('investigation');chooseOwner(builder);
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(menu().value===builder && options()[0].textContent==='All (1)' &&
    options().every(option=>!option.textContent.endsWith('(0)')),'refresh retains the matching owner list and selection');
  const legacy=document.createElement('iframe');
  legacy.src='/reload#'+new URLSearchParams({owner:builder,type:'question',show:'open'});
  const legacyLoaded=new Promise(resolve=>legacy.addEventListener('load',resolve,{once:true}));document.body.append(legacy);
  await legacyLoaded;await flush();await flush();
  check(legacy.contentDocument.querySelector('#list-owner').value==='' &&
    [...legacy.contentDocument.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1 &&
    !new URLSearchParams(legacy.contentWindow.location.hash.slice(1)).get('owner'),
    'F007: a legacy owner without the selected type resets to All.');legacy.remove();
  chooseType('question');
  const navigated=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams({owner:'builder-unit',type:'investigation',show:'open'});
  await navigated;await flush();await flush();
  check(menu().value===builder && [...document.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1,
    'F004: hash navigation resolves an owner name whose option was hidden by Questions');
  const translated=document.createElement('iframe');
  translated.src='/reload#'+new URLSearchParams({legacy:'investigation',owner:'no-investigations',type:'investigation',show:'not-read'});
  const translatedLoaded=new Promise(resolve=>translated.addEventListener('load',resolve,{once:true}));
  document.body.append(translated);await translatedLoaded;await flush();await flush();
  const translatedPage=translated.contentDocument,translatedAddress=new URLSearchParams(translated.contentWindow.location.hash.slice(1));
  check(translatedPage.querySelector('#list-owner').value==='' &&
    translatedPage.querySelector('#list-owner').selectedOptions[0].textContent==='All (1)' &&
    !translatedAddress.get('owner') && !translatedAddress.has('source-owner') &&
    [...translatedPage.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden).length===1 &&
    count('open',translatedPage)?.textContent==='1',
    'F003: a legacy investigation reset clears both owner constraints and shows the All rows');
  translated.remove();
  chooseType('question');chooseOwner(options().find(option=>option.dataset.ownerName==='page-owner')?.value);
  const question=document.querySelector('[data-type="question"]');question.querySelector('.list-toggle').click();await readyReviewCard(question);
  const form=question.querySelector('[data-review-form]');form.elements.feedback.value='Use this layout.';
  form.querySelector('[value="answer"]').click();await new Promise(resolve=>nativeTimeout(resolve,1100));
  check(options().length===2 && options()[0].textContent==='All (0)' && menu().value!=='' && question.hidden &&
    count('Done').textContent==='1',
    'A save retains the selected owner and moves its count to Done.');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(options().length===2 && options()[0].textContent==='All (0)' && menu().value!=='' &&
    count('Done').textContent==='1',
    'Refresh retains the zero option for the selected owner.');
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
  entries.r.find(entry=>entry[2]===0)[6]=2;
  entries.r.find(entry=>entry[0]==='page-owner/C2')[6]=1;payload.textContent=JSON.stringify(entries);}
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
  check(counts('[data-show-chip]')==='3|1|1|5','The index counts states outside the rendered view.');
  const owner=()=>document.querySelector('#list-owner');
  const builder=document.querySelector('[data-type="change"]').dataset.sourceOwner;
  const chooseOwner=value=>{owner().value=value;owner().dispatchEvent(new Event('change'));};
  show('all').click();await flush();await flush();
  chooseOwner(builder);
  check(counts('[data-show-chip]')==='0|1|0|1','Show counts use the selected owner.');
  check(type('change').querySelector('b').textContent==='0' && type('investigation').querySelector('b').textContent==='1',
   'Type counts use the selected owner.');
  type('change').click();
  check(owner().value==='' && counts('[data-show-chip]')==='2|0|1|3',
   'F007: an owner without the selected type resets to All.');
  type('investigation').click();
  chooseOwner(builder);show('open').click();await flush();await flush();
  check(owner().value===builder && counts('[data-show-chip]')==='0|1|0|1' &&
   [...document.querySelectorAll('[data-list-item]')].every(row=>row.hidden),
   'F007: the user case keeps an owner with rows in another Show.');
  show('all').click();await flush();await flush();
  check(counts('[data-show-chip]')==='0|1|0|1','Show counts combine the type and owner.');
  search('gate');
  check(counts('[data-show-chip]')==='0|1|0|1' && owner().options[0].textContent==='All (1)',
   'All counts combine the words, owner, type and Show filters.');
  search('');chooseOwner('');type('').click();show('open').click();await flush();await flush();
  type('change').click();
  check(counts('[data-show-chip]')==='2|0|1|3','Show counts use the selected type.');
  search('Numbered');
  check(counts('[data-show-chip]')==='1|0|1|2','Show counts use the words filter.');
  check(type('change').querySelector('b').textContent==='1' && type('question').querySelector('b').textContent==='0',
   'Type counts use the words filter and Show.');
  check(document.querySelector('#list-owner').selectedOptions[0].textContent==='All (1)',
   'Owner counts use the type, Show and words.');
  search('no matching title');
  check(counts('[data-show-chip]')==='0|0|0|0' && [...document.querySelectorAll('[data-type-chip] b')].every(n=>n.textContent==='0') &&
   document.querySelector('#list-owner').selectedOptions[0].textContent==='All (0)',
   'An empty filtered view has zero in every applicable count.');
  search('');show('all').click();await flush();await flush();
  check(type('change').querySelector('b').textContent==='3','Type counts include the selected Show.');
  type('question').click();
  const row=document.querySelector('[data-list-item][data-type="question"]');
  row.querySelector('.list-toggle').click();await readyReviewCard(row);
  const form=row.querySelector('form');form.elements.feedback.value='Use the first layout.';
  form.requestSubmit(form.querySelector('[value="answer"]'));
  await new Promise(resolve=>nativeTimeout(resolve,1100));
  check(counts('[data-show-chip]')==='0|0|1|1','A save moves the index entry to Done.');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();await flush();
  check(counts('[data-show-chip]')==='0|0|1|1','A refresh retains the saved state from the server index.');
  type('change').click();
  check(counts('[data-show-chip]')==='1|1|1|3','A refresh replaces an unsaved entry in the index.');
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

    def test_server_supplies_the_selected_show_and_complete_index(self) -> None:
        ready = self.investigation()
        waiting = self.investigation(False)
        _ = investigation.mark_read(ready.owner, ready.id)
        done_key = f'investigation/{ready.owner}/{ready.id}'
        waiting_key = f'investigation/{waiting.owner}/{waiting.id}'
        done = review_page.render(self.root, show='Done').decode()
        self.assertEqual(next(entry['state'] for entry in unpack_index(done.split('data-review-index>',1)[1].split('</script>',1)[0]) if entry['key']==done_key),'Done')
        all_items = review_page.render(self.root, show='all').decode()
        self.assertIn(done_key,{entry['key'] for entry in unpack_index(all_items.split('data-review-index>',1)[1].split('</script>',1)[0])})
        self.assertIn(waiting_key,{entry['key'] for entry in unpack_index(all_items.split('data-review-index>',1)[1].split('</script>',1)[0])})
        for show in ('open', 'Waiting'):
            with self.subTest(show=show):
                source = review_page.render(self.root, show=show).decode()
                self.assertIn(f'data-show="{show}"', source)
                owner_menu = source.partition('<select id="list-owner">')[2].partition('</select>')[0]
                self.assertIn(f'value="{waiting.owner}"', owner_menu,
                              'F002: the unit Owner option remains when its Show view has no rows')

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
                investigation, 'load', wraps=investigation.load) as load_investigation:
            opened = review_items.read(self.root, show='open', owner_options=owners)
        self.assertEqual(opened, [item for item in all_items if item.state == review_items.ReviewState.OPEN])
        self.assertEqual(owners, expected_owners, 'The owner menu includes owners of omitted Done records.')
        self.assertTrue(project_change.called)
        self.assertFalse(any(cast(dict[str, str], call.args[1]).get('Verdict') for call in project_change.call_args_list),
                         'An Open read does not project a Done change.')
        self.assertFalse(project_question.called, 'An Open read does not project a closed question.')
        self.assertNotIn(ready.id, [call.args[1] for call in load_investigation.call_args_list],
                         'An Open read does not load a read investigation.')
        for show in ('Open', 'Waiting', 'Done', 'all'):
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
        update_registration.write_question_record(record)
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
            return counts, questions, queues, list_page.counts_strip(members)

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
                self.assertIn('The item returns to you',form)
                self.assertIn('type="datetime-local"',form)
                self.assertIn('value="later">Later',form)
                self.assertNotIn('value="skip"',form)

    def test_ask_question_and_one_owner_answer_command_for_each_type(self) -> None:
        decisions = self.decisions()
        question = self.question()
        ready = self.investigation()
        keys = (question,'page-owner/C1',f'investigation/{ready.owner}/{ready.id}',*decisions)
        for key in keys:
            with self.subTest(key=key):
                item = next(item for item in review_items.read(self.root) if item.key == key)
                delivery, result = review_page.submit(self.root,self.body(key,'question','Check the second case.'),self.status)
                self.assertEqual(result,'question sent')
                self.assertIsInstance(delivery,change_page.MessageDelivery)
                assert isinstance(delivery,change_page.MessageDelivery)
                self.assertIn(item.owner,delivery.recipients)
                self.assertIn('change_page.py answer-question --item '+key,delivery.message)
                change_page.deliver(delivery,self.status)
                waiting = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(waiting.state,review_items.ReviewState.WAITING)
                args = ['answer-question','--item',key,'--text','The second case passes.']
                request = self.status.slots(delivery.page,delivery.row).question
                assert isinstance(request,change_page.OpenQuestion)
                args += ['--question',request.question_id]
                self.assertEqual(change_page.main(args),0)
                returned = next(item for item in review_items.read(self.root) if item.key == key)
                self.assertEqual(returned.state, review_items.ReviewState.OPEN)
                self.assertEqual(sum('The second case passes.' in entry.words for entry in returned.log),1)
                self.assertEqual(sum('Check the second case.' in entry.words for entry in returned.log),1)

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
        keys = (question, 'page-owner/C1', f'investigation/{report.owner}/{report.id}', *decisions)
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
                    if kind == review_items.ReviewType.CHANGE:
                        key = 'page-owner/C1'
                    elif kind == review_items.ReviewType.QUESTION:
                        key = case.question()
                    elif kind == review_items.ReviewType.INVESTIGATION:
                        report = case.investigation(wait != 'native')
                        key = f'investigation/{report.owner}/{report.id}'
                    else:
                        proposal, efficiency = case.decisions()
                        key = proposal if kind == review_items.ReviewType.PROPOSAL else efficiency
                        _ = shutil.copyfile(Path(__file__).resolve().parents[2]/'config/nightly_review.conf',
                                            case.fixture.temporary/'config/nightly_review.conf')
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
                    if isinstance(updated.source, review_items.ChangeSource):
                        self.assertEqual(updated.source.slots.validation_requested, action == 'validate')
                        self.assertEqual(isinstance(updated.source.slots.question, change_page.OpenQuestion), action == 'question')
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

    def test_waiting_change_question_accepts_each_action(self) -> None:
        self.waiting_actions(review_items.ReviewType.CHANGE, ('works', 'not works', 'validate', 'feedback', 'question'))

    def test_waiting_change_validation_accepts_each_action(self) -> None:
        self.waiting_actions(review_items.ReviewType.CHANGE, ('works', 'not works', 'validate', 'feedback', 'question'), 'validate')

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

    def test_feedback_closes_each_type_and_uses_startup_delivery_recovery(self) -> None:
        decisions = self.decisions()
        question = self.question()
        ready = self.investigation()
        keys = (question,'page-owner/C1',f'investigation/{ready.owner}/{ready.id}',*decisions)
        for key in keys:
            with self.subTest(key=key):
                item = next(item for item in review_items.read(self.root) if item.key == key)
                delivery,result = review_page.submit(self.root,self.body(key,'feedback','Use the second layout.'),self.status)
                self.assertEqual(result,'feedback sent')
                assert isinstance(delivery,change_page.MessageDelivery)
                self.assertIn(item.owner,delivery.recipients)
                self.assertEqual(next(item.state for item in review_items.read(self.root) if item.key == key),review_items.ReviewState.DONE)
        restarted = change_page.DeliveryStatus(self.root)
        change_page.recover_pending(restarted)
        self.assertEqual(sum('Use the second layout.' in message for _,_,message in self.fixture.messages),6,
                         'One message for each type, and the change sends to two staff rows.')
        before = len(self.fixture.messages)
        change_page.recover_pending(change_page.DeliveryStatus(self.root))
        self.assertEqual(len(self.fixture.messages),before)

    def test_later_hides_each_type_from_the_shared_queue_and_then_returns(self) -> None:
        _ = change_review.append_rows(self.fixture.review_list,
            [change_review.Visible('Unrouted change', 'Read the log.', 'The result is present.')],
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
                with self.assertRaisesRegex(change_page.AnswerRefused, '^the production document is not available$'):
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
        _ = shutil.copyfile(Path(__file__).resolve().parents[2]/'config/nightly_review.conf',
                            self.fixture.temporary/'config/nightly_review.conf')
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
                with self.assertRaisesRegex(change_page.AnswerRefused, 'choose an action'):
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
        self.assertEqual({item.type for item in items},set(review_items.ReviewType))
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
        done_source = browser_markup(self.root, show='Done')
        setup = r"""<script>
const nativeFetch=fetch, nativeTimeout=setTimeout, original=MARKUP;
window.doneMarkup=DONE_MARKUP;
window.waitingMarkup=WAITING_MARKUP;
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
window.fetch=async(_url,options={})=>{
 if(options.method==='POST') return {ok:true,json:async()=>({answer:'works'})};
 window.refreshRequests.push(_url);return {ok:true,text:async()=>new URL(_url,location.href).searchParams.get('show')==='Waiting' ? window.waitingMarkup : window.refreshMarkup};
};
</script>""".replace('WAITING_MARKUP',json.dumps(browser_markup(self.root, show='Waiting')).replace('</','<\\/')).replace('DONE_MARKUP',json.dumps(done_source).replace('</','<\\/')).replace('MARKUP',json.dumps(source).replace('</','<\\/'))
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>nativeTimeout(resolve,60));
 const rows=()=>[...document.querySelectorAll('[data-list-item]')];
 const shown=()=>rows().filter(row=>!row.hidden);
 const choose=(id,value,event='change')=>{
  if(id==='list-show'){document.querySelector('[data-show-chip="'+value+'"]').click();return;}
  const field=document.getElementById(id);field.value=value;field.dispatchEvent(new Event(event,{bubbles:true}));
 };
 const showValue=(doc=document)=>doc.querySelector('[data-show-chip][aria-pressed="true"]').dataset.showChip;
 try{
  await flush();
  check(shown().length===5 && shown().every(row=>row.dataset.state==='Open'),'Open includes only Open records');
  check(document.querySelector('[data-show-chip="open"] b').textContent==='5',
    'F007: Open excludes Waiting and Done records');
  document.querySelector('[data-type-chip="question"]').click();
  choose('list-search','Q1','input');check(shown().length===1,'type and ID filters');
  choose('list-search','','input');document.querySelector('[data-type-chip=""]').click();
  choose('list-show','Waiting');await flush();await flush();check(shown().length===1,'Waiting filter');
  choose('list-show','open');await flush();await flush();
  const question=rows().find(row=>row.dataset.type==='question');question.querySelector('.list-toggle').click();await readyReviewCard(question);
  const change=rows().find(row=>row.dataset.type==='change');change.querySelector('.list-toggle').click();await readyReviewCard(change);
  check(question.querySelector('[data-detail]').hidden,'only one card opens');
  question.querySelector('.list-toggle').click();await readyReviewCard(question);
  const draftLater=question.querySelector('[name="later"]');draftLater.value='2098-12-31T16:30';
  draftLater.dispatchEvent(new Event('change',{bubbles:true}));
  const picked=question.querySelector('input[name=choice]');picked.click();
  question.querySelector('textarea').value='Keep this draft';question.querySelector('textarea').dispatchEvent(new Event('input'));
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  let fresh=rows().find(row=>row.dataset.type==='question');
  check(!fresh.querySelector('[data-detail]').hidden && fresh.querySelector('textarea').value==='Keep this draft','refresh keeps the card and draft');
  check(fresh.querySelector('[name="later"]').value==='2098-12-31T16:30','F018: refresh keeps the picked Later date and time');
  check(fresh.querySelector('input[name=choice]:checked')?.value===picked.value,'F018: refresh keeps the selected choice');
  check(fresh.querySelector('form').elements.row.value===fresh.dataset.key+'-refreshed' &&
    fresh.querySelector('form').elements.token.value.endsWith('-refreshed'),'F018: refresh supplies the row and token');
  const target=rows().find(row=>row.dataset.type==='change');target.querySelector('.list-toggle').click();await readyReviewCard(target);
  const form=target.querySelector('form');form.requestSubmit(form.querySelector('[value="works"]'));
  await flush();check(form.querySelector('[data-send-status]').textContent==='Saved: works','the row shows the saved result');
  await new Promise(resolve=>nativeTimeout(resolve,950));
  check(target.hidden,'the saved row leaves the view');
  check(document.querySelector('[data-show-chip="open"] b').textContent==='4','the count excludes the saved row');
  check(rows().every(row=>row.querySelector('[data-detail]').hidden),'no next card opens');
  choose('list-show','all');
  const allTarget=rows().find(row=>row.dataset.type==='change' && row!==target);allTarget.querySelector('.list-toggle').click();await readyReviewCard(allTarget);
  const allForm=allTarget.querySelector('form');allForm.requestSubmit(allForm.querySelector('[value="works"]'));
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  check(allTarget.hidden,'F010: the saved row leaves All');
  location.hash=new URLSearchParams({item:target.dataset.key});await flush();
  check(target.hidden,'F010: a fragment change keeps the saved row hidden');
  const reloaded=document.createElement('iframe');reloaded.src='/console/review.html#'+new URLSearchParams({item:target.dataset.key});
  const reloadComplete=new Promise(resolve=>reloaded.addEventListener('load',resolve,{once:true}));document.body.append(reloaded);await reloadComplete;
  const linkedTarget=reloaded.contentDocument.querySelector('[data-key="'+target.dataset.key+'"]');
  check(!linkedTarget.hidden && !linkedTarget.querySelector('[data-detail]').hidden,'a Review item link shows the saved row');
  check(showValue(reloaded.contentDocument)==='all' && reloaded.contentDocument.getElementById('list-owner').value==='' &&
    reloaded.contentDocument.getElementById('list-search').value==='','a Review item link clears filters');
  reloaded.remove();
  const retained={item:fresh.dataset.key,type:'question',owner:fresh.dataset.owner,show:'open',q:'Q1',sort:'Age',dir:'desc'};
  location.hash=new URLSearchParams(retained);await flush();
  fresh=rows().find(row=>row.dataset.type==='question');
  check(document.getElementById('list-owner').value===retained.owner && showValue()==='open' &&
    document.getElementById('list-search').value==='Q1' && document.querySelector('[data-type-chip="question"]').getAttribute('aria-pressed')==='true',
    'F014: restore the filters with an open row');
  check(!fresh.hidden && !fresh.querySelector('[data-detail]').hidden,'F014: restore the open row');
  check(document.querySelector('[data-sort="5"]').parentElement.getAttribute('aria-sort')==='descending','F014: restore the sort');

  const legacy=document.createElement('iframe');
  legacy.src='/console/changes.html?type=questions#'+new URLSearchParams({showrunner:'page-owner',show:'Waiting',q:'layout',sort:'Age',dir:'desc'});
  const loaded=new Promise(resolve=>legacy.addEventListener('load',resolve,{once:true}));document.body.append(legacy);await loaded;
  await flush();await flush();
  const restored=legacy.contentDocument;
  check(restored.getElementById('list-owner').selectedOptions[0].textContent==='page-owner (0)' && showValue(restored)==='Waiting' &&
    restored.getElementById('list-search').value==='layout' && restored.querySelector('[data-type-chip="question"]').getAttribute('aria-pressed')==='true',
    'F012: a legacy link retains the zero owner and its other filters.');
  check(restored.querySelector('[data-sort="5"]').parentElement.getAttribute('aria-sort')==='descending','F012: the legacy link keeps its sort');
  legacy.remove();

  check(document.documentElement.scrollWidth<=innerWidth,'no horizontal overflow');
  if(innerWidth<960) check(getComputedStyle(shown()[0]).display==='block','phone rows are cards');
  const laterForm=fresh.querySelector('form');laterForm.elements.later.value='2099-01-01T09:00';
  laterForm.requestSubmit(laterForm.querySelector('[value="later"]'));
  await new Promise(resolve=>nativeTimeout(resolve,1000));
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
  pendingForm.requestSubmit(pendingForm.querySelector('[value="question"]'));
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  const revision=pending.dataset.updated;
  for(const [state,updated,revealed] of [
    ['Open',revision,false],['Waiting',revision,false],
    ['Waiting','2099-02-01T09:00:00+00:00',false],['Open','2099-02-01T10:00:00+00:00',true]]){
   const update=new DOMParser().parseFromString(original,'text/html');
   const payload=update.querySelector('[data-review-index]'),entries=JSON.parse(payload.textContent);
   const entry=entries.r.find(entry=>entry[0]===pendingKey);entry[6]=['Open','Waiting','Done'].indexOf(state);entry[8]=updated;
   payload.textContent=JSON.stringify(entries);
   window.refreshMarkup=update.documentElement.outerHTML;
   dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
   const shownPending=rows().find(row=>row.dataset.key===pendingKey);
   check(Boolean(shownPending && !shownPending.hidden)===revealed,'F021: state and revision control return for '+state+' '+updated);
   check(Number(document.querySelector('[data-show-chip="open"] b').textContent)===initialCount-(revealed?0:1),
     'F021: only the new Open response restores the count');
  }
  window.refreshMarkup=window.doneMarkup;
  const beforeShowRequests=window.refreshRequests.length;
  choose('list-show','Done');await flush();await flush();
  check(document.querySelector('[data-show-chip="Done"] b').textContent==='6',
    'Done counts the three server records and three saved records.');
  check(window.refreshRequests.length===beforeShowRequests,
    'the Show chips select rows without a request');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(new URL(window.refreshRequests.at(-1),location.href).searchParams.has('version') &&
    showValue()==='Done','the refresh keeps the selected Show view');
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
    def test_review_uses_the_shared_status_with_the_real_loader(self) -> None:
        _ = self.question()
        _ = self.investigation()
        assert_status_states(self, browser_markup(self.root),15)


if __name__ == '__main__':
    _ = unittest.main()
