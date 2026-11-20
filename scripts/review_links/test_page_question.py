"""Test shared page questions with private records and a fake sender."""
from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from io import StringIO
import threading
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.lib import atomic_file, conf, process_start
from scripts.message import send_budget, send_result
from scripts.production import delivery_state, staff, unrouted_alert
from scripts.review_links import change_page, image_store, page_question, question_record


SEND_QUESTION = change_page.send_question

def question_outcome(outcome: send_result.Outcome = "sent", detail: str = "fixture receipt"
                     ) -> tuple[send_result.ConfirmedSendOutcome, str]:
    return send_result.ConfirmedSendOutcome(send_result.OutcomeHeader(outcome, detail, "")), detail


def confirmed_schema_two(record: delivery_state.QuestionAttempt) -> dict[str, object]:
    return {"question_attempt": {
        "schema": 2, "source": asdict(record.source), "state": "sent", "attempt_id": record.attempt_id,
        "message_id": record.message_id, "message": record.message, "recipient_id": record.recipient_id,
        "revision": record.revision, "owner": None, "receipt": {"outcome": "sent", "detail": ""}, "error": "",
    }}


def saved_progress(path: Path) -> change_page.QuestionProgress:
    fields = delivery_state.read_fields(path)
    if "question_attempt" in fields:
        return change_page.page_progress(change_page.decode_page_attempt(fields))
    return change_page.read_question_progress(fields["question_delivery"])


def expire_saved_question(status: change_page.DeliveryStatus, path: Path) -> None:
    progress = saved_progress(path)
    deadline = progress.deadline if progress.deadline is not None else progress.saved_at + 86400
    with patch.object(time, "time", return_value=deadline):
        _ = change_page.expire_saved_questions(status, (path,))


class FakeQuestionTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.receipts: set[str] = set()

    def send(self, _recipient: staff.StaffId, _message: str, message_id: str
             ) -> tuple[send_result.ConfirmedSendOutcome, str]:
        change_page.mark_sender_started()
        self.calls.append(message_id)
        self.receipts.add(message_id)
        return question_outcome()


class FixtureStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[question_record.Question, ...]] = {}

    def questions(self, item: str) -> tuple[question_record.Question, ...]:
        return self.items.get(item, ())

    def add(self, item: str, question: question_record.Question) -> None:
        if any(saved.question_id == question.question_id for saved in self.questions(item)):
            raise question_record.AnswerRefused('duplicate id')
        self.items[item] = (*self.questions(item), question)

    def set_answer(self, item: str, question_id: str, answer: question_record.Answer) -> None:
        questions = self.questions(item)
        current = next((question for question in questions if question.question_id == question_id), None)
        if current is None or current.answer is not None:
            raise question_record.AnswerRefused('not an open question')
        self.items[item] = tuple(replace(question, answer=answer) if question.question_id == question_id else question
                                 for question in questions)


