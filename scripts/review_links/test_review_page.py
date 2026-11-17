"""Check the shared review records, signed actions and table behavior."""
from __future__ import annotations

import shutil
import html
import json
import os
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Self, TextIO, cast
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import conf
from scripts.lib.user_zone import user_zone
import unittest
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

from scripts.agents.remote import local_machine
from scripts.production.production_doc import read_production
from scripts.production.issues import Issue
from scripts.production import issues as issue_store
from scripts.production.staff_test_support import fixture_member
from scripts.production import change_review, change_routing, footer_counts, investigation, next_item, staff, update_registration
from scripts.review_links import change_browser, change_page, console, record_table, review_items, review_page
from scripts.review_links import list_page, staff_names, test_change_page
from scripts.review_links import efficiency_reviews, review_actions, review_later
from scripts.review_links.test_page_frame import assert_status_states
from scripts.statusline import counts_row


class ReviewPageTests(unittest.TestCase):
    fixture: test_change_page.ChangePageTests = test_change_page.ChangePageTests("runTest")
    root: Path = Path()
    status: change_page.DeliveryStatus = change_page.DeliveryStatus(Path())

    def test_staff_owners_sources_and_log_names_have_role_colors(self) -> None:
        _ = self.question()
        body = review_page.render(self.root).decode()
        self.assertRegex(body, r'data-color-text="role.showrunner"[^>]*>page-owner</span>')
        self.assertRegex(body, r'data-color-text="role.unit-director"[^>]*>builder-unit</span>')
        self.assertNotRegex(body, r'class="staff-name"[^>]*>nate</span>')
        item = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.QUESTION)
        unknown = replace(item, owner=staff.StaffId('nate'), source_unit=staff.StaffId('unknown-source'))
        plain = review_page.item_rows(unknown, change_page.ReviewBrowser('private'), staff.snapshot(),
                                      datetime.now(user_zone()), 0, 100, 3)
        self.assertIn('<td data-label="Owner">nate<small>· unknown-source</small></td>', plain)
        self.assertIn('Owner: nate · Source: unknown-source</p>', plain)

    @override
    def setUp(self) -> None:
        self.fixture = test_change_page.ChangePageTests('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        config = self.fixture.temporary / 'config'
        _ = (config/'production.conf').write_text('# whole: 20..\nquestion_title_max_chars=100\n')
        _ = shutil.copyfile(Path(__file__).resolve().parents[2]/'config/console.conf',config/'console.conf')
        self.root = self.fixture.root
        self.status = change_page.DeliveryStatus(self.root)
        document = self.fixture.production.read_text()
        _ = self.fixture.production.write_text(document+'- **Merge branch:** main\n- **Log:** progress.log\n')

    def question(self, title: str = 'Which layout should the page use?') -> str:
        return self.fixture.file_user_question(title)

    def assert_row_title(self, row: str, title: str, visible: str) -> None:
        element = row.split('<strong', 1)[1].split('</strong>', 1)[0]
        attributes, _, text = element.partition('>')
        self.assertIn('class="review-title"', attributes)
        self.assertIn('title="'+html.escape(title, quote=True)+'"', attributes)
        self.assertEqual(text, html.escape(visible))
        self.assertNotIn(html.escape(title), text)

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
                rendered = review_page.render(self.root).decode()
                row = rendered.split('data-key="'+html.escape(key, quote=True)+'"', 1)[1].split('</tbody>', 1)[0]
                self.assert_row_title(row, title, title.splitlines()[0][:100]+'…')
                self.assertIn('data-title="'+html.escape(title.casefold(), quote=True)+'"', row)
                detail = row.split('data-detail', 1)[1]
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
                rendered = review_page.render(self.root).decode()
                row = rendered.split('data-key="'+html.escape(key, quote=True)+'"', 1)[1].split('</tbody>', 1)[0]
                self.assertIn('>'+title+'</strong>', row)
                detail = row.split('data-review-body>', 1)[1]
                self.assertEqual(detail.count(title), 1)
                self.assertNotIn('<h3>Item</h3>', detail)

    def test_short_change_title_appears_in_the_item_section(self) -> None:
        item = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        title = 'Check the spacing'
        token, initial = change_browser.browser_manifest(change_page.console_folder(self.root))
        browser = change_page.ReviewBrowser(token, initial)
        row = review_page.item_rows(replace(item, title=title), browser, staff.snapshot(), datetime.now(user_zone()), 0, 100, 4)
        detail = row.split('data-detail', 1)[1]
        self.assertIn('<h3>Item</h3><p>'+title+'</p>', detail)
        self.assertEqual(detail.count(title), 1)

    def test_question_with_a_distinct_ask_shows_title_and_ask_once_in_details(self) -> None:
        key = self.question()
        source = next(item.source for item in review_items.read(self.root) if item.key == key)
        assert isinstance(source, review_items.QuestionSource)
        title, ask = 'Layout choice', 'Which width should the page use?'
        source.record.item['text'] = title
        source.record.item['parts'] = update_registration.QuestionParts(
            ask=ask, context='Choose the width.', items=[], choices=[], recommend='')
        update_registration.write_question_record(source.record)
        rendered = review_page.render(self.root).decode()
        row = rendered.split('data-key="'+html.escape(key, quote=True)+'"', 1)[1].split('</tbody>', 1)[0]
        detail = row.split('data-detail', 1)[1]
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
        token, initial = change_browser.browser_manifest(change_page.console_folder(self.root))
        browser = change_page.ReviewBrowser(token, initial)
        detail = review_page.details(item, browser, staff.snapshot(), 4).split('data-review-body>', 1)[1]
        self.assertIn('<h3>You asked</h3>', detail)
        self.assertEqual(detail.count(html.escape(item.title)), 1)
        self.assertNotIn('<h3>Item</h3>', detail)
        self.assertIn(html.escape(investigation_record.answer), detail)

    def test_change_shows_its_title_once_when_the_proof_contains_it(self) -> None:
        item = next(item for item in review_items.read(self.root) if item.type == review_items.ReviewType.CHANGE)
        assert isinstance(item.source, review_items.ChangeSource)
        title = 'The spacing agrees'
        source = replace(item.source, row={**item.source.row, 'What you see': title})
        token, initial = change_browser.browser_manifest(change_page.console_folder(self.root))
        browser = change_page.ReviewBrowser(token, initial)
        detail = review_page.details(replace(item, title=title, source=source), browser, staff.snapshot(), 4).split('data-review-body>', 1)[1]
        self.assertIn('<h3>Proof</h3><p>'+title+'</p>', detail)
        self.assertEqual(detail.count(title), 1)
        self.assertNotIn('<h3>Item</h3>', detail)

    def test_each_review_type_limits_the_title_and_keeps_it_in_details(self) -> None:
        _ = self.question()
        members = staff.snapshot()
        token, initial = change_browser.browser_manifest(change_page.console_folder(self.root))
        browser = change_page.ReviewBrowser(token, initial)
        title = 'A full <title> '+ 'x'*100+'\nMore facts.'
        _ = self.investigation()
        _ = self.decisions()
        for item in review_items.read(self.root):
            with self.subTest(kind=item.type):
                row = review_page.item_rows(replace(item, title=title), browser, members, datetime.now(user_zone()), 0, 100, 4)
                self.assert_row_title(row, title, title.splitlines()[0][:100]+'…')
                detail = row.split('data-detail', 1)[1]
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
        self.assertIn(html.escape(title[:40])+'…</strong>', rendered)
        self.assertIn(html.escape(title), rendered.split('data-detail', 1)[1])

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
        identities = update_registration.QuestionStaff.for_record(staff.snapshot())
        stored = update_registration.question_records_for_document(self.fixture.production, identities)[0]
        fields = cast(dict[str, object], json.loads(stored.path.read_text().split('registration: ', 1)[1].splitlines()[0]))
        _ = fields.pop('review_id', None)
        text = stored.path.read_text()
        registration = next(line for line in text.splitlines() if line.startswith('registration: '))
        _ = stored.path.write_text(text.replace(registration, 'registration: '+json.dumps(fields)))
        original = next(item for item in review_items.read(self.root) if item.key == key)
        self.assertEqual(original.id, 'page-owner/Q1', 'F013: show the allocated ID on the first read')
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
        for action in ('Answer','Works','Does not work','Ask showrunner to check','Ask question','Send feedback','Mark read'):
            self.assertIn('>'+action+'</button>',source)
        self.assertIn('>Later</button>',source)
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
                row = markup.partition('data-key="'+key+'"')[2].partition('</tbody>')[0]
                self.assertIn('data-state="Open"', row)
                self.assertIn('>Open</span>', row)
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
        entries = review_page.filter_index(records, members, datetime.now(user_zone()))
        self.assertEqual(len(entries), len(records))
        self.assertEqual({entry['state'] for entry in entries}, {'Open', 'Waiting', 'Done'})
        self.assertIn(f'investigation/{waiting.owner}/{waiting.id}', {entry['key'] for entry in entries})
        self.assertEqual(set(entries[0]), {'key', 'id', 'type', 'owner', 'sourceOwner', 'ownerName',
                                        'title', 'state', 'filed', 'updated', 'hiddenUntil'})
        self.assertEqual([entry['updated'] for entry in entries], [item.times.updated.isoformat() for item in records])
        encoded = review_page.index_json(entries)
        self.assertNotIn('<', encoded)
        self.assertNotIn('&', encoded)
        self.assertEqual(json.loads(encoded), entries)
        for show, _ in review_page.SHOWS:
            source = review_page.render(self.root, show=show).decode()
            payload = source.split('data-review-index>', 1)[1].split('</script>', 1)[0]
            self.assertEqual(json.loads(payload), entries, 'Every Show carries the same complete index.')
            self.assertNotIn('<span data-list-count', source)

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_browser_show_chips_keep_counts_address_reload_and_refresh(self) -> None:
        self.maxDiff: int | None = None
        _ = self.decisions()
        _ = self.question()
        _ = self.investigation()
        _ = self.investigation(False)
        finished = self.investigation()
        _ = investigation.mark_read(finished.owner, finished.id)
        _ = review_page.submit(self.root, self.body('page-owner/C1', 'validate'), self.status)
        pages = {show: review_page.render(self.root, show=show).decode() for show, _ in review_page.SHOWS}
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
 try{
  await flush();
  check([...document.querySelectorAll('[data-show-chip]')].map(button=>button.dataset.showChip).join('|')===
    'open|Waiting|Done|all','Show chips have the required order');
  check(selected('open'),'exactly one Open chip starts pressed');
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
  check(count('Done')?.textContent==='1' && count('open')?.textContent==='6' && count('Waiting')?.textContent==='2' &&
    [...document.querySelectorAll('[data-show-chip] b')].every(number=>!number.hidden),
    'F001: Done retains all complete Show counts');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(selected('Done') && new URL(window.refreshRequests.at(-1),location.href).searchParams.get('show')==='Done',
    'refresh requests the pressed Show view');
  const reload=document.createElement('iframe');reload.src='/reload'+location.hash;
  const loaded=new Promise(resolve=>reload.addEventListener('load',resolve,{once:true}));document.body.append(reload);await loaded;
  await flush();await flush();
  check(selected('Done',reload.contentDocument) && reload.contentDocument.querySelector('[data-review]').dataset.show==='Done',
    'a reload keeps the shared Show address');reload.remove();
  for(const value of ['Waiting','all','open']){
   chip(value)?.click();await flush();await flush();
   check(selected(value),'exactly one chip stays pressed for '+value);
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
  const question=document.querySelector('[data-type="question"]');question.querySelector('.list-toggle').click();
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
    def test_filter_counts_share_the_complete_index(self) -> None:
        _ = self.question()
        _ = self.investigation(False)
        _ = review_page.submit(self.root, self.body('page-owner/C1', 'works'), self.status)
        pages = {show: review_page.render(self.root, show=show).decode() for show, _ in review_page.SHOWS}
        setup = r"""<script>
const nativeFetch=fetch,nativeTimeout=setTimeout;
window.setInterval=()=>0;window.clearInterval=()=>{};
const showPages=PAGES;
window.fetch=async(url,options)=>{
 if(options?.method==='POST'){window.didSave=true;return {ok:true,json:async()=>({answer:'saved'})};}
 const show=new URL(url,location.href).searchParams.get('show') || 'open';
 const page=new DOMParser().parseFromString(showPages[show],'text/html');
 if(window.didSave){const payload=page.querySelector('[data-review-index]'),entries=JSON.parse(payload.textContent);
  entries.find(entry=>entry.type==='question').state='Done';
  entries.find(entry=>entry.id==='page-owner/C2').state='Waiting';payload.textContent=JSON.stringify(entries);}
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
  row.querySelector('.list-toggle').click();
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

    def test_server_renders_only_the_selected_show_view(self) -> None:
        ready = self.investigation()
        waiting = self.investigation(False)
        _ = investigation.mark_read(ready.owner, ready.id)
        done_key = f'investigation/{ready.owner}/{ready.id}'
        waiting_key = f'investigation/{waiting.owner}/{waiting.id}'
        opened = review_page.render(self.root).decode()
        self.assertNotIn(f'data-key="{done_key}"', opened)
        self.assertNotIn(f'data-key="{waiting_key}"', opened)
        self.assertNotIn('data-state="Done"', opened)
        done = review_page.render(self.root, show='Done').decode()
        self.assertIn(f'data-key="{done_key}"', done)
        self.assertNotIn(f'data-key="{waiting_key}"', done)
        self.assertNotIn('data-state="For you"', done)
        self.assertNotIn('data-state="Waiting"', done)
        all_items = review_page.render(self.root, show='all').decode()
        self.assertIn(f'data-key="{done_key}"', all_items)
        self.assertIn(f'data-key="{waiting_key}"', all_items)
        for show in ('open', 'Waiting'):
            with self.subTest(show=show):
                source = review_page.render(self.root, show=show).decode()
                self.assertIn(f'data-show="{show}"', source)
                self.assertNotIn('data-state="Done"', source)
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

    def test_open_allocates_question_id_without_decoding_malformed_closed_registration(self) -> None:
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
                form = review_page.form(item,change_page.ReviewBrowser(token,initial),4)
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
                self.assertEqual(review_page.form(waiting, browser, words_rows), review_page.form(opened, browser, words_rows))

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
        body = review_page.proposal_body(self.root, proposal).decode()
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
            source = review_page.render(self.root).decode()
        self.assertIn('<section data-title-fallback hidden>', source)
        self.assertIn('<p data-title-ignore>Loading proposal…</p>', source)
        setup = r'''<script>
const nativeFetch=fetch,nativeTimeout=setTimeout;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.refreshMarkup=MARKUP;window.bodyRequests=[];window.pendingBodies=new Map();
window.fetch=async(url)=>{
 const key=new URL(url,location.href).searchParams.get('body');
 if(key){
  window.bodyRequests.push(key);
  return new Promise(resolve=>window.pendingBodies.set(key,resolve));
 }
 return {ok:true,text:async()=>window.refreshMarkup};
};
</script>'''.replace('MARKUP', json.dumps(source).replace('</', '<\\/'))
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
   open(key);
   check(fallback(key).hidden===hidden,key+': fallback follows rendered text');
   check(visibleText(key).split(title).length-1===1,key+': one visible title');
  }
  check(panel('question/code').querySelector('code').textContent.includes('let x = 1;'),'the code block renders');
  check(panel('question/formatted').querySelector('strong').textContent==='wide','the ask formatting renders');
  check(panel('change/image').querySelector('img').alt.includes('Check the blue marker'),'the image alt contains the title');
  open('investigation/paragraphs');
  const paragraphs=[...panel('investigation/paragraphs').querySelectorAll('[data-review-body] p')];
  check(paragraphs.some(p=>p.textContent==='Which check failed?'),'the first question paragraph renders');
  check(paragraphs.some(p=>p.textContent==='Which repair works?'),'the second question paragraph renders');
  check(fallback('investigation/paragraphs').hidden,'paragraph boundaries preserve the title');
  open('investigation/spaced');
  check([...panel('investigation/spaced').querySelectorAll('[data-review-body] p')]
   .some(p=>p.textContent==='Run ab'),'the body keeps the joined word');
  check(!fallback('investigation/spaced').hidden,'different word spacing keeps the fallback visible');
  const outcomes=[
   ['proposal/with-title','<h2>P1</h2><p>Use fewer checks.</p>',true],
   ['proposal/without-title','<p>Use fewer checks.</p>',false],
   ['proposal/empty','',false],['proposal/failure',null,false]];
  for(const [key,body,hidden] of outcomes){
   check(!fallback(key).hidden,key+': fallback shows before loading');
   open(key);
   check(!fallback(key).hidden,key+': fallback shows while loading');
   check(panel(key).querySelector('[data-proposal-body]').textContent.includes('Loading proposal'),key+': loading text');
   const resolve=window.pendingBodies.get(key);
   check(typeof resolve==='function',key+': a body request starts');
   resolve(body===null ? {ok:false,status:503} : {ok:true,text:async()=>body});
   await flush();await flush();
   check(fallback(key).hidden===hidden,key+': fallback follows the loaded body');
   if(body===null) check(panel(key).textContent.includes('not available'),key+': failure stays visible');
   else check(visibleText(key).split(row(key).querySelector('.review-title').textContent).length-1===1,key+': one title after loading');
  }
  for(const [key,body,hidden] of outcomes){
   open('question/equal');
   const before=window.bodyRequests.length;
   await refresh();open(key);await flush();
   if(body===null){
    check(window.bodyRequests.length===before+1,key+': refresh retries the failed load');
    check(!fallback(key).hidden,key+': fallback shows during the retry');
    window.pendingBodies.get(key)({ok:false,status:503});await flush();
   }else{
    check(window.bodyRequests.length===before,key+': refresh uses the cached body');
    check(panel(key).querySelector('[data-proposal-body]').innerHTML===body,key+': cache replaces the loading text');
   }
   check(fallback(key).hidden===hidden,key+': fallback follows the refreshed body');
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
        source = review_page.render(self.root).decode()
        done_source = review_page.render(self.root, show='Done').decode()
        setup = r"""<script>
const nativeFetch=fetch, nativeTimeout=setTimeout, original=MARKUP;
window.doneMarkup=DONE_MARKUP;
window.waitingMarkup=WAITING_MARKUP;
window.setInterval=()=>0;window.clearInterval=()=>{};
const refreshed=new DOMParser().parseFromString(original,'text/html');
const refreshedForm=refreshed.querySelector('[data-type="question"] form');
refreshedForm.elements.token.value+='-refreshed';refreshedForm.elements.row.value+='-refreshed';
window.refreshMarkup=refreshed.documentElement.outerHTML;
window.refreshRequests=[];
window.fetch=async(_url,options={})=>{
 if(options.method==='POST') return {ok:true,json:async()=>({answer:'works'})};
 window.refreshRequests.push(_url);return {ok:true,text:async()=>new URL(_url,location.href).searchParams.get('show')==='Waiting' ? window.waitingMarkup : window.refreshMarkup};
};
</script>""".replace('WAITING_MARKUP',json.dumps(review_page.render(self.root, show='Waiting').decode()).replace('</','<\\/')).replace('DONE_MARKUP',json.dumps(done_source).replace('</','<\\/')).replace('MARKUP',json.dumps(source).replace('</','<\\/'))
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
  check(document.querySelector('[data-show-chip="Waiting"] b').textContent==='1',
    'F007: Waiting has its own count');
  document.querySelector('[data-type-chip="question"]').click();
  choose('list-search','Q1','input');check(shown().length===1,'type and ID filters');
  choose('list-search','','input');document.querySelector('[data-type-chip=""]').click();
  choose('list-show','Waiting');await flush();await flush();check(shown().length===1,'Waiting filter');
  choose('list-show','open');await flush();await flush();
  const question=rows().find(row=>row.dataset.type==='question');question.querySelector('.list-toggle').click();
  const title=question.querySelector('.review-title'),titleHeight=title.getBoundingClientRect().height;
  title.textContent='A long review title '.repeat(20);
  check(title.getBoundingClientRect().height===titleHeight && title.scrollWidth>title.clientWidth,
    'a long row title stays on one line and hides its overflow');
  const change=rows().find(row=>row.dataset.type==='change');change.querySelector('.list-toggle').click();
  check(question.querySelector('[data-detail]').hidden,'only one card opens');
  question.querySelector('.list-toggle').click();
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
  const target=rows().find(row=>row.dataset.type==='change');target.querySelector('.list-toggle').click();
  const form=target.querySelector('form');form.requestSubmit(form.querySelector('[value="works"]'));
  await flush();check(form.querySelector('[data-send-status]').textContent==='Saved: works','the row shows the saved result');
  await new Promise(resolve=>nativeTimeout(resolve,950));
  check(target.hidden,'the saved row leaves the view');
  check(document.querySelector('[data-show-chip="open"] b').textContent==='4','the count excludes the saved row');
  check(rows().every(row=>row.querySelector('[data-detail]').hidden),'no next card opens');
  choose('list-show','all');
  const allTarget=rows().find(row=>row.dataset.type==='change' && row!==target);allTarget.querySelector('.list-toggle').click();
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
  const restoredOwner=restored.getElementById('list-owner');
  check(restoredOwner.value===fresh.dataset.owner && restoredOwner.selectedOptions[0].textContent==='page-owner (0)' &&
    showValue(restored)==='Waiting' &&
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
  returned.querySelector('.list-toggle').click();
  const answerForm=returned.querySelector('form');answerForm.elements.later.value='';
  answerForm.requestSubmit(answerForm.querySelector('[value="later"]'));await flush();
  answerForm.requestSubmit(answerForm.querySelector('[value="answer"]'));await flush();
  check(answerForm.querySelector('[data-send-status]').textContent==='Saved: works',
    'F023: an empty Later does not block Answer');
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  choose('list-show','open');choose('list-search','','input');choose('list-owner','');
  document.querySelector('[data-type-chip=""]').click();
  const pending=rows().find(row=>row.dataset.number==='page-owner/c3'),pendingKey=pending.dataset.key;
  pending.querySelector('.list-toggle').click();
  const pendingForm=pending.querySelector('form');pendingForm.elements.feedback.value='Wait for a new answer.';
  const initialCount=Number(document.querySelector('[data-show-chip="open"] b').textContent);
  pendingForm.requestSubmit(pendingForm.querySelector('[value="question"]'));
  await new Promise(resolve=>nativeTimeout(resolve,1000));
  const revision=pending.dataset.updated;
  for(const [state,updated,revealed] of [
    ['Open',revision,false],['Waiting',revision,false],
    ['Waiting','2099-02-01T09:00:00+00:00',false],['Open','2099-02-01T10:00:00+00:00',true]]){
   const update=new DOMParser().parseFromString(original,'text/html');
   const updatedRow=update.querySelector('[data-key="'+pendingKey+'"]');
   updatedRow.dataset.state=state;updatedRow.dataset.updated=updated;
   const payload=update.querySelector('[data-review-index]'),entries=JSON.parse(payload.textContent);
   const entry=entries.find(entry=>entry.key===pendingKey);entry.state=state;entry.updated=updated;
   payload.textContent=JSON.stringify(entries);
   window.refreshMarkup=update.documentElement.outerHTML;
   dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
   const shownPending=rows().find(row=>row.dataset.key===pendingKey);
   check(shownPending.hidden!==revealed,'F021: state and revision control return for '+state+' '+updated);
   check(Number(document.querySelector('[data-show-chip="open"] b').textContent)===initialCount-(revealed?0:1),
     'F021: only the new Open response restores the count');
  }
  window.refreshMarkup=window.doneMarkup;
  choose('list-show','Done');await flush();await flush();
  check(document.querySelector('[data-show-chip="Done"] b').textContent==='6',
    'Done counts the three server records and three saved records.');
  check(new URL(window.refreshRequests.at(-1),location.href).searchParams.get('show')==='Done',
    'the Show chips request the selected rows');
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
  check(new URL(window.refreshRequests.at(-1),location.href).searchParams.get('show')==='Done' &&
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
        assert_status_states(self, review_page.render(self.root).decode(),15)


if __name__ == '__main__':
    _ = unittest.main()
