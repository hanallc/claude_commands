"""Test the read-only inventory with native record fixtures."""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, current_thread
from typing import override
from unittest.mock import patch

from scripts.console_text import check as console_text
from scripts.lib import atomic_file
from scripts.lib.user_zone import user_zone
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import (
    change_review, change_routing, delivery_state, investigation, list_exchange, record_store, staff, the_list,
    unrouted_alert, update_registration,
)
from scripts.production.issues import Issue
from scripts.review_links import (
    answer_sources, change_browser, change_page, image_store, images, new_item_dialog, question_path, question_record,
    reply_tokens, review_items, review_page, staff_names,
)
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
        for name in ("production", "review_links", "the_list", "statusline"):
            _ = write_config_fixture(config / f"{name}.conf")
        (config / "producer_map.conf").touch()
        _ = (config / "change_review.conf").write_text(f"lists_directory={self.root / 'changes'}\n")
        _ = write_config_fixture(config / "console.conf", f"page.list.owner={KEEPER.id}\n")
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
            answer_sources.QuestionWithoutAnswer.OPEN, (destination,),
            binding=list_exchange.ExchangeBinding(list_exchange.QuestionRecord("initial"), list_exchange.ReplyWithoutEvent.NO_ANSWER)))
        self.assertEqual(inline_read, answer_sources.Exchange(answer_sources.Source.LIST, f"list/{KEEPER.id}/L7", "Q1",
            answer_sources.Direction.ANSWER_TO_NATE, "Question Q1?", START, answer_sources.Nate.USER, OWNER.id,
            answer_sources.ExchangeAnswer("Answer Q1.", FIRST_REPLY, answer_sources.AnswerRead.READ), (destination,),
            binding=list_exchange.ExchangeBinding(list_exchange.QuestionRecord("Q1"), list_exchange.ReplyWithoutEvent.NATIVE_RECORD)))
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

    def migrate(self, *, apply: bool) -> tuple[int, str]:
        out = io.StringIO()
        args = ["answer_sources", "migrate", *(["--apply"] if apply else [])]
        with patch.object(sys, "argv", args), redirect_stdout(out):
            result = answer_sources.main()
        return result, out.getvalue()

    def test_migrate_creates_one_row_and_keeps_every_followup(self) -> None:
        key = self.native("open", {"since": FILED, "text": "Pick a route", "question": "Which route?",
            "followups": [{"followup": "How long?", "reply": "Ten minutes."},
                          {"followup": "What next?", "reply": "Wait here."}],
        })
        _ = self.native("closed", {"since": FILED, "text": "No longer needed", "question": "Still needed?"},
                        status="closed")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result, dry = self.migrate(apply=False)
        self.assertEqual(result, 0)
        self.assertIn("staff-question: sources=1 exchanges=3 linked=0 new-rows=1 unchanged=0", dry)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        result, applied = self.migrate(apply=True)
        self.assertEqual(result, 0)
        self.assertIn(f"{key} -> {KEEPER.id}/L1", applied)
        item, = the_list.records(KEEPER.id)
        self.assertEqual(item.owner, ASKER.id)
        self.assertIs(type(item.filed_exchanges[0]), list_exchange.FiledExchange)
        self.assertEqual({exchange.source_key for exchange in item.filed_exchanges}, {key})
        self.assertEqual({exchange.exchange_id for exchange in item.filed_exchanges},
                         {"initial", "followup:1", "followup:2"})
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result, repeated = self.migrate(apply=True)
        self.assertEqual(result, 0)
        self.assertIn("staff-question: sources=0 exchanges=0 linked=0 new-rows=0 unchanged=1", repeated)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_linked_question_migrates_on_its_parked_row(self) -> None:
        key = self.native("linked", {"since": FILED, "text": "Select a plan", "question": "Which plan?"})
        item = the_list.add(KEEPER.id, "Review the plan", owner=OWNER.id, source="nate", changes={
            "state": the_list.ItemState.WORKING})
        item = the_list.park(KEEPER.id, item.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")
        item = the_list.link(KEEPER.id, item.id, (key,), source="nate")
        result, applied = self.migrate(apply=True)
        self.assertEqual(result, 0)
        self.assertIn(f"{key} -> {KEEPER.id}/{item.id}", applied)
        filed, = the_list.records(KEEPER.id)
        exchange, = filed.filed_exchanges
        self.assertEqual(exchange.source_key, key)
        self.assertEqual(filed.state, item.state)
        self.assertEqual(filed.parked_until, item.parked_until)
        self.assertEqual(filed.log, item.log)

    def test_failed_list_save_keeps_review_answer_and_retry_uses_the_reserved_number(self) -> None:
        key = self.native("sent", {"since": FILED, "text": "Choose a plan", "question": "Which plan?",
            "review_id": "Q1", "review_submission": new_item_dialog.stored_submission(origin("request-1234567890")),
            "followups": [{"followup": "Which plan?", "reply": "Use plan A."}],
        })
        with patch.object(the_list, "_save", side_effect=OSError("The List write failed.")):
            self.assertEqual(self.migrate(apply=True)[0], 1)
        available, = review_items.read(self.review, members=MEMBERS, show="open")
        self.assertEqual(available.key, key)
        self.assertEqual(available.response.answer if available.response else "", "Use plan A.")
        self.assertEqual(the_list.records(KEEPER.id), [])
        self.assertEqual(self.migrate(apply=True)[0], 0)
        item, = the_list.records(KEEPER.id)
        self.assertEqual(item.id, "L1")
        self.assertEqual(review_items.read(self.review, members=MEMBERS, show="open"), [])
        _ = change_page.console_folder(self.review)
        snapshot = review_page.build_snapshot(self.review)
        self.assertEqual(snapshot.filed_item_url(self.review, key),
                         review_page.report_url(investigation.ListDestination(KEEPER.id, 1)))

    def test_filed_proposal_answer_keeps_the_pending_verdict(self) -> None:
        log = (ReviewLog(SECOND_ASK, "nate", "question: What changed?"),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: The filter moved."))
        issue = Issue(self.review / "proposal.md", {"review_log": [json.dumps(row) for row in rows(log)]})
        proposal = review_items.ReviewItem("proposal/fixture", "owner/P1", review_items.ReviewType.PROPOSAL,
            OWNER.id, OWNER.id, "Move the filter", review_items.ReviewState.OPEN,
            review_items.ReviewTimes(START, SECOND_REPLY), log, ("approve", "decline"), review_items.ProposalSource(issue))
        with patch.object(review_items, "read_current", return_value=proposal):
            answer_sources.file_saved_review(proposal, MEMBERS)
        current, = answer_sources.apply_review_handoffs([proposal], user_zone())
        self.assertFalse(review_items.on_list(current))
        self.assertFalse(current.unread_answer)
        self.assertEqual(current.actions, ("approve", "decline"))
        self.assertTrue(current.for_you(SECOND_REPLY))
        item, = the_list.records(KEEPER.id)
        self.assertEqual(item.filed_exchanges[0].source_key, proposal.key)

    def test_dry_migration_does_not_alert_for_an_unrouted_change(self) -> None:
        folder = self.root / "changes"
        folder.mkdir()
        listing = folder / "change-review - fixture.md"
        listing.touch()
        row = {"#": "C1", "Producer": "missing", "Change": "Check the route"}
        record = replace(MEMBERS, production=True)
        snapshot = change_review.ReviewListSnapshot("", [row], [], [])
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with (patch.object(staff, "snapshot", return_value=record),
              patch.object(change_review, "review_snapshot", return_value=snapshot),
              patch.object(change_routing, "waits_for_user", return_value=True),
              patch.object(unrouted_alert, "notify") as alert,
              patch("scripts.production.session_staff.member_sources") as live_members):
            self.assertEqual(self.migrate(apply=False)[0], 0)
            alert.assert_not_called()
            live_members.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_failed_list_directory_sync_keeps_the_answer_in_review(self) -> None:
        key = self.native("during-sync", {"since": FILED, "text": "Choose a plan", "question": "Which plan?",
            "review_id": "Q1", "review_submission": new_item_dialog.stored_submission(origin("request-1234567890")),
            "followups": [{"followup": "Which plan?", "reply": "Use plan A."}],
        })
        row = the_list.folder(KEEPER.id) / "L1.md"
        seen: list[str] = []
        original = atomic_file.sync_folder

        def sync(folder: Path) -> None:
            if folder == row.parent and row.exists() and not seen:
                self.assertTrue(the_list.load(KEEPER.id, "L1").filed_exchanges)
                seen.extend(item.key for item in review_items.read(self.review, members=MEMBERS, show="open"))
                raise OSError("List directory sync failed after replacement.")
            original(folder)

        with patch.object(atomic_file, "sync_folder", sync):
            self.assertEqual(self.migrate(apply=True)[0], 1)
        self.assertEqual(seen, [key])
        self.assertFalse(row.exists())
        self.assertEqual(self.migrate(apply=True)[0], 0)
        self.assertTrue(row.exists())
        self.assertEqual(review_items.read(self.review, members=MEMBERS, show="open"), [])

    def test_stale_inventory_cannot_file_a_withdrawn_question(self) -> None:
        for file_open_first in (False, True):
            with self.subTest(file_open_first=file_open_first):
                key = self.native("withdraw-" + str(file_open_first),
                                  {"since": FILED, "text": "Choose a plan", "question": "Which plan?"})
                before = answer_sources.read_exchanges(self.review, members=MEMBERS)
                if file_open_first:
                    self.assertEqual(self.migrate(apply=True)[0], 0)
                path = record_store.default_question_folder(OWNER.id) / key.rsplit("/", 1)[-1]
                _ = path.write_text(path.read_text().replace("status: open", "status: closed"))
                if file_open_first:
                    current, = update_registration.selected_question_records([path], (), None)
                    answer_sources.file_saved_question(current, MEMBERS)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(answer_sources.migrate(before, MEMBERS, apply=True), 0)
                filed = [exchange for item in the_list.records(KEEPER.id) for exchange in item.filed_exchanges
                         if exchange.source_key == key]
                self.assertEqual([exchange.answer for exchange in filed],
                    [answer_sources.QuestionWithoutAnswer.CLOSED] if file_open_first else [])

    def test_withdrawal_serializes_with_the_first_list_save(self) -> None:
        key = self.native("withdraw-during-save", {"since": FILED, "text": "Choose a plan",
                                                  "question": "Which plan?", "review_id": "Q1"})
        path = record_store.default_question_folder(OWNER.id) / key.rsplit("/", 1)[-1]
        selected, = update_registration.selected_question_records([path], (), None)
        outstanding = update_registration.outstanding_path_for_owner(OWNER.id)
        original_lock = update_registration.outstanding_lock
        save = the_list.file_exchanges
        migration_thread = current_thread()
        waiting = Event()
        withdrawals: list[Future[update_registration.OutstandingItem]] = []

        @contextmanager
        def observe_lock(target: Path) -> Generator[None]:
            if current_thread() is not migration_thread and target == outstanding:
                waiting.set()
            with original_lock(target):
                yield

        with ThreadPoolExecutor(max_workers=1) as threads:
            def save_while_withdrawal_starts(
                    source: the_list.LockedExchangeSource, exchanges: tuple[list_exchange.Exchange, ...], title: str, *,
                    linked: investigation.ListDestination | the_list.NextNumber, owner: staff.StaffId,
                    members: staff.StaffSnapshot) -> investigation.ListDestination:
                if current_thread() is migration_thread:
                    self.assertEqual(the_list.records(KEEPER.id), [])
                    with outstanding.with_suffix(".lock").open("a") as lock, self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    withdrawal = threads.submit(update_registration.write_selected_item, selected,
                        update_registration.ProducerQuestions(OWNER), "withdraw", lambda _item, _now: None, close=True)
                    withdrawals.append(withdrawal)
                    self.assertTrue(waiting.wait(timeout=3))
                    self.assertFalse(withdrawal.done())
                    current, = update_registration.selected_question_records([path], (), None)
                    self.assertEqual(current.status, "open")
                return save(source, exchanges, title, linked=linked, owner=owner, members=members)

            with (patch.object(update_registration, "outstanding_lock", observe_lock),
                  patch.object(the_list, "file_exchanges", side_effect=save_while_withdrawal_starts)):
                self.assertEqual(self.migrate(apply=True)[0], 0)
        withdrawal, = withdrawals
        _ = withdrawal.result()
        current, = update_registration.selected_question_records([path], (), None)
        self.assertEqual(current.status, "closed")
        row, = the_list.records(KEEPER.id)
        self.assertEqual([exchange.answer for exchange in row.filed_exchanges],
                         [answer_sources.QuestionWithoutAnswer.CLOSED])

    def test_migration_studio_ask_and_review_answer_complete_together(self) -> None:
        # Run the real locks in a child process so a deadlock cannot retain the fixture.
        test = "scripts.review_links.test_answer_sources.AnswerSourceTests._exercise_filing_lock_order"
        try:
            result = subprocess.run([sys.executable, "-B", "-m", "unittest", test], capture_output=True,
                                    text=True, timeout=8, env={**os.environ, "TMPDIR": str(self.root)})
        except subprocess.TimeoutExpired:
            self.fail("Timed out while migration, Studio Ask, and Review Answer held their locks.")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _exercise_filing_lock_order(self) -> None:
        key = self.native("lock-order", {"since": FILED, "text": "Choose a plan",
                                         "question": "Which plan?", "review_id": "Q1"})
        path = record_store.default_question_folder(OWNER.id) / key.rsplit("/", 1)[-1]
        record, = update_registration.selected_question_records([path], (), None)
        question = change_browser.WaitingQuestion(key, OWNER.name, record, OWNER, OWNER.id, ASKER.name)
        item = the_list.add(KEEPER.id, "Check the result", owner=KEEPER.id, source="test", changes={})
        request = the_list.PageRequest("lock-order", "question", "Is it ready?")
        status = change_page.DeliveryStatus(self.review)
        owner_path = update_registration.outstanding_path_for_owner(OWNER.id)
        at_list, at_owner, at_delivery, proceed = Event(), Event(), Event(), Event()
        save_exchanges = the_list.file_exchanges
        owner_lock = update_registration.outstanding_lock
        delivery_lock = status.locked

        def pause_before_list(source: the_list.LockedExchangeSource, exchanges: tuple[list_exchange.Exchange, ...],
                              title: str, *, linked: investigation.ListDestination | the_list.NextNumber,
                              owner: staff.StaffId, members: staff.StaffSnapshot) -> investigation.ListDestination:
            at_list.set()
            self.assertTrue(proceed.wait(timeout=4))
            return save_exchanges(source, exchanges, title, linked=linked, owner=owner, members=members)

        @contextmanager
        def pause_before_owner(target: Path) -> Generator[None]:
            if current_thread().name.startswith("review-answer") and target == owner_path:
                at_owner.set()
                self.assertTrue(proceed.wait(timeout=4))
            with owner_lock(target):
                yield

        @contextmanager
        def signal_before_delivery() -> Generator[None]:
            if current_thread().name.startswith("studio-ask"):
                at_delivery.set()
            with delivery_lock():
                yield

        with (patch.object(the_list, "file_exchanges", side_effect=pause_before_list),
              patch.object(update_registration, "outstanding_lock", pause_before_owner),
              patch.object(status, "locked", signal_before_delivery),
              ThreadPoolExecutor(max_workers=1, thread_name_prefix="migration") as migration,
              ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-answer") as answer,
              ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-ask") as ask):
            filing = migration.submit(self.migrate, apply=True)
            try:
                self.assertTrue(at_list.wait(timeout=3))
                answering = answer.submit(change_browser.submit_question, question, "answer", "Use plan A.", status,
                                          save=change_browser.SelectedQuestionSave())
                self.assertTrue(at_owner.wait(timeout=3))
                asking = ask.submit(the_list.ask_question, KEEPER.id, item.id, request, status)
                self.assertTrue(at_delivery.wait(timeout=3))
            finally:
                proceed.set()
            self.assertEqual(filing.result()[0], 0)
            self.assertEqual(answering.result()[1], "Use plan A.")
            delivery = asking.result()
            self.assertIsInstance(delivery, change_page.PageQuestionSend)
            assert isinstance(delivery, change_page.PageQuestionSend)
            if isinstance(delivery.job, change_page.QuestionJob):
                delivery.job.release()
        saved_question, = the_list.load(KEEPER.id, item.id).questions
        self.assertEqual(saved_question.words, request.words)
        self.assertTrue(any(exchange.source_key == key for row in the_list.records(KEEPER.id)
                            for exchange in row.filed_exchanges))

    def test_reservation_recovers_after_counter_advances(self) -> None:
        _ = self.native("interrupted", {"since": FILED, "text": "Choose a plan", "question": "Which plan?"})
        with patch.object(the_list, "_save_exchange_reservation", side_effect=OSError("Reservation write failed.")):
            self.assertEqual(self.migrate(apply=True)[0], 1)
        self.assertEqual((the_list.folder(KEEPER.id) / ".next-number").read_text(), "2\n")
        other = the_list.add(KEEPER.id, "Another item", owner=OWNER.id, source="test", changes={})
        self.assertEqual(other.id, "L2")
        self.assertEqual(self.migrate(apply=True)[0], 0)
        self.assertEqual({item.id for item in the_list.records(KEEPER.id)}, {"L1", "L2"})

    def test_keyed_staff_ask_retry_files_the_existing_question(self) -> None:
        key = self.native("keyed", {"since": FILED, "text": "Choose a plan", "question": "Which plan?",
                                    "request_key": "the-same-request"})
        path = record_store.default_question_folder(OWNER.id) / key.rsplit("/", 1)[-1]
        saved, = update_registration.selected_question_records([path], (), None)
        scope = update_registration.ProducerQuestions(OWNER)
        parts = update_registration.QuestionParts(ask="Which plan?", context="", items=[], recommend="", choices=[])
        with patch.object(update_registration, "keyed_question", return_value=saved):
            result = update_registration.ask(scope, saved.item["text"], parts, "", update_registration.Ranking(),
                identities=update_registration.QuestionStaff.for_record(MEMBERS),
                outgoing=update_registration.KeyedStaffQuestion("the-same-request"))
        self.assertEqual(result, saved)
        item, = the_list.records(KEEPER.id)
        self.assertEqual(item.filed_exchanges[0].source_key, key)

    def change_with_log(self, log: tuple[ReviewLog, ...],
                        question: delivery_state.OpenQuestion | delivery_state.AnsweredQuestion) -> review_items.ReviewItem:
        path = self.root / "changes" / "change-review - fixture.md"
        assignment = change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(ASKER.id, ASKER.name),
                                                    path.with_name("production.md"))
        slots = delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery(), question)
        source = review_items.ChangeSource(path, {"Review log": encode(log)}, assignment, slots)
        return review_items.ReviewItem("fixture/C1", "C1", review_items.ReviewType.CHANGE, ASKER.id, ASKER.id,
            "Check the result", review_items.ReviewState.OPEN, review_items.ReviewTimes(START, SECOND_REPLY),
            log, ("works", "not works"), source, answer_time=SECOND_REPLY)

    def test_reply_uses_its_exchange_id_and_saved_author_after_owner_changes(self) -> None:
        first_id = "first"
        log = (ReviewLog(START, "nate", "question: Which plan?", first_id),
               ReviewLog(SECOND_ASK, "nate", "question: How long?", "second"),
               ReviewLog(SECOND_REPLY, OWNER.name, "Answer: Use plan A.", question_path.answer_log_id(first_id)))
        item = self.change_with_log(log, delivery_state.OpenQuestion("second", "How long?", delivery_state.NoDelivery()))
        _ = (self.root / "config" / "producer_map.conf").write_text(f"{OWNER.id}={OWNER.id}\n")
        with patch.object(review_items, "read_current", return_value=item):
            answer_sources.file_saved_review(item, MEMBERS)
        row, = the_list.records(KEEPER.id)
        exchange, = row.filed_exchanges
        self.assertEqual((exchange.exchange_id, exchange.question, row.owner, row.producer),
                         ("question:first", "Which plan?", OWNER.id, OWNER.id))

    def test_legacy_change_reply_uses_the_saved_slot_question(self) -> None:
        log = (ReviewLog(START, "nate", "question: An older question?"),
               ReviewLog(SECOND_ASK, "nate", "question: Which side?", "saved"),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: The east side."))
        item = self.change_with_log(log, delivery_state.AnsweredQuestion("saved", "Which side?", "The west side."))
        assert isinstance(item.source, review_items.ChangeSource)
        item = replace(item, source=replace(item.source, answered_by=OWNER.id, answered_at=SECOND_REPLY))
        with patch.object(review_items, "read", return_value=[item]):
            exchange, = answer_sources.read_exchanges(self.review, members=MEMBERS).exchanges
        self.assertEqual((exchange.exchange_id, exchange.question, exchange.answerer),
                         ("question:saved", "Which side?", OWNER.id))
        self.assertEqual(exchange.answer, answer_sources.ExchangeAnswer(
            "The west side.", SECOND_REPLY, answer_sources.AnswerRead.UNREAD))

    def test_slot_only_answer_keeps_earlier_untagged_feedback_and_uses_the_saved_author(self) -> None:
        log = (ReviewLog(START, "nate", "feedback: Check the result."),
               ReviewLog(FIRST_REPLY, ASKER.id, "Answer: Yes."),
               ReviewLog(SECOND_ASK, "nate", "question: Is it ready?", "saved"))
        item = self.change_with_log(log, delivery_state.AnsweredQuestion("saved", "Is it ready?", "Yes."))
        assert isinstance(item.source, review_items.ChangeSource)
        item = replace(item, source=replace(item.source, answered_by=OWNER.id, answered_at=SECOND_REPLY))
        with patch.object(review_items, "read_current", return_value=item):
            answer_sources.file_saved_review(item, MEMBERS)
        row, = the_list.records(KEEPER.id)
        exchange, = row.filed_exchanges
        self.assertEqual((exchange.exchange_id, exchange.answerer, row.owner), ("question:saved", OWNER.id, OWNER.id))
        self.assertEqual(exchange.answer, answer_sources.ExchangeAnswer(
            "Yes.", SECOND_REPLY, answer_sources.AnswerRead.UNREAD))
        projected, = answer_sources.apply_review_handoffs([item], user_zone())
        handoff = projected.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertEqual(handoff.answer_events, frozenset())
        self.assertTrue(projected.unread_answer)
        inputs = review_page.ReviewBodyInputs(change_page.ReviewBrowser("test"), MEMBERS, 4, {}, user_zone(),
                                             staff_names.StaffNames(MEMBERS))
        markup = review_page.details(projected, inputs)
        self.assertIn("Yes.", markup)
        self.assertIn('value="read">Mark read', markup)

    def test_slot_answer_keeps_late_untagged_feedback_with_the_same_words(self) -> None:
        log = (ReviewLog(START, "nate", "feedback: Check the result."),
               ReviewLog(SECOND_ASK, "nate", "question: Is it ready?", "saved"),
               ReviewLog(SECOND_REPLY, ASKER.id, "Answer: Yes."))
        saved_time = SECOND_REPLY + timedelta(minutes=10)
        item = self.change_with_log(log, delivery_state.AnsweredQuestion("saved", "Is it ready?", "Yes."))
        assert isinstance(item.source, review_items.ChangeSource)
        item = replace(item, source=replace(item.source, answered_by=OWNER.id, answered_at=saved_time),
                       answer_time=saved_time)
        with patch.object(review_items, "read_current", return_value=item):
            answer_sources.file_saved_review(item, MEMBERS)
        row, = the_list.records(KEEPER.id)
        exchange, = row.filed_exchanges
        self.assertEqual((exchange.exchange_id, exchange.answerer, row.owner), ("question:saved", OWNER.id, OWNER.id))
        self.assertEqual(exchange.answer, answer_sources.ExchangeAnswer(
            "Yes.", saved_time, answer_sources.AnswerRead.UNREAD))
        projected, = answer_sources.apply_review_handoffs([item], user_zone())
        handoff = projected.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertEqual(handoff.answer_events, frozenset())
        self.assertTrue(projected.unread_answer)
        inputs = review_page.ReviewBodyInputs(change_page.ReviewBrowser("test"), MEMBERS, 4, {}, user_zone(),
                                             staff_names.StaffNames(MEMBERS))
        markup = review_page.details(projected, inputs)
        self.assertIn("Yes.", markup)
        self.assertIn('value="read">Mark read', markup)

    def test_migration_keeps_the_legacy_reply_id_after_the_slot_changes(self) -> None:
        for index, (tag, expected_id) in enumerate((("saved", "question:saved"), ("", "log:0")), 1):
            with self.subTest(tag=tag):
                log = (ReviewLog(START, "nate", "question: Which side?", tag),
                       ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The east side."),
                       ReviewLog(SECOND_ASK, "nate", "question: How long?", "later"))
                original = self.change_with_log(log[:2], delivery_state.AnsweredQuestion(
                    "saved", "Which side?", "The east side."))
                if not tag:
                    original = replace(original, key=f"fixture/C{index}")
                    with patch.object(review_items, "read", return_value=[original]):
                        unresolved = answer_sources.read_exchanges(self.review, members=MEMBERS)
                    self.assertEqual(unresolved.unresolved, ((original.key, 1),))
                    candidate, = [exchange for exchange in unresolved.exchanges if exchange.source_key == original.key]
                    self.assertEqual(candidate.binding, list_exchange.ExchangeBinding(
                        list_exchange.QuestionSlot("saved"), list_exchange.ReplyWithoutEvent.SLOT))
                assert isinstance(original.source, review_items.ChangeSource)
                original = replace(original, source=replace(
                    original.source, answered_by=OWNER.id, answered_at=FIRST_REPLY))
                later = self.change_with_log(log, delivery_state.OpenQuestion("later", "How long?", delivery_state.NoDelivery()))
                key = f"fixture/C{index}"
                for current in (original, later):
                    item = replace(current, key=key, answer_time=FIRST_REPLY)
                    with (patch.object(review_items, "read", return_value=[item]),
                          patch.object(review_items, "read_current", return_value=item)):
                        inventory = answer_sources.read_exchanges(self.review, members=MEMBERS)
                        identity, = (exchange.exchange_id for exchange in inventory.exchanges)
                        if tag:
                            self.assertEqual(identity, expected_id)
                        else:
                            self.assertTrue(identity.startswith("log:sha256:"))
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(answer_sources.migrate(inventory, MEMBERS, apply=True), 0)
                    exchanges = [exchange for row in the_list.records(KEEPER.id)
                                 for exchange in row.filed_exchanges if exchange.source_key == key]
                    self.assertEqual([exchange.exchange_id for exchange in exchanges], [identity])
                    projected, = answer_sources.apply_review_handoffs([item], user_zone())
                    handoff = projected.exchange_handoff
                    assert isinstance(handoff, review_items.FiledReviewExchange)
                    self.assertEqual(handoff.answer_events, frozenset({1}))
                    self.assertFalse(projected.unread_answer)

    def test_older_tagged_question_reports_an_unresolved_untagged_reply(self) -> None:
        log = (ReviewLog(START, "nate", "feedback: Check the result."),
               ReviewLog(SECOND_ASK, "nate", "question: Which side?", "saved"),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: The east side."),
               ReviewLog(SECOND_REPLY + timedelta(hours=1), "nate", "question: How long?", "later"))
        item = self.change_with_log(log, delivery_state.OpenQuestion("later", "How long?", delivery_state.NoDelivery()))
        result, preview = self.migrate_change(item, apply=False)
        self.assertEqual(result, 0)
        self.assertIn("Unresolved legacy replies: 1", preview)
        self.assertIn(f"  {item.key}: 1", preview)
        self.assertNotIn("The east side.", preview)
        self.assertEqual(self.migrate_change(item)[0], 0)
        self.assertEqual(the_list.records(KEEPER.id), [])
        projected, = answer_sources.apply_review_handoffs([item], user_zone(), members=MEMBERS)
        self.assertTrue(projected.unread_answer)
        self.assertEqual(projected.log, log)

    def migrate_change(self, item: review_items.ReviewItem, *, apply: bool = True) -> tuple[int, str]:
        with (patch.object(review_items, "read", return_value=[item]),
              patch.object(review_items, "read_current", return_value=item)):
            return self.migrate(apply=apply)

    def check_change_handoff(self, item: review_items.ReviewItem, positions: frozenset[int], *,
                             unread: bool) -> review_items.FiledReviewExchange:
        projected, = answer_sources.apply_review_handoffs([item], user_zone(), members=MEMBERS)
        handoff = projected.exchange_handoff
        self.assertIsInstance(handoff, review_items.FiledReviewExchange)
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertEqual(handoff.answer_events, positions)
        self.assertEqual(projected.unread_answer, unread)
        return handoff

    def test_filed_untagged_reply_survives_another_question_and_feedback(self) -> None:
        log = (ReviewLog(START, "nate", "question: Which side?", "saved"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The east side."))
        original = self.change_with_log(log, delivery_state.AnsweredQuestion("saved", "Which side?", "The east side."))
        assert isinstance(original.source, review_items.ChangeSource)
        original = replace(original, source=replace(original.source, answered_at=FIRST_REPLY, answered_by=OWNER.id))
        self.assertEqual(self.migrate_change(original)[0], 0)
        row, = the_list.records(KEEPER.id)
        saved, = row.filed_exchanges
        later = self.change_with_log((*log,
            ReviewLog(SECOND_ASK, "nate", "question: How long?", "later"),
            ReviewLog(SECOND_REPLY, "nate", "feedback: Check the result.", "feedback")),
            review_items.OpenFeedback("feedback", "Check the result.", delivery_state.NoDelivery()))
        for _ in range(2):
            handoff = self.check_change_handoff(replace(later, answer_time=FIRST_REPLY), frozenset({1}), unread=False)
            self.assertEqual(handoff.destination.number, row.number)
            self.assertEqual(self.migrate_change(later)[0], 0)
            current, = the_list.records(KEEPER.id)
            self.assertEqual(current.filed_exchanges, (saved,))
        self.assertEqual(saved.exchange_id, "question:saved")

    def test_delayed_feedback_cannot_replace_a_filed_slot_answer(self) -> None:
        log = (ReviewLog(START, "nate", "feedback: Check the result."),
               ReviewLog(SECOND_ASK, "nate", "question: Which side?", "saved"),
               ReviewLog(SECOND_REPLY, ASKER.id, "Answer: The east side."))
        original = self.change_with_log(log, delivery_state.AnsweredQuestion("saved", "Which side?", "The west side."))
        saved_at = SECOND_REPLY + timedelta(minutes=10)
        assert isinstance(original.source, review_items.ChangeSource)
        original = replace(original, source=replace(original.source, answered_at=saved_at, answered_by=OWNER.id))
        self.assertEqual(self.migrate_change(original)[0], 0)
        row, = the_list.records(KEEPER.id)
        saved, = row.filed_exchanges
        later = self.change_with_log((*log,
            ReviewLog(saved_at + timedelta(hours=1), "nate", "question: How long?", "later")),
            delivery_state.OpenQuestion("later", "How long?", delivery_state.NoDelivery()))
        self.assertEqual(self.migrate_change(later)[0], 0)
        current, = the_list.records(KEEPER.id)
        self.assertEqual(current.filed_exchanges, (saved,))
        self.assertEqual(saved.answer, list_exchange.ExchangeAnswer("The west side.", saved_at, list_exchange.AnswerRead.UNREAD))
        _ = self.check_change_handoff(later, frozenset(), unread=True)

    def test_filed_id_survives_later_feedback_with_partial_legacy_metadata(self) -> None:
        asked_at = START + timedelta(minutes=25)
        log = (ReviewLog(asked_at, "nate", "question: Which side?"),
               ReviewLog(FIRST_REPLY, OWNER.name, "Answer: The east side."),
               ReviewLog(READ, "nate", "Marked read"),
               ReviewLog(SECOND_ASK, "nate", "feedback: Check the result.", "feedback"),
               ReviewLog(SECOND_REPLY, OWNER.name, "Answer: The east side."))
        for number, (has_author, has_time) in enumerate(((True, False), (False, True), (True, True), (False, False)), 1):
            with self.subTest(author=has_author, time=has_time):
                original = self.change_with_log(log, delivery_state.AnsweredQuestion("saved", "Which side?", "The east side."))
                assert isinstance(original.source, review_items.ChangeSource)
                key = f"fixture/C{number}"
                original = replace(original, key=key, answer_time=SECOND_REPLY,
                    source=replace(original.source, answered_by=OWNER.id if has_author else staff.StaffId(""),
                        answered_at=FIRST_REPLY if has_time else question_path.NO_ANSWER_TIME))
                old = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, key, "question:saved",
                    list_exchange.Direction.ANSWER_TO_NATE, "Which side?", START, list_exchange.Nate.USER, OWNER.id,
                    list_exchange.ExchangeAnswer("The east side.", FIRST_REPLY, list_exchange.AnswerRead.UNREAD))
                with the_list.lock_exchange_source(key) as locked:
                    row = the_list.file_exchanges(locked, (old,), "Which side?", linked=the_list.NextNumber.AVAILABLE,
                                                  owner=OWNER.id, members=MEMBERS)
                self.assertEqual(self.migrate_change(original)[0], 0)
                saved_exchanges = the_list.load(row.keeper, f"L{row.number}").filed_exchanges
                self.assertEqual(len(saved_exchanges), 1)
                saved, = saved_exchanges
                self.assertEqual((saved.exchange_id, saved.asked_at, saved.answerer), ("question:saved", asked_at, OWNER.id))
                self.assertEqual(saved.answer, list_exchange.ExchangeAnswer(
                    "The east side.", FIRST_REPLY, list_exchange.AnswerRead.READ))
                _ = self.check_change_handoff(original, frozenset({1}), unread=True)
                later = self.change_with_log((*log,
                    ReviewLog(SECOND_REPLY + timedelta(hours=1), "nate", "question: How long?", "later"),
                    ReviewLog(SECOND_REPLY + timedelta(hours=2), "nate", "feedback: Check again.", "again")),
                    review_items.OpenFeedback("again", "Check again.", delivery_state.NoDelivery()))
                later = replace(later, key=key)
                self.assertEqual(self.migrate_change(later)[0], 0)
                self.assertEqual(the_list.load(row.keeper, f"L{row.number}").filed_exchanges, (saved,))
                _ = self.check_change_handoff(later, frozenset({1}), unread=True)

    def test_old_filed_id_gains_its_log_locator_after_slot_replacement(self) -> None:
        asked_at = START + timedelta(minutes=10)
        log = (ReviewLog(asked_at, "nate", "question: Which side?"),
               ReviewLog(FIRST_REPLY, OWNER.name, "Answer: The east side."),
               ReviewLog(SECOND_ASK, "nate", "question: How long?", "later"),
               ReviewLog(SECOND_REPLY, "nate", "feedback: Check the result.", "feedback"))
        later = self.change_with_log(log, review_items.OpenFeedback("feedback", "Check the result.",
                                                                   delivery_state.NoDelivery()))
        later = replace(later, answer_time=FIRST_REPLY)
        old = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, later.key, "question:saved",
            list_exchange.Direction.ANSWER_TO_NATE, "Which side?", START, list_exchange.Nate.USER, OWNER.id,
            list_exchange.ExchangeAnswer("The east side.", FIRST_REPLY, list_exchange.AnswerRead.UNREAD))
        with the_list.lock_exchange_source(later.key) as locked:
            row = the_list.file_exchanges(locked, (old,), "Which side?", linked=the_list.NextNumber.AVAILABLE,
                                          owner=OWNER.id, members=MEMBERS)
        for _ in range(2):
            self.assertEqual(self.migrate_change(later)[0], 0)
            recorded = the_list.load(row.keeper, f"L{row.number}").filed_exchanges
            self.assertEqual(len(recorded), 1)
            saved, = recorded
            self.assertEqual((saved.exchange_id, saved.asked_at), ("question:saved", asked_at))
            self.assertEqual(saved.answer, old.answer)
            _ = self.check_change_handoff(later, frozenset({1}), unread=False)

    def test_slot_without_a_question_event_keeps_its_id_after_replacement(self) -> None:
        original = self.change_with_log((), delivery_state.AnsweredQuestion("saved", "Which side?", "The east side."))
        assert isinstance(original.source, review_items.ChangeSource)
        original = replace(original, source=replace(original.source, answered_by=OWNER.id, answered_at=FIRST_REPLY))
        self.assertEqual(self.migrate_change(original)[0], 0)
        row, = the_list.records(KEEPER.id)
        saved, = row.filed_exchanges
        later = self.change_with_log((ReviewLog(SECOND_ASK, "nate", "question: Which side?", "later"),),
                                     delivery_state.OpenQuestion("later", "Which side?", delivery_state.NoDelivery()))
        self.assertEqual(self.migrate_change(later)[0], 0)
        self.assertEqual(self.migrate_change(later)[0], 0)
        rows_after = the_list.records(KEEPER.id)
        self.assertEqual(rows_after, [row])
        self.assertEqual(saved.exchange_id, "question:saved")
        _ = self.check_change_handoff(replace(later, answer_time=question_path.NO_ANSWER_TIME), frozenset(), unread=False)

    def test_unconfirmed_change_binding_survives_slot_replacement_and_retry(self) -> None:
        for number, fail_receipt in enumerate((False, True), 1):
            with self.subTest(receipt=fail_receipt):
                log = (ReviewLog(START, "nate", "question: Which side?", "saved"),
                       ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The east side."))
                original = self.change_with_log(log, delivery_state.AnsweredQuestion("saved", "Which side?", "The east side."))
                assert isinstance(original.source, review_items.ChangeSource)
                key = f"fixture/C{number}"
                original = replace(original, key=key, answer_time=FIRST_REPLY,
                    source=replace(original.source, answered_at=FIRST_REPLY, answered_by=OWNER.id))
                write = atomic_file.write_text_atomic

                def write_receipt(path: Path, words: str, *, new_file_mode: int) -> None:
                    if path.suffix == ".saved":
                        raise OSError("The receipt write failed.")
                    write(path, words, new_file_mode=new_file_mode)

                failure = (patch.object(the_list, "write_text_atomic", write_receipt) if fail_receipt
                           else patch.object(the_list, "_save", side_effect=OSError("The List write failed.")))
                with failure:
                    self.assertEqual(self.migrate_change(original)[0], 1)
                projected, = answer_sources.apply_review_handoffs([original], user_zone(), members=MEMBERS)
                self.assertIs(projected.exchange_handoff, review_items.NoFiledReviewExchange.ABSENT)
                self.assertTrue(projected.unread_answer)
                later = self.change_with_log((*log,
                    ReviewLog(SECOND_ASK, "nate", "question: How long?", "later"),
                    ReviewLog(SECOND_REPLY, "nate", "feedback: Check the result.", "feedback")),
                    review_items.OpenFeedback("feedback", "Check the result.", delivery_state.NoDelivery()))
                later = replace(later, key=key, answer_time=FIRST_REPLY)
                for _ in range(2):
                    self.assertEqual(self.migrate_change(later)[0], 0)
                    handoff = self.check_change_handoff(later, frozenset({1}), unread=False)
                    self.assertEqual(handoff.destination.number, number)
                row = the_list.load(KEEPER.id, f"L{number}")
                self.assertEqual([exchange.exchange_id for exchange in row.filed_exchanges], ["question:saved"])

    def test_filed_staff_question_keeps_its_identity_when_answered(self) -> None:
        fields: update_registration.OutstandingItem = {"since": FILED, "text": "Choose a plan",
                                                        "question": "Which plan?"}
        key = self.native("answered-after-filing", fields)
        self.assertEqual(self.migrate(apply=True)[0], 0)
        before, = the_list.records(KEEPER.id)
        _ = self.native("answered-after-filing", {**fields, "review_log": encode((
            ReviewLog(FIRST_REPLY, "nate", "answer: Use plan A."),))}, status="closed")
        self.assertEqual(self.migrate(apply=True)[0], 0)
        after, = the_list.records(KEEPER.id)
        saved, = after.filed_exchanges
        self.assertEqual((before.id, before.filed_exchanges[0].exchange_id), (after.id, saved.exchange_id))
        self.assertEqual(saved.source_key, key)
        self.assertEqual(saved.answer, list_exchange.ExchangeAnswer("Use plan A.", FIRST_REPLY, list_exchange.AnswerRead.NOT_TRACKED))

    def test_filed_reply_round_trips_its_verified_origins(self) -> None:
        log = (ReviewLog(START, "nate", "question: Which side?", "saved"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The east side."))
        original = self.change_with_log(log, delivery_state.AnsweredQuestion("saved", "Which side?", "The east side."))
        self.assertEqual(self.migrate_change(original)[0], 0)
        row, = the_list.records(KEEPER.id)
        filed, = row.filed_exchanges
        binding = filed.binding
        self.assertIsInstance(binding, list_exchange.ExchangeBinding)
        assert isinstance(binding, list_exchange.ExchangeBinding)
        self.assertEqual(binding.question, list_exchange.QuestionSlot("saved"))
        self.assertIsInstance(binding.reply, list_exchange.LoggedReply)
        assert isinstance(binding.reply, list_exchange.LoggedReply)
        self.assertEqual(binding.reply.position, 1)
        self.assertEqual(len(binding.reply.digest), 64)

    def test_repeated_questions_without_a_locator_keep_their_own_ids(self) -> None:
        log = (ReviewLog(START, "nate", "question: Which side?"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The east side."),
               ReviewLog(SECOND_ASK, "nate", "question: Which side?"),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: The east side."))
        original = self.change_with_log(log, delivery_state.AnsweredQuestion("second", "Which side?", "The east side."))
        assert isinstance(original.source, review_items.ChangeSource)
        original = replace(original, source=replace(original.source, answered_by=OWNER.id, answered_at=SECOND_REPLY))
        old = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, original.key, "question:first",
            list_exchange.Direction.ANSWER_TO_NATE, "Which side?", START, list_exchange.Nate.USER, OWNER.id,
            list_exchange.ExchangeAnswer("The east side.", FIRST_REPLY, list_exchange.AnswerRead.UNREAD))
        with the_list.lock_exchange_source(original.key) as locked:
            row = the_list.file_exchanges(locked, (old,), "Which side?", linked=the_list.NextNumber.AVAILABLE,
                                          owner=OWNER.id, members=MEMBERS)
        self.assertEqual(self.migrate_change(original)[0], 0)
        saved = the_list.load(row.keeper, f"L{row.number}").filed_exchanges
        self.assertEqual([exchange.exchange_id for exchange in saved], ["question:first", "question:second"])
        _ = self.check_change_handoff(original, frozenset(), unread=True)

    def test_migration_retains_the_id_already_filed_for_a_legacy_reply(self) -> None:
        log = (ReviewLog(START, "nate", "question: Which side?"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The east side."))
        answered = delivery_state.AnsweredQuestion("saved", "Which side?", "The east side.")
        original = self.change_with_log(log, answered)
        earlier_filing = list_exchange.Exchange(
            list_exchange.Source.SCOPED_REPLY, original.key, "question:saved", list_exchange.Direction.ANSWER_TO_NATE,
            "Which side?", START, list_exchange.Nate.USER, OWNER.id,
            list_exchange.ExchangeAnswer("The east side.", FIRST_REPLY, list_exchange.AnswerRead.UNREAD))
        with the_list.lock_exchange_source(original.key) as locked:
            row = the_list.file_exchanges(locked, (earlier_filing,), "Which side?",
                                         linked=the_list.NextNumber.AVAILABLE, owner=OWNER.id, members=MEMBERS)
        read_log = (*log, ReviewLog(READ, "nate", "Marked read"))
        changed_slot = delivery_state.OpenQuestion("later", "How long?", delivery_state.NoDelivery())
        for item in (self.change_with_log(read_log, answered),
                     self.change_with_log((*read_log, ReviewLog(SECOND_ASK, "nate", "question: How long?", "later")),
                                          changed_slot)):
            with (patch.object(review_items, "read", return_value=[item]),
                  patch.object(review_items, "read_current", return_value=item)):
                result, _ = self.migrate(apply=True)
            self.assertEqual(result, 0)
            saved = the_list.load(row.keeper, f"L{row.number}").filed_exchanges
            self.assertEqual([exchange.exchange_id for exchange in saved], ["question:saved"])
            self.assertEqual(saved[0].answer, list_exchange.ExchangeAnswer(
                "The east side.", FIRST_REPLY, list_exchange.AnswerRead.READ))
            projected, = answer_sources.apply_review_handoffs([item], user_zone())
            handoff = projected.exchange_handoff
            assert isinstance(handoff, review_items.FiledReviewExchange)
            self.assertEqual(handoff.answer_events, frozenset({1}))
            self.assertIn("question:saved", handoff.exchange_ids)

    def test_migration_retains_the_filed_id_when_a_legacy_slot_has_author_and_time(self) -> None:
        asked_at = START + timedelta(minutes=25)
        log = (ReviewLog(asked_at, "nate", "question: Which side?"),
               ReviewLog(FIRST_REPLY, OWNER.name, "Answer: The east side."),
               ReviewLog(READ, "nate", "Marked read"))
        original = self.change_with_log(log, delivery_state.AnsweredQuestion("saved", "Which side?", "The east side."))
        assert isinstance(original.source, review_items.ChangeSource)
        original = replace(original, source=replace(original.source, answered_by=OWNER.id, answered_at=FIRST_REPLY))
        earlier_filing = list_exchange.Exchange(
            list_exchange.Source.SCOPED_REPLY, original.key, "question:saved", list_exchange.Direction.ANSWER_TO_NATE,
            "Which side?", START, list_exchange.Nate.USER, OWNER.id,
            list_exchange.ExchangeAnswer("The east side.", FIRST_REPLY, list_exchange.AnswerRead.UNREAD))
        with the_list.lock_exchange_source(original.key) as locked:
            row = the_list.file_exchanges(locked, (earlier_filing,), "Which side?",
                                         linked=the_list.NextNumber.AVAILABLE, owner=OWNER.id, members=MEMBERS)
        later = self.change_with_log((*log, ReviewLog(SECOND_ASK, "nate", "question: How long?", "later")),
                                     delivery_state.OpenQuestion("later", "How long?", delivery_state.NoDelivery()))
        for item in (original, later):
            with (patch.object(review_items, "read", return_value=[item]),
                  patch.object(review_items, "read_current", return_value=item)):
                inventory = answer_sources.read_exchanges(self.review, members=MEMBERS)
                with redirect_stdout(io.StringIO()):
                    result = answer_sources.migrate(inventory, MEMBERS, apply=True)
            self.assertEqual(result, 0)
            self.assertEqual([exchange.exchange_id for exchange in inventory.exchanges], ["question:saved"])
            saved, = the_list.load(row.keeper, f"L{row.number}").filed_exchanges
            self.assertEqual((saved.exchange_id, saved.asked_at, saved.answerer), ("question:saved", asked_at, OWNER.id))
            self.assertEqual(saved.answer, list_exchange.ExchangeAnswer(
                "The east side.", FIRST_REPLY, list_exchange.AnswerRead.READ))
            projected, = answer_sources.apply_review_handoffs([item], user_zone())
            handoff = projected.exchange_handoff
            assert isinstance(handoff, review_items.FiledReviewExchange)
            self.assertEqual(handoff.answer_events, frozenset({1}))
            self.assertEqual(handoff.exchange_ids, frozenset({"question:saved"}))

    def test_filed_answer_keeps_an_unread_feedback_reply_with_the_same_text(self) -> None:
        log = (ReviewLog(START, "nate", "question: Which plan?", "first"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: Yes.", question_path.answer_log_id("first")),
               ReviewLog(SECOND_ASK, "nate", "feedback: Check the result.", "feedback"),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: Yes.", question_path.answer_log_id("feedback")))
        item = self.change_with_log(log, review_items.CompletedFeedback("feedback", "Check the result.", "Yes."))
        with patch.object(review_items, "read_current", return_value=item):
            answer_sources.file_saved_review(item, MEMBERS)
        current, = answer_sources.apply_review_handoffs([item], user_zone())
        handoff = current.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertEqual(handoff.answer_events, frozenset({1}))
        self.assertEqual(handoff.exchange_ids, frozenset({"question:first"}))
        self.assertTrue(review_page.can_acknowledge_answer(current))
        names = staff_names.StaffNames(MEMBERS)
        inputs = review_page.ReviewBodyInputs(change_page.ReviewBrowser("test"), MEMBERS, 4, {}, user_zone(), names)
        markup = review_page.details(current, inputs)
        self.assertIn("Answer filed on the List", markup)
        self.assertIn("Yes.", markup)
        self.assertIn('value="read">Mark read', markup)

    def test_new_investigation_answer_files_when_the_source_saves(self) -> None:
        with patch.object(console_text, "require"):
            record = investigation.file(OWNER.id, "Choose a route", "Which route?",
                                        waiting_why="The owner must check the route.")
            self.assertEqual(the_list.records(KEEPER.id), [])
            _ = investigation.answer(OWNER.id, record.id, "Use the north route.")
        item, = the_list.records(KEEPER.id)
        exchange, = item.filed_exchanges
        self.assertEqual((item.owner, exchange.asker, exchange.answerer), (OWNER.id, answer_sources.Nate.USER, OWNER.id))
        answer = exchange.answer
        self.assertIsInstance(answer, answer_sources.ExchangeAnswer)
        assert isinstance(answer, answer_sources.ExchangeAnswer)
        self.assertEqual((answer.text, answer.read), ("Use the north route.", answer_sources.AnswerRead.UNREAD))

    def test_identical_slot_question_does_not_reuse_an_anonymous_log_question(self) -> None:
        log = (ReviewLog(START, "nate", "question: Which side?"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The east side."))
        for number, reply in enumerate(("The east side.", "The west side."), 1):
            with self.subTest(reply=reply):
                first = self.change_with_log(log, delivery_state.AnsweredQuestion(
                    "first", "Which side?", "The east side."))
                assert isinstance(first.source, review_items.ChangeSource)
                first = replace(first, key=f"fixture/C{number}",
                                source=replace(first.source, answered_at=FIRST_REPLY, answered_by=OWNER.id))
                self.assertEqual(self.migrate_change(first)[0], 0)
                second = self.change_with_log(log, delivery_state.AnsweredQuestion("second", "Which side?", reply))
                second = replace(second, key=first.key)
                for _ in range(2):
                    self.assertEqual(self.migrate_change(second)[0], 0)
                    row = the_list.load(KEEPER.id, f"L{number}")
                    first_saved, second_saved = row.filed_exchanges
                    self.assertTrue(first_saved.exchange_id.startswith("log:sha256:"))
                    self.assertEqual(second_saved.exchange_id, "question:second")
                    self.assertEqual(second_saved.answer, list_exchange.ExchangeAnswer(
                        reply, list_exchange.TimeNotRecorded.ABSENT, list_exchange.AnswerRead.UNKNOWN))
                    handoff = self.check_change_handoff(second, frozenset({1}), unread=False)
                    self.assertEqual(handoff.filed_slot_id, "second")

    def test_investigation_first_filing_after_two_replies_keeps_the_pairs(self) -> None:
        with (patch.object(console_text, "require"),
              patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed."))):
            report = investigation.file(OWNER.id, "Choose a route", "Which route?", waiting_why="Check the route.")
            _ = investigation.answer(OWNER.id, report.id, "Use the north route.")
            _ = investigation.review_action(OWNER.id, report.id, "question", "How long?")
            _ = investigation.answer(OWNER.id, report.id, "Ten minutes.")
        for _ in range(2):
            self.assertEqual(self.migrate(apply=True)[0], 0)
            row, = the_list.records(KEEPER.id)
            self.assertEqual(self.saved_pairs(row), {"Which route?": "Use the north route.", "How long?": "Ten minutes."})
            self.assertEqual(len(row.filed_exchanges), 2)
            self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))

    def test_replacement_investigation_question_owns_the_first_reply(self) -> None:
        kind = new_item_dialog.SubmissionKind.INVESTIGATION
        submission = origin(f"r1-{'0' * 32}-{kind}-{OWNER.id}")
        report = investigation.file(OWNER.id, "Choose a route", "Which route?", waiting_why="Check the route.",
                                    submission=submission)
        proof = reply_tokens.mint()
        _ = investigation.review_action(OWNER.id, report.id, "question", "How long?", new_reply_proof=proof)
        path = investigation.folder(OWNER.id) / (report.id + ".md")
        key = new_item_dialog.read_submission_key(new_item_dialog.submission_key(
            kind, OWNER.id, report.id, submission.request_id))
        with patch.object(console_text, "require"):
            self.assertTrue(investigation.answer_submission(path, key, "Ten minutes.", reply_tokens.derive(proof), owner=OWNER.id))
        self.assertEqual(self.migrate(apply=True)[0], 0)
        row, = the_list.records(KEEPER.id)
        self.assertEqual(self.saved_pairs(row), {"How long?": "Ten minutes."})
        self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))

    def saved_pairs(self, row: the_list.ListItem) -> dict[str, str]:
        return {exchange.question: exchange.answer.text for exchange in row.filed_exchanges
                if isinstance(exchange.answer, list_exchange.ExchangeAnswer)}

    def test_investigation_stays_visible_until_the_pending_reply_has_a_receipt(self) -> None:
        with (patch.object(console_text, "require"),
              patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed."))):
            report = investigation.file(OWNER.id, "Choose a route", "Which route?", waiting_why="Check the route.")
            _ = investigation.answer(OWNER.id, report.id, "Use the north route.")
        key = review_items.investigation_record_key(report)
        _ = investigation.review_action(OWNER.id, report.id, "question", "How long?")
        self.assertEqual(self.migrate(apply=True)[0], 0)
        waiting, = review_items.read(self.review, members=MEMBERS)
        self.assertEqual((waiting.key, waiting.state), (key, review_items.ReviewState.WAITING))
        waiting_handoff = waiting.exchange_handoff
        assert isinstance(waiting_handoff, review_items.FiledReviewExchange)
        self.assertFalse(waiting_handoff.hides_card)

        write = atomic_file.write_text_atomic

        def fail_receipt(path: Path, words: str, *, new_file_mode: int) -> None:
            if path.suffix == ".saved":
                raise OSError("The receipt write failed.")
            write(path, words, new_file_mode=new_file_mode)

        with patch.object(console_text, "require"), patch.object(the_list, "write_text_atomic", fail_receipt):
            _ = investigation.answer(OWNER.id, report.id, "Ten minutes.")
        visible, = review_items.read(self.review, members=MEMBERS, show="open")
        self.assertEqual(visible.key, key)
        self.assertTrue(visible.unread_answer)
        inputs = review_page.ReviewBodyInputs(change_page.ReviewBrowser("test"), MEMBERS, 4, {}, user_zone(),
                                             staff_names.StaffNames(MEMBERS))
        self.assertIn("Ten minutes.", review_page.details(visible, inputs))
        self.assertEqual(self.migrate(apply=True)[0], 0)
        self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))

    def test_suspect_investigation_filing_leaves_the_later_reply_in_review(self) -> None:
        with (patch.object(console_text, "require"),
              patch.object(the_list, "file_exchanges", side_effect=OSError("The List save failed."))):
            report = investigation.file(OWNER.id, "Choose a route", "Which route?", waiting_why="Check the route.")
            _ = investigation.answer(OWNER.id, report.id, "Use the north route.")
            _ = investigation.review_action(OWNER.id, report.id, "question", "How long?")
            report = investigation.answer(OWNER.id, report.id, "Ten minutes.")
        key = review_items.investigation_record_key(report)
        filed = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, key, "initial",
            list_exchange.Direction.ANSWER_TO_NATE, report.question, datetime.fromisoformat(report.filed),
            list_exchange.Nate.USER, OWNER.id,
            list_exchange.ExchangeAnswer(report.answer, datetime.fromisoformat(report.answered), list_exchange.AnswerRead.UNREAD),
            binding=list_exchange.ExchangeBinding(list_exchange.QuestionRecord("initial"),
                                                   list_exchange.ReplyWithoutEvent.NATIVE_RECORD))
        with the_list.lock_exchange_source(key) as locked:
            row = the_list.file_exchanges(locked, (filed,), report.title, linked=the_list.NextNumber.AVAILABLE,
                                          owner=OWNER.id, members=MEMBERS)
        result, preview = self.migrate(apply=True)
        self.assertEqual(result, 0)
        self.assertIn(f"  {key}: 2", preview)
        visible, = review_items.read(self.review, members=MEMBERS, show="open")
        self.assertTrue(visible.unread_answer)
        self.assertEqual(the_list.load(row.keeper, f"L{row.number}").filed_exchanges,
                         (list_exchange.FiledExchange.from_exchange(filed),))

    def test_native_reply_times_claim_only_one_verified_event(self) -> None:
        for number, count in enumerate((0, 1, 2), 1):
            with self.subTest(count=count):
                reply: update_registration.FollowupExchange = {
                    "followup": "How long?", "reply": "Ten minutes.", "replied_at": FIRST_REPLY.isoformat(),
                }
                log = tuple(ReviewLog(FIRST_REPLY, ASKER.id, "Answer: Ten minutes.") for _ in range(count))
                key = self.native(f"time-{number}", {
                    "since": FILED, "text": "Choose a route", "question": "Which route?", "review_id": f"Q{number}",
                    "review_submission": new_item_dialog.stored_submission(origin(f"request-123456789{number}")),
                    "followups": [reply], "review_log": encode((*log, ReviewLog(READ, "nate", "Marked read"))),
                }, asker=OWNER.id)
                inventory = answer_sources.read_exchanges(self.review, members=MEMBERS)
                exchange, = (row for row in inventory.exchanges if row.source_key.endswith(f"request-123456789{number}"))
                self.assertEqual(exchange.answerer, ASKER.id if count == 1 else OWNER.id)
                self.assertEqual(exchange.question, "How long?")
                assert isinstance(exchange.binding, list_exchange.ExchangeBinding)
                self.assertEqual(isinstance(exchange.binding.reply, list_exchange.LoggedReply), count == 1)
                self.assertEqual(dict(inventory.unresolved).get(exchange.source_key, 0), count if count != 1 else 0)
                self.assertEqual(self.migrate(apply=True)[0], 0)
                visible = {item.key for item in review_items.read(self.review, members=MEMBERS, show="open")}
                self.assertEqual(key in visible, count > 1)

    def test_native_replies_cannot_reuse_one_event_or_hide_an_unmatched_reply(self) -> None:
        log = (ReviewLog(FIRST_REPLY, ASKER.id, "Answer: Ten minutes."),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: Use the north route."))
        request = "request-1234567890"
        fields: update_registration.OutstandingItem = {
            "since": FILED, "text": "Choose a route", "question": "Which route?", "review_id": "Q1",
            "review_submission": new_item_dialog.stored_submission(origin(request)),
            "followups": [{"followup": "How long?", "reply": "Ten minutes.", "replied_at": FIRST_REPLY.isoformat()},
                          {"followup": "Is that the limit?", "reply": "Ten minutes.", "replied_at": FIRST_REPLY.isoformat()}],
            "review_log": encode(log),
        }
        key = self.native("competing", fields, asker=OWNER.id)
        source = f"submission/question/{request}"
        inventory = answer_sources.read_exchanges(self.review, members=MEMBERS)
        self.assertEqual(inventory.unresolved, ((source, 2),))
        for exchange in inventory.exchanges:
            assert isinstance(exchange.binding, list_exchange.ExchangeBinding)
            self.assertIs(exchange.binding.reply, list_exchange.ReplyWithoutEvent.NATIVE_RECORD)
        self.assertEqual(self.migrate(apply=True)[0], 0)
        item, = review_items.read(self.review, members=MEMBERS, show="open")
        self.assertEqual(item.key, key)
        handoff = item.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertFalse(handoff.answer_events)
        self.assertEqual(len(handoff.exchange_ids), 2)
        self.assertIn(log[-1], item.log)
        _ = self.native("competing", {**fields, "reply_log_positions": {"0": 0}}, asker=OWNER.id)
        inventory = answer_sources.read_exchanges(self.review, members=MEMBERS)
        self.assertEqual(inventory.unresolved, ((source, 1),))
        self.assertEqual(self.migrate(apply=True)[0], 0)
        current, = review_items.read(self.review, members=MEMBERS, show="open")
        handoff = current.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertEqual(len(handoff.answer_events), 1)
        self.assertIn(log[-1], current.log)

    def test_old_binding_row_pending_write_and_receipt_migrate_in_place(self) -> None:
        log = (ReviewLog(START, "nate", "question: Which side?", "saved"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: The east side.", question_path.answer_log_id("saved")))
        legacy = {"question": {"kind": "event", "position": 0, "tag": "saved", "slot_id": ""},
                  "reply": {"kind": "tag", "tag": question_path.answer_log_id("saved")}}
        for number, pending in enumerate((False, True), 1):
            with self.subTest(pending=pending):
                item = self.change_with_log(log, delivery_state.AnsweredQuestion(
                    "saved", "Which side?", "The east side."))
                item = replace(item, key=f"fixture/C{number}", answer_time=FIRST_REPLY)
                with (patch.object(list_exchange, "_store_binding", return_value=legacy),
                      patch.object(the_list, "_save", side_effect=OSError("The List write failed.")) if pending
                      else nullcontext()):
                    self.assertEqual(self.migrate_change(item)[0], int(pending))
                with the_list.lock_exchange_source(item.key) as locked:
                    journal = locked.reservation.with_suffix(".pending")
                    receipt = locked.reservation.with_suffix(".saved")
                saved, = the_list.exchange_history(item.key)
                self.assertTrue(saved.legacy_binding)
                raw = list_exchange.encode((saved,))
                digest = hashlib.sha256(raw.encode()).hexdigest()
                if pending:
                    self.assertEqual(journal.read_text(), raw)
                    self.assertFalse(receipt.exists())
                else:
                    self.assertEqual(receipt.read_text(), f"{KEEPER.id}/L{number} {digest}\n")
                    _ = self.check_change_handoff(item, frozenset({1}), unread=False)

                    def fail_receipt(path: Path, words: str, *, new_file_mode: int) -> None:
                        if path == receipt:
                            raise OSError("The replacement receipt write failed.")
                        atomic_file.write_text_atomic(path, words, new_file_mode=new_file_mode)

                    with patch.object(the_list, "write_text_atomic", fail_receipt):
                        self.assertEqual(self.migrate_change(item)[0], 1)
                    self.assertIn(digest, receipt.read_text())
                    projected, = answer_sources.apply_review_handoffs([item], user_zone(), members=MEMBERS)
                    self.assertIs(projected.exchange_handoff, review_items.NoFiledReviewExchange.ABSENT)
                    self.assertTrue(projected.unread_answer)
                self.assertEqual(self.migrate_change(item)[0], 0)
                upgraded, = the_list.load(KEEPER.id, f"L{number}").filed_exchanges
                self.assertEqual(upgraded, replace(saved, legacy_binding=""))
                self.assertFalse(journal.exists())
                self.assertNotIn(digest, receipt.read_text())
                handoff = self.check_change_handoff(item, frozenset({1}), unread=False)
                self.assertEqual(handoff.destination, investigation.ListDestination(KEEPER.id, number))
                before = receipt.read_bytes()
                self.assertEqual(self.migrate_change(item)[0], 0)
                self.assertEqual(receipt.read_bytes(), before)

    def legacy_report(self, number: int, log: tuple[ReviewLog, ...], *,
                      answer: str = "Use the north route.", read: str = "",
                      kind: investigation.InvestigationKind = investigation.InvestigationKind.BUG) -> str:
        store = record_store.NumberedRecords(OWNER.id, "investigations", "I", "investigation",
                                             investigation.InvestigationError)
        store.folder().mkdir(parents=True, exist_ok=True)
        identity = f"I{number}"
        question = "Which route?"
        answered_at = max(event.time for event in log if event.words.startswith("Answer: "))
        store.write(identity, {"id": identity, "owner": OWNER.id, "title": "Choose a route", "kind": kind,
                               "state": investigation.InvestigationState.READ if read else investigation.InvestigationState.ANSWER_READY,
                               "filed": START.isoformat(), "answered": answered_at.isoformat(), "read": read,
                               "question_length": str(len(question)), "review_log": encode(log)},
                    question + record_store.section("Answer") + answer)
        return f"investigation/{OWNER.id}/{identity}"

    def test_legacy_bug_initial_reply_uses_native_question(self) -> None:
        keys: list[str] = []
        expected: dict[str, list_exchange.ExchangeAnswer] = {}
        for number in (2, 3, 4):
            read = number == 3
            reply = f"Use route {number}."
            log = (ReviewLog(START, "nate", "Sent"),
                   ReviewLog(START + timedelta(minutes=1), OWNER.id, "Started checking."),
                   ReviewLog(FIRST_REPLY, OWNER.name, "Answer: " + reply))
            if read:
                log += (ReviewLog(READ, "nate", "Marked read"),)
            if number == 4:
                log += (ReviewLog(READ, "nate", "feedback: Check the result."),)
            key = self.legacy_report(number, log, answer=reply, read=READ.isoformat() if read else "")
            keys.append(key)
            expected[key] = list_exchange.ExchangeAnswer(
                reply, FIRST_REPLY, list_exchange.AnswerRead.READ if read else list_exchange.AnswerRead.UNREAD)
        self.assertEqual({item.key for item in review_items.read(self.review, members=MEMBERS)}, set(keys))
        for _ in range(2):
            self.assertEqual(self.migrate(apply=True)[0], 0)
            exchanges = [entry for item in the_list.records(KEEPER.id) for entry in item.filed_exchanges]
            self.assertEqual({entry.source_key for entry in exchanges}, set(keys))
            self.assertEqual(len(exchanges), len(keys))
            for entry in exchanges:
                self.assertEqual((entry.exchange_id, entry.question, entry.asked_at, entry.answerer, entry.answer),
                                 ("initial", "Which route?", START, OWNER.id, expected[entry.source_key]))
            self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))
            archived = review_items.read(self.review, members=MEMBERS)
            self.assertEqual({item.key for item in archived}, set(keys))
            for item in archived:
                handoff = item.exchange_handoff
                assert isinstance(handoff, review_items.FiledReviewExchange)
                self.assertTrue(handoff.hides_card)
                self.assertEqual(handoff.answer_events, frozenset({3}))

    def test_legacy_bug_initial_reply_rejects_conflicts(self) -> None:
        answer = ReviewLog(FIRST_REPLY, OWNER.id, "Answer: Use the north route.")
        cases = (
            ((ReviewLog(START, "nate", "question: Which other route?"), answer), "Use the north route."),
            ((ReviewLog(START, "nate", "feedback: Check the map."), answer), "Use the north route."),
            ((replace(answer, who=ASKER.id),), "Use the north route."),
            ((answer, replace(answer, time=SECOND_REPLY)), "Use the north route."),
            ((answer,), "Use the west route."),
        )
        keys = {self.legacy_report(number, log, answer=body)
                for number, (log, body) in enumerate(cases, 1)}
        result, preview = self.migrate(apply=True)
        self.assertEqual(result, 0)
        self.assertIn("Unresolved legacy replies: 6", preview)
        self.assertFalse(the_list.records(KEEPER.id))
        visible = review_items.read(self.review, members=MEMBERS, show="open")
        self.assertEqual({item.key for item in visible if item.unread_answer}, keys)

    def test_legacy_bug_keeps_initial_and_followup_answers(self) -> None:
        log = (ReviewLog(START, "nate", "Sent"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: Use the north route."),
               ReviewLog(SECOND_ASK, "nate", "question: How long?"),
               ReviewLog(SECOND_REPLY, OWNER.id, "Answer: Ten minutes."))
        _ = self.legacy_report(1, log, answer="Ten minutes.")
        for _ in range(2):
            self.assertEqual(self.migrate(apply=True)[0], 0)
            row, = the_list.records(KEEPER.id)
            self.assertEqual(self.saved_pairs(row), {"Which route?": "Use the north route.", "How long?": "Ten minutes."})
            initial, later = row.filed_exchanges
            self.assertEqual(initial.exchange_id, "initial")
            self.assertTrue(later.exchange_id.startswith("log:sha256:"))
            self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))

    def test_native_interval_recovers_legacy_metadata(self) -> None:
        for number, tag in enumerate(("", "Q1"), 1):
            prompt = ReviewLog(SECOND_ASK, "nate", "followup: How long?", tag)
            reply = ReviewLog(SECOND_REPLY, ASKER.name, "Answer: Ten minutes.")
            log = (prompt, ReviewLog(SECOND_ASK + timedelta(minutes=1), OWNER.id, "Started checking."), reply)
            followup: update_registration.FollowupExchange = {"followup": "How long?", "reply": "Ten minutes."}
            if tag:
                followup["question_id"] = tag
                log += (ReviewLog(SECOND_REPLY + timedelta(minutes=1), "nate", "feedback sent: Thanks."),)
            key = self.native(f"interval-{number}", {
                "since": FILED, "text": "Choose a route", "question": "Which route?",
                "followups": [followup], "review_log": encode(log),
            })
            self.assertEqual(self.migrate(apply=True)[0], 0)
            saved, = (exchange for exchange in the_list.exchange_history(key) if exchange.exchange_id == "followup:1")
            self.assertEqual((saved.question, saved.asked_at, saved.answerer, saved.answer),
                             ("How long?", SECOND_ASK, ASKER.id, list_exchange.ExchangeAnswer(
                                 "Ten minutes.", SECOND_REPLY,
                                 list_exchange.AnswerRead.READ if tag else list_exchange.AnswerRead.UNREAD)))
            self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))

    def test_native_interval_respects_stronger_evidence(self) -> None:
        original: update_registration.FollowupExchange = {"followup": "How long?", "reply": "Ten minutes."}
        prompt = ReviewLog(START, "nate", "question: How long?")
        reply = ReviewLog(FIRST_REPLY, ASKER.id, "Answer: Ten minutes.")
        timed: update_registration.FollowupExchange = {**original, "replied_at": SECOND_REPLY.isoformat()}
        tagged: update_registration.FollowupExchange = {**original, "question_id": "Q1"}
        cases = (
            ((prompt, reply), [timed]),
            ((replace(prompt, feedback_request_id="Q2"), reply), [tagged]),
            ((prompt, replace(reply, feedback_request_id=question_path.answer_log_id("Q2"))), [tagged]),
            ((prompt, replace(prompt, time=START + timedelta(minutes=1)), reply), [original]),
            ((prompt, ReviewLog(START + timedelta(minutes=1), "nate", "feedback: Check again."), reply), [original]),
            ((prompt, reply), [original, original]),
        )
        for number, (log, followups) in enumerate(cases, 1):
            key = self.native(f"conflict-{number}", {
                "since": FILED, "text": "Choose a route", "question": "Which route?",
                "followups": followups, "review_log": encode(log),
            })
            self.assertEqual(self.migrate(apply=True)[0], 0)
            pairs = [saved for saved in the_list.exchange_history(key) if saved.exchange_id.startswith("followup:")]
            self.assertEqual([(pair.question, pair.answerer) for pair in pairs], [("How long?", OWNER.id)] * len(followups))
            for pair in pairs:
                assert isinstance(pair.answer, list_exchange.ExchangeAnswer)
                assert isinstance(pair.binding, list_exchange.ExchangeBinding)
                self.assertEqual(pair.answer.text, "Ten minutes.")
                self.assertIs(pair.binding.reply, list_exchange.ReplyWithoutEvent.NATIVE_RECORD)
            visible = {item.key: item for item in review_items.read(self.review, members=MEMBERS, show="open")}
            handoff = visible[key].exchange_handoff
            assert isinstance(handoff, review_items.FiledReviewExchange)
            self.assertFalse(handoff.answer_events)
            self.assertEqual(dict(answer_sources.read_exchanges(self.review, members=MEMBERS).unresolved)[key], 1)

    def test_handoff_survives_reply_relocation(self) -> None:
        reply = ReviewLog(FIRST_REPLY, OWNER.id, "Answer: Use the north route.")
        key = self.legacy_report(1, (reply,), kind=investigation.InvestigationKind.INVESTIGATION)
        self.assertEqual(self.migrate(apply=True)[0], 0)
        saved, = the_list.exchange_history(key)
        with the_list.lock_exchange_source(key) as locked:
            receipt = locked.reservation.with_suffix(".saved")
        original = receipt.read_bytes()
        note = ReviewLog(START, OWNER.id, "Started checking.")
        _ = self.legacy_report(1, (note, reply), kind=investigation.InvestigationKind.INVESTIGATION)
        self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))
        self.assertEqual(self.migrate(apply=True)[0], 0)
        self.assertEqual(the_list.exchange_history(key), (saved,))
        self.assertEqual(receipt.read_bytes(), original)
        receipt.unlink()
        item, = review_items.read(self.review, members=MEMBERS, show="open")
        self.assertTrue(item.unread_answer)
        self.assertIs(item.exchange_handoff, review_items.NoFiledReviewExchange.ABSENT)
        _ = receipt.write_bytes(original)
        _ = self.legacy_report(1, (note, reply, reply), kind=investigation.InvestigationKind.INVESTIGATION)
        item, = review_items.read(self.review, members=MEMBERS, show="open")
        self.assertTrue(item.unread_answer)
        self.assertIs(item.exchange_handoff, review_items.NoFiledReviewExchange.ABSENT)
        self.assertEqual(the_list.exchange_history(key), (saved,))
        self.assertEqual(receipt.read_bytes(), original)

    def test_moved_question_does_not_block_next_reply(self) -> None:
        first_pair = (ReviewLog(SECOND_ASK, "nate", "question: How long?"),
                      ReviewLog(SECOND_REPLY, OWNER.id, "Answer: Ten minutes."))
        log = (ReviewLog(FIRST_REPLY, OWNER.id, "Answer: Use the north route."), *first_pair)
        key = self.legacy_report(1, log, answer="Ten minutes.", kind=investigation.InvestigationKind.INVESTIGATION)
        self.assertEqual(self.migrate(apply=True)[0], 0)
        before = the_list.exchange_history(key)
        initial, later = before
        self.assertEqual(initial.exchange_id, "initial")
        self.assertTrue(later.exchange_id.startswith("log:sha256:"))
        second_pair = (ReviewLog(SECOND_REPLY + timedelta(minutes=1), "nate", "question: Which side?"),
                       ReviewLog(SECOND_REPLY + timedelta(minutes=2), OWNER.id, "Answer: The east side."))
        log = (ReviewLog(START, OWNER.id, "Started checking."), *log, *second_pair)
        _ = self.legacy_report(1, log, answer="The east side.", kind=investigation.InvestigationKind.INVESTIGATION)
        pending, = review_items.read(self.review, members=MEMBERS, show="open")
        self.assertTrue(pending.unread_answer)
        handoff = pending.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertEqual(handoff.answer_events, frozenset({2, 4}))
        self.assertEqual(self.migrate(apply=True)[0], 0)
        row, = the_list.records(KEEPER.id)
        self.assertEqual(self.saved_pairs(row), {"Which route?": "Use the north route.",
                                                "How long?": "Ten minutes.", "Which side?": "The east side."})
        self.assertEqual(the_list.exchange_history(key)[:2], before)
        self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))

    def test_native_reply_cannot_take_anonymous_interval_with_conflicting_later_tag(self) -> None:
        prompt = ReviewLog(START, "nate", "question: How long?")
        reply = ReviewLog(FIRST_REPLY, ASKER.id, "Answer: Ten minutes.")
        conflict = ReviewLog(SECOND_ASK, "nate", "question: Which route?", "Q1")
        key = self.native("later-tag-conflict", {
            "since": FILED, "text": "Choose a route", "question": "Pick a plan?",
            "followups": [{"followup": "How long?", "reply": "Ten minutes.", "question_id": "Q1"}],
            "review_log": encode((prompt, reply, conflict)),
        })
        result, preview = self.migrate(apply=True)
        self.assertEqual(result, 0)
        self.assertIn(f"  {key}: 1", preview)
        saved, = (entry for entry in the_list.exchange_history(key) if entry.exchange_id == "followup:1")
        self.assertEqual((saved.question, saved.answerer), ("How long?", OWNER.id))
        self.assertEqual(saved.answer, list_exchange.ExchangeAnswer(
            "Ten minutes.", list_exchange.TimeNotRecorded.ABSENT, list_exchange.AnswerRead.UNKNOWN))
        self.assertEqual(saved.binding, list_exchange.ExchangeBinding(
            list_exchange.QuestionRecord("followup:1"), list_exchange.ReplyWithoutEvent.NATIVE_RECORD))
        visible, = review_items.read(self.review, members=MEMBERS, show="open")
        handoff = visible.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertFalse(handoff.answer_events)
        self.assertTrue(visible.unread_answer)
        self.assertIn(reply, visible.log)

    def test_new_question_can_use_a_moved_questions_old_position(self) -> None:
        first = (ReviewLog(START, "nate", "question: How long?"),
                 ReviewLog(FIRST_REPLY, OWNER.id, "Answer: Ten minutes."))
        second = (ReviewLog(SECOND_ASK, "nate", "question: Which side?"),
                  ReviewLog(SECOND_REPLY, OWNER.id, "Answer: The east side."))
        kind = investigation.InvestigationKind.INVESTIGATION
        log = (ReviewLog(START - timedelta(minutes=2), OWNER.id, "Started checking."),
               ReviewLog(START - timedelta(minutes=1), OWNER.id, "Checked the map."), *first)
        key = self.legacy_report(1, log, answer="Ten minutes.", kind=kind)
        inventory = answer_sources.read_exchanges(self.review, members=MEMBERS)
        candidate, = (value for value in inventory.exchanges if value.source_key == key)
        with the_list.lock_exchange_source(key) as locked:
            _ = the_list.file_exchanges(locked, (replace(candidate, exchange_id="log:2"),), "How long?",
                                       linked=the_list.NextNumber.AVAILABLE, owner=OWNER.id, members=MEMBERS)
            receipt = locked.reservation.with_suffix(".saved")
        saved, = the_list.exchange_history(key)
        self.assertEqual(saved.exchange_id, "log:2")
        original = receipt.read_bytes()
        _ = self.legacy_report(1, (*first, *second), answer="The east side.", kind=kind)
        item, = review_items.read(self.review, members=MEMBERS, show="open")
        handoff = item.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertEqual(handoff.answer_events, frozenset({2}))
        self.assertTrue(item.unread_answer)
        ids: list[str] = []
        for _ in range(2):
            self.assertEqual(self.migrate(apply=True)[0], 0)
            previous, added = the_list.exchange_history(key)
            self.assertEqual(previous, saved)
            self.assertNotEqual(added.exchange_id, saved.exchange_id)
            ids.append(added.exchange_id)
            row, = the_list.records(KEEPER.id)
            self.assertEqual(self.saved_pairs(row), {"How long?": "Ten minutes.", "Which side?": "The east side."})
            self.assertTrue(receipt.read_bytes().startswith(original))
            self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))
        self.assertEqual(ids[0], ids[1])

    def test_legacy_bug_feedback_reply_does_not_prove_a_different_initial_answer(self) -> None:
        first = ReviewLog(FIRST_REPLY, OWNER.id, "Answer: Use the north route.")
        last = ReviewLog(SECOND_REPLY, OWNER.id, "Answer: The west route is faster.")
        for number, request in enumerate(("feedback: Check the map.", "question: Which side?"), 1):
            log = (ReviewLog(START, "nate", "Sent"), first,
                   ReviewLog(SECOND_ASK, "nate", request),
                   last if number == 1 else replace(last, who=ASKER.id))
            key = self.legacy_report(number, log, answer="The west route is faster.")
            result, preview = self.migrate(apply=True)
            self.assertEqual(result, 0)
            self.assertIn(f"  {key}: 2", preview)
            self.assertFalse(the_list.exchange_history(key))
            visible = {item.key: item for item in review_items.read(self.review, members=MEMBERS, show="open")}
            self.assertTrue(visible[key].unread_answer)
            self.assertIs(visible[key].exchange_handoff, review_items.NoFiledReviewExchange.ABSENT)

    def test_change_history_files_three_verified_pairs_and_keeps_ambiguous_slot_reply(self) -> None:
        words = (("Which part?", "The filter."), ("When?", "At noon."),
                 ("Who checks?", "The owner."), ("Is it ready?", "Yes."))
        log = tuple(event for index, (prompt, reply) in enumerate(words)
                    for event in (ReviewLog(START + timedelta(minutes=2 * index), "nate", "question: " + prompt),
                                  ReviewLog(START + timedelta(minutes=2 * index + 1), OWNER.name, "Answer: " + reply)))
        item = self.change_with_log(log, delivery_state.AnsweredQuestion("current", *words[-1]))
        item = replace(item, answer_time=log[-1].time)
        for _ in range(2):
            result, preview = self.migrate_change(item)
            self.assertEqual(result, 0)
            self.assertIn(f"  {item.key}: 1", preview)
            row, = the_list.records(KEEPER.id)
            self.assertEqual(self.saved_pairs(row), dict(words))
            saved = {exchange.exchange_id: exchange for exchange in row.filed_exchanges}
            history = [exchange for exchange in row.filed_exchanges if exchange.exchange_id.startswith("log:sha256:")]
            self.assertEqual([(exchange.question, exchange.answerer) for exchange in history],
                             [(prompt, OWNER.id) for prompt, _ in words[:-1]])
            self.assertEqual(saved["question:current"].binding, list_exchange.ExchangeBinding(
                list_exchange.QuestionSlot("current"), list_exchange.ReplyWithoutEvent.SLOT))
            _ = self.check_change_handoff(item, frozenset({1, 3, 5}), unread=True)

    def test_change_history_keeps_feedback_response_separate(self) -> None:
        feedback = review_items.CompletedFeedback("feedback", "Check the map.", "The route is safe.")
        log = (ReviewLog(START, "nate", "question: Which route?"),
               ReviewLog(FIRST_REPLY, OWNER.name, "Answer: Use the north route."),
               ReviewLog(SECOND_ASK, "nate", "feedback: " + feedback.text),
               ReviewLog(SECOND_REPLY, OWNER.name, "Answer: " + feedback.answer))
        item = self.change_with_log(log, feedback)
        result, preview = self.migrate_change(item)
        self.assertEqual(result, 0)
        self.assertIn("Unresolved legacy replies: 0", preview)
        row, = the_list.records(KEEPER.id)
        self.assertEqual(self.saved_pairs(row), {"Which route?": "Use the north route."})
        _ = self.check_change_handoff(item, frozenset({1}), unread=True)
        uncertain = self.change_with_log((log[0], replace(log[1], who="Unknown author"), *log[2:]), feedback)
        uncertain = replace(uncertain, key="fixture/C2")
        result, preview = self.migrate_change(uncertain)
        self.assertEqual(result, 0)
        self.assertIn(f"  {uncertain.key}: 1", preview)
        self.assertFalse(the_list.exchange_history(uncertain.key))
        projected, = answer_sources.apply_review_handoffs([uncertain], user_zone(), members=MEMBERS)
        self.assertTrue(projected.unread_answer)

    def test_sent_question_recovers_initial_reply_before_the_next_request(self) -> None:
        reply = ReviewLog(FIRST_REPLY, OWNER.name, "Answer: Use the north route.")
        approval = ReviewLog(READ, "nate", "answer: Proceed.")
        extra = (ReviewLog(SECOND_ASK, "nate", "question: How long?"),
                 ReviewLog(SECOND_REPLY, OWNER.id, "Answer: Ten minutes."))
        original: update_registration.FollowupExchange = {"followup": "Which route?", "reply": "Use the north route."}
        later: update_registration.FollowupExchange = {"followup": "How long?", "reply": "Ten minutes."}
        for number, log in enumerate(((reply, approval), (reply, *extra)), 1):
            request = f"request-123456789{number}"
            key = self.native(f"sent-initial-{number}", {
                "since": FILED, "text": "Choose a route", "question": "Which route?", "review_id": f"Q{number}",
                "review_submission": new_item_dialog.stored_submission(origin(request)), "review_log": encode(log),
                "followups": [original] if number == 1 else [original, later],
            }, asker=OWNER.id, status="closed" if number == 1 else "open")
            self.assertEqual(self.migrate(apply=True)[0], 0)
            entries = the_list.exchange_history(f"submission/question/{request}")
            self.assertEqual([entry.question for entry in entries],
                             ["Which route?"] if number == 1 else ["Which route?", "How long?"])
            for position, saved in enumerate(entries):
                expected = original if position == 0 else later
                self.assertEqual(saved.answerer, OWNER.id)
                self.assertEqual(saved.answer, list_exchange.ExchangeAnswer(
                    expected["reply"], FIRST_REPLY if position == 0 else SECOND_REPLY,
                    list_exchange.AnswerRead.READ if number == 1 else list_exchange.AnswerRead.UNREAD))
            self.assertEqual(entries[0].asked_at, START)
            self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))
            all_items = {item.key: item for item in review_items.read(self.review, members=MEMBERS)}
            handoff = all_items[key].exchange_handoff
            assert isinstance(handoff, review_items.FiledReviewExchange)
            self.assertTrue(handoff.hides_card)
            self.assertEqual(len(handoff.answer_events), number)

    def test_closed_staff_question_recovers_verified_log_pair_without_followups(self) -> None:
        log = (ReviewLog(START, "nate", "question: How long?\nPlease check."),
               ReviewLog(FIRST_REPLY, ASKER.name, "Answer: Ten minutes."),
               ReviewLog(READ, "nate", "answer: Proceed."))
        fields: update_registration.OutstandingItem = {
            "since": FILED, "text": "Choose a route", "question": "Which route?",
            "waiting_why": "How long? Please check.", "review_log": encode(log),
        }
        key = self.native("closed-staff", fields, status="closed")
        for _ in range(2):
            self.assertEqual(self.migrate(apply=True)[0], 0)
            entries = {entry.exchange_id: entry for entry in the_list.exchange_history(key)}
            saved = entries["legacy-followup"]
            self.assertEqual((saved.question, saved.asked_at, saved.answerer), ("How long?\nPlease check.", START, ASKER.id))
            self.assertEqual(saved.answer, list_exchange.ExchangeAnswer("Ten minutes.", FIRST_REPLY, list_exchange.AnswerRead.READ))
            self.assertEqual(entries["initial"].answer, list_exchange.ExchangeAnswer(
                "Proceed.", READ, list_exchange.AnswerRead.NOT_TRACKED))
            item, = review_items.read(self.review, members=MEMBERS)
            handoff = item.exchange_handoff
            assert isinstance(handoff, review_items.FiledReviewExchange)
            self.assertTrue(handoff.hides_card)
            self.assertEqual(handoff.answer_events, frozenset({2}))
        other = self.native("closed-staff-other", {**fields, "waiting_why": "A different reason."}, status="closed")
        result, preview = self.migrate(apply=True)
        self.assertEqual(result, 0)
        self.assertIn(f"  {other}: 1", preview)
        self.assertEqual([entry.exchange_id for entry in the_list.exchange_history(other)], ["initial"])
        items = {item.key: item for item in review_items.read(self.review, members=MEMBERS)}
        unresolved = items[other].exchange_handoff
        assert isinstance(unresolved, review_items.FiledReviewExchange)
        self.assertFalse(unresolved.answer_events)
        self.assertFalse(unresolved.hides_card)

    def test_duplicate_native_tag_keeps_both_saved_pairs_unbound(self) -> None:
        log = (ReviewLog(START, "nate", "question: How long?"),
               ReviewLog(FIRST_REPLY, ASKER.id, "Answer: Ten minutes."))
        key = self.native("duplicate-tag", {
            "since": FILED, "text": "Choose a route", "question": "Which route?", "review_log": encode(log),
            "followups": [{"followup": "How long?", "reply": "Ten minutes.", "question_id": "Q1"},
                          {"followup": "Which side?", "reply": "East.", "question_id": "Q1"}],
        })
        self.assertEqual(self.migrate(apply=True)[0], 0)
        replies = [entry for entry in the_list.exchange_history(key) if entry.exchange_id.startswith("followup:")]
        self.assertEqual([(entry.exchange_id, entry.question, entry.answerer) for entry in replies], [
            ("followup:1", "How long?", OWNER.id), ("followup:2", "Which side?", OWNER.id)])
        self.assertEqual([entry.answer for entry in replies], [
            list_exchange.ExchangeAnswer(text, list_exchange.TimeNotRecorded.ABSENT, list_exchange.AnswerRead.UNKNOWN)
            for text in ("Ten minutes.", "East.")])
        for entry in replies:
            self.assertEqual(entry.binding, list_exchange.ExchangeBinding(list_exchange.QuestionRecord(entry.exchange_id),
                                                                          list_exchange.ReplyWithoutEvent.NATIVE_RECORD))
        item, = review_items.read(self.review, members=MEMBERS, show="open")
        handoff = item.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertFalse(handoff.answer_events)
        self.assertIn(log[1], item.log)

    def test_moved_legacy_row_recovers_its_id_before_filing_new_reply(self) -> None:
        first = (ReviewLog(START, "nate", "question: How long?"),
                 ReviewLog(FIRST_REPLY, OWNER.id, "Answer: Ten minutes."))
        second = (ReviewLog(SECOND_ASK, "nate", "question: Which side?"),
                  ReviewLog(SECOND_REPLY, OWNER.id, "Answer: East."))
        key = self.legacy_report(1, (*first, *second), answer="East.", kind=investigation.InvestigationKind.INVESTIGATION)
        candidate = next(entry for entry in answer_sources.read_exchanges(self.review, members=MEMBERS).exchanges
                         if entry.source_key == key and entry.question == "How long?")
        old = replace(candidate, exchange_id="log:2", binding=list_exchange.UnboundExchange.LEGACY)
        with the_list.lock_exchange_source(key) as locked:
            _ = the_list.file_exchanges(locked, (old,), "How long?", linked=the_list.NextNumber.AVAILABLE,
                                       owner=OWNER.id, members=MEMBERS)
            receipt = locked.reservation.with_suffix(".saved")
        original = receipt.read_bytes()
        self.assertEqual(self.migrate(apply=False)[0], 0)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(self.migrate(apply=True)[0], 0)
        saved = the_list.exchange_history(key)
        self.assertEqual([(entry.question, entry.answerer) for entry in saved],
                         [("How long?", OWNER.id), ("Which side?", OWNER.id)])
        self.assertEqual(saved[0].exchange_id, "log:2")
        self.assertTrue(saved[1].exchange_id.startswith("log:sha256:"))
        self.assertEqual(saved[0].answer, old.answer)
        row, = the_list.records(KEEPER.id)
        destination = investigation.ListDestination(KEEPER.id, row.number)
        self.assertEqual(the_list.confirmed_exchanges(key, destination, saved), frozenset(saved))
        upgraded = receipt.read_bytes()
        self.assertEqual(self.migrate(apply=True)[0], 0)
        self.assertEqual(the_list.exchange_history(key), saved)
        self.assertEqual(receipt.read_bytes(), upgraded)
        self.assertFalse(review_items.read(self.review, members=MEMBERS, show="open"))

    def test_bug_replacement_uses_the_same_ownership_rules_as_history(self) -> None:
        first = ReviewLog(FIRST_REPLY, OWNER.id, "Answer: Use the north route.")
        request = ReviewLog(SECOND_ASK, "nate", "question: Which side?", "Q1")
        last = ReviewLog(SECOND_REPLY, OWNER.id, "Answer: East.", "answer:Q1")
        later = ReviewLog(SECOND_REPLY, "nate", "question: How long?", "Q1")
        for number, (log, saved) in enumerate((((first, request, last), "West."),
                                                ((first, request, later, last), "East.")), 1):
            key = self.legacy_report(number, log, answer=saved)
            self.assertEqual(self.migrate(apply=True)[0], 0)
            self.assertFalse(the_list.exchange_history(key))
            visible = {item.key: item for item in review_items.read(self.review, members=MEMBERS, show="open")}
            self.assertIs(visible[key].exchange_handoff, review_items.NoFiledReviewExchange.ABSENT)
            self.assertIn(last, visible[key].log)

    def test_tagged_slot_conflict_keeps_the_log_visible(self) -> None:
        log = (ReviewLog(START, "nate", "question: Which side?", "current"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: East.", "answer:current"))
        item = self.change_with_log(log, delivery_state.AnsweredQuestion("current", "Which side?", "West."))
        assert isinstance(item.source, review_items.ChangeSource)
        item = replace(item, source=replace(item.source, answered_by=OWNER.id))
        self.assertEqual(self.migrate_change(item)[0], 0)
        saved, = the_list.exchange_history(item.key)
        self.assertEqual((saved.exchange_id, saved.question), ("question:current", "Which side?"))
        self.assertEqual(saved.answer, list_exchange.ExchangeAnswer("West.", list_exchange.TimeNotRecorded.ABSENT,
                                                                    list_exchange.AnswerRead.UNKNOWN))
        self.assertEqual(saved.binding, list_exchange.ExchangeBinding(list_exchange.QuestionSlot("current"),
                                                                     list_exchange.ReplyWithoutEvent.SLOT))
        handoff = self.check_change_handoff(item, frozenset(), unread=True)
        self.assertEqual(handoff.filed_slot_id, "current")

    def test_legacy_row_cannot_supply_slot_proof_without_a_reply_time(self) -> None:
        log = (ReviewLog(START, "nate", "question: Which side?"),
               ReviewLog(FIRST_REPLY, OWNER.id, "Answer: East."))
        item = self.change_with_log(log, delivery_state.AnsweredQuestion("current", "Which side?", "West."))
        old = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, item.key, "log:0",
            list_exchange.Direction.ANSWER_TO_NATE, "Which side?", START, list_exchange.Nate.USER, OWNER.id,
            list_exchange.ExchangeAnswer("East.", list_exchange.TimeNotRecorded.ABSENT, list_exchange.AnswerRead.UNKNOWN))
        with the_list.lock_exchange_source(item.key) as locked:
            _ = the_list.file_exchanges(locked, (old,), "Which side?", linked=the_list.NextNumber.AVAILABLE,
                                       owner=OWNER.id, members=MEMBERS)
        self.assertEqual(self.migrate_change(item)[0], 0)
        legacy, current = the_list.exchange_history(item.key)
        self.assertEqual(legacy, list_exchange.FiledExchange.from_exchange(old))
        self.assertEqual((current.exchange_id, current.question), ("question:current", "Which side?"))
        _ = self.check_change_handoff(item, frozenset(), unread=True)

    def test_submitted_initial_pair_cannot_take_a_later_reply(self) -> None:
        prompt = ReviewLog(SECOND_ASK, "nate", "question: Which route?")
        reply = ReviewLog(SECOND_REPLY, OWNER.id, "Answer: North.")
        cases = (("approve: Good.", OWNER.id), ("decline: Wait.", OWNER.id),
                 ("feedback: Check again.", OWNER.id), ("question: Which other route?", OWNER.id),
                 ("superseded followup: Which route?", ASKER.id))
        for number, (boundary, author) in enumerate(cases, 1):
            with self.subTest(boundary=boundary):
                request = f"request-123456789{number}"
                key = self.native(f"initial-boundary-{number}", {
                    "since": FILED, "text": "Choose a route", "question": "Which route?", "review_id": f"Q{number}",
                    "review_submission": new_item_dialog.stored_submission(origin(request)),
                    "review_log": encode((ReviewLog(FIRST_REPLY, "nate", boundary), prompt, replace(reply, who=author))),
                    "followups": [{"followup": "Which route?", "reply": "North."}],
                })
                self.assertEqual(self.migrate(apply=True)[0], 0)
                saved, = the_list.exchange_history(f"submission/question/{request}")
                self.assertEqual((saved.exchange_id, saved.question, saved.asked_at, saved.answerer),
                                 ("followup:1", "Which route?", START, OWNER.id))
                self.assertEqual(saved.binding, list_exchange.ExchangeBinding(list_exchange.QuestionRecord("followup:1"),
                                                                              list_exchange.ReplyWithoutEvent.NATIVE_RECORD))
                visible = {item.key: item for item in review_items.read(self.review, members=MEMBERS, show="open")}
                handoff = visible[key].exchange_handoff
                assert isinstance(handoff, review_items.FiledReviewExchange)
                self.assertFalse(handoff.answer_events)
                self.assertIn(replace(reply, who=author), visible[key].log)

    def test_staff_handoff_survives_a_note_before_its_prompt(self) -> None:
        log = (ReviewLog(START, "nate", "question: How long?"),
               ReviewLog(FIRST_REPLY, ASKER.id, "Answer: Ten minutes."),
               ReviewLog(READ, "nate", "answer: Proceed."))
        fields: update_registration.OutstandingItem = {
            "since": FILED, "text": "Choose a route", "question": "Which route?", "waiting_why": "How long?",
            "review_log": encode(log),
        }
        key = self.native("moved-staff", fields, status="closed")
        self.assertEqual(self.migrate(apply=True)[0], 0)
        saved = the_list.exchange_history(key)
        with the_list.lock_exchange_source(key) as locked:
            receipt = locked.reservation.with_suffix(".saved")
        before = receipt.read_bytes()
        note = ReviewLog(START - timedelta(minutes=1), ASKER.id, "I checked the map.")
        _ = self.native("moved-staff", {**fields, "review_log": encode((note, *log))}, status="closed")
        item, = review_items.read(self.review, members=MEMBERS)
        handoff = item.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertTrue(handoff.hides_card)
        self.assertEqual(handoff.answer_events, frozenset({3}))
        self.assertEqual(self.migrate(apply=True)[0], 0)
        self.assertEqual(the_list.exchange_history(key), saved)
        self.assertEqual(receipt.read_bytes(), before)

    def test_staff_handoff_keeps_a_conflicting_reply_visible(self) -> None:
        prompt = ReviewLog(START, "nate", "question: How long?")
        reply = ReviewLog(FIRST_REPLY, ASKER.id, "Answer: Ten minutes.")
        approval = ReviewLog(READ, "nate", "answer: Proceed.")
        fields: update_registration.OutstandingItem = {
            "since": FILED, "text": "Choose a route", "question": "Which route?", "waiting_why": "How long?",
            "review_log": encode((prompt, reply, approval)),
        }
        key = self.native("staff-reply-conflict", fields, status="closed")
        self.assertEqual(self.migrate(apply=True)[0], 0)
        before = the_list.exchange_history(key)
        changed = replace(reply, words="Answer: Twenty minutes.")
        _ = self.native("staff-reply-conflict", {**fields, "review_log": encode((prompt, changed, approval))},
                        status="closed")
        result, preview = self.migrate(apply=True)
        self.assertEqual(result, 0)
        self.assertIn(f"  {key}: 1", preview)
        self.assertEqual(the_list.exchange_history(key), before)
        item, = review_items.read(self.review, members=MEMBERS)
        handoff = item.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertFalse(handoff.hides_card)
        self.assertFalse(handoff.answer_events)
        self.assertIn(changed, item.log)

    def file_fixture_exchange(self, exchange: list_exchange.Exchange) -> None:
        with the_list.lock_exchange_source(exchange.source_key) as locked:
            _ = the_list.file_exchanges(locked, (exchange,), exchange.question, linked=the_list.NextNumber.AVAILABLE,
                                       owner=OWNER.id, members=MEMBERS)

    def test_copied_reply_digest_cannot_file_under_another_request(self) -> None:
        first = (ReviewLog(START, "nate", "question: Which side?"),
                 ReviewLog(FIRST_REPLY, OWNER.id, "Answer: East."))
        other = ReviewLog(SECOND_ASK, "nate", "question: Which road?")
        slot = delivery_state.OpenQuestion("current", "When?", delivery_state.NoDelivery())
        for number, legacy in enumerate((False, True), 1):
            key = f"fixture/C{number}"
            original = replace(self.change_with_log(first, slot), key=key, answer_time=FIRST_REPLY)
            if legacy:
                self.file_fixture_exchange(list_exchange.Exchange(
                    list_exchange.Source.SCOPED_REPLY, key, "log:2", list_exchange.Direction.ANSWER_TO_NATE,
                    "Which side?", START, list_exchange.Nate.USER, OWNER.id,
                    list_exchange.ExchangeAnswer("East.", FIRST_REPLY, list_exchange.AnswerRead.UNREAD)))
            else:
                self.assertEqual(self.migrate_change(original)[0], 0)
            old, = the_list.exchange_history(key)
            later = replace(self.change_with_log((*first, other, first[1]), slot), key=key, answer_time=FIRST_REPLY)
            status, preview = self.migrate_change(later)
            self.assertEqual(status, 0)
            self.assertIn(f"  {key}: 1", preview)
            saved, = the_list.exchange_history(key)
            self.assertEqual((saved.exchange_id, saved.question, saved.answer), (old.exchange_id, old.question, old.answer))
            self.assertEqual(saved.binding, list_exchange.ExchangeBinding(list_exchange.QuestionEvent(0),
                             list_exchange.LoggedReply(1, answer_sources.event_digest(first[1]))))
            _ = self.check_change_handoff(later, frozenset({1}), unread=True)

    def test_slot_only_filing_reserves_the_reply_against_a_late_log_copy(self) -> None:
        item = self.change_with_log((), delivery_state.AnsweredQuestion("saved", "Which side?", "East."))
        assert isinstance(item.source, review_items.ChangeSource)
        item = replace(item, source=replace(item.source, answered_by=OWNER.id, answered_at=FIRST_REPLY))
        self.assertEqual(self.migrate_change(item)[0], 0)
        old, = the_list.exchange_history(item.key)
        self.assertEqual(old.exchange_id, "question:saved")
        log = (ReviewLog(START, "nate", "question: Which road?"),
               ReviewLog(FIRST_REPLY, OWNER.name, "Answer: East."))
        slot = delivery_state.OpenQuestion("current", "When?", delivery_state.NoDelivery())
        later = replace(self.change_with_log(log, slot), answer_time=FIRST_REPLY)
        status, preview = self.migrate_change(later)
        self.assertEqual(status, 0)
        self.assertIn(f"  {item.key}: 1", preview)
        self.assertEqual(the_list.exchange_history(item.key), (old,))
        handoff = self.check_change_handoff(later, frozenset(), unread=True)
        self.assertFalse(handoff.filed_slot_id)

    def test_foreign_filed_replacement_cannot_prove_a_bug_initial_reply(self) -> None:
        first = ReviewLog(FIRST_REPLY, OWNER.id, "Answer: North.")
        prompt = ReviewLog(SECOND_ASK, "nate", "question: Which side?", "Q1")
        reply = ReviewLog(SECOND_REPLY, ASKER.id, "Answer: East.", "answer:Q1")
        binding = list_exchange.ExchangeBinding(list_exchange.QuestionSlot("Q1"), list_exchange.TaggedReply("answer:Q1"))
        for number, prior in enumerate((binding, list_exchange.UnboundExchange.LEGACY), 1):
            with self.subTest(binding=prior):
                key = self.legacy_report(number, (first, prompt, reply), answer="East.")
                old = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, key, "question:Q1",
                    list_exchange.Direction.ANSWER_TO_NATE, "Which side?", SECOND_ASK, list_exchange.Nate.USER, ASKER.id,
                    list_exchange.ExchangeAnswer("East.", SECOND_REPLY, list_exchange.AnswerRead.UNREAD), binding=prior)
                self.file_fixture_exchange(old)
                status, preview = self.migrate(apply=True)
                self.assertEqual(status, 0)
                self.assertIn(f"  {key}: 2", preview)
                self.assertEqual(the_list.exchange_history(key), (list_exchange.FiledExchange.from_exchange(old),))
                visible = {item.key: item for item in review_items.read(self.review, members=MEMBERS, show="open")}
                self.assertTrue(visible[key].unread_answer)
                self.assertIn(reply, visible[key].log)
                handoff = visible[key].exchange_handoff
                if isinstance(handoff, review_items.FiledReviewExchange):
                    self.assertFalse(handoff.answer_events)
                    self.assertFalse(handoff.filed_investigation_answer)

    def test_open_change_slot_keeps_its_tagged_reply_visible_even_with_a_receipt(self) -> None:
        prompt = ReviewLog(START, "nate", "question: Which side?", "current")
        reply = ReviewLog(FIRST_REPLY, OWNER.id, "Answer: East.", "answer:current")
        open_slot = delivery_state.OpenQuestion("current", "Which side?", delivery_state.NoDelivery())
        item = replace(self.change_with_log((prompt, reply), open_slot), answer_time=FIRST_REPLY)
        status, preview = self.migrate_change(item)
        self.assertEqual(status, 0)
        self.assertIn(f"  {item.key}: 1", preview)
        self.assertFalse(the_list.exchange_history(item.key))
        answer = delivery_state.AnsweredQuestion("current", "Which side?", "East.")
        self.assertEqual(self.migrate_change(self.change_with_log((prompt, reply), answer))[0], 0)
        saved, = the_list.exchange_history(item.key)
        self.assertEqual((saved.exchange_id, saved.question), ("question:current", "Which side?"))
        self.assertEqual(saved.answer, list_exchange.ExchangeAnswer("East.", FIRST_REPLY, list_exchange.AnswerRead.UNREAD))
        self.assertEqual(self.migrate_change(item)[0], 0)
        self.assertEqual(the_list.exchange_history(item.key), (saved,))
        handoff = self.check_change_handoff(item, frozenset(), unread=True)
        self.assertFalse(handoff.filed_slot_id)

    def test_submitted_tagged_initial_requires_owner_and_respects_terminal_boundaries(self) -> None:
        prompt = ReviewLog(START, "nate", "question: Which route?", "Q1")
        reply = ReviewLog(SECOND_REPLY, OWNER.id, "Answer: North.", "answer:Q1")
        cases = (((), ASKER.id), *(((ReviewLog(READ, "nate", words),), OWNER.id) for words in (
            "approve: Continue.", "decline: Stop.", "Superseded followup: Which route?",
            "User chose approve before the answer; superseded the previous wait.",
            "Review C1: works superseded the question Q1 before the answer arrived.")))
        for number, (events, author) in enumerate(cases, 1):
            with self.subTest(events=events, author=author):
                name, request = f"tagged-initial-{number}", f"request-123456789{number}"
                fields: update_registration.OutstandingItem = {
                    "since": FILED, "text": "Choose a route", "question": "Which route?", "review_id": f"Q{number}",
                    "review_submission": new_item_dialog.stored_submission(origin(request)),
                    "review_log": encode((prompt, reply)),
                    "followups": [{"followup": "Which route?", "reply": "North.", "question_id": "Q1"}],
                }
                key = self.native(name, fields)
                self.assertEqual(self.migrate(apply=True)[0], 0)
                source_key = f"submission/question/{request}"
                saved, = the_list.exchange_history(source_key)
                self.assertEqual((saved.exchange_id, saved.question, saved.answerer), ("followup:1", "Which route?", OWNER.id))
                self.assertEqual(saved.answer, list_exchange.ExchangeAnswer(
                    "North.", SECOND_REPLY, list_exchange.AnswerRead.UNREAD))
                wrong = replace(reply, who=author)
                _ = self.native(name, {**fields, "review_log": encode((prompt, *events, wrong))})
                status, preview = self.migrate(apply=True)
                self.assertEqual(status, 0)
                self.assertIn(f"  {source_key}: 1", preview)
                self.assertEqual(the_list.exchange_history(source_key), (saved,))
                visible = {item.key: item for item in review_items.read(self.review, members=MEMBERS, show="open")}
                self.assertTrue(visible[key].unread_answer)
                self.assertIn(wrong, visible[key].log)
                handoff = visible[key].exchange_handoff
                if isinstance(handoff, review_items.FiledReviewExchange):
                    self.assertFalse(handoff.answer_events)
                    self.assertFalse(handoff.hides_card)

    def test_scoped_tagged_reply_after_supersession_cannot_file_or_claim(self) -> None:
        prompt = ReviewLog(START, "nate", "question: Which side?", "Q1")
        reply = ReviewLog(SECOND_REPLY, OWNER.id, "Answer: East.", "answer:Q1")
        boundary = ReviewLog(READ, "nate", "Superseded followup: Which side?")
        slot = delivery_state.OpenQuestion("current", "When?", delivery_state.NoDelivery())
        for number, filing in enumerate(("new", "bound", "legacy"), 1):
            key = f"fixture/C{number}"
            original = replace(self.change_with_log((prompt, reply), slot), key=key)
            if filing == "bound":
                self.assertEqual(self.migrate_change(original)[0], 0)
            elif filing == "legacy":
                self.file_fixture_exchange(list_exchange.Exchange(
                    list_exchange.Source.SCOPED_REPLY, key, "question:Q1", list_exchange.Direction.ANSWER_TO_NATE,
                    "Which side?", START, list_exchange.Nate.USER, OWNER.id,
                    list_exchange.ExchangeAnswer("East.", SECOND_REPLY, list_exchange.AnswerRead.UNREAD)))
            before = the_list.exchange_history(key)
            changed = replace(self.change_with_log((prompt, boundary, reply), slot), key=key)
            status, preview = self.migrate_change(changed)
            self.assertEqual(status, 0)
            self.assertIn(f"  {key}: 1", preview)
            self.assertEqual(the_list.exchange_history(key), before)
            projected, = answer_sources.apply_review_handoffs([changed], user_zone(), members=MEMBERS)
            self.assertTrue(projected.unread_answer)
            self.assertIn(reply, projected.log)
            handoff = projected.exchange_handoff
            if isinstance(handoff, review_items.FiledReviewExchange):
                self.assertFalse(handoff.answer_events)

    def test_same_tag_cannot_restore_a_staff_reply_after_supersession(self) -> None:
        prompt = ReviewLog(START, "nate", "question: Which side?", "Q1")
        reply = ReviewLog(SECOND_REPLY, ASKER.id, "Answer: East.", "answer:Q1")
        approval = ReviewLog(SECOND_REPLY + timedelta(minutes=1), "nate", "answer: Proceed.")
        superseded = ReviewLog(READ, "nate", "Superseded followup: Which side?")
        fields: update_registration.OutstandingItem = {
            "since": FILED, "text": "Choose a route", "question": "Which route?", "waiting_why": "Which side?",
            "review_log": encode((prompt, reply, approval)),
        }
        key = self.native("staff-tag-supersession", fields, status="closed")
        self.assertEqual(self.migrate(apply=True)[0], 0)
        saved = the_list.exchange_history(key)
        self.assertEqual([exchange.exchange_id for exchange in saved], ["initial", "legacy-followup"])
        _ = self.native("staff-tag-supersession", {**fields, "review_log": encode((prompt, superseded, reply, approval))},
                        status="closed")
        status, preview = self.migrate(apply=True)
        self.assertEqual(status, 0)
        self.assertIn(f"  {key}: 1", preview)
        self.assertEqual(the_list.exchange_history(key), saved)
        item, = review_items.read(self.review, members=MEMBERS)
        self.assertIn(reply, item.log)
        handoff = item.exchange_handoff
        assert isinstance(handoff, review_items.FiledReviewExchange)
        self.assertFalse(handoff.answer_events)
        self.assertFalse(handoff.hides_card)

    def test_tagged_replacement_after_supersession_cannot_prove_a_bug_initial_reply(self) -> None:
        first = ReviewLog(FIRST_REPLY, OWNER.id, "Answer: North.")
        prompt = ReviewLog(SECOND_ASK, "nate", "question: Which side?", "Q1")
        boundary = ReviewLog(SECOND_ASK + timedelta(minutes=1), "nate", "Superseded followup: Which side?")
        reply = ReviewLog(SECOND_REPLY, OWNER.id, "Answer: East.", "answer:Q1")
        for number, prior in enumerate((False, True), 1):
            key = self.legacy_report(number, (first, prompt, reply), answer="East.")
            if prior:
                self.assertEqual(self.migrate(apply=True)[0], 0)
            before = the_list.exchange_history(key)
            self.assertEqual([entry.exchange_id for entry in before], ["initial", "question:Q1"] if prior else [])
            _ = self.legacy_report(number, (first, prompt, boundary, reply), answer="East.")
            status, preview = self.migrate(apply=True)
            self.assertEqual(status, 0)
            self.assertIn(f"  {key}: 2", preview)
            self.assertEqual(the_list.exchange_history(key), before)
            visible = {item.key: item for item in review_items.read(self.review, members=MEMBERS, show="open")}
            self.assertTrue(visible[key].unread_answer)
            self.assertIn(reply, visible[key].log)

    def test_shared_verifier_applies_conflicts_to_all_reply_proofs(self) -> None:
        tag = "Q1"
        prompt = ReviewLog(START, "nate", "question: Which side?", tag)
        reply = ReviewLog(FIRST_REPLY, OWNER.id, "Answer: East.")
        note = ReviewLog(START + timedelta(minutes=1), OWNER.id, "I checked the map.")
        boundary = replace(note, who="nate", words="Superseded followup: Which side?")
        open_slot = delivery_state.OpenQuestion(tag, "Which side?", delivery_state.NoDelivery())
        answered = delivery_state.AnsweredQuestion(tag, "Which side?", "East.")
        key = self.legacy_report(90, (prompt, reply), answer="East.")
        bug, = (item for item in review_items.read(self.review, members=MEMBERS, include_filed=True, sources=[])
                if item.key == key)
        assert isinstance(bug.source, review_items.InvestigationSource)
        for proof in answer_sources.ReplyProof:
            qualified = replace(reply, feedback_request_id="answer:Q1") if proof is answer_sources.ReplyProof.TAG else reply
            tagged = replace(reply, feedback_request_id="answer:Q1")
            cases = {
                "valid": ((prompt, note, qualified), answered, False),
                "duplicate tag": ((prompt, note, qualified, replace(prompt, words="question: When?")), answered, True),
                "foreign author": ((prompt, note, replace(qualified, who=ASKER.id)), answered, True),
                "foreign Bug replacement": ((prompt, note, replace(qualified, who=ASKER.id)), answered, True),
                "tagged after supersession": ((prompt, boundary, tagged), answered, True),
                "open slot": ((prompt, note, tagged), open_slot, True),
                "slot has another answer": ((prompt, note, qualified), replace(answered, answer="West."), True),
                "copied digest": ((prompt, note, qualified), answered, True),
            }
            for name, (log, slot, conflict) in cases.items():
                with self.subTest(proof=proof, case=name):
                    item = self.change_with_log(log, slot)
                    if name == "foreign Bug replacement":
                        item = replace(bug, source=replace(bug.source, record=replace(bug.source.record, log=log)))
                    binding = (list_exchange.TaggedReply(log[2].feedback_request_id) if log[2].feedback_request_id
                               else list_exchange.LoggedReply(2, answer_sources.event_digest(log[2])))
                    previous = ()
                    if name == "copied digest":
                        old = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, item.key, "log:1",
                            list_exchange.Direction.ANSWER_TO_NATE, "Which road?", START, list_exchange.Nate.USER, OWNER.id,
                            list_exchange.ExchangeAnswer("East.", FIRST_REPLY, list_exchange.AnswerRead.UNREAD),
                            binding=list_exchange.ExchangeBinding(list_exchange.QuestionEvent(0),
                                list_exchange.LoggedReply(1, answer_sources.event_digest(log[2]))))
                        previous = (list_exchange.FiledExchange.from_exchange(old),)
                    verifier = answer_sources.ExchangeVerifier(item, user_zone(), MEMBERS, previous)
                    evidence = (answer_sources.ReplyEvidence("East.", FIRST_REPLY, locator=2, binding=binding)
                                if name == "foreign Bug replacement" else
                                answer_sources.ReplyEvidence("East.", FIRST_REPLY, OWNER.id, 2, binding))
                    result = verifier._verify(  # pyright: ignore[reportPrivateUsage]
                        answer_sources.RequestInterval("Which side?", 0), 2, proof,
                        "question:Q1", tag=tag, evidence=evidence)
                    self.assertIs(result, answer_sources.Verification.CONFLICT if conflict
                                  else answer_sources.Verification.VERIFIED)
        item = self.change_with_log((replace(prompt, feedback_request_id=""), reply), answered)
        verifier = answer_sources.ExchangeVerifier(item, user_zone(), MEMBERS)
        result = verifier._verify(  # pyright: ignore[reportPrivateUsage]
            answer_sources.RequestInterval("Which side?", 0), 1, answer_sources.ReplyProof.INTERVAL,
            "log:0", evidence=answer_sources.ReplyEvidence())
        self.assertIs(result, answer_sources.Verification.MISSING)

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