class PageQuestionTests(unittest.TestCase):
    root: Path = Path()
    store: FixtureStore = FixtureStore()
    status: change_page.DeliveryStatus = change_page.DeliveryStatus(Path())
    sent: list[tuple[staff.StaffId, str, str]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='page-question-')))
        config = self.root / 'config'
        config.mkdir()
        _ = (config / 'review_links.conf').write_text('# whole: 1..20\ndelivery_tries=2\n# range: 0..300\ndelivery_pause_seconds=0\n')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'CONF_DIR': str(config)})))
        self.store = FixtureStore()
        self.status = change_page.DeliveryStatus(self.root / 'delivery')
        self.sent = []
        _ = self.enterContext(patch.object(change_page, "send_question", side_effect=self.capture))

    def capture(self, recipient: staff.StaffId, message: str, message_id: str
                ) -> tuple[send_result.ConfirmedSendOutcome, str]:
        change_page.mark_sender_started()
        self.sent.append((recipient, message_id, message))
        return question_outcome()

    def ask(self, item: str = 'image-1', words: str = 'Which check passed?') -> change_page.MessageDelivery:
        target = change_page.QuestionDeliveryTarget(self.root / item, item, staff.StaffId('fixture-owner'),
            'Images '+item, 'The gate', 'images.py answer --id {item} --question {question_id} --text "<answer>"')
        return page_question.ask(self.store, target, words, self.status)

    def test_question_ids_start_at_one_for_each_item_and_increase_after_answers(self) -> None:
        _ = self.ask()
        _ = self.ask('image-2')
        result = page_question.answer(self.store, 'image-1', 'Q1', 'The gate passed.')
        self.assertIsInstance(result, page_question.Answered)
        _ = self.ask(words='Which repair worked?')
        self.assertEqual([question.question_id for question in self.store.questions('image-1')], ['Q1', 'Q2'])
        self.assertEqual([question.question_id for question in self.store.questions('image-2')], ['Q1'])

    def test_ask_saves_before_sending_and_a_repeat_sends_only_once(self) -> None:
        def capture(recipient: staff.StaffId, message: str, message_id: str
                    ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            self.assertEqual(self.store.questions("image-1")[0].words, "Which check passed?")
            self.assertEqual(self.attempt(delivery).state, delivery_state.QuestionSendState.CLAIMED)
            return self.capture(recipient, message, message_id)

        with patch.object(change_page, "send_question", side_effect=capture):
            delivery = self.ask()
            self.assertEqual(self.sent, [])
            change_page.deliver(delivery, self.status)
            change_page.deliver(self.ask(), self.status)
            self.status = change_page.DeliveryStatus(self.status.root)
            change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.store.questions('image-1')), 1)
        self.assertEqual(self.sent[0][2], 'Images image-1: Ask question\nThe gate\nWhich check passed?\n'
            +'Answer with this command: images.py answer --id image-1 --question Q1 --text "<answer>"')
        self.assertEqual(self.status.slots(delivery.page, delivery.row).intents(), ())

    def attempt(self, delivery: change_page.MessageDelivery) -> delivery_state.QuestionAttempt:
        return change_page.read_question_attempt(self.status.path(delivery.page, delivery.row))

    def effective(self, delivery: change_page.MessageDelivery) -> delivery_state.QuestionSendState:
        with self.status.locked():
            return change_page.question_delivery_view(self.status, self.job(delivery).delivery.saved).state

    def job(self, delivery: change_page.MessageDelivery) -> change_page.QuestionJob:
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        return delivery.job

    def test_schema_two_migration_keeps_the_key_deadline_and_final_result(self) -> None:
        states = (
            (delivery_state.QuestionSendState.PREPARED, "", change_page.QuestionDeliveryState.NOT_STARTED),
            (delivery_state.QuestionSendState.UNKNOWN, "", change_page.QuestionDeliveryState.NOT_STARTED),
            (delivery_state.QuestionSendState.UNKNOWN, "The saved send refused.", change_page.QuestionDeliveryState.FAILED),
            (delivery_state.QuestionSendState.FAILED, "", change_page.QuestionDeliveryState.FAILED),
            (delivery_state.QuestionSendState.SENT, "", change_page.QuestionDeliveryState.CONFIRMED),
        )
        for index, (state, error, expected) in enumerate(states):
            with self.subTest(state=state, error=error):
                delivery = self.ask(item=f"image-{index}")
                job = self.job(delivery)
                job.release()
                old = job.record
                path = self.status.path(delivery.page, delivery.row)
                receipt = {"outcome": "sent", "detail": "Saved receipt"} if state is delivery_state.QuestionSendState.SENT else None
                reason = "The owner refused." if state is delivery_state.QuestionSendState.FAILED else ""
                data = {"schema": 2, "source": asdict(old.source), "state": state, "attempt_id": old.attempt_id,
                        "message_id": old.message_id, "message": old.message, "recipient_id": old.recipient_id,
                        "revision": old.revision, "owner": None, "receipt": receipt, "error": reason,
                        "earlier_unconfirmed": state is delivery_state.QuestionSendState.UNKNOWN}
                _ = path.write_text(json.dumps({"question_attempt": data}))
                deadline = time.time() + 150
                recovery = path.with_name(
                    change_page.QUESTION_RECOVERY_PREFIX + path.name.removeprefix(change_page.DELIVERY_PREFIX))
                _ = recovery.write_text(json.dumps({"attempt": old.attempt_id, "question_id": old.source.question_id,
                                                    "deadline": deadline, "error": error}))
                change_page.migrate_delivery_file(self.status, path)
                migrated = self.attempt(delivery)
                progress = change_page.page_progress(migrated)
                self.assertEqual((migrated.message_id, migrated.recipient_id), (old.message_id, old.recipient_id))
                self.assertEqual(progress.attempt, old.attempt_id)
                self.assertEqual(progress.deadline, deadline)
                self.assertEqual(progress.state, expected)
                self.assertEqual(progress.error, error or reason)

    def test_only_a_proven_non_start_can_send_again(self) -> None:
        detail = "The owner has no route."
        non_start = send_result.ConfirmedSendOutcome(
            send_result.OutcomeHeader("retry", detail, change_page.QUESTION_NOT_STARTED))
        with patch.object(change_page, "send_question", return_value=(non_start, detail)) as send:
            delivery = self.ask()
            change_page.deliver(delivery, self.status)
        send.assert_called_once()
        first = self.attempt(delivery)
        self.assertEqual(change_page.page_progress(first).state, change_page.QuestionDeliveryState.NOT_STARTED)
        self.status = change_page.DeliveryStatus(self.status.root)
        retry = self.ask()
        self.assertEqual(retry.attempt, delivery.attempt)
        change_page.deliver(retry, self.status)
        self.assertEqual([call[1] for call in self.sent], [first.message_id])
        self.assertEqual(len(self.store.questions("image-1")), 1)
        self.assertEqual(self.attempt(retry).state, delivery_state.QuestionSendState.SENT)

    def test_a_live_claim_prevents_a_second_sender(self) -> None:
        started, release = threading.Event(), threading.Event()
        transport = FakeQuestionTransport()

        def capture(_recipient: staff.StaffId, _message: str, message_id: str
                    ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            change_page.mark_sender_started()
            started.set()
            self.assertTrue(release.wait(5))
            transport.calls.append(message_id)
            transport.receipts.add(message_id)
            return question_outcome()

        first = self.ask()
        status = self.status
        with patch.object(change_page, "send_question", side_effect=capture), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(change_page.deliver, first, status)
            try:
                self.assertTrue(started.wait(5))
                self.status = change_page.DeliveryStatus(status.root)
                change_page.deliver(self.ask(), self.status)
                self.assertEqual(self.effective(first), delivery_state.QuestionSendState.SENDING)
            finally:
                release.set()
            result.result(5)
        self.assertEqual(transport.calls, [self.attempt(first).message_id])
        self.assertEqual(transport.receipts, set(transport.calls))

    def test_a_retry_finishes_a_save_that_stops_after_the_question_write(self) -> None:
        original = self.store.add

        def interrupted_add(item: str, question: question_record.Question) -> None:
            original(item, question)
            raise OSError('interrupted after write')

        with patch.object(self.store, 'add', side_effect=interrupted_add):
            with self.assertRaisesRegex(OSError, 'interrupted after write'):
                _ = self.ask()
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.store.questions('image-1')), 1)
        self.assertEqual(list(self.status.root.glob(change_page.DELIVERY_PREFIX + '*')), [])
        self.status = change_page.DeliveryStatus(self.status.root)
        change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.store.questions('image-1')), 1)

    def test_an_old_attempt_without_a_saved_question_allows_a_new_ask(self) -> None:
        text = 'Which check passed?'
        orphan = self.ask(words=text)
        _ = self.job(orphan).release()
        self.store.items.clear()

        replacement = self.ask(words='Which repair passed?')
        self.assertNotEqual(replacement.attempt, orphan.attempt)
        self.assertEqual([question.question_id for question in self.store.questions('image-1')], ['Q1'])
        self.assertEqual(self.attempt(replacement).source.words, 'Which repair passed?')
        change_page.deliver(replacement, self.status)
        self.assertEqual(len(self.sent), 1)

    def test_a_first_delivery_write_failure_preserves_the_source(self) -> None:
        with (patch.object(change_page, 'publish_question_attempt', side_effect=OSError('Cannot save delivery.')),
              self.assertRaisesRegex(OSError, 'Cannot save delivery')):
            _ = self.ask()
        self.assertEqual([question.question_id for question in self.store.questions('image-1')], ['Q1'])
        self.assertEqual(self.sent, [])
        change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(self.store.questions('image-1')), 1)
        self.assertEqual(len(self.sent), 1)

    def test_claim_write_failure_keeps_the_saved_question(self) -> None:
        publish = change_page.publish_question_attempt

        def fail_claim(path: Path, attempt: delivery_state.QuestionAttempt) -> None:
            if attempt.state is delivery_state.QuestionSendState.CLAIMED:
                raise OSError("Cannot save the delivery claim.")
            publish(path, attempt)

        with patch.object(change_page, "publish_question_attempt", side_effect=fail_claim):
            refused = self.ask()
        assert isinstance(refused, change_page.PageQuestionSend)
        self.assertIsInstance(refused.job, change_page.NoQuestionJob)
        saved = self.attempt(refused)
        self.assertEqual(saved.state, delivery_state.QuestionSendState.READY)
        self.assertIsInstance(saved.owner, delivery_state.NoQuestionSendOwner)
        self.assertEqual(len(self.store.questions("image-1")), 1)
        self.assertEqual(self.sent, [])
        change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.attempt(refused).message_id, saved.message_id)

    def test_a_held_lock_blocks_resend_for_an_unverified_claim(self) -> None:
        first = self.ask()
        for identity in (
            process_start.Started("reused"), process_start.Zombie("zombie"),
            process_start.Unreadable("cannot read"),
        ):
            with self.subTest(identity=identity), patch.object(process_start, "process_start", return_value=identity):
                self.assertEqual(self.effective(first), delivery_state.QuestionSendState.UNKNOWN)
                with self.status.locked():
                    view = change_page.question_delivery_view(self.status, self.job(first).delivery.saved)
                self.assertIsInstance(view, change_page.ResendBlocked)
        _ = self.job(first).release()
        self.assertEqual(self.effective(first), delivery_state.QuestionSendState.READY)
        retry = self.ask()
        change_page.deliver(retry, self.status)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_never_reuses_the_send_marker(self) -> None:
        store = image_store.ImageQuestionStore(self.status.root / image_store.IMAGES_FOLDER)
        for number, accepted in enumerate((False, True)):
            with self.subTest(accepted=accepted):
                item = f"{number:012x}"
                folder = store.root / item
                folder.mkdir(parents=True)
                image: image_store.ImageRecord = {"id": item, "file": "gate.png", "caption": "The gate",
                    "owner": "fixture-owner", "subject": "Test", "registered_at": "2026-11-20T12:00:00+00:00"}
                image_store.write_record(folder, image)
                target = change_page.image_question_target(folder / image_store.RECORD, image)
                first = page_question.ask(store, target, "Which check passed?", self.status)
                path = self.status.path(first.page, first.row)
                attempt = self.attempt(first)
                with self.status.locked():
                    change_page.advance_question_attempt(path, attempt,
                                                         replace(attempt, state=delivery_state.QuestionSendState.SENDING))
                _ = self.job(first).release()
                self.status = change_page.DeliveryStatus(self.status.root)
                self.assertEqual(self.effective(first), delivery_state.QuestionSendState.UNKNOWN)
                outcome = question_outcome("skipped" if accepted else "sent")
                with patch.object(change_page, "send_question", return_value=outcome) as send:
                    change_page.recover_review_messages(self.status)
                    change_page.recover_review_messages(self.status)
                send.assert_not_called()
                recovered = self.attempt(first)
                self.assertEqual(change_page.page_progress(recovered).state, change_page.QuestionDeliveryState.UNKNOWN)
                self.assertEqual(recovered.attempt_id, attempt.attempt_id)
                self.assertIsInstance(recovered.receipt, delivery_state.NoQuestionSendReceipt)
                self.assertEqual([question.question_id for question in store.questions(item)], ["Q1"])

    def test_stale_job_cannot_replace_the_new_result(self) -> None:
        first = self.ask()
        old = self.attempt(first)
        _ = self.job(first).release()
        second = self.ask()
        change_page.deliver(second, self.status)
        sent = self.attempt(second)
        with self.status.locked(), self.assertRaisesRegex(ValueError, "stale"):
            change_page.advance_question_attempt(self.status.path(first.page, first.row), old,
                                                 replace(old, state=delivery_state.QuestionSendState.FAILED))
        self.assertEqual(self.attempt(second), sent)

    def test_sender_spawn_failure_keeps_the_saved_key_ready(self) -> None:
        member = staff.StaffRow(staff.StaffId("fixture-owner"), "showrunner", "fixture", staff.StaffRoot(),
                                current_session=staff.StaffId("fixture-owner-session"))
        first = self.ask()
        path = self.status.path(first.page, first.row)
        key = self.attempt(first).message_id
        before: list[change_page.QuestionDeliveryState] = []

        def fail_spawn(*_args: object, **_options: object) -> None:
            before.append(saved_progress(path).state)
            raise BlockingIOError(11, "The sender could not start.")

        with (patch.object(change_page, "send_question", new=SEND_QUESTION),
              patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((member,))),
              patch.object(send_budget, "seconds", return_value=10),
              patch.object(subprocess, "Popen", side_effect=fail_spawn)):
            change_page.deliver(first, self.status)
        self.assertEqual(before, [change_page.QuestionDeliveryState.NOT_STARTED])
        saved = self.attempt(first)
        self.assertEqual(change_page.page_progress(saved).state, change_page.QuestionDeliveryState.NOT_STARTED)
        self.assertEqual(saved.message_id, key)
        self.assertTrue(self.status.has_deferred_questions())
        change_page.recover_deferred_questions(self.status)
        self.assertEqual([key for _, key, _ in self.sent], [saved.message_id])

    def test_sync_failure_after_spawn_keeps_the_confirmation(self) -> None:
        first = self.ask()
        sync = atomic_file.sync_folder
        marked: list[change_page.QuestionDeliveryState] = []

        def fail_after_marker(folder: Path) -> None:
            if self.attempt(first).state is delivery_state.QuestionSendState.SENDING:
                marked.append(change_page.page_progress(self.attempt(first)).state)
                raise OSError("Cannot sync the send marker.")
            sync(folder)

        with patch.object(atomic_file, "sync_folder", side_effect=fail_after_marker):
            change_page.deliver(first, self.status)
        self.assertEqual(marked, [change_page.QuestionDeliveryState.UNKNOWN])
        self.assertEqual(self.effective(first), delivery_state.QuestionSendState.SENT)
        self.assertEqual(len(self.sent), 1)

    def test_result_publication_after_rename_keeps_acceptance(self) -> None:
        first = self.ask()
        path = self.status.path(first.page, first.row)
        transport = FakeQuestionTransport()
        sync = atomic_file.sync_folder

        def fail_after_result(folder: Path) -> None:
            if self.attempt(first).state is delivery_state.QuestionSendState.SENT:
                raise OSError("Cannot sync the receipt.")
            sync(folder)

        with (patch.object(change_page, "send_question", side_effect=transport.send),
              patch.object(atomic_file, "sync_folder", side_effect=fail_after_result)):
            change_page.deliver(first, self.status)
            self.status = change_page.DeliveryStatus(self.status.root)
            change_page.recover_review_messages(self.status)
            change_page.deliver(self.ask(), self.status)
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.receipts), 1)

    def test_failed_rename_keeps_the_published_send_marker(self) -> None:
        first = self.ask()
        path = self.status.path(first.page, first.row)
        transport = FakeQuestionTransport()
        replace_file = os.replace

        def fail_result_rename(source: Path, destination: Path) -> None:
            if destination == path:
                candidate = change_page.read_question_attempt(source)
                if candidate.state is delivery_state.QuestionSendState.SENT:
                    raise OSError("Cannot replace the delivery record.")
            replace_file(source, destination)

        with (patch.object(change_page, "send_question", side_effect=transport.send),
              patch.object(os, "replace", side_effect=fail_result_rename)):
            change_page.deliver(first, self.status)
        self.assertTrue(self.status.has_deferred_questions())
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENDING)
        self.assertEqual(self.effective(first), delivery_state.QuestionSendState.UNKNOWN)
        with patch.object(change_page, "send_question", side_effect=transport.send):
            change_page.recover_review_messages(change_page.DeliveryStatus(self.status.root))
            change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.receipts), 1)

    def test_typed_transport_results_set_the_delivery_state(self) -> None:
        state = delivery_state.QuestionSendState
        cases: tuple[tuple[send_result.Outcome, delivery_state.QuestionSendState], ...] = (
            ("sent", state.SENT), ("kept", state.UNKNOWN), ("queued", state.UNKNOWN),
            ("failed", state.UNKNOWN), ("refused", state.FAILED), ("skipped", state.SENT),
            ("replaced", state.UNKNOWN), ("held", state.UNKNOWN), ("pending", state.UNKNOWN),
            ("retry", state.UNKNOWN),
        )
        for outcome, expected in cases:
            with (self.subTest(outcome=outcome), patch.object(change_page, "alert_delivery_failures"),
                  patch.object(change_page, "send_question", return_value=question_outcome(outcome)) as send):
                delivery = self.ask(outcome)
                change_page.deliver(delivery, self.status)
                result = self.attempt(delivery)
                self.assertEqual(result.state, expected)
                send.assert_called_once_with(result.recipient_id, result.message, result.message_id)
                path = self.status.path(delivery.page, delivery.row)
                self.assertNotIn('"resend_requests"', path.read_text())
                self.assertEqual(set(self.status.take_deferred_questions()), {path} if expected is state.UNKNOWN else set())
                if outcome == "skipped":
                    self.assertEqual(result.receipt, delivery_state.QuestionSendReceipt("kept", "fixture receipt"))

    def test_unreadable_attempt_stays_pending_until_the_record_can_be_read(self) -> None:
        delivery = self.ask()
        path = self.status.path(delivery.page, delivery.row)
        with patch.object(change_page, "send_question", return_value=question_outcome("pending")):
            change_page.deliver(delivery, self.status)
        pending = path.read_bytes()
        deadline = saved_progress(path).deadline
        assert deadline is not None
        _ = path.write_bytes(b"{")
        restarted = change_page.DeliveryStatus(self.status.root)

        with (patch.object(time, "time", return_value=deadline),
              patch.object(change_page, "send_question") as transport, redirect_stderr(StringIO()) as errors):
            change_page.recover_review_messages(restarted)
            change_page.recover_deferred_questions(restarted)
            self.assertTrue(restarted.has_deferred_questions())
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
            self.status = restarted
            self.assertEqual(self.effective(delivery), delivery_state.QuestionSendState.UNKNOWN)
            _ = path.write_bytes(pending)
            change_page.recover_deferred_questions(restarted)
        transport.assert_not_called()
        self.assertFalse(restarted.has_deferred_questions())
        self.assertEqual(self.effective(delivery), delivery_state.QuestionSendState.FAILED)

    def test_an_unusable_setting_leaves_the_question_saved_with_a_failure(self) -> None:
        _ = conf.path("review_links").write_text("question_recovery_seconds=invalid\n")
        with patch.object(change_page, "send_question") as transport:
            delivery = self.ask()
            change_page.deliver(delivery, self.status)
        transport.assert_not_called()
        self.assertEqual(self.store.questions("image-1")[0].question_id, "Q1")
        self.assertEqual(self.effective(delivery), delivery_state.QuestionSendState.FAILED)
        self.assertFalse(self.status.has_deferred_questions())
        _ = conf.path("review_links").write_text("question_recovery_seconds=1800\n")
        change_page.deliver(self.ask(), self.status)
        self.assertEqual(self.sent, [])

    def test_a_missing_staff_row_fails_without_transport(self) -> None:
        with (patch.object(change_page, "send_question", new=SEND_QUESTION),
              patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(change_page, "question_transport") as transport):
            delivery = self.ask()
            change_page.deliver(delivery, self.status)
        transport.assert_not_called()
        self.assertEqual(self.effective(delivery), delivery_state.QuestionSendState.FAILED)
        self.assertEqual(self.attempt(delivery).error, "No staff record for fixture-owner.")
        self.assertFalse(self.status.has_deferred_questions())

    def test_an_unusable_transport_setting_fails_before_transport(self) -> None:
        member = staff.StaffRow(staff.StaffId("fixture-owner"), "showrunner", "fixture", staff.StaffRoot(),
                                current_session=staff.StaffId("fixture-owner-session"))
        with (patch.object(change_page, "send_question", new=SEND_QUESTION),
              patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((member,))),
              patch.object(send_budget, "seconds", side_effect=conf.Unusable("Invalid delivery timeout.")),
              patch.object(unrouted_alert, "message_outcome") as transport):
            delivery = self.ask()
            change_page.deliver(delivery, self.status)
        transport.assert_not_called()
        self.assertEqual(self.effective(delivery), delivery_state.QuestionSendState.FAILED)
        self.assertEqual(self.attempt(delivery).error, "Invalid delivery timeout.")
        self.assertFalse(self.status.has_deferred_questions())

    def test_page_and_review_questions_share_staff_preflight(self) -> None:
        recipient = staff.StaffId("fixture-owner")
        progress = change_page.QuestionProgress("attempt", "Q1", 0)
        with (patch.object(staff, "snapshot", side_effect=OSError("Cannot read staff.")) as staff_read,
              patch.object(change_page, "send_question", new=SEND_QUESTION),
              patch.object(change_page, "question_transport") as transport):
            page, _ = change_page.attempt_question_send(progress, lambda: change_page.question_send_result(
                *change_page.send_question(recipient, "Which check passed?", "page-key")))
            review, _ = change_page.attempt_question_send(progress, lambda: change_page.send_review_question(
                recipient, "Which check passed?", "review-key"))
        self.assertEqual(staff_read.call_count, 2)
        transport.assert_not_called()
        self.assertEqual(page, review)
        self.assertEqual(page.state, change_page.QuestionDeliveryState.NOT_STARTED)
        self.assertEqual(page.error, "Cannot read staff.")

    def test_the_deadline_check_follows_the_staff_lookup(self) -> None:
        delivery = self.ask()
        path = self.status.path(delivery.page, delivery.row)
        state = saved_progress(path)
        deadline = state.saved_at + change_page.QUESTION_RECOVERY_DEFAULT_SECONDS
        now = deadline - 1
        member = staff.StaffRow(staff.StaffId("fixture-owner"), "showrunner", "fixture", staff.StaffRoot(),
                                current_session=staff.StaffId("fixture-owner-session"))

        def lookup() -> staff.StaffSnapshot:
            nonlocal now
            now = deadline
            return staff.StaffSnapshot((member,))

        with (patch.object(time, "time", side_effect=lambda: now),
              patch.object(staff, "snapshot", side_effect=lookup),
              patch.object(change_page, "send_question", new=SEND_QUESTION),
              patch.object(send_budget, "seconds", return_value=10),
              patch.object(unrouted_alert, "message_outcome") as transport):
            change_page.deliver(delivery, self.status)
            self.assertFalse(self.effective(delivery).waits)
            change_page.recover_deferred_questions(self.status)
        transport.assert_not_called()
        self.assertEqual(self.effective(delivery), delivery_state.QuestionSendState.FAILED)

    def test_unreadable_job_lock_blocks_send_and_expiry_until_readable(self) -> None:
        delivery = self.ask()
        path = self.status.path(delivery.page, delivery.row)
        progress = saved_progress(path)
        deadline = progress.saved_at + change_page.QUESTION_RECOVERY_DEFAULT_SECONDS
        _ = self.job(delivery).release()
        self.status = change_page.DeliveryStatus(self.status.root)

        with (patch.object(change_page, "_question_lock_state", return_value="unreadable") as lock_state,
              patch.object(change_page, "send_question") as transport):
            with patch.object(time, "time", return_value=deadline - 1):
                change_page.recover_review_messages(self.status)
                self.assertTrue(self.status.has_deferred_questions())
            with patch.object(time, "time", return_value=deadline):
                change_page.recover_deferred_questions(self.status)
                self.assertTrue(self.status.has_deferred_questions())
                self.assertEqual(saved_progress(path), progress)
                lock_state.return_value = "released"
                change_page.recover_deferred_questions(self.status)
            self.assertEqual(self.effective(delivery), delivery_state.QuestionSendState.FAILED)
        transport.assert_not_called()
        self.assertFalse(self.status.has_deferred_questions())

    def test_uncertain_delivery_does_not_pause_or_send_again(self) -> None:
        delivery = self.ask()
        path = self.status.path(delivery.page, delivery.row)
        with (patch.object(time, "sleep") as pause,
              patch.object(change_page, "send_question", return_value=question_outcome("pending")) as transport):
            change_page.deliver(delivery, self.status)
            change_page.recover_deferred_questions(self.status)
        transport.assert_called_once()
        pause.assert_not_called()
        self.assertTrue(self.status.has_deferred_questions())
        expire_saved_question(self.status, path)
        self.assertEqual(self.effective(delivery), delivery_state.QuestionSendState.FAILED)

    def test_an_empty_question_does_not_write_or_claim(self) -> None:
        with self.assertRaisesRegex(question_record.AnswerRefused, 'Enter a question first'):
            _ = self.ask(words='  ')
        self.assertEqual(self.store.questions('image-1'), ())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.status.request_attempts, {})

    def test_distinct_open_questions_get_distinct_ids_and_equal_words_reuse_the_claim(self) -> None:
        first = self.ask()
        second = self.ask(words='Another question?')
        repeat = self.ask()
        self.assertEqual([question.question_id for question in self.store.questions('image-1')], ['Q1', 'Q2'])
        self.assertEqual(first.attempt, repeat.attempt)
        self.assertNotEqual(first.attempt, second.attempt)
        self.assertEqual(self.sent, [])
        change_page.deliver(first, self.status)
        change_page.deliver(second, self.status)
        change_page.deliver(repeat, self.status)
        self.assertEqual(len(self.sent), 2)

    def test_an_answer_refuses_unknown_empty_and_already_answered_ids(self) -> None:
        _ = self.ask()
        before = self.store.questions('image-1')
        for identity, words in (('Q2', 'Answer'), ('Q1', '  ')):
            self.assertIsInstance(page_question.answer(self.store, 'image-1', identity, words), page_question.AnswerRefused)
        self.assertEqual(self.store.questions('image-1'), before)
        result = page_question.answer(self.store, 'image-1', 'Q1', '  The gate passed.  ')
        assert isinstance(result, page_question.Answered)
        self.assertEqual(result.question.answer, self.store.questions('image-1')[0].answer)
        self.assertIsInstance(page_question.answer(self.store, 'image-1', 'Q1', 'The gate passed.'),
                              page_question.AnswerRefused)
        self.assertIsInstance(page_question.answer(self.store, 'image-1', 'Q1', 'Changed answer'),
                              page_question.AnswerRefused)
        self.assertEqual(self.store.questions('image-1')[0].answer, result.question.answer)

    def test_a_store_refusal_preserves_the_first_answer_in_a_race(self) -> None:
        _ = self.ask()
        original = self.store.set_answer

        def answer_first(item: str, identity: str, answer: question_record.Answer) -> None:
            original(item, identity, question_record.Answer('First answer', answer.answered_at))
            original(item, identity, answer)

        with patch.object(self.store, 'set_answer', side_effect=answer_first):
            result = page_question.answer(self.store, 'image-1', 'Q1', 'Late answer')
        self.assertIsInstance(result, page_question.AnswerRefused)
        answer = self.store.questions('image-1')[0].answer
        assert answer is not None
        self.assertEqual(answer.text, 'First answer')

    def test_question_and_answer_html_escape_words_and_keep_both_texts(self) -> None:
        _ = self.ask(words='Use <wide> & tall?')
        question = self.store.questions('image-1')[0]
        opened = page_question.open_question_html(question)
        self.assertIn('Use &lt;wide&gt; &amp; tall?', opened)
        self.assertIn('waiting for an answer', opened)
        self.assertNotIn('<time ', opened)
        result = page_question.answer(self.store, 'image-1', 'Q1', '<Yes> & checked')
        assert isinstance(result, page_question.Answered)
        answer = result.question.answer
        assert answer is not None
        rendered = page_question.answered_question_html(result.question, answer)
        self.assertIn('<h3>Answer</h3>', rendered)
        self.assertNotIn('<time ', rendered)
        self.assertIn('Use &lt;wide&gt; &amp; tall?', rendered)
        self.assertIn('&lt;Yes&gt; &amp; checked', rendered)
        self.assertNotIn('waiting for an answer', rendered)
        self.assertEqual(question_record.read(question_record.stored(result.question)), result.question)

    def test_invalid_stored_question_data_is_refused(self) -> None:
        for value in ({}, {'question_id': 'Q0', 'asked_at': 'today', 'words': 'Ask', 'answer': None},
                      {'question_id': 'Q1', 'asked_at': '2026-11-17T12:00:00+00:00', 'words': 'Ask', 'answer': 5}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = question_record.read(value)
