"""Test Waiting reasons and the Change state."""
from __future__ import annotations

import io
import json
import fcntl
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from urllib.parse import urlencode

from scripts.nightly_review import candidates
from scripts.lib import file_lock
from scripts.production import investigation, proposal_issue, staff, update_registration, waiting_why
from scripts.production.production_doc import Refusal
from scripts.production.record_store import state_root
from scripts.review_links import change_page, efficiency_reviews, review_events, review_items, test_change_page

WHY = 'The filer must answer before review continues.'


class WaitingWhyTests(unittest.TestCase):
    fixture: test_change_page.ChangePageTests = test_change_page.ChangePageTests('runTest')
    root: Path = Path()
    question: str = ''
    proposal: Path = Path()
    ledger: Path = Path()

    @override
    def setUp(self) -> None:
        self.fixture = test_change_page.ChangePageTests('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.root
        _ = self.enterContext(patch.object(candidates, 'state_root', return_value=self.fixture.temporary / 'candidates'))
        folder = self.fixture.temporary / 'proposals'
        folder.mkdir()
        doc = self.fixture.production
        _ = doc.write_text(doc.read_text() + f'- **Proposals:** {folder}\n')
        self.question = self.fixture.file_user_question()
        self.proposal = folder / 'P1.md'
        _ = self.proposal.write_text('---\nstatus: open\ntags: [proposal]\nstage: backlog\n'
            + f'proposed_by: {self.fixture.unit.id}\nproposal_id: P1\ndate_created: 2026-11-01\n---\nProposal\n')
        self.ledger = self.fixture.temporary / 'efficiency.md'
        _ = self.ledger.write_text(f'- 2026-11-01 tests: Faster checks [by: {self.fixture.unit.id}] — proposed\n')
        doc = self.fixture.production
        _ = doc.write_text(doc.read_text() + f'- **Efficiency ledger:** {self.ledger}\n')

    def enter_waiting(self, kind: review_items.ReviewType, reason: str) -> review_items.ReviewItem:
        if kind == review_items.ReviewType.QUESTION:
            identities = update_registration.QuestionStaff.read()
            source = next(item.source for item in review_items.read(self.root) if item.key == self.question)
            assert isinstance(source, review_items.QuestionSource)
            production = update_registration.question_scope(source.record.owner, identities.record)
            _ = update_registration.followup(production, source.record.item['text'], reason, identities=identities)
        elif kind == review_items.ReviewType.INVESTIGATION:
            _ = investigation.file(self.fixture.unit.id, 'Find the cause', 'What caused this?', waiting_why=reason)
        elif kind == review_items.ReviewType.PROPOSAL:
            _ = proposal_issue.record_review(self.proposal, 'Question: Which change?', review_events.ReviewMark.WAITING,
                                              date(2026, 11, 17), waiting_why=reason)
        else:
            _ = candidates.record_outcome('fixture/E1', 'deferred', '', self.ledger, waiting_why=reason)
        return next(item for item in review_items.read(self.root)
                    if item.type == kind and item.state == review_items.ReviewState.WAITING)

    def command(self, *args: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = waiting_why.main(['--root', str(self.root), *args])
        return result, output.getvalue(), errors.getvalue()

    def remove_reason(self, item: review_items.ReviewItem) -> None:
        source = item.source
        if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
            _ = source.record.item.pop('waiting_why', None)
            _ = source.record.item.pop('review_id', None)
            lines = source.record.path.read_text().splitlines()
            _ = source.record.path.write_text('\n'.join(
                'registration: ' + json.dumps(source.record.item) if line.startswith('registration: ') else line
                for line in lines) + '\n')
            return
        elif isinstance(source, review_items.InvestigationSource):
            path = investigation.folder(source.record.owner) / (source.record.id + '.md')
        elif isinstance(source, review_items.ProposalSource):
            path = source.record.path
        else:
            from scripts.review_links import efficiency_reviews
            path = efficiency_reviews.path(self.ledger)
        _ = path.write_text(path.read_text().replace(WHY, ''))

    def check_refusal(self, kind: review_items.ReviewType) -> None:
        if kind == review_items.ReviewType.QUESTION:
            source = next(item.source for item in review_items.read(self.root) if item.key == self.question)
            assert isinstance(source, review_items.QuestionSource)
            identities = update_registration.QuestionStaff.read()
            scope = update_registration.question_scope(source.record.owner, identities.record)
            with self.assertRaisesRegex(update_registration.RegistrationFailure, 'the follow-up is empty'):
                _ = update_registration.followup(scope, source.record.item['text'], ' ', identities=identities)
            return
        with self.assertRaisesRegex(ValueError, 'Waiting items need'):
            if kind == review_items.ReviewType.INVESTIGATION:
                _ = investigation.file(self.fixture.unit.id, 'Find the cause', 'What caused this?')
            elif kind == review_items.ReviewType.PROPOSAL:
                _ = proposal_issue.record_review(self.proposal, 'Question: Which change?', review_events.ReviewMark.WAITING,
                                                  date(2026, 11, 17))
            else:
                _ = candidates.record_outcome('E1', 'deferred', '', self.ledger)

    def check_store(self, kind: review_items.ReviewType) -> None:
        item = self.enter_waiting(kind, WHY)
        self.assertEqual(item.waiting_why, WHY)

    def check_list(self, kind: review_items.ReviewType) -> None:
        item = self.enter_waiting(kind, WHY)
        self.remove_reason(item)
        paths = [path for path in self.fixture.temporary.rglob('*') if path.is_file()]
        before = {path: path.read_bytes() for path in paths}
        code, output, errors = self.command('list')
        self.assertEqual((code, errors), (0, ''))
        self.assertEqual(len(output.splitlines()), 1)
        self.assertTrue(output.startswith(item.key + ' | ' + item.type.value + ' | '))
        self.assertIn(item.title, output)
        self.assertIn('filed by ' + review_items.name(item.source_unit, staff.snapshot()), output)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def check_set(self, kind: review_items.ReviewType) -> None:
        item = self.enter_waiting(kind, WHY)
        reason = 'The owner must finish the requested check.'
        self.assertEqual(self.command('set', '--item', item.key, '--text', reason), (0, '', ''))
        current = next(value for value in review_items.read(self.root) if value.key == item.key)
        self.assertEqual(current.waiting_why, reason)
        with patch.object(review_items, 'read', return_value=[replace(current, state=review_items.ReviewState.DONE)]):
            code, _, error = self.command('set', '--item', item.key, '--text', WHY)
        self.assertEqual(code, 1)
        self.assertEqual(len(error.splitlines()), 1)
        self.assertIn('not Waiting', error)

    def test_validating_change_is_outside_waiting_reasons(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        page = self.fixture.page
        path = status.path(page, 'C1')
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text('{"feedback":"Check this change."}\n')
        key = 'page-owner/C1'
        items = review_items.read(self.root, show=review_items.ReviewState.VALIDATING)
        self.assertEqual([(item.key, item.state) for item in items], [(key, review_items.ReviewState.VALIDATING)])
        self.assertEqual(self.command('list'), (0, '', ''))
        before = path.read_bytes()
        self.assertEqual(self.command('set', '--item', key, '--text', WHY),
                         (1, '', 'waiting_why: The item is not Waiting.\n'))
        self.assertEqual(path.read_bytes(), before)

    def test_validate_uses_automatic_reason_for_spaces(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        delivery = change_page.submit(self.fixture.page, urlencode(self.fixture.answer(outcome='validate', feedback='   ')), status)
        self.assertIsInstance(delivery, change_page.MessageDelivery)
        self.assertEqual(status.slots(self.fixture.page, 'C1').waiting_why,
                         'The user asked the showrunner to validate this change.')

    def test_delivery_rewrite_keeps_unknown_handoff_time(self) -> None:
        status = change_page.DeliveryStatus(self.root)
        page = self.fixture.page
        path = status.path(page, 'C1')
        _ = path.write_text('{"validation":{"attempt":"check","state":"sent"}}\n')
        with status.locked():
            slots = replace(status.slots(page, 'C1'), waiting_why=WHY)
            status.write_slots(page, 'C1', slots)
        self.assertEqual(status.slots(page, 'C1').waiting_why, WHY)
        self.assertNotIn('handed_at', cast(dict[str, object], json.loads(path.read_text())))

    def test_set_updates_unowned_question_under_canonical_lock(self) -> None:
        item = self.enter_waiting(review_items.ReviewType.QUESTION, WHY)
        assert isinstance(item.source, review_items.QuestionSource)
        owner = staff.StaffId('former-owner')
        path = state_root() / owner / 'questions' / item.source.record.path.name
        path.parent.mkdir(parents=True)
        record = replace(item.source.record, path=path, owner=owner)
        update_registration.write_question_record(record)
        unowned = next(value for value in review_items.read(self.root)
                       if isinstance(value.source, review_items.UnownedQuestionSource) and value.source.record.path == path)
        write = update_registration.write_question_record

        def write_under_lock(current: update_registration.QuestionRecord) -> None:
            lock = state_root() / 'outstanding' / (owner + '.lock')
            with lock.open('a') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            write(current)

        reason = 'The former owner must answer the question.'
        with patch.object(update_registration, 'write_question_record', side_effect=write_under_lock):
            self.assertEqual(self.command('set', '--item', unowned.key, '--text', reason), (0, '', ''))
        current = next(value for value in review_items.read(self.root) if value.key == unowned.key)
        self.assertEqual(current.waiting_why, reason)

    def test_set_prints_one_line_for_refusal(self) -> None:
        item = self.enter_waiting(review_items.ReviewType.QUESTION, WHY)
        with patch.object(update_registration, 'set_waiting_why', side_effect=Refusal('Owner unavailable.\nTry another item.')):
            code, output, errors = self.command('set', '--item', item.key, '--text', WHY)
        self.assertEqual((code, output), (1, ''))
        self.assertEqual(errors, 'waiting_why: Owner unavailable. Try another item.\n')

    def test_set_refuses_proposal_completed_before_writer_lock(self) -> None:
        item = self.enter_waiting(review_items.ReviewType.PROPOSAL, WHY)
        lock = proposal_issue.issue_lock
        completed: list[bytes] = []

        @contextmanager
        def complete_before_write(path: Path) -> Generator[None]:
            with lock(path):
                _ = path.write_text(path.read_text().replace('status: open', 'status: closed'))
                completed.append(path.read_bytes())
                yield

        with patch.object(proposal_issue, 'issue_lock', side_effect=complete_before_write):
            code, output, errors = self.command('set', '--item', item.key, '--text', 'A stale reason.')
        self.assertEqual((code, output), (1, ''))
        self.assertEqual(errors, 'waiting_why: The item is not Waiting.\n')
        self.assertEqual(self.proposal.read_bytes(), completed[0])

    def test_set_refuses_efficiency_completed_before_writer_lock(self) -> None:
        item = self.enter_waiting(review_items.ReviewType.EFFICIENCY, WHY)
        lock = file_lock.exclusive
        target = efficiency_reviews.path(self.ledger)
        before = target.read_bytes()

        @contextmanager
        def complete_before_write(path: Path) -> Generator[None]:
            with lock(path):
                _ = self.ledger.write_text(self.ledger.read_text().replace(' — deferred', ' — done'))
                yield

        with patch.object(file_lock, 'exclusive', side_effect=complete_before_write):
            code, output, errors = self.command('set', '--item', item.key, '--text', 'A stale reason.')
        self.assertEqual((code, output), (1, ''))
        self.assertEqual(errors, 'waiting_why: The item is not Waiting.\n')
        self.assertEqual(target.read_bytes(), before)


def case(kind: review_items.ReviewType, check: Callable[[WaitingWhyTests, review_items.ReviewType], None]
         ) -> Callable[[WaitingWhyTests], None]:
    def run(test: WaitingWhyTests) -> None:
        check(test, kind)
    return run


for kind in (review_items.ReviewType.QUESTION, review_items.ReviewType.INVESTIGATION,
             review_items.ReviewType.PROPOSAL, review_items.ReviewType.EFFICIENCY):
    for name, check in (('refusal', WaitingWhyTests.check_refusal), ('store', WaitingWhyTests.check_store),
                        ('list', WaitingWhyTests.check_list), ('set', WaitingWhyTests.check_set)):
        setattr(WaitingWhyTests, f'test_{kind.value}_{name}', case(kind, check))


if __name__ == '__main__':
    _ = unittest.main()
