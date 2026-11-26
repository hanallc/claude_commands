"""Test the read-only inventory with native record fixtures."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import investigation, record_store, staff, the_list, update_registration
from scripts.production.issues import Issue
from scripts.review_links import answer_sources, image_store, images, new_item_dialog, question_record, review_items
from scripts.review_links.review_events import ReviewLog, encode, rows


def member(number: int, kind: staff.Kind, name: str) -> staff.StaffRow:
    identity = staff.StaffId(f"00000000-0000-4000-8000-{number:012x}")
    return staff.StaffRow(identity, kind, name, staff.StaffRoot(), current_session=identity)


ASKER = member(1, "showrunner", "question-asker")
OWNER = member(2, "producer", "question-owner")
KEEPER = member(3, "producer", "morty")
MEMBERS = staff.StaffSnapshot((ASKER, OWNER, KEEPER))
START = datetime(2026, 11, 24, 13, tzinfo=UTC)
FILED = START.strftime("%Y-%m-%dT%H:%M")
FIRST_REPLY = START + timedelta(hours=1)
READ = FIRST_REPLY + timedelta(hours=1)
SECOND_ASK = READ + timedelta(hours=1)
SECOND_REPLY = SECOND_ASK + timedelta(hours=1)


def origin(request: str) -> new_item_dialog.ReviewSubmission:
    return new_item_dialog.ReviewSubmission(request, "1" * 64, new_item_dialog.NoEvidence(), (), "2" * 64)


def answered_question(identity: str, asked: datetime, answered: datetime) -> question_record.Question:
    return question_record.Question(identity, asked.isoformat(), f"Question {identity}?",
                                    question_record.Answer(f"Answer {identity}.", answered.isoformat()), OWNER.id)


class AnswerSourceTests(unittest.TestCase):
    root: Path = Path()
    state: Path = Path()
    review: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "state"
        self.review = self.root / "review"
        config = self.root / "config"
        config.mkdir()
        for name in ("production", "review_links", "the_list"):
            _ = write_config_fixture(config / f"{name}.conf")
        _ = (config / "console.conf").write_text(f"page.list.owner={KEEPER.id}\n", encoding="utf-8")
        staff_file = self.root / "staff.json"
        _ = staff_file.write_text(json.dumps({"schema": 1, "rows": [row.to_json() for row in MEMBERS.rows]}),
                                  encoding="utf-8")
        self.enterContext(patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.state),
            "REVIEW_LINKS_ROOT": str(self.review), "USER_ZONE": "UTC", "CONF_DIR": str(config),
            "STAFF_FILE": str(staff_file)}))

    def native(self, name: str, fields: update_registration.OutstandingItem, *,
               asker: staff.StaffId = ASKER.id, status: str = "open") -> str:
        path = record_store.default_question_folder(OWNER.id) / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        registration = {**fields, "asked_by": asker}
        _ = path.write_text("\n".join((
            "---", "record_type: question", "tags: [question]", f"status: {status}",
            f"production_id: {OWNER.id}", f"asked_by: {asker}",
            "registration: " + json.dumps(registration), "---", "", "# Fixture", "",
        )), encoding="utf-8")
        return f"question/{OWNER.id}/{path.name}"

    def picture(self, identity: str, **link: str) -> None:
        folder = images.images_root(self.review) / identity
        folder.mkdir(parents=True)
        questions = (answered_question("Q1", START, FIRST_REPLY), answered_question("Q2", SECOND_ASK, SECOND_REPLY),
                     question_record.Question("Q3", SECOND_ASK.isoformat(), "Pending picture question."))
        record: image_store.ImageRecord = {
            "id": identity, "owner": OWNER.id, "registered_at": START.isoformat(), "title": "Fixture picture",
            "subject": "", "look_for": [], "file": "fixture.png",
            "questions": [question_record.stored(question) for question in questions],
        }
        if "list_item" in link:
            record["list_item"] = link["list_item"]
        if "list_keeper" in link:
            record["list_keeper"] = link["list_keeper"]
        image_store.write_record(folder, record)
        attempts = image_store.new_answers(record)
        attempts["attempts"]["sent"] = {"message": "Picture feedback", "answered": ["Q1"],
                                          "recipients": [OWNER.id], "confirmed": [OWNER.id]}
        attempts["attempts"]["pending"] = {"message": "More feedback", "answered": ["Q2"],
                                              "recipients": [OWNER.id], "confirmed": []}
        image_store.write_answers(folder.parent, attempts)

    def test_a_list_item_keeps_both_directions_and_excludes_go_and_approval(self) -> None:
        native = self.native("staff", {"since": FILED, "text": "Select a plan", "question": "Which plan?"})
        questions = (answered_question("Q1", START, FIRST_REPLY), answered_question("Q2", SECOND_ASK, SECOND_REPLY),
                     question_record.Question("Q3", SECOND_ASK.isoformat(), "Pending List question."))
        added = the_list.FieldChange("Added", {"title": "Review the plan", "owner": OWNER.id, "producer": KEEPER.id,
                                              "state": the_list.ItemState.WORKING, "eta": the_list.EtaStatus.NOT_ASKED,
                                              "next_step": "", "latest_status": ""})
        log = (the_list.ItemChange(START, added, "nate"),
               the_list.ItemChange(FIRST_REPLY, the_list.QuestionAnswered("Q1"), OWNER.name),
               the_list.ItemChange(READ, the_list.MarkedRead(), the_list.PAGE_SOURCE),
               the_list.ItemChange(SECOND_REPLY, the_list.QuestionAnswered("Q2"), KEEPER.name))
        asks = (the_list.Ask("A1", the_list.AskKind.GO, "Give the go.", "Ready.", OWNER.id, False, START),
                the_list.Ask("A2", the_list.AskKind.APPROVAL, "Approve it.", "Ready.", OWNER.id, False, START))
        item = the_list.ListItem(
            number=7, title="Review the plan", owner=OWNER.id, producer=KEEPER.id, unit="",
            state=the_list.ItemState.WORKING, next_check=the_list.NoTimestamp.UNSET, eta=the_list.EtaStatus.NOT_ASKED,
            unread_since=SECOND_REPLY, parked_until=the_list.NoTimestamp.UNSET, blocked_by=(), related_to=(),
            requested_by=the_list.RequestedBy.NATE, opened=START, updated=SECOND_REPLY,
            closed=the_list.NoTimestamp.UNSET, closing_words="", next_step="", latest_status="", evidence="",
            done_when="", log=log, reviews=(native,), questions=questions, asks=asks,
        )
        the_list.create_records(KEEPER.id, (item,))
        self.assertEqual(the_list.load(KEEPER.id, "L7").questions, questions)
        result = answer_sources.read_exchanges(self.review, members=MEMBERS)
        inline_read, inline_unread, inbound = result.exchanges
        destination = investigation.ListDestination(KEEPER.id, 7)
        self.assertEqual(inbound, answer_sources.Exchange(answer_sources.Source.STAFF_QUESTION, native, "initial",
            answer_sources.Direction.QUESTION_TO_NATE, "Which plan?", START, ASKER.id, answer_sources.Nate.USER,
            answer_sources.QuestionWithoutAnswer.OPEN, (destination,)))
        self.assertEqual(inline_read, answer_sources.Exchange(answer_sources.Source.LIST, f"list/{KEEPER.id}/L7", "Q1",
            answer_sources.Direction.ANSWER_TO_NATE, "Question Q1?", START, answer_sources.Nate.USER, OWNER.id,
            answer_sources.ExchangeAnswer("Answer Q1.", FIRST_REPLY, answer_sources.AnswerRead.READ), (destination,)))
        self.assertEqual((inline_unread.exchange_id, inline_unread.answerer, inline_unread.answer), ("Q2", KEEPER.id,
                         answer_sources.ExchangeAnswer("Answer Q2.", SECOND_REPLY, answer_sources.AnswerRead.UNREAD)))

    def test_staff_followups_share_the_source_key_and_keep_their_read_state(self) -> None:
        log = (ReviewLog(SECOND_ASK, "nate", "question: How long?"),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: Ten minutes."))
        key = self.native("legacy", {
            "since": FILED, "text": "Choose a route", "question": "Which route?",
            "followups": [{"followup": "Which side?", "reply": "The north side."},
                          {"followup": "How long?", "reply": "Ten minutes.", "question_id": "Q1"}],
            "reply_log_positions": {"1": 1}, "review_log": encode(log),
        })
        exchanges = answer_sources.read_exchanges(self.review, members=MEMBERS).exchanges
        first, second, original = exchanges
        self.assertEqual([exchange.source_key for exchange in exchanges], [key] * 3)
        self.assertEqual((first.exchange_id, first.asked_at, first.answer),
                         ("followup:1", answer_sources.TimeNotRecorded.ABSENT,
                          answer_sources.ExchangeAnswer("The north side.", answer_sources.TimeNotRecorded.ABSENT,
                                                        answer_sources.AnswerRead.UNKNOWN)))
        self.assertEqual((second.direction, second.asked_at, second.answer),
                         (answer_sources.Direction.ANSWER_TO_NATE, SECOND_ASK,
                          answer_sources.ExchangeAnswer("Ten minutes.", SECOND_REPLY, answer_sources.AnswerRead.UNREAD)))
        self.assertEqual(original.direction, answer_sources.Direction.QUESTION_TO_NATE)

    def test_sent_question_keeps_all_replies_even_while_a_followup_waits(self) -> None:
        log = (ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The first answer."),
               ReviewLog(READ, "nate", "Marked read"),
               ReviewLog(SECOND_ASK, "nate", "question: Then what?"),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: The second answer."))
        _ = self.native("sent", {"since": FILED, "text": "Ask for steps", "question": "What first?", "review_id": "Q3",
            "review_submission": new_item_dialog.stored_submission(origin("request-1234567890")),
            "followup": {"text": "What next?", "sent": "2026-11-24T19:00"},
            "followups": [{"followup": "What first?", "reply": "The first answer."},
                          {"followup": "Then what?", "reply": "The second answer."}],
            "reply_log_positions": {"0": 0, "1": 3}, "review_log": encode(log),
        }, asker=OWNER.id)
        first, second = answer_sources.read_exchanges(self.review, members=MEMBERS).exchanges
        self.assertEqual(first.source_key, "submission/question/request-1234567890")
        self.assertEqual(second.source_key, first.source_key)
        self.assertEqual((first.asked_at, first.answer), (START,
                         answer_sources.ExchangeAnswer("The first answer.", FIRST_REPLY, answer_sources.AnswerRead.READ)))
        self.assertEqual((second.asked_at, second.answer), (SECOND_ASK,
                         answer_sources.ExchangeAnswer("The second answer.", SECOND_REPLY, answer_sources.AnswerRead.UNREAD)))

    def test_scoped_proposal_reply_requires_an_explicit_question(self) -> None:
        log = (ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The verdict was applied."),
               ReviewLog(SECOND_ASK, "nate", "question: What changed?"),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: The filter moved."))
        issue = Issue(self.review / "proposal.md", {
            "review_submission": [json.dumps(new_item_dialog.stored_submission(origin("request-9876543210")))],
            "review_log": [json.dumps(row) for row in rows(log)],
        })
        proposal = review_items.ReviewItem("proposal/fixture", "owner/P1", review_items.ReviewType.PROPOSAL,
            OWNER.id, OWNER.id, "Move the filter", review_items.ReviewState.OPEN,
            review_items.ReviewTimes(START, SECOND_REPLY), log, (), review_items.ProposalSource(issue))
        with patch.object(review_items, "read", return_value=[proposal]):
            reply, = answer_sources.read_exchanges(self.review, members=MEMBERS).exchanges
        self.assertEqual((reply.source, reply.question, reply.asked_at, reply.answer),
                         (answer_sources.Source.SCOPED_REPLY, "What changed?", SECOND_ASK,
                          answer_sources.ExchangeAnswer("The filter moved.", SECOND_REPLY, answer_sources.AnswerRead.UNREAD)))

    def test_count_reports_bad_links_and_keeps_repeat_keys_without_writes(self) -> None:
        self.picture("000000000001", list_item="L29", list_keeper=KEEPER.id)
        self.picture("000000000002")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        first = answer_sources.read_exchanges(self.review, members=MEMBERS)
        second = answer_sources.read_exchanges(self.review, members=MEMBERS)
        self.assertEqual(first, second)
        self.assertEqual([(exchange.source_key, exchange.exchange_id) for exchange in first.exchanges],
                         [("image/000000000001", "Q1"), ("image/000000000001", "Q2"),
                          ("image/000000000002", "Q1"), ("image/000000000002", "Q2")])
        out = io.StringIO()
        with patch.object(sys, "argv", ["answer_sources", "count"]), redirect_stdout(out):
            self.assertEqual(answer_sources.main(), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "staff-question: records=0 exchanges=0 unread=0 read=0 linked=0 new-rows=0",
            "sent-question/scoped-reply: records=0 exchanges=0 unread=0 read=0 linked=0 new-rows=0",
            "list: records=0 exchanges=0 unread=0 read=0 linked=0 new-rows=0",
            "picture: records=2 exchanges=4 unread=2 read=2 linked=1 new-rows=1",
            f"  Bad List link: image/000000000001 -> {KEEPER.id}/L29 (item missing)",
        ])
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
