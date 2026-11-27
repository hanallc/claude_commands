"""Verify Nate's List questions and Images answers."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast, override
from unittest import TestCase
from unittest.mock import patch

from scripts.production import behavior_request, delivery_state, staff, the_list
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import change_page, image_store, images


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

    def legacy_approval(self, words: str = APPROVAL) -> tuple[Path, image_store.ImageAnswers]:
        keeper, item, source = self.new_list_item()
        event = {"time": MOMENT.isoformat(), "source": the_list.IMAGE_ANSWER_SOURCE, "note": words,
                 "image_answer": {"key": KEY, "producer": item.producer}}
        with source.open("a", encoding="utf-8") as stream:
            _ = stream.write("\n- " + json.dumps(event) + "\n")
        directory = images.images_root(self.root)
        directory.mkdir()
        answers: image_store.ImageAnswers = {"id": IMAGE, "owner": keeper, "registered_at": MOMENT.isoformat(),
            "list_item": item.id, "list_keeper": keeper, "attempts": {KEY: {
                "message": images.ANSWER_PREFIX + images.ANSWERS["approved"], "words": words,
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

    def test_unsigned_images_approval_verifies_with_its_server_record(self) -> None:
        source, answers = self.legacy_approval()
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {})
        image_store.write_answers(images.images_root(self.root), answers)
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {APPROVAL: MOMENT.date()})

    def test_unsigned_approval_requires_the_same_list_item_and_keeper(self) -> None:
        source, answers = self.legacy_approval()
        directory = images.images_root(self.root)
        wrong_references: tuple[image_store.ImageAnswers, ...] = (
            {**answers, "list_item": "L99"}, {**answers, "list_keeper": "another-keeper"},
        )
        for wrong in wrong_references:
            with self.subTest(wrong=wrong):
                image_store.write_answers(directory, wrong)
                self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {})
        image_store.write_answers(directory, answers)
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {APPROVAL: MOMENT.date()})

    def test_unsigned_approval_requires_the_same_attempt_key_and_full_words(self) -> None:
        words = APPROVAL + "\nKeep the second row."
        source, answers = self.legacy_approval(words)
        directory = images.images_root(self.root)
        attempt = answers["attempts"][KEY]
        wrong_attempts: tuple[dict[str, image_store.ImageAnswer], ...] = (
            {"review:" + "b" * 64: attempt}, {KEY: {**attempt, "words": APPROVAL}},
        )
        for attempts in wrong_attempts:
            with self.subTest(attempts=attempts):
                image_store.write_answers(directory, {**answers, "attempts": attempts})
                self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {})
        image_store.write_answers(directory, answers)
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {APPROVAL: MOMENT.date()})

    def test_images_approval_and_feedback_have_distinct_words(self) -> None:
        feedback = "Keep the second row."
        approval_note = APPROVAL + "\n" + feedback
        source, answers = self.legacy_approval(approval_note)
        directory = images.images_root(self.root)
        attempt = answers["attempts"][KEY]
        feedback_message = images.ANSWER_PREFIX + images.ANSWERS["feedback"]
        image_store.write_answers(directory, {**answers, "attempts": {KEY: {**attempt, "message": feedback_message}}})
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL}), {})
        image_store.write_answers(directory, answers)
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL, feedback, approval_note}),
                         {APPROVAL: MOMENT.date()})

        keeper, item, source = self.new_list_item()
        feedback_line = f"Image {IMAGE}: {images.ANSWERS['feedback']}"
        feedback_note = feedback_line + "\n" + feedback
        _ = the_list.save_image_answer(keeper, item.id, KEY, feedback_note,
                                       image_id=IMAGE, approved=False, signed_at=MOMENT)
        self.assertEqual(behavior_request.verified_dates(source, {APPROVAL, feedback_line, feedback_note, feedback}),
                         {feedback: MOMENT.date()})

    def test_an_images_answer_on_the_list_verifies_as_nates_words(self) -> None:
        keeper = fixture_member("producer", name="morty")
        item = the_list.add(keeper.id, "Check the picture", owner=keeper.id, source="nate", changes={})
        feedback = "Keep the second row."
        moment = datetime(2026, 11, 23, 19, 40, tzinfo=UTC)
        words = f"Image abcdef123456: your image is approved.\n{feedback}"
        _ = the_list.save_image_answer(keeper.id, item.id, "review:" + "a" * 64, words,
                                       image_id="abcdef123456", approved=True, signed_at=moment)
        entry = the_list.load(keeper.id, item.id).log[-1]
        assert isinstance(entry, the_list.ImageAnswerChange)
        self.assertTrue(entry.signed)
        source = the_list.folder(keeper.id) / f"{item.id}.md"
        self.assertEqual(behavior_request.verified_dates(source, {feedback}), {feedback: date(2026, 11, 23)})

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
        folder = images.decision_root(self.root)
        folder.mkdir()
        identity = "abcdef123456"
        source = folder / f"{identity}-{'a' * 64}.json"
        feedback = "Keep the second row."
        fields = {"image_id": identity, "source": "nate via Images", "answer": "approved",
                  "feedback": feedback, "time": "2026-11-23T19:40:00+00:00"}
        _ = source.write_text(json.dumps(fields))
        expected = {quote: date(2026, 11, 23) for quote in ("approved", feedback)}
        self.assertEqual(behavior_request.verified_dates(source, set(expected)), expected)
        question = "Should we remove the second row?"
        fields["image_text"] = question
        _ = source.write_text(json.dumps(fields))
        quotes = {*expected, question}
        self.assertEqual(behavior_request.verified_dates(source, quotes), expected)
        copy = self.root / source.name
        _ = copy.write_bytes(source.read_bytes())
        self.assertEqual(behavior_request.verified_dates(copy, quotes), {})
        _ = source.write_text(json.dumps({**fields, "source": "producer"}))
        self.assertEqual(behavior_request.verified_dates(source, quotes), {})
