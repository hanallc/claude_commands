"""The user's question supplies the Waiting reason for each Review type."""
from __future__ import annotations

import unittest
from dataclasses import replace
from shlex import split
from typing import cast, override
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from scripts.lib.fixture_test_support import set_up_fixture
from scripts.agents.remote import local_machine
from scripts.production import investigation, staff, update_registration
from scripts.production import test_waiting_why as fixtures
from scripts.review_links import change_browser, change_page, new_item_dialog, reply_tokens, review_actions, review_items, review_page, review_submissions


class WaitingActionTests(unittest.TestCase):
    fixture: fixtures.WaitingWhyTests = fixtures.WaitingWhyTests('runTest')

    @override
    def setUp(self) -> None:
        self.fixture = set_up_fixture(self.addCleanup, fixtures.WaitingWhyTests('runTest'))

    def test_user_question_automatically_stores_its_text_for_each_type(self) -> None:
        doc = self.fixture.fixture.production
        _ = doc.write_text(doc.read_text() + '\n> **Status: PRODUCTION — running.**\n')
        recipient = staff.snapshot().by_doc(local_machine(), doc)
        assert recipient is not None
        request = new_item_dialog.NewItemRequest(f'r1-{uuid4().hex}-question-{recipient.id}',
            new_item_dialog.SubmissionKind.QUESTION, recipient.id, 'What caused this?', new_item_dialog.NoEvidence())
        sent = review_submissions.submit(request)
        original = sent.reply_token
        self.assertTrue(review_submissions.answer(sent.key, 'The cause is known.', original))

        owner = self.fixture.fixture.unit.id
        for report_kind in investigation.InvestigationKind:
            record = investigation.file(owner, f'Check the {report_kind}', 'What caused this?',
                                        waiting_why=fixtures.WHY, kind=report_kind)
            _ = investigation.answer(owner, record.id, 'The cause is known.')
        for kind in review_items.REVIEW_TYPES:
            with self.subTest(kind=kind):
                item = next(item for item in review_items.read(self.fixture.root) if item.type == kind)
                words = 'Which evidence resolves this ' + kind.value + '?'
                status = change_page.DeliveryStatus(self.fixture.root)
                if kind == review_items.ReviewType.CHANGE:
                    _ = status.ask(self.fixture.fixture.page, 'C1', words)
                elif kind == review_items.ReviewType.QUESTION:
                    browser = change_page.ReviewBrowser('private', kind='questions')
                    question = next(value for value in change_browser.prepare(browser, status).questions if value.key == item.key)
                    identities = update_registration.QuestionStaff.read()
                    scope = update_registration.question_scope(item.owner, identities.record)
                    _ = change_browser.save_outcome(scope, question, 'question', words, identities)
                elif kind == review_items.ReviewType.RESPONSE:
                    folder = change_page.console_folder(self.fixture.root)
                    browser = change_page.ReviewBrowser(*change_browser.browser_manifest(folder))
                    body = urlencode({'row': item.key, 'token': change_browser.row_token(browser, item.key),
                                      'outcome': 'question', 'feedback': words})
                    _ = review_page.submit(self.fixture.root, body, status)
                elif isinstance(item.source, review_items.InvestigationSource):
                    _ = review_actions.investigation_action(item, 'question', words, status)
                else:
                    _ = review_actions.decision_action(item, 'question', words, status)
                current = next(value for value in review_items.read(self.fixture.root) if value.key == item.key)
                self.assertEqual(current.state, review_items.ReviewState.WAITING)
                self.assertEqual(current.waiting_why, words)

    def test_review_sends_a_followup_with_trailing_spaces_and_a_newline(self) -> None:
        doc = self.fixture.fixture.production
        _ = doc.write_text(doc.read_text() + '\n> **Status: PRODUCTION — running.**\n')
        recipient = staff.snapshot().by_doc(local_machine(), doc)
        assert recipient is not None
        request = new_item_dialog.NewItemRequest(f'r1-{uuid4().hex}-question-{recipient.id}',
            new_item_dialog.SubmissionKind.QUESTION, recipient.id, 'What caused this?', new_item_dialog.NoEvidence())
        sent = review_submissions.submit(request)
        original = sent.reply_token
        self.assertTrue(review_submissions.answer(sent.key, 'The cause is known.', original))
        root = self.fixture.root
        item = next(item for item in review_items.read(root) if item.type == review_items.ReviewType.RESPONSE)
        browser = change_page.ReviewBrowser(*change_browser.browser_manifest(change_page.console_folder(root)))
        words = 'Which evidence shows the cause?'
        body = urlencode({'row': item.key, 'token': change_browser.row_token(browser, item.key),
                          'outcome': 'question', 'feedback': words + '  \n'})
        status = change_page.DeliveryStatus(root)
        delivery, _ = review_page.submit(root, body, status)
        assert isinstance(delivery, change_page.MessageDelivery)
        with patch.object(change_page, 'send_to_staff', return_value=None) as send:
            change_page.deliver(delivery, status)
        self.assertIn('\n' + words + '\nAnswer with this command:', delivery.message)
        self.assertEqual(send.call_count, 1)
        message = cast(str, send.call_args.args[2])
        command = split(message.splitlines()[-1])
        token = command[command.index('--question') + 1]
        self.assertTrue(token != original)
        proof = review_submissions.current_reply_proof(sent.key)
        self.assertTrue(reply_tokens.derive(proof) == token)
        assert isinstance(delivery.page, change_page.UserQuestionPage)
        legacy: change_page.StoredUserAnswer = {
            **delivery.page.saved, "scoped_reply": {"key": sent.key, "token_sha256": proof.sha256},
        }
        before_move = replace(delivery, page=replace(delivery.page, saved=legacy))
        self.assertTrue(change_page.outbound_review_message(before_move).text == message)
        for path in (sent.path, status.path(delivery.page, delivery.row), status.closeout_path(delivery.page, delivery.row)):
            note = path.read_text(encoding="utf-8")
            self.assertFalse(token in note)
            self.assertFalse(proof.sha256 in note)
        send.assert_called_once_with(recipient.id, change_page.SENDER, message,
                                     key=change_page.review_delivery_key(delivery.page.review_list, delivery.row, delivery.attempt))


if __name__ == '__main__':
    _ = unittest.main()
