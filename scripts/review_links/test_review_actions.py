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
from scripts.production.staff_test_support import fixture_member
from scripts.production import test_waiting_why as fixtures
from scripts.review_links import change_browser, change_page, new_item_dialog, reply_tokens, review_actions, review_items, review_page, review_submissions


class WaitingActionTests(unittest.TestCase):
    fixture: fixtures.WaitingWhyTests = fixtures.WaitingWhyTests('runTest')

    @override
    def setUp(self) -> None:
        self.fixture = set_up_fixture(self.addCleanup, fixtures.WaitingWhyTests('runTest'))

    def test_interrupted_mark_read_recovers_words_from_the_native_log(self) -> None:
        owner = self.fixture.fixture.unit.id
        root = self.fixture.root
        words = 'Keep the saved explanation.  \n'
        for recovery in ('startup', 'retry'):
            with self.subTest(recovery=recovery):
                record = investigation.file(owner, 'Check the reply', 'What caused this?', waiting_why=fixtures.WHY)
                _ = investigation.answer(owner, record.id, 'The cause is known.')
                key = review_items.investigation_record_key(record)
                item = next(item for item in review_items.read(root) if item.key == key)
                status = change_page.DeliveryStatus(root)
                with patch.object(status, 'mark_review_saved', side_effect=OSError('The receipt did not save.')):
                    with self.assertRaisesRegex(OSError, 'The receipt did not save'):
                        _ = review_actions.investigation_action(item, 'read', words, status)
                saved = investigation.load(owner, record.id)
                self.assertIs(saved.state, investigation.InvestigationState.READ)
                self.assertEqual(saved.log[-1].words, 'Marked read: ' + words)

                restarted = change_page.DeliveryStatus(root)
                with (patch.object(change_page, 'send_to_staff', return_value=None) as send,
                      patch.object(investigation, 'mark_read', side_effect=AssertionError('Mark read repeated'))):
                    if recovery == 'retry':
                        result = review_actions.retry_action_feedback(restarted, key, 'read', words, '', '')
                        assert not isinstance(result, review_actions.NoActionFeedbackReceipt)
                        delivery, answer = result
                        self.assertEqual(answer, 'marked read')
                        change_page.deliver(delivery, restarted)
                    change_page.recover_review_messages(restarted)
                    change_page.recover_review_messages(restarted)
                    send.assert_called_once()
                    self.assertTrue(send.call_args.kwargs['key'] == saved.log[-1].feedback_request_id)
                    self.assertIn("Nate's words:\n" + words.strip(), cast(str, send.call_args.args[2]))
                    result = review_actions.retry_action_feedback(restarted, key, 'read', words, '', '')
                    assert not isinstance(result, review_actions.NoActionFeedbackReceipt)
                    self.assertIsInstance(result[0], change_page.NoMessageDelivery)
                    self.assertEqual(result[1], 'marked read')
                self.assertEqual(investigation.load(owner, record.id), saved)

    def test_unsaved_mark_read_cannot_claim_a_later_requests_words(self) -> None:
        owner = self.fixture.fixture.unit.id
        root = self.fixture.root
        record = investigation.file(owner, 'Check the reply', 'What caused this?', waiting_why=fixtures.WHY)
        key = review_items.investigation_record_key(record)
        words = 'Keep this explanation.'

        def waiting_item() -> review_items.ReviewItem:
            return next(item for item in review_items.read(root) if item.key == key)

        first = waiting_item()
        first_revision = review_page.record_revision(first, {})
        status = change_page.DeliveryStatus(root)
        # Stop after the provisional words save.
        write_slots = status.write_slots

        def stop_before_mark_read(page: change_page.ReviewPage, row: str,
                                  slots: change_page.DeliverySlots) -> None:
            write_slots(page, row, slots)
            raise OSError('The action did not start.')

        with patch.object(status, 'write_slots', side_effect=stop_before_mark_read):
            with self.assertRaisesRegex(OSError, 'The action did not start'):
                _ = review_actions.investigation_action(first, 'read', words, status, feedback_revision=first_revision)
        self.assertEqual(investigation.load(owner, record.id), record)

        status = change_page.DeliveryStatus(root)
        with patch.object(change_page, 'send_to_staff', return_value=None) as send:
            investigation.set_waiting_why(owner, record.id, 'Wait for the revised explanation.')
            second = waiting_item()
            second_revision = review_page.record_revision(second, {})
            self.assertNotEqual(second_revision, first_revision)
            delivery, _ = review_actions.investigation_action(second, 'read', words, status,
                                                              feedback_revision=second_revision)
            change_page.deliver(delivery, status)
            restarted = change_page.DeliveryStatus(root)
            change_page.recover_review_messages(restarted)
            send.assert_called_once()
            result = review_actions.retry_action_feedback(restarted, key, 'read', words, first_revision, '')
            self.assertIsInstance(result, review_actions.NoActionFeedbackReceipt)

    def test_startup_releases_an_unsaved_mark_read_for_the_same_form(self) -> None:
        owner = self.fixture.fixture.unit.id
        root = self.fixture.root
        record = investigation.file(owner, 'Check the reply', 'What caused this?', waiting_why=fixtures.WHY)
        key = review_items.investigation_record_key(record)
        _ = change_page.console_folder(root)
        snapshot = review_page.build_snapshot(root)
        words = 'Keep this explanation.'
        revision = review_page.record_revision(snapshot.records[key], snapshot.assets)
        fields = {'row': [key], 'token': [change_browser.row_token(snapshot.browser, key)],
                  'outcome': ['read'], 'feedback': [words], 'revision': [revision]}
        status = change_page.DeliveryStatus(root)
        write_slots = status.write_slots

        def interrupt(page: change_page.ReviewPage, row: str, slots: change_page.DeliverySlots) -> None:
            write_slots(page, row, slots)
            raise OSError('The action did not start.')

        with patch.object(status, 'write_slots', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'The action did not start'):
                _ = review_page.submit_snapshot(root, snapshot, fields, status)
        self.assertEqual(investigation.load(owner, record.id), record)

        restarted = change_page.DeliveryStatus(root)
        with patch.object(change_page, 'send_to_staff', return_value=None) as send:
            change_page.recover_review_messages(restarted)
            change_page.recover_review_messages(restarted)
            send.assert_not_called()
            page = review_actions.action_feedback_page(restarted, key, 'read', words, revision, '')
            stored = restarted.metadata_page(page)
            assert isinstance(stored, change_page.ReviewMessagePage)
            self.assertFalse(stored.saved['saved'])
            self.assertIsInstance(restarted.slots(stored, page.rows[0]).verdict, change_page.NoDelivery)
            delivery, answer = review_page.submit_snapshot(root, snapshot, fields, restarted)
            self.assertEqual(answer, 'marked read')
            change_page.deliver(delivery, restarted)
            change_page.recover_review_messages(change_page.DeliveryStatus(root))
            send.assert_called_once()
            saved = investigation.load(owner, record.id)
            self.assertIs(saved.state, investigation.InvestigationState.READ)
            mark, = [event for event in saved.log if event.words == 'Marked read: ' + words]
            self.assertTrue(send.call_args.kwargs['key'] == mark.feedback_request_id)

    def test_producer_notice_says_to_assign_a_bug_with_no_assignee(self) -> None:
        producer = fixture_member('producer', name='bob')
        request = new_item_dialog.NewItemRequest(
            f'r6-{uuid4().hex}', new_item_dialog.SubmissionKind.BUG, producer.id,
            'Check the Builds queue.', new_item_dialog.NoEvidence(), page='builds',
            page_recipient=new_item_dialog.PageRecipient.PRODUCER, assignment=new_item_dialog.ProducerAssigns())
        saved = review_submissions.submit(request)
        deliveries = review_actions.prepare_submission_deliveries(
            saved, request.words, change_page.DeliveryStatus(self.fixture.root),
            evidence_page='', members=staff.snapshot())
        assert isinstance(deliveries.primary, change_page.MessageDelivery)
        self.assertEqual(deliveries.primary.recipients, (producer.id,))
        self.assertIn('\nYou assign it.\n', deliveries.primary.message)
        self.assertIsInstance(deliveries.assignee, change_page.NoMessageDelivery)

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
