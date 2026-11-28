"""Verify Nate's List questions and Images answers."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from http.client import HTTPConnection
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import cast, override
from unittest import TestCase
from urllib.parse import urlencode
from unittest.mock import Mock, patch

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message import send_result
from scripts.production import behavior_request, delivery_state, staff, the_list
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import change_page, image_store, images, list_page, send_hold, serve
from scripts.review_links.server_test_support import started_review_server
from scripts.review_links.test_page_question import question_outcome


IMAGE = "5c6c129422b8"
KEY = "review:" + "a" * 64
APPROVAL = f"Image {IMAGE}: {images.ANSWERS['approved']}"
MOMENT = datetime(2026, 11, 27, 17, 18, tzinfo=UTC)


class BehaviorRequestImagesTests(TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "STAFF_FILE": str(self.root / "staff.json"),
            "REVIEW_LINKS_ROOT": str(self.root),
        })))

    def new_list_item(self) -> tuple[staff.StaffId, the_list.ListItem, Path]:
        keeper = fixture_member("producer", name="morty").id
        item = the_list.add(keeper, "Check the picture", owner=keeper, source="nate", changes={})
        return keeper, item, the_list.folder(keeper) / f"{item.id}.md"

    def legacy_approval(self) -> tuple[Path, image_store.ImageAnswers]:
        keeper, item, source = self.new_list_item()
        directory = images.images_root(self.root)
        key = change_page.review_delivery_key(directory / IMAGE / image_store.RECORD, IMAGE,
                                              json.dumps([MOMENT.isoformat(), "approved", ""]))
        event = {"time": MOMENT.isoformat(), "source": the_list.IMAGE_ANSWER_SOURCE, "note": APPROVAL,
                 "image_answer": {"key": key, "producer": item.producer}}
        with source.open("a", encoding="utf-8") as stream:
            _ = stream.write("\n- " + json.dumps(event) + "\n")
        directory.mkdir()
        answers: image_store.ImageAnswers = {"id": IMAGE, "owner": keeper, "registered_at": MOMENT.isoformat(),
            "list_item": item.id, "list_keeper": keeper, "attempts": {key: {
                "message": images.ANSWER_PREFIX + images.ANSWERS["approved"], "words": APPROVAL,
                "answered": [], "recipients": [keeper], "confirmed": [keeper],
            }}}
        return source, answers

    def test_list_question_requires_a_matching_sent_delivery(self) -> None:
        keeper, item, source = self.new_list_item()
        words = "Can you keep the second row?"
        status = change_page.DeliveryStatus(self.root)
        with patch.object(the_list, "now", return_value=MOMENT):
            delivery = the_list.ask_question(keeper, item.id, the_list.PageRequest("ask-test", "question", words), status)
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        self.addCleanup(delivery.job.release)
        self.assertEqual(behavior_request.verified_dates(source, {words}), {})
        attempt = delivery.job.record
        path = status.path(delivery.page, delivery.row)
        sent = replace(attempt, state=delivery_state.QuestionSendState.SENT,
                       owner=delivery_state.NoQuestionSendOwner(), receipt=delivery_state.QuestionSendReceipt("sent", ""))
        change_page.publish_question_attempt(path, replace(sent, state=delivery_state.QuestionSendState.QUEUED,
                                                          receipt=delivery_state.QuestionSendReceipt("queued", "")))
        self.assertEqual(behavior_request.verified_dates(source, {words}), {})
        change_page.publish_question_attempt(path, replace(sent, message=sent.message.replace(words, "A different question?")))
        self.assertEqual(behavior_request.verified_dates(source, {words}), {})
        change_page.publish_question_attempt(path, sent)
        self.assertEqual(behavior_request.verified_dates(source, {words}), {words: MOMENT.date()})

    def signed_answer(self, keeper: staff.StaffId, *, item: str = "", action: str = "approved",
                      feedback: str = "", request_id: str = "a" * 32) -> image_store.ImageAnswers:
        directory = images.images_root(self.root)
        folder = directory / IMAGE
        folder.mkdir(parents=True)
        record: image_store.ImageRecord = {"id": IMAGE, "owner": keeper, "registered_at": MOMENT.isoformat(),
            "file": "image.png", "subject": "The picture", "title": "Check the picture", "look_for": []}
        if item:
            record["list_item"] = item
            record["list_keeper"] = keeper
        image_store.write_record(folder, record)
        fields = {"image": IMAGE, "token": images.request_token(images.console_token(self.root), IMAGE),
                  "action": action, "feedback": feedback}
        if request_id:
            fields["answer_request"] = request_id
        with (patch.object(images, "datetime", wraps=datetime, now=Mock(return_value=MOMENT)),
              patch.object(change_page, "send_with_retries", return_value=[])):
            self.assertIsInstance(images.submit(self.root, urlencode(fields), change_page.DeliveryStatus(self.root)),
                                  images.ImageAnswerSent)
        return image_store.read_answers(directory, IMAGE)

    def test_old_direct_sender_records_cannot_verify_an_approval(self) -> None:
        source, answers = self.legacy_approval()
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {})
        image_store.write_answers(images.images_root(self.root), answers)
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {})

    def test_images_requires_a_complete_attempt_with_recipients(self) -> None:
        keeper, item, source = self.new_list_item()
        answers = self.signed_answer(keeper, item=item.id)
        directory = images.images_root(self.root)
        key, attempt = next(iter(answers["attempts"].items()))
        incomplete: tuple[image_store.ImageAnswer, ...] = (
            {**attempt, "recipients": [], "confirmed": []}, {**attempt, "confirmed": []},
        )
        for saved in incomplete:
            with self.subTest(saved=saved):
                image_store.write_answers(directory, {**answers, "attempts": {key: saved}})
                self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {})
        image_store.write_answers(directory, answers)
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {APPROVAL: MOMENT.date()})

    def test_signed_list_answer_requires_the_same_item_keeper_key_and_words(self) -> None:
        keeper, item, source = self.new_list_item()
        feedback = "Keep the second row."
        answers = self.signed_answer(keeper, item=item.id, feedback=feedback)
        directory = images.images_root(self.root)
        key, attempt = next(iter(answers["attempts"].items()))
        signed = attempt.get("signed_answer")
        assert signed is not None
        cases: dict[str, image_store.ImageAnswers] = {
            "item": {**answers, "list_item": "L99"},
            "keeper": {**answers, "list_keeper": "another-keeper"},
            "key": {**answers, "attempts": {KEY: attempt}},
            "words": {**answers, "attempts": {key: {**attempt, "words": APPROVAL}}},
            "signed words": {**answers, "attempts": {key: {**attempt, "signed_answer": {**signed, "feedback": "Change the title."}}}},
            "message": {**answers, "attempts": {key: {**attempt, "message": images.ANSWER_PREFIX + images.ANSWERS["feedback"]}}},
        }
        for part, wrong in cases.items():
            with self.subTest(part=part):
                image_store.write_answers(directory, wrong)
                self.assertEqual(behavior_request.verified_dates(source, {APPROVAL, feedback}), {})
        image_store.write_answers(directory, answers)
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL, feedback}),
                         {APPROVAL: MOMENT.date(), feedback: MOMENT.date()})
        entry = the_list.load(keeper, item.id).log[-1]
        assert isinstance(entry, the_list.ImageAnswerChange)
        self.assertNotEqual(entry.key, key)
        _ = source.write_text(source.read_text().replace(entry.key, key))
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL, feedback}), {})

    def test_list_question_rejects_an_inferred_legacy_send(self) -> None:
        keeper, item, source = self.new_list_item()
        words = "Can you keep the second row?"
        status = change_page.DeliveryStatus(self.root)
        delivery = the_list.ask_question(keeper, item.id, the_list.PageRequest("ask-test", "question", words), status)
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        _ = delivery.job.release()
        record = delivery.job.record
        path = status.path(delivery.page, delivery.row)
        _ = path.write_text(json.dumps({"review_message": {"source": record.source.path,
            "owner": keeper, "row": record.source.row, "saved": True, "message": record.message}}))
        current = the_list.load(keeper, item.id)
        saved = the_list.list_question_target(keeper, current, keeper).saved(current.questions[0])
        self.assertIs(change_page.published_question_delivery_view(status, saved).state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(behavior_request.verified_dates(source, {words}), {})

    def test_list_browser_question_verifies_only_after_the_send_receipt(self) -> None:
        keeper, item, source = self.new_list_item()
        words = "Can you keep the second row?"
        quotes = {words}
        seen_during_send: list[tuple[delivery_state.QuestionSendState, bool]] = []

        def send(_recipient: staff.StaffId, _message: str, _message_id: str
                 ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            current = the_list.load(keeper, item.id)
            saved = the_list.list_question_target(keeper, current, keeper).saved(current.questions[0])
            record = change_page.read_question_attempt(server.delivery_status.path(saved.page(), saved.source.row))
            seen_during_send.append((record.state, bool(behavior_request.verified_dates(source, quotes))))
            return question_outcome()

        with (patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(the_list, "now", return_value=MOMENT),
              patch.object(change_page, "send_question", side_effect=send) as transport,
              started_review_server(("127.0.0.1", 0), self.root) as server):
            request, token = list_page.signed_item_request(list_page.ListRecord(keeper, item), list_page.form_secret(self.root))
            fields = {"keeper": keeper, "id": item.id, "request": request, "action": "question", "words": words, "until": ""}
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=TEST_HANG_GUARD_SECS)) as connection:
                    headers = {"Content-Type": "application/x-www-form-urlencoded", send_hold.REPLAY_HEADER: "1"}
                    for form_token, expected in (("bad", 400), (token, 200)):
                        connection.request("POST", list_page.PAGE_PATH, urlencode({**fields, "token": form_token}), headers)
                        with connection.getresponse() as response:
                            self.assertEqual(response.status, expected, response.read())
                transport.assert_called_once()
                current = the_list.load(keeper, item.id)
                saved = the_list.list_question_target(keeper, current, keeper).saved(current.questions[0])
                path = server.delivery_status.path(saved.page(), saved.source.row)
            finally:
                server.shutdown()
                thread.join(TEST_HANG_GUARD_SECS)
        self.assertEqual(seen_during_send, [(delivery_state.QuestionSendState.SENDING, False)])
        self.assertEqual(behavior_request.verified_dates(source, quotes), {words: MOMENT.date()})
        record = change_page.read_question_attempt(path)
        self.assertEqual(record.receipt, delivery_state.QuestionSendReceipt("sent", "fixture receipt"))
        legacy = {"source": record.source.path, "owner": keeper, "row": record.source.row, "saved": True,
                  "message": record.message, "image_question_dispatch": {"attempt": record.attempt_id, "state": "accepted"}}
        _ = path.write_text(json.dumps({"review_message": legacy}))
        self.assertEqual(behavior_request.verified_dates(source, quotes), {})

    def test_images_approval_and_feedback_have_distinct_words(self) -> None:
        keeper, item, source = self.new_list_item()
        feedback = "Keep the second row."
        _ = self.signed_answer(keeper, item=item.id, feedback=feedback)
        approval_note = APPROVAL + "\n" + feedback
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL, feedback, approval_note}),
                         {APPROVAL: MOMENT.date(), feedback: MOMENT.date()})

    def test_images_requires_all_delivered_feedback_to_match(self) -> None:
        keeper, item, source = self.new_list_item()
        feedback = "Keep the second row.\nKeep the third row."
        answers = self.signed_answer(keeper, item=item.id, feedback=feedback)
        key, attempt = next(iter(answers["attempts"].items()))
        directory = images.images_root(self.root)
        quotes = {APPROVAL, "Keep the third row."}
        expected = {quote: MOMENT.date() for quote in quotes}
        self.assertEqual(behavior_request.verified_dates(source, quotes), expected)
        _, _, details = attempt["message"].partition(feedback)
        for changed in ("Keep the second row.", "Keep the second row.\nRemove the third row.",
                        feedback + "\nRemove the last row.", feedback + details):
            with self.subTest(changed=changed):
                wrong: image_store.ImageAnswer = {**attempt, "message": attempt["message"].replace(feedback, changed, 1)}
                image_store.write_answers(directory, {**answers, "attempts": {key: wrong}})
                self.assertEqual(behavior_request.verified_dates(source, quotes), {})

    def test_signed_images_feedback_on_the_list_verifies_as_nates_words(self) -> None:
        keeper, item, source = self.new_list_item()
        feedback = "Keep the second row."
        feedback_line = f"Image {IMAGE}: {images.ANSWERS['feedback']}"
        _ = self.signed_answer(keeper, item=item.id, action="feedback", feedback=feedback, request_id="")
        self.assertEqual(behavior_request.verified_dates(source, {feedback_line, feedback_line + "\n" + feedback, feedback}),
                         {feedback: MOMENT.date()})

    def test_a_list_answer_without_the_signed_mark_cannot_verify(self) -> None:
        keeper = fixture_member("producer", name="morty")
        item = the_list.add(keeper.id, "Check the picture", owner=keeper.id, source="nate", changes={})
        source = the_list.folder(keeper.id) / f"{item.id}.md"
        title = "Title: Remove the red row."
        words = "From the user (via the Images page): your image is approved.\n" + title
        legacy_entry = {"time": "2026-11-23T19:40:00+00:00", "source": "nate via Images", "note": words,
                        "image_answer": {"key": "review:" + "a" * 64, "producer": item.producer}}
        with source.open("a", encoding="utf-8") as file:
            _ = file.write("\n- " + json.dumps(legacy_entry) + "\n")
        entry = the_list.load(keeper.id, item.id).log[-1]
        assert isinstance(entry, the_list.ImageAnswerChange)
        self.assertIn(title, entry.what)
        self.assertEqual(behavior_request.verified_dates(source, {title}), {})

    def test_only_a_stored_images_answer_with_the_right_source_verifies(self) -> None:
        keeper = fixture_member("producer", name="morty").id
        feedback = "Keep the second row."
        answers = self.signed_answer(keeper, feedback=feedback)
        source, = images.decision_root(self.root).glob(f"{IMAGE}-*.json")
        expected = {quote: MOMENT.date() for quote in ("approved", feedback)}
        directory = images.images_root(self.root)
        key, attempt = next(iter(answers["attempts"].items()))
        image_store.write_answers(directory, {**answers, "attempts": {key: {**attempt, "confirmed": []}}})
        self.assertEqual(behavior_request.verified_dates(source, set(expected)), {})
        image_store.write_answers(directory, answers)
        self.assertEqual(behavior_request.verified_dates(source, set(expected)), expected)
        question = "Should we remove the second row?"
        fields = delivery_state.read_fields(source)
        _ = source.write_text(json.dumps({**fields, "image_text": question}))
        quotes = {*expected, question}
        self.assertEqual(behavior_request.verified_dates(source, quotes), expected)
        wrong_key = source.with_name(f"{IMAGE}-{'b' * 64}.json")
        _ = wrong_key.write_bytes(source.read_bytes())
        self.assertEqual(behavior_request.verified_dates(wrong_key, quotes), {})
        copy = self.root / source.name
        _ = copy.write_bytes(source.read_bytes())
        self.assertEqual(behavior_request.verified_dates(copy, quotes), {})
        _ = source.write_text(json.dumps({**fields, "source": "producer"}))
        self.assertEqual(behavior_request.verified_dates(source, quotes), {})
