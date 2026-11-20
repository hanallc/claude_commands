"""The user's question supplies the Waiting reason for each Review type."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import cast, override
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from scripts.agents.remote import local_machine
from scripts.lib import atomic_file, file_lock
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import change_review, delivery_state, investigation, record_store, staff, update_registration
from scripts.production import test_waiting_why as fixtures
from scripts.review_links import change_browser, change_page, new_item_dialog, review_actions, review_items, review_page, review_submissions


class WaitingActionTests(unittest.TestCase):
    fixture: fixtures.WaitingWhyTests = fixtures.WaitingWhyTests('runTest')

    @override
    def setUp(self) -> None:
        self.fixture = fixtures.WaitingWhyTests('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_user_question_automatically_stores_its_text_for_each_type(self) -> None:
        doc = self.fixture.fixture.production
        _ = doc.write_text(doc.read_text() + '\n> **Status: PRODUCTION — running.**\n')
        recipient = staff.snapshot().by_doc(local_machine(), doc)
        assert recipient is not None
        request = new_item_dialog.NewItemRequest(f'r1-{uuid4().hex}-question-{recipient.id}',
            new_item_dialog.SubmissionKind.QUESTION, recipient.id, 'What caused this?', new_item_dialog.NoEvidence())
        sent = review_submissions.submit(request)
        self.assertTrue(review_submissions.answer(sent.key, 'The cause is known.', sent.reply_token))

        owner = self.fixture.fixture.unit.id
        for report_kind in investigation.InvestigationKind:
            record = investigation.file(owner, f'Check the {report_kind}', 'What caused this?',
                                        waiting_why=fixtures.WHY, kind=report_kind)
            _ = investigation.answer(owner, record.id, 'The cause is known.')
        for kind in review_items.REVIEW_TYPES:
            with self.subTest(kind=kind):
                item = next(item for item in review_items.read(self.fixture.root, assign_ids=False) if item.type == kind)
                words = 'Which evidence resolves this ' + kind.value + '?'
                status = change_page.DeliveryStatus(self.fixture.root)
                if kind == review_items.ReviewType.CHANGE:
                    _ = status.ask(self.fixture.fixture.page, 'C1', words)
                elif kind == review_items.ReviewType.QUESTION:
                    browser = change_page.ReviewBrowser('private', kind='questions')
                    question = next(value for value in change_browser.prepare(browser, status).questions if value.key == item.key)
                    _ = change_browser.submit_question(question, 'followup', words, status)
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
                current = next(value for value in review_items.read(self.fixture.root, assign_ids=False) if value.key == item.key)
                self.assertEqual(current.state, review_items.ReviewState.WAITING)
                self.assertEqual(current.waiting_why, words)

    def test_failed_source_save_leaves_the_previous_uncertain_send(self) -> None:
        status = change_page.DeliveryStatus(self.fixture.root)
        source = self.fixture.root / "I1.md"
        recipient = self.fixture.fixture.unit.id
        saved: change_page.StoredReviewMessage = {
            "source": str(source), "row": "I1", "owner": recipient, "message": "Which check passed?", "saved": False}
        page = change_page.ReviewMessagePage(source, source.parent, ("I1",), "", saved)
        question = change_page.OpenQuestion("one", saved["message"], change_page.NoDelivery())
        first = change_page.DeliveryIntent("first-attempt", "verdict", recipients=(recipient,))
        slots = change_page.DeliverySlots(change_page.NoDelivery(), first, question)
        with status.locked():
            status.write_slots(page, "I1", slots)
        path = status.path(page, "I1")
        fields = delivery_state.read_fields(path)
        progress = change_page.read_question_progress(fields["question_delivery"])
        with status.locked():
            change_page.write_review_progress(path, fields, change_page.begin_question_send(progress))
        previous = path.read_bytes()

        def fail() -> None:
            self.assertEqual(path.read_bytes(), previous)
            raise OSError("The source write failed.")

        replacement = change_page.OpenQuestion("two", "What result followed?", change_page.NoDelivery())
        with self.assertRaisesRegex(OSError, "source write failed"):
            _ = review_actions.save_delivery(page, recipient, fail, status, replacement)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(change_page.read_question_progress(delivery_state.read_fields(path)["question_delivery"]).attempt,
                         first.attempt)

    def test_published_question_keeps_recovery_when_source_sync_fails(self) -> None:
        owner = self.fixture.fixture.unit.id
        record = investigation.file(owner, "Review the layout", "What caused this?", waiting_why=fixtures.WHY)
        _ = investigation.answer(owner, record.id, "The options changed.")
        root = self.fixture.root
        status = change_page.DeliveryStatus(root)
        item = next(item for item in review_items.read(root, assign_ids=False)
                    if item.key == f"investigation/{record.owner}/{record.id}")
        words = "Which check passed?"
        review_action = investigation.review_action

        def publish(*_args: object, **_kwargs: object) -> None:
            self.assertEqual(list(root.glob(change_page.DELIVERY_PREFIX + "*")), [])
            _ = review_action(owner, record.id, "question", words)
            raise atomic_file.AtomicDurabilityUnconfirmed("Cannot sync the source folder.")

        with patch.object(investigation, "review_action", side_effect=publish):
            delivery, _ = review_actions.investigation_action(item, "question", words, status)
        assert isinstance(delivery, change_page.MessageDelivery)
        self.assertEqual(investigation.load(owner, record.id).waiting_why, words)
        question = status.slots(delivery.page, delivery.row).question
        assert isinstance(question, change_page.OpenQuestion)
        self.assertEqual(question.text, words)
        self.assertTrue(status.has_deferred_questions())
        assert isinstance(delivery.page, change_page.ReviewMessagePage)
        self.assertEqual(delivery.page.saved.get("question_source_key"), delivery.attempt)

    def test_restart_recovers_a_question_after_the_first_delivery_write_fails(self) -> None:
        owner = self.fixture.fixture.unit.id
        record = investigation.file(owner, "Review the layout", "What caused this?", waiting_why=fixtures.WHY)
        _ = investigation.answer(owner, record.id, "The options changed.")
        root = self.fixture.root
        item = next(item for item in review_items.read(root, assign_ids=False)
                    if item.key == f"investigation/{record.owner}/{record.id}")
        status = change_page.DeliveryStatus(root)
        with (patch.object(status, "write_slots", side_effect=OSError("Cannot save delivery.")),
              self.assertRaisesRegex(OSError, "Cannot save delivery")):
            _ = review_actions.investigation_action(item, "question", "Which option changed?", status)

        saved = investigation.load(owner, record.id)
        self.assertEqual(saved.state, investigation.InvestigationState.OPEN)
        self.assertEqual(saved.waiting_why, "Which option changed?")
        self.assertEqual(list(root.glob(change_page.DELIVERY_PREFIX + "*")), [])
        with patch.object(change_page, "send_to_staff", return_value=None) as sender:
            restarted = change_page.DeliveryStatus(root)
            with patch.object(investigation, "read_review_record",
                              side_effect=investigation.InvestigationError("Cannot read the saved Ask.")):
                change_page.recover_pending(restarted)
                self.assertTrue(restarted.question_sources_pending.is_set())
                sender.assert_not_called()
            change_page.recover_deferred_questions(restarted)
            change_page.recover_pending(restarted)
        self.assertFalse(restarted.question_sources_pending.is_set())
        sender.assert_called_once()
        path, = root.glob(change_page.DELIVERY_PREFIX + "*")
        progress = change_page.read_question_progress(delivery_state.read_fields(path)["question_delivery"])
        self.assertEqual(progress.state, change_page.QuestionDeliveryState.CONFIRMED)
        self.assertIn("--question " + progress.question_id, cast(str, sender.call_args.args[2]))

    def test_snapshot_detects_a_recovered_question_failure(self) -> None:
        owner = self.fixture.fixture.unit.id
        record = investigation.file(owner, "Review the layout", "What changed?", waiting_why=fixtures.WHY)
        _ = investigation.answer(owner, record.id, "The options changed.")
        _ = investigation.review_action(owner, record.id, "question", "Which option changed?")
        root = self.fixture.root
        _ = change_page.console_folder(root)
        cache = review_page.ReviewSnapshotCache(root, start_worker=False)
        before = cache.get()
        item = next(value for value in before.records.values() if isinstance(value.source, review_items.InvestigationSource)
                    and value.source.record.id == record.id and value.source.record.owner == owner)
        asset = "question-delivery:" + item.key
        self.assertNotIn(asset, before.assets)
        status = change_page.DeliveryStatus(root)
        with patch.object(change_page, "send_to_staff", side_effect=change_page.QuestionSendStopped("The owner refused.")):
            change_page.recover_pending(status)
        self.assertNotEqual(before.source_stamps[root], record_store.file_stamp(root))
        self.assertIn("Question not delivered.", cache.get().assets[asset])

    def test_snapshot_reads_the_delivery_stamp_before_the_slots(self) -> None:
        owner = self.fixture.fixture.unit.id
        record = investigation.file(owner, "Review the layout", "What changed?", waiting_why=fixtures.WHY)
        _ = investigation.answer(owner, record.id, "The options changed.")
        _ = investigation.review_action(owner, record.id, "question", "Which option changed?")
        root = self.fixture.root
        _ = change_page.console_folder(root)
        status = change_page.DeliveryStatus(root)
        read = change_page.DeliveryStatus.slots
        changed = False

        def write_after_read(reader: change_page.DeliveryStatus, page: change_page.ReviewPage,
                             row: str) -> change_page.DeliverySlots:
            nonlocal changed
            old = read(reader, page, row)
            if row == record.id and not changed:
                changed = True
                self.assertIsInstance(old.question, change_page.NoQuestion)
                with patch.object(change_page, "send_to_staff",
                                  side_effect=change_page.QuestionSendStopped("The owner refused.")):
                    change_page.recover_pending(status)
            return old

        with patch.object(change_page.DeliveryStatus, "slots", new=write_after_read):
            cache = review_page.ReviewSnapshotCache(root, start_worker=False)
            snapshot = cache.get()
        self.assertTrue(changed)
        item = next(value for value in snapshot.records.values() if isinstance(value.source, review_items.InvestigationSource)
                    and value.source.record.id == record.id and value.source.record.owner == owner)
        self.assertIn("Question not delivered.", snapshot.assets["question-delivery:" + item.key])

    def test_review_sends_a_followup_with_trailing_spaces_and_a_newline(self) -> None:
        doc = self.fixture.fixture.production
        _ = doc.write_text(doc.read_text() + '\n> **Status: PRODUCTION — running.**\n')
        recipient = staff.snapshot().by_doc(local_machine(), doc)
        assert recipient is not None
        request = new_item_dialog.NewItemRequest(f'r1-{uuid4().hex}-question-{recipient.id}',
            new_item_dialog.SubmissionKind.QUESTION, recipient.id, 'What caused this?', new_item_dialog.NoEvidence())
        sent = review_submissions.submit(request)
        self.assertTrue(review_submissions.answer(sent.key, 'The cause is known.', sent.reply_token))
        root = self.fixture.root
        item = next(item for item in review_items.read(root, assign_ids=False) if item.type == review_items.ReviewType.RESPONSE)
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
        send.assert_called_once_with(recipient.id, change_page.SENDER, delivery.message,
                                     key=change_page.review_delivery_key(delivery.page.review_list, delivery.row, delivery.attempt),
                                     question=True)

        self.assertTrue(change_page.answer_item(sent.key, "The test report shows the cause.", sent.reply_token, status))
        path = status.path(delivery.page, delivery.row)
        result = change_page.read_question_progress(delivery_state.read_fields(path)["question_delivery"])
        self.assertEqual(result.state, change_page.QuestionDeliveryState.ANSWERED)
        self.assertEqual(result.attempt, delivery.attempt)


    def test_scoped_reply_and_native_sender_do_not_hold_opposite_locks(self) -> None:
        doc = self.fixture.fixture.production
        _ = doc.write_text(doc.read_text() + '\n> **Status: PRODUCTION — running.**\n')
        recipient = staff.snapshot().by_doc(local_machine(), doc)
        assert recipient is not None
        request = new_item_dialog.NewItemRequest(f"r1-{uuid4().hex}-question-{recipient.id}",
            new_item_dialog.SubmissionKind.QUESTION, recipient.id, "What changed?", new_item_dialog.NoEvidence())
        sent = review_submissions.submit(request)
        self.assertTrue(review_submissions.answer(sent.key, "The layout changed.", sent.reply_token))
        root = self.fixture.root
        status = change_page.DeliveryStatus(root)
        item = next(item for item in review_items.read(root, assign_ids=False) if item.type == review_items.ReviewType.RESPONSE)
        browser = change_page.ReviewBrowser(*change_browser.browser_manifest(change_page.console_folder(root)))
        body = urlencode({"row": item.key, "token": change_browser.row_token(browser, item.key),
                          "outcome": "question", "feedback": "Which screen changed?"})
        delivery, _ = review_page.submit(root, body, status)
        assert isinstance(delivery, change_page.MessageDelivery)
        question = status.slots(delivery.page, delivery.row).question
        assert isinstance(question, change_page.OpenQuestion)
        sender_ready, reply_ready = Event(), Event()
        source_reply = update_registration.reply_to_submission

        def reply(path: Path, key: new_item_dialog.ScopedSubmissionKey, words: str, token: str, *,
                  owner: staff.StaffId) -> bool:
            reply_ready.set()
            return source_reply(path, key, words, token, owner=owner)

        locks: list[file_lock.LockResult] = []

        def transport(*_args: object, **_kwargs: object) -> None:
            sender_ready.set()
            if not reply_ready.wait(TEST_HANG_GUARD_SECS):
                raise AssertionError("The reply did not reach the source writer.")
            with change_review.review_delivery_lock(root, wait=False) as lock:
                pass
            locks.append(lock)
            if lock is file_lock.LockResult.ACQUIRED:
                change_page.mark_sender_started()

        with (ThreadPoolExecutor(max_workers=2) as executor,
              patch.object(update_registration, "reply_to_submission", side_effect=reply),
              patch.object(change_page, "send_to_staff", side_effect=transport) as sender):
            sending = executor.submit(change_page.deliver, delivery, status)
            self.assertTrue(sender_ready.wait(TEST_HANG_GUARD_SECS))
            answering = executor.submit(review_submissions.answer, sent.key, "The Review screen changed.",
                                        question.question_id, root=root)
            sending.result(TEST_HANG_GUARD_SECS)
            self.assertTrue(answering.result(TEST_HANG_GUARD_SECS))
            sender.assert_called_once()
        self.assertEqual(locks, [file_lock.LockResult.ACQUIRED])
        path = status.path(delivery.page, delivery.row)
        self.assertEqual(change_page.read_question_progress(delivery_state.read_fields(path)["question_delivery"]).state,
                         change_page.QuestionDeliveryState.ANSWERED)

    def test_final_submission_failure_retires_the_intent(self) -> None:
        doc = self.fixture.fixture.production
        _ = doc.write_text(doc.read_text() + '\n> **Status: PRODUCTION — running.**\n')
        recipient = staff.snapshot().by_doc(local_machine(), doc)
        assert recipient is not None
        request = new_item_dialog.NewItemRequest(f"r1-{uuid4().hex}-question-{recipient.id}",
            new_item_dialog.SubmissionKind.QUESTION, recipient.id, "What changed?", new_item_dialog.NoEvidence())
        saved = review_submissions.submit(request)
        status = change_page.DeliveryStatus(self.fixture.root)
        delivery = review_actions.prepare_submission_delivery(saved, request.words, status)
        assert isinstance(delivery, change_page.MessageDelivery)
        with (patch.object(change_page, "send_to_staff", side_effect=change_page.QuestionSendStopped("Refused.")) as sender,
              patch.object(change_page, "review_alert_showrunners", return_value=())):
            change_page.deliver(delivery, status)
            self.assertFalse(status.has_deferred_questions())
            self.assertEqual(status.slots(delivery.page, delivery.row).intents(), ())
            failed = status.failed_delivery(delivery.page, delivery.row)
            assert isinstance(failed, change_page.FailedDelivery)
            self.assertEqual(failed.last_error, "Refused.")
            closeout = status.closeout_path(delivery.page, delivery.row)
            stamp = closeout.stat()
            change_page.recover_pending(change_page.DeliveryStatus(self.fixture.root))
            self.assertEqual(closeout.stat().st_ino, stamp.st_ino)
            self.assertIsInstance(review_actions.prepare_submission_delivery(saved, request.words, status),
                                  change_page.NoMessageDelivery)
            sender.assert_called_once()

    def test_native_answer_recovers_when_the_delivery_write_fails(self) -> None:
        doc = self.fixture.fixture.production
        _ = doc.write_text(doc.read_text() + '\n> **Status: PRODUCTION — running.**\n')
        recipient = staff.snapshot().by_doc(local_machine(), doc)
        assert recipient is not None
        request = new_item_dialog.NewItemRequest(f"r1-{uuid4().hex}-question-{recipient.id}",
            new_item_dialog.SubmissionKind.QUESTION, recipient.id, "What changed?", new_item_dialog.NoEvidence())
        sent = review_submissions.submit(request)
        self.assertTrue(review_submissions.answer(sent.key, "The layout changed.", sent.reply_token))
        root = self.fixture.root
        status = change_page.DeliveryStatus(root)
        item = next(item for item in review_items.read(root, assign_ids=False) if item.type == review_items.ReviewType.RESPONSE)
        browser = change_page.ReviewBrowser(*change_browser.browser_manifest(change_page.console_folder(root)))
        body = urlencode({"row": item.key, "token": change_browser.row_token(browser, item.key),
                          "outcome": "question", "feedback": "Which screen changed?"})
        delivery, _ = review_page.submit(root, body, status)
        assert isinstance(delivery, change_page.MessageDelivery)
        with patch.object(change_page, "send_to_staff", return_value=None):
            change_page.deliver(delivery, status)
        path = status.path(delivery.page, delivery.row)
        fields = delivery_state.read_fields(path)
        progress = change_page.read_question_progress(fields["question_delivery"])
        self.assertEqual(progress.state, change_page.QuestionDeliveryState.CONFIRMED)
        with (patch.object(delivery_state, "complete_review_reply", side_effect=OSError("Cannot save the delivery.")),
              self.assertRaisesRegex(OSError, "Cannot save the delivery")):
            _ = review_submissions.answer(sent.key, "The Review screen changed.", progress.question_id, root=root)
        for old in (False, True):
            with self.subTest(old_delivery=old):
                before = dict(fields)
                if old:
                    _ = before.pop("question_delivery")
                atomic_file.write_text_atomic(path, json.dumps(before) + "\n", new_file_mode=0o600)
                with patch.object(change_page, "send_to_staff") as sender:
                    change_page.recover_pending(change_page.DeliveryStatus(root))
                sender.assert_not_called()
                after = delivery_state.read_fields(path)
                question = delivery_state.decode_value(after).question
                assert isinstance(question, change_page.AnsweredQuestion)
                self.assertEqual(question.answer, "The Review screen changed.")
                result = change_page.read_question_progress(after["question_delivery"])
                self.assertEqual(result.state, change_page.QuestionDeliveryState.ANSWERED)
                self.assertEqual(result.question_id, progress.question_id)


if __name__ == '__main__':
    _ = unittest.main()
