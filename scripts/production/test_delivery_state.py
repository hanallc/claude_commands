"""Check durable record decoding for waiting counts and delivery recovery."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.production import delivery_state, staff


class DeliveryStateTests(unittest.TestCase):
    def test_legacy_page_question_evidence_keeps_acceptance_and_uncertainty(self) -> None:
        state = delivery_state.QuestionSendState
        recipient = staff.StaffId("morty")
        source = delivery_state.QuestionSource(str(Path("/fixture/L1.md")), "L1", "Q1", "Why?", "List item 1/Q1")
        saved = {"source": source.path, "owner": recipient, "row": source.row, "saved": True, "message": "Why?"}
        accepted = {**saved, "image_question_dispatch": {"attempt": "new", "state": "accepted"}}
        pending = {"attempt": "new", "state": "pending", "recipients": [recipient]}
        failure = {"review_list": source.path, "row": source.row, "attempt": "new", "last_error": "offline"}
        cases: tuple[tuple[dict[str, object], dict[str, object] | None, bool, delivery_state.QuestionSendState], ...] = (
            ({"review_message": saved}, {}, False, state.SENT),
            ({"review_message": accepted}, {"failed_delivery": {**failure, "attempt": "old"}}, False, state.SENT),
            ({"review_message": saved, "verdict": pending}, {}, True, state.CLAIMED),
            ({"review_message": saved, "verdict": pending}, {}, False, state.UNKNOWN),
            ({"review_message": saved, "verdict": pending}, {"failed_delivery": failure}, True, state.FAILED),
            ({"review_message": saved}, {"failed_delivery": failure}, False, state.FAILED),
            ({}, {}, False, state.UNKNOWN),
            ({"review_message": {**saved, "image_question_dispatch": {}}}, {}, False, state.UNKNOWN),
            ({"review_message": saved, "verdict": "damaged"}, {}, False, state.UNKNOWN),
            ({"review_message": saved}, None, False, state.UNKNOWN),
            ({"review_message": accepted, "verdict": pending}, {}, False, state.SENT),
            ({"review_message": accepted, "verdict": {**pending, "attempt": "other"}}, {}, False, state.UNKNOWN),
        )
        for fields, closeout, live, expected in cases:
            with self.subTest(fields=fields, closeout=closeout, live=live):
                self.assertEqual(delivery_state.legacy_question_state(
                    fields, closeout, lambda attempt: live and attempt == "new", source, recipient,
                    lambda message: message == "Why?"), expected)

    def test_a_legacy_fault_has_no_message_to_deliver(self) -> None:
        slots = delivery_state.decode(json.dumps({"attempt": "old", "state": "failed", "kind": "verdict"}))
        intent = slots.verdict
        self.assertEqual(intent, delivery_state.NoDelivery())

    def test_a_sent_validation_transfers_ownership_without_an_attempt_to_retry(self) -> None:
        slots = delivery_state.decode(json.dumps({"validation": {"attempt": "sent", "state": "sent", "feedback": "Check it"}}))
        self.assertEqual(slots.validation, delivery_state.ValidationRequested("sent", "Check it"))
        self.assertEqual(slots.intents(), ())

    def test_question_delivery_and_answer_records_preserve_identity_and_ownership(self) -> None:
        for record in ({"delivery": None},
                       {"delivery": {"attempt": "a", "state": "pending", "recipients": ["staff-id"]}},
                       {"delivery": {"attempt": "a", "state": "failed", "failure_note": "no showrunner to send to"}}):
            with self.subTest(record=record):
                slots = delivery_state.decode(json.dumps({"question": {"question_id": "q1", "text": "Why?", **record}}))
                question = slots.question
                assert isinstance(question, delivery_state.OpenQuestion)
                self.assertEqual((question.question_id, question.text), ("q1", "Why?"))
        answered = delivery_state.decode(json.dumps({"question": {"question_id": "q1", "text": "Why?", "answer": "Because"}}))
        self.assertEqual(answered.question, delivery_state.AnsweredQuestion("q1", "Why?", "Because"))
        self.assertEqual(answered.intents(), ())

    def test_corrupt_records_do_not_create_a_delivery_attempt(self) -> None:
        slots = delivery_state.decode("not json")
        self.assertEqual(slots.verdict, delivery_state.NoDelivery())

    def test_a_saved_verdict_selects_its_own_recipients_before_an_open_question(self) -> None:
        slots = delivery_state.decode(json.dumps({"verdict": {"attempt": "v", "state": "pending", "recipients": ["unit", "runner"]},
            "question": {"question_id": "q1", "text": "Why?", "delivery": {"attempt": "q", "state": "pending"}}}))
        self.assertEqual(slots.verdict, delivery_state.DeliveryIntent("v", "verdict", "",
                         (staff.StaffId("unit"), staff.StaffId("runner"))))
        assert isinstance(slots.question, delivery_state.OpenQuestion)
        self.assertEqual(slots.question.delivery, delivery_state.DeliveryIntent("q", "question"))
