"""Test how /new_item sends a request and reports the List number."""
from __future__ import annotations

import io
from base64 import b64decode
from http.client import HTTPConnection
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn, cast, override
from unittest import mock
from zoneinfo import ZoneInfo
from uuid import uuid4

from scripts.agents.remote import local_machine
from scripts.lib import conf, file_lock, main_publish
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import investigation, record_store, staff, the_list
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import (
    change_page, for_you, new_item, new_item_dialog, review_actions, review_items, review_page, review_submissions,
    test_new_item_dialog, test_review_submissions, test_serve,
)

from scripts.review_links.browser_test_support import firefox_probe_results


REPLY_TYPES = (new_item.ItemType.BUG, new_item.ItemType.QUESTION, new_item.ItemType.INVESTIGATION,
               new_item.ItemType.ENHANCEMENT, new_item.ItemType.PROPOSAL,
               new_item.ItemType.EFFICIENCY, new_item.ItemType.FEEDBACK)

def member(identity: str, kind: staff.Kind, name: str, parent: staff.StaffRow | None = None) -> staff.StaffRow:
    reports: staff.ReportsTo | staff.StaffRoot = staff.ReportsTo(parent.id) if parent is not None else staff.StaffRoot()
    return staff.StaffRow(staff.StaffId(identity), kind, name, reports, current_session=staff.StaffId(identity))


DWIGHT = member("920b0a64-6eee-479e-8c07-e03e4dcd4ef6", "producer", "dwight")
BOB = member("2c1f7d2e-5a43-4a3e-9d0b-6f1f2b9e8c11", "producer", "bob")
SPACETIME = member("a0882376-677b-4da5-adbb-f11db9146021", "showrunner", "spacetime")
MODELS = member("f5636030-6745-4ff4-aeda-4c2773d18da8", "unit", "models", SPACETIME)
NATEDEV = member("96b016bf-87cf-4a19-af7a-77425852c2e2", "showrunner", "natedev")
POSTMASTER = member("e404d92e-8cc6-4714-952f-528c8de5cb7c", "unit", "postmaster", NATEDEV)
CARGO = member("4dbdb78e-a83b-4248-b8f9-ff278dcbc60c", "showrunner", "cargo-liner")
TILE = member("10000000-0000-4000-8000-000000000006", "unit", "tile", CARGO)
HIDDEN = member("10000000-0000-4000-8000-000000000007", "showrunner", "hidden")
CARGO_STYLE = member("10000000-0000-4000-8000-000000000008", "unit", "style", CARGO)
SPACETIME_STYLE = member("10000000-0000-4000-8000-000000000009", "unit", "style", SPACETIME)
MORTY = member("01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a", "producer", "morty")
ROWS = (DWIGHT, BOB, SPACETIME, MODELS, NATEDEV, POSTMASTER, CARGO, TILE, HIDDEN, CARGO_STYLE, SPACETIME_STYLE)
MEMBERS = staff.StaffSnapshot(ROWS, retired_producers=(MORTY,))
ADDRESS = for_you.ConsoleAddress("192.0.2.1", 8765)
EXCHANGE = new_item.exchange


class SilentConnection:
    """Take the request, then give no reply."""

    def __init__(self, *_arguments: object, **_options: object) -> None:
        pass

    def request(self, *_arguments: object, **_options: object) -> None:
        pass

    def getresponse(self) -> NoReturn:
        raise TimeoutError("timed out")

    def close(self) -> None:
        pass


class FakeServer:
    """Answer the window's two requests without a socket."""

    def __init__(self) -> None:
        self.recipients: tuple[staff.StaffRow, ...] = (BOB, DWIGHT)
        self.posts: list[dict[str, object]] = []
        self.reads: int = 0
        self.list_number: int | None = 3
        self.review_key: str | None = "fixture-key"

    def exchange(self, _address: for_you.ConsoleAddress, method: Literal["GET", "POST"], target: str,
                 body: bytes = b"") -> new_item.ServerReply:
        if method == "GET":
            assert target == new_item.OPTIONS_TARGET
            self.reads += 1
            return new_item.ServerReply(200, {"token": "fixture-token", "recipients": [
                {"id": row.id, "name": row.name, "proposal": True} for row in self.recipients]})
        assert target == review_page.NEW_ITEM_PATH
        self.posts.append(cast(dict[str, object], json.loads(body)))
        fields: dict[str, object] = {"id": "I7", "version": "1"}
        if self.review_key is not None:
            fields["key"] = self.review_key
        if self.list_number is not None:
            fields["list_number"] = self.list_number
        return new_item.ServerReply(200, fields)


class ServerFixture(unittest.TestCase):
    """Patch the console server, the address and the producer map for each test."""

    config: Path = Path()
    folder: Path = Path()
    server: FakeServer = FakeServer()

    @override
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.folder / "producer_map.conf"
        _ = self.config.write_text("# The producer of a showrunner or of a unit.\n", encoding="utf-8")
        _ = write_config_fixture(self.folder / "review_links.conf")
        _ = write_config_fixture(self.folder / "user_zone.conf")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(self.folder),
                                                                        "XDG_STATE_HOME": str(self.folder / "state")})))
        self.server = FakeServer()
        _ = self.enterContext(mock.patch.object(new_item, "exchange", side_effect=self.server.exchange))
        _ = self.enterContext(mock.patch.object(for_you, "console_address", return_value=ADDRESS))
        _ = self.enterContext(mock.patch("platform.system", return_value="Linux"))

    def map_row(self, row: staff.StaffRow, producer: staff.StaffRow) -> None:
        with self.config.open("a", encoding="utf-8") as stream:
            _ = stream.write(f"{row.id} = {producer.id}  # {row.name} = {producer.name}\n")

    def scheduled_members(self) -> staff.StaffSnapshot:
        document = self.config.parent / "natedev.md"
        _ = document.write_text("> **Status: PRODUCTION — running.**\n", encoding="utf-8")
        natedev = replace(NATEDEV, doc=staff.LiveDoc(local_machine(), document))
        return staff.StaffSnapshot(tuple(natedev if row == NATEDEV else row for row in MEMBERS.rows) + (MORTY,))

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = new_item.main([*arguments, "--session", "another-session"]) if arguments else new_item.main([])
        return code, output.getvalue(), errors.getvalue()


class NewItemTests(ServerFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(staff, "snapshot", return_value=MEMBERS))

    def test_no_arguments_print_the_usage(self) -> None:
        code, output, _ = self.run_main()
        self.assertEqual(code, new_item.Outcome.USAGE)
        self.assertEqual(output.splitlines(), [
            "/new_item <type> <assignee> <description> [extra, such as a time]",
            "Types: bug, question, investigation, proposal, efficiency, feedback, enhancement, scheduled",
            "Assignee: a producer, a showrunner or a unit",
            "/new_item scheduled natedev Set the time zone back to Eastern time. Start Fri Nov 27 13:00.",
            "Resend a reply command: new_item.py resend-reply <item-id>",
        ])
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_seven_review_types_file_as_server_kinds(self) -> None:
        self.assertEqual([*new_item.ItemType, new_item_dialog.SCHEDULED_KIND],
                         ["bug", "question", "investigation", "proposal", "efficiency", "feedback", "enhancement", "scheduled"])
        investigation = new_item_dialog.SubmissionKind.INVESTIGATION
        self.assertEqual(new_item.FILED_KINDS[new_item.ItemType.FEEDBACK], investigation)
        self.assertEqual(new_item.FILED_KINDS[new_item.ItemType.EFFICIENCY], investigation)
        for kind in new_item_dialog.SubmissionKind:
            self.assertEqual(new_item.FILED_KINDS[new_item.ItemType(kind.value)], kind)
        for item_type in new_item.ItemType:
            with self.subTest(item_type=item_type):
                code, output, _ = self.run_main(item_type.value.upper(), "bob", "Check the chart.")
                self.assertEqual(code, 0)
                self.assertEqual(output, f"{item_type.value.capitalize()} \u2192 bob. List item 3.\n")
                self.assertEqual(self.server.posts[-1]["kind"], new_item.FILED_KINDS[item_type])
                self.assertEqual(self.server.posts[-1]["notice"], {"kind": item_type.value, "from_command": True})
                words = "Efficiency: Check the chart." if item_type == new_item.ItemType.EFFICIENCY else "Check the chart."
                self.assertEqual(self.server.posts[-1]["words"], words)
        with self.assertRaises(SystemExit):
            _ = self.run_main("chore", "bob", "Check the chart.")

    def test_scheduled_menu_and_custom_rules_save_the_named_owner_and_read_back(self) -> None:
        folder = self.config.parent
        staff_file = folder / "staff.json"
        members = self.scheduled_members()
        _ = staff_file.write_text(json.dumps({"schema": 1, "rows": [
            row.to_json() for row in staff.active(members, "producer")
        ]}))
        _ = write_config_fixture(folder / "console.conf", f"page.list.owner={MORTY.id}\n")
        _ = (folder / "the_list.conf").write_text("# whole: 1..\nseries_run_limit=730\n")
        _ = (folder / "review_links.conf").write_text("review_attachments_root=review-attachments\n")
        self.map_row(NATEDEV, DWIGHT)
        self.map_row(POSTMASTER, BOB)
        notices: list[tuple[staff.StaffId, str]] = []

        def send(recipient: staff.StaffRow, text: str) -> None:
            notices.append((recipient.id, text))

        now = datetime(2026, 11, 23, 18, tzinfo=UTC)
        first_words = "Set the time zone back to Eastern time."
        words = "Check the account predictions."
        monday = "Start Mon Nov 30 09:00."
        cases = (
            (NATEDEV, DWIGHT, first_words, "Start Fri Nov 27 13:00.", "",
             "2026-11-27T13:00:00-08:00", "", "Does not repeat"),
            (NATEDEV, DWIGHT, "Check the queue?", monday, "",
             "2026-11-30T09:00:00-08:00", "", "Does not repeat"),
            (NATEDEV, DWIGHT, words, monday, "Does not repeat.",
             "2026-11-30T09:00:00-08:00", "", "Does not repeat"),
            (NATEDEV, DWIGHT, words, monday, "Repeat Daily.",
             "2026-11-30T09:00:00-08:00", "FREQ=DAILY", "Repeats: Daily"),
            (NATEDEV, DWIGHT, words, monday, "Repeat Weekly on Monday.",
             "2026-11-30T09:00:00-08:00", "FREQ=WEEKLY;BYDAY=MO", "Repeats: Weekly on Monday"),
            (NATEDEV, DWIGHT, words, monday, "Repeat Monthly on the last Monday.",
             "2026-11-30T09:00:00-08:00", "FREQ=MONTHLY;BYDAY=-1MO", "Repeats: Monthly on the last Monday"),
            (NATEDEV, DWIGHT, words, monday, "Repeat Annually on November 30.",
             "2026-11-30T09:00:00-08:00", "FREQ=YEARLY", "Repeats: Annually on November 30"),
            (NATEDEV, DWIGHT, words, monday, "Repeat Every weekday (Monday to Friday).",
             "2026-11-30T09:00:00-08:00", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
             "Repeats: Every weekday (Monday to Friday)"),
            (POSTMASTER, BOB, words, monday, "Repeat every 2 weeks on Monday and Thursday.",
             "2026-11-30T09:00:00-08:00", "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH",
             "Repeats: Every 2 weeks on Monday and Thursday"),
            (NATEDEV, DWIGHT, words, monday, "Repeat every 3 months on the same day number. Ends after 13 occurrences.",
             "2026-11-30T09:00:00-08:00", "FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=30;COUNT=13",
             "Repeats: Every 3 months on day 30 for 13 runs"),
            (NATEDEV, DWIGHT, words, monday, "Repeat every 3 months on the same weekday of the month. Ends never.",
             "2026-11-30T09:00:00-08:00", "FREQ=MONTHLY;INTERVAL=3;BYDAY=-1MO",
             "Repeats: Every 3 months on the last Monday"),
            (BOB, BOB, words, "Start 2026-11-30T09:00 America/New_York.",
             "Repeat every 2 days. Ends on Mar 15, 2027.", "2026-11-30T09:00:00-05:00",
             "FREQ=DAILY;INTERVAL=2;UNTIL=20270316T035959Z", "Repeats: Every 2 days until Mar 15, 2027"),
            (NATEDEV, DWIGHT, words, "Start 2026-11-30T09:00+02:00.", "Repeat every 2 years.",
             "2026-11-30T09:00:00+02:00", "FREQ=YEARLY;INTERVAL=2", "Repeats: Every 2 years on November 30"),
            (NATEDEV, DWIGHT, words, "Start Sun Nov 7 2027 01:30-08:00[America/Los_Angeles].", "Repeat Daily.",
             "2027-11-07T01:30:00-08:00", "FREQ=DAILY", "Repeats: Daily"),
            (NATEDEV, DWIGHT, words, "Start Mon Feb 22 2027 09:00.", "Repeat Monthly on the fourth Monday.",
             "2027-02-22T09:00:00-08:00", "FREQ=MONTHLY;BYDAY=4MO", "Repeats: Monthly on the fourth Monday"),
            (NATEDEV, DWIGHT, words, "Start Mon Feb 22 2027 09:00.", "Repeat Monthly on the last Monday.",
             "2027-02-22T09:00:00-08:00", "FREQ=MONTHLY;BYDAY=-1MO", "Repeats: Monthly on the last Monday"),
            (MORTY, MORTY, words, monday, "Repeat daily after 730 occurrences.",
             "2026-11-30T09:00:00-08:00", "FREQ=DAILY;COUNT=730", "Repeats: Daily for 730 runs"),
        )
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(folder / "state"),
                                          "STAFF_FILE": str(staff_file), "USER_ZONE": "America/Los_Angeles",
                                          "REVIEW_LINKS_ROOT": str(folder / "review")}),
              mock.patch.object(staff, "snapshot", return_value=members),
              mock.patch.object(the_list, "_send_assignment_notice", side_effect=send),
              mock.patch.object(the_list, "now", return_value=now)):
            for number, (owner, producer, instruction, start_words, repeat, stamp, rule, readback) in enumerate(cases, 1):
                with self.subTest(start=start_words, repeat=repeat):
                    notices.clear()
                    arguments = ("scheduled", owner.name, f"{instruction} {start_words} {repeat}".strip())
                    code, preview, errors = self.run_main(*arguments, "--preview")
                    self.assertEqual((code, errors), (0, ""))
                    self.assertEqual(len(the_list.records(MORTY.id)), number - 1)
                    code, output, errors = self.run_main(*arguments)
                    self.assertEqual(code, 0)
                    item = the_list.load(MORTY.id, f"L{number}")
                    self.assertIs(item.state, the_list.ItemState.SCHEDULED)
                    self.assertIs(item.new_item_draft_id, review_submissions.NoNewItemDraft.ABSENT)
                    schedule = item.schedule
                    if isinstance(schedule, the_list.ScheduledSeries):
                        run, rrule = schedule.first, schedule.rule.rrule
                    else:
                        assert isinstance(schedule, the_list.ScheduledRun)
                        run, rrule = schedule, ""
                    self.assertEqual((item.owner, run.words, run.start.isoformat(), rrule),
                                     (owner.id, instruction, stamp, rule))
                    self.assertEqual(item.producer, producer.id)
                    self.assertEqual(item.unit, owner.name if owner.kind == "unit" else "")
                    self.assertEqual(item.log[0].source, "nate via /new_item")
                    if owner is BOB:
                        self.assertIn("the owner has no current showrunner staff row", errors)
                        self.assertIn("At least one assignment notice failed. The List item is saved.", output)
                        expected = [BOB.id]
                    elif producer is MORTY:
                        self.assertEqual(errors, "")
                        expected = []
                    else:
                        self.assertEqual(errors, "")
                        expected = [producer.id, NATEDEV.id]
                    self.assertEqual([target for target, _ in notices], expected)
                    for notice_target, text in notices:
                        self.assertIn(f"item {number}: {instruction}", text, notice_target)
                        self.assertNotIn(instruction + ".", text, notice_target)
                    zone = ("America/New_York" if owner is BOB else "America/Los_Angeles")
                    if not start_words.endswith("+02:00."):
                        self.assertEqual(run.start.tzinfo, ZoneInfo(zone))
                    self.assertEqual(preview.splitlines()[1:], [readback])
                    self.assertIn(stamp[11:16], preview.splitlines()[0])
                    if number == 1:
                        self.assertEqual(preview.splitlines()[0], "Starts: Fri Nov 27 2026 13:00 PST")
                    if owner is BOB:
                        self.assertEqual(preview.splitlines()[0], "Starts: Mon Nov 30 2026 09:00 EST (06:00 PST)")
                    self.assertEqual(output.splitlines()[:2], preview.splitlines())
                    self.assertIn(f"List item {number}.", output.splitlines()[2])
            with self.subTest(recovery="closed Scheduled item"):
                before = the_list.records(MORTY.id, include_closed=True)
                draft_id = new_item_dialog.ScheduledDraftId("s1-" + "c" * 32)
                request = new_item_dialog.ScheduledSubmission(
                    draft_id, MORTY.id, f"{words} {monday}", "list")
                saved = new_item.submit_scheduled(request, live_words_limit=100)
                closed = the_list.close(MORTY.id, saved.id, "The user cancelled this item.", source="morty")
                receipts = list((folder / "review-attachments").glob(f"**/{draft_id.value}/receipt.json"))
                self.assertEqual(len(receipts), 2)
                for receipt in receipts:
                    receipt.unlink()
                self.assertEqual(new_item.submit_scheduled(request, live_words_limit=100), saved)
                self.assertEqual(the_list.load(MORTY.id, saved.id), closed)
                self.assertEqual(closed.new_item_draft_id, review_submissions.NewItemDraft(draft_id, saved.claim))
                self.assertCountEqual(the_list.records(MORTY.id, include_closed=True), [*before, closed])
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_scheduled_refuses_past_times_and_repeats_outside_the_menu(self) -> None:
        self.map_row(NATEDEV, BOB)
        _ = (self.config.parent / "the_list.conf").write_text("# whole: 1..\nseries_run_limit=730\n")
        now = datetime(2026, 11, 23, 18, tzinfo=UTC)
        cases = (
            ("Start Fri Nov 20 13:00.", "The start must be in the future."),
            ("Start Fri Nov 27 13:00. Repeat twice a day.", "Use a repeat from the List menu or a Custom repeat."),
            ("Start Sun Nov 7 2027 01:30 America/Los_Angeles.", "This start time occurs twice. Give its UTC offset and time zone."),
            ("Start 2027-11-07T01:30-06:00[America/Los_Angeles].", "The start offset does not match its time zone."),
            ("Start Mon Feb 22 2027 09:00. Repeat every month on the same weekday.",
             "Choose the fourth Monday or the last Monday."),
            ("Start Fri Nov 27 13:00. Repeat daily after 731 occurrences.", "The repeat exceeds the series limit of 730 runs."),
        )
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.config.parent / "state"),
                                          "USER_ZONE": "America/Los_Angeles"}),
              mock.patch.object(the_list, "now", return_value=now)):
            for start, reason in cases:
                for flags in ((), ("--preview",)):
                    with self.subTest(start=start, flags=flags):
                        code, output, errors = self.run_main("scheduled", "natedev", f"Check the chart. {start}", *flags)
                        self.assertEqual((code, output, errors), (new_item.Outcome.REFUSED, "", f"new_item: {reason}\n"))
            available = self.scheduled_members()
            for target, owner in ((BOB, NATEDEV), (NATEDEV, NATEDEV), (POSTMASTER, POSTMASTER)):
                for reason in ("exit", "pending retirement"):
                    if reason == "exit":
                        exited = staff.Exited("2026-11-20", "nate", "Test exit")
                        members = replace(available, rows=tuple(replace(row, exited=exited) if row.id == target.id
                                                               else row for row in available.rows))
                    else:
                        members = replace(available, pending_retire=(target.id,))
                    with self.subTest(target=target.name, reason=reason), mock.patch.object(staff, "snapshot", return_value=members):
                        result = self.run_main("scheduled", owner.name, "Check the chart. Start Fri Nov 27 13:00.")
                        self.assertEqual(result, (new_item.Outcome.REFUSED, "",
                                                  f"new_item: {target.name} is unavailable. Choose another recipient.\n"))
            self.assertEqual(the_list.records(MORTY.id), [])
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_producer_assignee_has_no_lookup(self) -> None:
        code, output, _ = self.run_main("bug", "bob", "A waiting row has no drag grip.")
        self.assertEqual((code, output), (0, "Bug \u2192 bob. List item 3.\n"))
        self.assertEqual(self.server.posts[0]["recipient_id"], BOB.id)
        self.assertEqual(self.server.posts[0]["assignee_id"], BOB.id)

    def test_a_showrunner_with_a_row_files_nate_extra_words_as_written(self) -> None:
        self.map_row(SPACETIME, DWIGHT)
        code, output, _ = self.run_main("question", "spacetime", "Can the server start with no saved world?",
                                        "Answer by Monday.")
        self.assertEqual(code, 0)
        self.assertEqual(output, "Question → spacetime. Producer: dwight (set for spacetime). "
                         + "List item 3.\n")
        self.assertEqual(self.server.posts[0]["words"], "Can the server start with no saved world?\n\nAnswer by Monday.")
        self.assertEqual(self.server.posts[0]["assignee_id"], SPACETIME.id)

    def test_a_unit_uses_its_showrunner_row(self) -> None:
        self.map_row(SPACETIME, DWIGHT)
        code, output, _ = self.run_main("bug", "models", "The frame time graph has a gap.")
        self.assertEqual(code, 0)
        self.assertEqual(output, "Bug → models, a unit of spacetime. Producer: dwight (from spacetime). "
                         + "List item 3.\n")
        self.assertEqual(self.server.posts[0]["recipient_id"], DWIGHT.id)
        self.assertEqual(self.server.posts[0]["assignee_id"], MODELS.id)

    def test_a_unit_row_wins_over_its_showrunner_row(self) -> None:
        self.map_row(NATEDEV, DWIGHT)
        self.map_row(POSTMASTER, BOB)
        code, output, _ = self.run_main("proposal", "postmaster", "One phone text for each nightly result.")
        self.assertEqual(code, 0)
        self.assertEqual(output, "Proposal → postmaster, a unit of natedev. Producer: bob (set for postmaster). "
                         + "List item 3.\n")
        self.assertEqual(self.server.posts[0]["recipient_id"], BOB.id)
        self.assertEqual(self.server.posts[0]["assignee_id"], POSTMASTER.id)

    def test_an_item_for_this_session_says_so(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": DWIGHT.id}):
            receipt = self.run_main("feedback", "dwight", "The status of item 65 is old.")
        self.assertEqual(receipt, (0, "Feedback → dwight, this session. List item 3.\n", ""))

    def test_a_unit_receipt_uses_the_reply_recipients_session(self) -> None:
        self.map_row(SPACETIME, DWIGHT)
        for caller, delivery in ((MODELS, ""), (SPACETIME, ""),
                                 (DWIGHT, " Reply command goes to dwight, this session.")):
            with (self.subTest(caller=caller.name),
                  mock.patch.dict(os.environ, {"CODEX_THREAD_ID": caller.current_session})):
                receipt = self.run_main("bug", "models", "The frame time graph has a gap.")
                self.assertEqual(receipt,
                                 (0, "Bug → models, a unit of spacetime. Producer: dwight (from spacetime). "
                                  + f"List item 3.{delivery}\n", ""))
                self.assertEqual(self.server.posts[-1]["recipient_id"], DWIGHT.id)

    def test_the_receipt_uses_the_owner_returned_by_the_server(self) -> None:
        def exchange(address: for_you.ConsoleAddress, method: Literal["GET", "POST"], target: str,
                     body: bytes = b"") -> new_item.ServerReply:
            reply = self.server.exchange(address, method, target, body)
            if method == "POST":
                return replace(reply, fields={**reply.fields, "reply_owner": BOB.id})
            return reply

        with (mock.patch.dict(os.environ, {"CODEX_THREAD_ID": BOB.current_session}),
              mock.patch.object(new_item, "exchange", side_effect=exchange)):
            receipt = self.run_main("bug", "dwight", "The frame time graph has a gap.")
        self.assertEqual(self.server.posts[-1]["recipient_id"], DWIGHT.id)
        self.assertEqual(receipt, (0, "Bug → dwight. List item 3. Reply command goes to bob, this session.\n", ""))

    def test_a_session_argument_cannot_identify_the_caller(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.dict(os.environ, {"CODEX_THREAD_ID": BOB.id}),
              redirect_stdout(output), redirect_stderr(errors)):
            code = new_item.main(["bug", "dwight", "The status of item 65 is old.", "--session", DWIGHT.id])
        self.assertEqual((code, output.getvalue(), errors.getvalue()), (0, "Bug → dwight. List item 3.\n", ""))

    def test_a_showrunner_with_no_row_asks_and_files_nothing(self) -> None:
        for kind, words in (
                ("investigation", "Why does the tile test take 4 minutes?"),
                ("scheduled", "Check the chart. Start 2099-11-30T09:00Z.")):
            with self.subTest(kind=kind):
                code, output, _ = self.run_main(kind, "cargo-liner", words)
                self.assertEqual(code, new_item.Outcome.NO_PRODUCER)
                self.assertEqual(output, "cargo-liner has no producer yet. Which producer?\ndwight / bob\n")
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_unit_with_no_row_asks_about_both(self) -> None:
        code, output, _ = self.run_main("bug", "tile", "The tile demo clips the last column.")
        self.assertEqual(code, new_item.Outcome.NO_PRODUCER)
        self.assertEqual(output, "tile is a unit of cargo-liner. Neither has a producer yet. Which producer?\n"
                         + "dwight / bob\n")
        self.assertEqual(self.server.posts, [])

    def test_an_unknown_name_asks_and_files_nothing(self) -> None:
        code, output, _ = self.run_main("bug", "til", "The demo clips the last column.")
        self.assertEqual(code, new_item.Outcome.UNKNOWN_NAME)
        self.assertEqual(output, 'No session has the name "til". Do you mean tile, a unit of cargo-liner? yes / a different name\n')
        for kind, words in (
                ("bug", "The demo clips the last column."),
                ("scheduled", "Check the chart. Start 2099-11-30T09:00Z.")):
            with self.subTest(kind=kind):
                code, output, _ = self.run_main(kind, "qqqq", words)
                self.assertEqual((code, output), (new_item.Outcome.UNKNOWN_NAME, 'No session has the name "qqqq".\n'))
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_name_with_many_matches_asks_which_one(self) -> None:
        code, output, _ = self.run_main("bug", "style", "The demo clips the last column.")
        self.assertEqual(code, new_item.Outcome.UNKNOWN_NAME)
        self.assertEqual(output, 'More than one session has the name "style": cargo-liner/style, spacetime/style. Which one?\n')
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_guess_that_many_sessions_share_asks_which_one(self) -> None:
        code, output, _ = self.run_main("bug", "styl", "The demo clips the last column.")
        self.assertEqual(code, new_item.Outcome.UNKNOWN_NAME)
        self.assertEqual(output, 'No session has the name "styl". More than one session has the name "style": '
                         + "cargo-liner/style, spacetime/style. Which one?\n")
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_stale_row_asks_for_its_holder_and_files_nothing(self) -> None:
        self.map_row(NATEDEV, DWIGHT)
        self.map_row(POSTMASTER, MORTY)
        for kind, words in (
                ("proposal", "One phone text for each nightly result."),
                ("scheduled", "Check the chart. Start 2099-11-30T09:00Z.")):
            with self.subTest(kind=kind):
                code, output, _ = self.run_main(kind, "postmaster", words)
                self.assertEqual(code, new_item.Outcome.NO_PRODUCER)
                self.assertEqual(output, "The row of postmaster names morty, who is not active. Which producer for postmaster?\n"
                                 + "dwight / bob\n")
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_member_with_no_to_row_producer_is_refused(self) -> None:
        self.map_row(HIDDEN, DWIGHT)
        self.server.recipients = (BOB,)
        code, _, errors = self.run_main("bug", "hidden", "Fix the chart.")
        self.assertEqual(code, new_item.Outcome.REFUSED)
        self.assertIn("dwight is not in the To row: bob", errors)
        self.assertEqual(self.server.posts, [])

    def test_a_server_that_does_not_answer_gives_one_error_line(self) -> None:
        self.map_row(SPACETIME, DWIGHT)
        with mock.patch.object(new_item, "exchange", side_effect=new_item.ServerUnavailable("the console server does not answer")):
            code, output, errors = self.run_main("bug", "spacetime", "Fix the chart.")
        self.assertEqual((code, output, errors), (1, "", "new_item: the console server does not answer\n"))
        lookup = subprocess.CalledProcessError(1, ["tailscale"])
        with mock.patch.object(for_you, "console_address", side_effect=lookup):
            code, output, errors = self.run_main("bug", "spacetime", "Fix the chart.")
        self.assertEqual((code, output), (1, ""))
        self.assertTrue(errors.startswith("new_item: cannot find the console server address: "), errors)

        self.map_row(POSTMASTER, MORTY)
        cases = (
            ("cargo-liner", "cargo-liner has no producer yet. Which producer?"),
            ("postmaster", "The row of postmaster names morty, who is not active. Which producer for postmaster?"),
        )
        for assignee, question in cases:
            with self.subTest(assignee=assignee), mock.patch.object(for_you, "console_address", side_effect=lookup):
                result = self.run_main("bug", assignee, "Fix the chart.")
                self.assertEqual(result, (new_item.Outcome.NO_PRODUCER, f"{question}\ndwight / bob\n", ""))
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_a_post_without_a_reply_says_to_check_the_list(self) -> None:
        with mock.patch("scripts.review_links.new_item.HTTPConnection", new=SilentConnection):
            cases: tuple[tuple[Literal["GET", "POST"], bool], ...] = (("POST", True), ("GET", False))
            for method, expected in cases:
                with self.subTest(method=method), self.assertRaises(new_item.ServerUnavailable) as caught:
                    _ = EXCHANGE(ADDRESS, method, review_page.NEW_ITEM_PATH, b"{}")
                self.assertEqual(new_item.MAY_BE_FILED in str(caught.exception), expected)

    def test_the_mac_forwards_the_command_and_its_session_to_natedev(self) -> None:
        arguments = ["bug", "models", "Fix the chart.", "Today.", "--session", "draft-session"]
        output = io.StringIO()
        with (mock.patch("platform.system", return_value="Darwin"),
              mock.patch.dict(os.environ, {"ZONE_SSH": "fixture-ssh", "CODEX_THREAD_ID": "",
                                           "CLAUDE_CODE_SESSION_ID": "mac-session"}),
              mock.patch.object(main_publish, "ssh", return_value=subprocess.CompletedProcess(
                  [], 0, "", "")) as ssh, redirect_stdout(output)):
            self.assertEqual(new_item.main(arguments), 0)
            self.assertEqual(new_item.main([]), new_item.Outcome.USAGE)
            self.assertEqual(new_item.main(["resend-reply", "I45"]), 0)
        filing, resend = ssh.call_args_list
        self.assertEqual(shlex.split(cast(str, filing.args[0])), [
            "env", "CODEX_THREAD_ID=", "CLAUDE_CODE_SESSION_ID=mac-session",
            "$HOME/.claude/scripts/lib/py", "$HOME/.claude/scripts/review_links/new_item.py", *arguments])
        self.assertEqual(shlex.split(cast(str, resend.args[0])), [
            "$HOME/.claude/scripts/lib/py", "$HOME/.claude/scripts/review_links/new_item.py", "resend-reply", "I45"])
        for call in (filing, resend):
            self.assertEqual((call.kwargs["host"], call.kwargs["ssh_program"]), ("natedev", "fixture-ssh"))
        self.assertEqual(output.getvalue(), new_item.usage() + "\n")
        self.assertEqual((self.server.reads, self.server.posts), (0, []))

    def test_post_body_has_the_window_fields(self) -> None:
        self.map_row(MODELS, BOB)
        code, _, _ = self.run_main("proposal", "models", "Add a chart.", "Next week.")
        self.assertEqual(code, 0)
        body = self.server.posts[0]
        request_id = body.pop("request_id")
        self.assertTrue(review_submissions.is_draft_id(request_id))
        self.assertEqual(body, {
            "token": "fixture-token", "kind": "proposal", "recipient_id": BOB.id,
            "notice": {"kind": "proposal", "from_command": True},
            "words": "Add a chart.\n\nNext week.", "evidence": {"kind": "none"}, "images": [],
            "page": "list", "page_recipient": "producer", "assignee_id": MODELS.id})
        request = new_item_dialog.read_request({**body, "request_id": request_id})
        self.assertEqual((request.kind, request.recipient_id, request.page_recipient),
                         (new_item_dialog.SubmissionKind.PROPOSAL, BOB.id, new_item_dialog.PageRecipient.PRODUCER))

    def test_pending_command_keeps_its_legacy_recipient_and_assignee(self) -> None:
        request_id = "r6-" + "a" * 32
        words = "Check the chart."
        draft = new_item.draft_path(ADDRESS, "fixture-session", MODELS.id, new_item.ItemType.BUG, words)
        draft.parent.mkdir(parents=True)
        _ = draft.write_text(f"{request_id}\n{SPACETIME.id}\n")
        code = new_item.main(["bug", "models", words, "--session", "fixture-session"])
        self.assertEqual(code, new_item.Outcome.FILED)
        post, = self.server.posts
        self.assertEqual((post["request_id"], post["recipient_id"], post["page_recipient"], post["list_assignee"]),
                         (request_id, SPACETIME.id, "selected", MODELS.id))
        self.assertFalse(draft.exists())


    def test_a_reply_without_a_list_number_reports_the_saved_review_item(self) -> None:
        self.server.list_number = None
        code, output, errors = self.run_main("bug", "bob", "A waiting row has no drag grip.")
        self.assertEqual((code, errors), (new_item.Outcome.FILED, ""))
        self.assertEqual(output, "Bug → bob. Filed as I7. The console server returned no List number.\n")

    def test_enhancement_with_no_producer_saves_nothing(self) -> None:
        code, output, errors = self.run_main("enhancement", "tile", "Show the next check in the row.")
        self.assertEqual((code, errors), (new_item.Outcome.NO_PRODUCER, ""))
        self.assertIn("Which producer?", output)
        self.assertEqual((self.server.reads, self.server.posts), (0, []))
        drafts = (self.folder / "state/new-item").glob("*")
        self.assertEqual([draft for draft in drafts if draft.suffix != ".lock"], [])

    def test_a_reply_without_a_review_key_reports_the_list_number(self) -> None:
        self.server.review_key = None
        code, output, errors = self.run_main("bug", "bob", "A waiting row has no drag grip.")
        self.assertEqual((code, output, errors), (new_item.Outcome.FILED, "Bug → bob. List item 3.\n", ""))

    def test_a_reply_without_a_review_key_or_list_number_exits_filed(self) -> None:
        self.server.review_key = ""
        self.server.list_number = None
        code, output, errors = self.run_main("bug", "bob", "A waiting row has no drag grip.")
        self.assertEqual((code, errors), (new_item.Outcome.FILED, ""))
        self.assertEqual(output, "Bug → bob. Filed as I7. The console server returned no List number.\n")

    def test_a_list_write_failure_after_filing_keeps_the_draft(self) -> None:
        def exchange(address: for_you.ConsoleAddress, method: Literal["GET", "POST"], target: str,
                     body: bytes = b"") -> new_item.ServerReply:
            reply = self.server.exchange(address, method, target, body)
            if method == "POST" and len(self.server.posts) <= 2:
                return new_item.ServerReply(503, {"error": "No space left on device"})
            return reply

        arguments = ("bug", "bob", "A waiting row has no drag grip.")
        with mock.patch.object(new_item, "exchange", side_effect=exchange):
            code, output, errors = self.run_main(*arguments)
            self.assertEqual((code, output), (new_item.Outcome.FAILED, ""))
            self.assertIn("No space left on device", errors)
            self.assertEqual(self.run_main(*arguments),
                             (new_item.Outcome.FILED, "Bug → bob. List item 3.\n", ""))
        self.assertEqual(len(self.server.posts), 3)
        self.assertEqual(self.server.posts, [self.server.posts[0]] * 3)


class NewItemServerTests(unittest.TestCase):
    def test_scheduled_send_checks_the_live_words_limit_only_on_first_filing(self) -> None:
        server = test_serve.ReviewLinksServerTests()
        self.addCleanup(server.doCleanups)
        server.setUp()
        _ = server.review_fixture()
        morty = fixture_member("producer", name="morty")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={morty.id}\n")
        configuration = conf.path("review_links")
        words = ("Check the Scheduled item and confirm the current notes and times on the List."
                 " Start 2099-11-30T09:00Z.")
        self.assertEqual(len(words), 102)
        request = {"token": server.server.new_item_token, "kind": new_item_dialog.SCHEDULED_KIND,
                   "page": "list", "page_recipient": "producer", "recipient_id": morty.id, "words": words}
        saved_body = json.dumps({**request, "request_id": "s1-" + "a" * 32}).encode()
        fresh_body = json.dumps({**request, "request_id": "s1-" + "b" * 32}).encode()

        _ = write_config_fixture(configuration, "new_item.max_words_chars=102\n")
        saved = server.new_item_http(saved_body, finished=True)
        self.assertEqual(saved.status, 200, saved.body)

        _ = write_config_fixture(configuration, "new_item.max_words_chars=100\n")
        refusal = server.new_item_http(fresh_body, finished=True)
        self.assertEqual(refusal.status, 400, refusal.body)
        self.assertEqual(json.loads(refusal.body), {"error": "Your words must be text with at most 100 characters."})
        replay = server.new_item_http(saved_body, finished=True)
        self.assertEqual((replay.status, replay.body), (200, saved.body))
        self.assertEqual([item.id for item in the_list.records(morty.id)], [json.loads(saved.body)["id"]])

    def test_report_filing_writes_the_number_and_owner_together(self) -> None:
        server = test_serve.ReviewLinksServerTests()
        self.addCleanup(server.doCleanups)
        server.setUp()
        case = server.review_fixture()
        owner = case.ready_submission_recipient()
        producer = fixture_member("producer", name="dwight")
        keeper = fixture_member("producer", name="morty")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        writes: list[tuple[str, str, str, str]] = []
        write_record = record_store.NumberedRecords.write

        def write(store: record_store.NumberedRecords, identity: str, fields: Mapping[str, str], body: str, *,
                  path: Path | None = None, new_record: bool = False) -> None:
            write_record(store, identity, fields, body, path=path, new_record=new_record)
            if store.collection == "list":
                writes.append((identity, fields["number"], fields["owner"], fields["producer"]))

        with (mock.patch.object(server.server, "review_cache", cache),
              mock.patch.object(server.server, "delivery_status", case.status),
              mock.patch.object(record_store.NumberedRecords, "write", autospec=True, side_effect=write),
              mock.patch.object(change_page, "send_to_staff") as send):
            cases: tuple[tuple[new_item_dialog.SubmissionKind | Literal["enhancement"], str], ...] = (
                (new_item_dialog.SubmissionKind.BUG, "list"), ("enhancement", "review"))
            for kind, page in cases:
                with self.subTest(kind=kind):
                    request = new_item_dialog.NewItemRequest(
                        "r6-" + uuid4().hex, kind, producer.id, "Keep the chosen owner.",
                        new_item_dialog.NoEvidence(), page=page, page_recipient=new_item_dialog.PageRecipient.PRODUCER,
                        assignment=new_item_dialog.ChosenAssignee(owner.id))
                    body = server.new_item_body(request)
                    writes.clear()
                    send.reset_mock()
                    if page == "review":
                        _ = write_config_fixture(conf.path("console"), "page.list.owner=missing\n")
                        failure = server.new_item_http(body, finished=True)
                        self.assertEqual(failure.status, 503, failure.body)
                        self.assertEqual(writes, [])
                        send.assert_not_called()
                    _ = write_config_fixture(conf.path("console"), f"page.list.owner={keeper.id}\n")
                    for _ in range(2):
                        reply = server.new_item_http(body, finished=True)
                        self.assertEqual(reply.status, 200, reply.body)
                        receipt = cast(dict[str, object], json.loads(reply.body))
                        number = receipt["list_number"]
                        self.assertEqual((receipt["list_owner"], receipt["list_producer"], receipt["reply_owner"]),
                                         (owner.name, producer.name, producer.id))
                        self.assertEqual(writes, [(f"L{number}", str(number), owner.id, producer.id)])
                    send.assert_called_once()

    def test_self_filed_items_give_the_owner_a_scoped_reply_from_both_entry_points(self) -> None:
        server = test_serve.ReviewLinksServerTests()
        self.addCleanup(server.doCleanups)
        server.setUp()
        case = server.review_fixture()
        assignee = case.ready_submission_recipient()
        owner = fixture_member("producer", name="native-owner")
        morty = fixture_member("producer", name="morty")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={morty.id}\n")
        _ = conf.path("producer_map").write_text(f"{assignee.id} = {owner.id}\n", encoding="utf-8")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        address = for_you.ConsoleAddress("127.0.0.1", server.port)
        received: list[tuple[str, str]] = []

        def capture(recipient: str, _sender: str, message: str, **_options: str) -> None:
            received.append((recipient, message.splitlines()[-1]))

        with (mock.patch.object(server.server, "review_cache", cache),
              mock.patch.object(server.server, "delivery_status", case.status),
              mock.patch.object(server.server.RequestHandlerClass, "log_request"),
              mock.patch.object(change_page, "send_message", side_effect=capture),
              mock.patch.object(for_you, "console_address", return_value=address),
              mock.patch("platform.system", return_value="Linux")):
            for item_type in REPLY_TYPES:
                with self.subTest(item_type=item_type):
                    output, errors = io.StringIO(), io.StringIO()
                    with (mock.patch.dict(os.environ, {"CODEX_THREAD_ID": owner.current_session}),
                          redirect_stdout(output), redirect_stderr(errors)):
                        outcome = new_item.main([item_type, assignee.name, "Check the command filing."])
                    self.assertEqual((outcome, errors.getvalue()), (new_item.Outcome.FILED, ""))
                    command_owner, command = received.pop()
                    self.assertEqual(command_owner, f"session:{owner.current_session}")
                    self.assertIn("List item ", output.getvalue())
                    self.assertTrue(output.getvalue().endswith(f". Reply command goes to {owner.name}, this session.\n"))
                    token = self.run_reply_command(command, "The command reply works.")
                    self.assertNotIn(token, output.getvalue())

                    kind = new_item.FILED_KINDS[item_type]
                    filed_kind = new_item_dialog.SubmissionKind.INVESTIGATION if kind == "enhancement" else kind
                    request = replace(case.submission_request(filed_kind, owner.id), kind=kind, page="list",
                                      request_id=f"r6-{uuid4().hex}",
                                      page_recipient=new_item_dialog.PageRecipient.PRODUCER,
                                      assignment=new_item_dialog.ChosenAssignee(assignee.id))
                    reply = server.new_item_http(server.new_item_body(request), finished=True)
                    self.assertEqual(reply.status, 200, reply.body)
                    receipt = cast(dict[str, object], json.loads(reply.body))
                    self.assertEqual(receipt["reply_owner"], owner.id)
                    row = the_list.load(morty.id, f"L{receipt['list_number']}")
                    self.assertEqual((row.owner, row.producer), (assignee.id, owner.id))
                    window_owner, command = received.pop()
                    self.assertEqual(window_owner, f"session:{owner.current_session}")
                    token = self.run_reply_command(command, "The window reply works.")
                    self.assertNotIn(token.encode(), reply.body)

            output, errors = io.StringIO(), io.StringIO()
            with (mock.patch.dict(os.environ, {"CODEX_THREAD_ID": morty.current_session}),
                  redirect_stdout(output), redirect_stderr(errors)):
                outcome = new_item.main(["bug", assignee.name, "Check the caller identity.",
                                         "--session", owner.current_session])
            self.assertEqual((outcome, errors.getvalue()), (new_item.Outcome.FILED, ""))
            self.assertNotIn("this session", output.getvalue())
            command_owner, command = received.pop()
            self.assertEqual(command_owner, f"session:{owner.current_session}")
            token = self.run_reply_command(command, "Only the owner received the command.")
            self.assertNotIn(token, output.getvalue())
            self.assertEqual(received, [])

    def test_bug_and_enhancement_use_the_list_from_the_new_item_window(self) -> None:
        server = test_serve.ReviewLinksServerTests()
        self.addCleanup(server.doCleanups)
        server.setUp()
        case = server.review_fixture()
        owner = case.ready_submission_recipient()
        keeper = fixture_member("producer", name="morty")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={keeper.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        with (mock.patch.object(server.server, "review_cache", cache),
              mock.patch.object(server.server, "delivery_status", case.status),
              mock.patch.object(change_page, "send_to_staff") as send):
            for kind, report_kind in ((new_item_dialog.SubmissionKind.BUG, the_list.ReportKind.BUG),
                                      ("enhancement", the_list.ReportKind.ENHANCEMENT)):
                with self.subTest(kind=kind):
                    words = f"The {report_kind} report keeps my words."
                    report = replace(case.submission_request(new_item_dialog.SubmissionKind.INVESTIGATION, keeper.id),
                                     kind=kind, page="list", request_id=f"r6-{uuid4().hex}", words=words,
                                     images=(test_review_submissions.IMAGE,),
                                     page_recipient=new_item_dialog.PageRecipient.PRODUCER,
                                     assignment=new_item_dialog.ChosenAssignee(owner.id))
                    response = server.new_item_http(server.new_item_body(report), finished=True)
                    self.assertEqual(response.status, 200, response.body)
                    fields = cast(dict[str, object], json.loads(response.body))
                    key = cast(str, fields["key"])
                    item = the_list.load(keeper.id, f'L{fields["list_number"]}')
                    self.assertIsInstance(item.report, the_list.ListReport)
                    assert isinstance(item.report, the_list.ListReport)
                    self.assertEqual((item.report.kind, item.report.evidence_key, item.report.words),
                                     (report_kind, key, words))
                    self.assertIs(item.state, the_list.ItemState.NEW)
                    snapshot = cache.get()
                    self.assertFalse(any(entry["key"] == key for entry in snapshot.entries))
                    self.assertIn(b"Open List item", snapshot.body(key))
                    self.assertNotIn(b"Mark as read", snapshot.body(key))
                    origin = review_items.submission_origin(snapshot.records[key])
                    assert isinstance(origin, new_item_dialog.ReviewSubmission)
                    image, = origin.images
                    connection = HTTPConnection("127.0.0.1", server.port, timeout=5)
                    self.addCleanup(connection.close)
                    connection.request("GET", review_page.attachment_url(key, image).replace("&amp;", "&"))
                    image_reply = connection.getresponse()
                    self.assertEqual(image_reply.status, 200)
                    self.assertEqual(image_reply.read(), b64decode(test_review_submissions.PNG))
                    self.assertIn("Evidence on the List", cast(str, send.call_args.args[2]))
                    source = snapshot.records[key].source
                    assert isinstance(source, review_items.InvestigationSource)
                    scoped = new_item_dialog.read_submission_key(source.record.submission_scope)
                    self.assertTrue(investigation.answer_submission(
                        source.path, scoped, "Check the menu again.", origin.reply_token, owner=keeper.id))
                    answered = review_page.build_snapshot(case.root).body(key)
                    self.assertIn(b"Check the menu again.", answered)
                    self.assertNotIn(b"data-review-form", answered)
                    self.assertNotIn(b"data-later-toggle", answered)
                    self.assertNotIn(b">Mark read</button>", answered)

    def test_report_recovery_waits_for_the_list_row_before_it_sends(self) -> None:
        server = test_serve.ReviewLinksServerTests()
        self.addCleanup(server.doCleanups)
        server.setUp()
        case = server.review_fixture()
        owner = case.ready_submission_recipient()
        keeper = fixture_member("producer", name="morty")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={keeper.id}\npage.review.owner={owner.id}\n")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        with (mock.patch.object(server.server, "review_cache", cache),
              mock.patch.object(server.server, "delivery_status", case.status),
              mock.patch.object(change_page, "send_to_staff") as send):
            for page, failure, recover in (("list", "disk", True), ("review", "disk", True),
                                           ("list", "keeper", True), ("list", "keeper", False)):
                with self.subTest(page=page, failure=failure, recover=recover):
                    send.reset_mock()
                    count = len(the_list.records(keeper.id))
                    request = replace(case.submission_request(new_item_dialog.SubmissionKind.BUG, keeper.id),
                                      page=page, request_id=f"r6-{uuid4().hex}",
                                      words=f"The {page} menu fails in case {count + 1}.",
                                      page_recipient=new_item_dialog.PageRecipient.PRODUCER,
                                      assignment=new_item_dialog.ChosenAssignee(owner.id))
                    if failure == "keeper":
                        _ = write_config_fixture(conf.path("console"), "page.list.owner=missing\n")
                        failed = server.new_item_http(server.new_item_body(request), finished=True)
                        review_actions.recover_submission_deliveries(case.status, review_page.PAGE_PATH)
                        self.assertEqual(len(the_list.records(keeper.id)), count)
                        _ = write_config_fixture(conf.path("console"), f"page.list.owner={keeper.id}\n")
                    else:
                        with mock.patch.object(the_list, "_save", side_effect=OSError("The List disk is full.")):
                            failed = server.new_item_http(server.new_item_body(request), finished=True)
                    self.assertEqual(failed.status, 503, failed.body)
                    send.assert_not_called()
                    native = next(record for record in investigation.records(keeper.id) if record.question == request.words)
                    key = review_items.investigation_record_key(native)
                    snapshot = review_page.build_snapshot(case.root)
                    self.assertFalse(any(entry["key"] == key for entry in snapshot.entries))

                    if recover:
                        review_actions.recover_submission_deliveries(case.status, review_page.PAGE_PATH)
                        self.assertEqual(len(the_list.records(keeper.id)), count + 1)
                        send.assert_called_once()
                    retry = server.new_item_http(server.new_item_body(request), finished=True)
                    self.assertEqual(retry.status, 200, retry.body)
                    destination = investigation.load(keeper.id, native.id).list_destination
                    assert isinstance(destination, investigation.ListDestination)
                    recovered = the_list.load(keeper.id, f"L{destination.number}")
                    assert isinstance(recovered.report, the_list.ListReport)
                    self.assertEqual((recovered.report.evidence_key, recovered.report.words), (key, request.words))
                    self.assertEqual(json.loads(retry.body)["list_number"], count + 1)
                    self.assertEqual(recovered.number, count + 1)
                    self.assertEqual(recovered.owner, owner.id)
                    self.assertEqual(len(the_list.records(keeper.id)), count + 1)
                    send.assert_called_once()

    def test_initial_report_delivery_checks_the_transferred_native_record(self) -> None:
        case = test_review_submissions.ReviewSubmissionTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        keeper = MORTY
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={keeper.id}\n")
        _ = write_config_fixture(conf.path("review_links"), "delivery_pause_seconds=0\n")
        _ = conf.path("producer_map").write_text("# Use the List producer.\n", encoding="utf-8")
        request = test_review_submissions.request(new_item_dialog.SubmissionKind.BUG)
        with investigation.file_for_list(investigation.InvestigationPurpose.BUG_REPORT):
            saved = review_submissions.submit(request)
        status = change_page.DeliveryStatus(case.root / "review")
        row = saved.request_id
        key = review_items.submission_review_key(saved)
        message = (f"{saved.words}\n\nEvidence in Review\nhttp://console/console/review.html#item={key}\n\n"
                   + change_page.reply_command(saved.key))
        record: change_page.StoredReviewMessage = {
            "source": str(saved.path), "row": row, "saved": True, "owner": saved.owner,
            "message": message, "submission_request": row,
            "scoped_reply": {"key": saved.key, "token_sha256": saved.details.reply_token_sha256},
        }
        page = change_page.ReviewMessagePage(saved.path, saved.path.parent, (row,), "", record)
        intent = change_page.DeliveryIntent(uuid4().hex, "verdict", recipients=(saved.owner,))
        with status.locked():
            status.write_slots(page, row, status.slots(page, row).updated("verdict", intent))
        delivery = change_page.MessageDelivery(page, row, intent.recipients, message, intent.attempt, (saved.owner,))
        wrapped = case.wrap_fixture()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(case.tidy_fixture(wrapped), (0, ""))
        _ = self.enterContext(mock.patch.object(staff, "snapshot", return_value=replace(wrapped, rows=(*wrapped.rows, keeper))))
        self.assertFalse(saved.path.exists())
        successor = test_review_submissions.PRODUCER
        with mock.patch.object(change_page, "send_to_staff") as send:
            self.assertIsInstance(change_page.deliver_with_outcome(delivery, status), change_page.DeliveryUnconfirmed)
            with self.assertRaisesRegex(change_page.ReportAwaitingList, "still needs a List item"):
                _ = change_page.resend_submission_reply(saved.key)
            send.assert_not_called()
            self.assertEqual(the_list.records(keeper.id), [])
            restarted = change_page.DeliveryStatus(status.root)
            review_actions.recover_interrupted_list_reports()
            change_page.recover_review_messages(restarted)
            send.assert_called_once()
            self.assertIn("Evidence on the List", cast(str, send.call_args.args[2]))
            self.assertIn("/console/list.html#", cast(str, send.call_args.args[2]))
            native = investigation.load(successor, saved.id)
            destination = native.list_destination
            assert isinstance(destination, investigation.ListDestination)
            self.assertEqual(destination.keeper, keeper.id)
            self.assertEqual([item.number for item in the_list.records(keeper.id)], [destination.number])
            review_actions.recover_submission_deliveries(status, review_page.PAGE_PATH)
            send.assert_called_once()

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for dialog behavior")
    def test_review_recovers_two_report_drafts_after_reload(self) -> None:
        probe = test_new_item_dialog.TWO_LIST_DRAFTS_PROBE.replace(
            "const prefix = 'console.list.pending-send.', probeKey = 'test-list-drafts';",
            """const probeKey = 'test-list-drafts';
const submitted = 'console.new-item.submitted';
function storedRecipient(id) {
 for (const key of [submitted, submitted + '.' + id]) {
  const draft = JSON.parse(sessionStorage.getItem(key) || 'null');
  if (draft?.payload.request_id === id) return draft.payload.recipient_id;
 }
 return null;
}""").replace("sessionStorage.getItem(prefix + ", "storedRecipient(")
        self.assertEqual(firefox_probe_results(test_new_item_dialog.bug_fixture_page(script=probe)), [[]])

    def test_resend_reply_reads_each_native_type_and_refuses_completed_or_unknown_items(self) -> None:
        server = test_serve.ReviewLinksServerTests()
        self.addCleanup(server.doCleanups)
        server.setUp()
        case = server.review_fixture()
        owner = case.ready_submission_recipient()
        with (mock.patch("platform.system", return_value="Linux"),
              mock.patch.object(change_page, "send_to_staff") as send,
              mock.patch.object(change_page.DeliveryStatus, "metadata_page",
                                side_effect=AssertionError("Resend read delivery metadata."))):
            for item_type in REPLY_TYPES:
                with self.subTest(item_type=item_type):
                    kind = new_item.FILED_KINDS[item_type]
                    filed_kind = new_item_dialog.SubmissionKind.INVESTIGATION if kind == "enhancement" else kind
                    request = replace(case.submission_request(filed_kind, owner.id), kind=kind)
                    saved = review_submissions.submit(request)
                    token = saved.details.reply_token
                    expected = change_page.reply_command(saved.key, token)
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        result = new_item.main(["resend-reply", saved.id])
                    self.assertEqual((result, errors.getvalue()), (0, ""))
                    self.assertEqual(output.getvalue(),
                                     f"Accepted the reply command for delivery to the owner of {saved.id}.\n")
                    send.assert_called_once_with(owner.id, change_page.SENDER,
                                                 f"Reply command for {saved.id}.\n\n{expected}")
                    send.reset_mock()
                    if item_type == new_item.ItemType.QUESTION:
                        with (mock.patch.object(change_page, "send_to_staff", side_effect=RuntimeError(token)),
                              redirect_stderr(errors)):
                            self.assertEqual(new_item.main(["resend-reply", saved.key]), new_item.Outcome.REFUSED)
                        self.assertEqual(errors.getvalue(), "new_item: [reply token]\n")
                        errors = io.StringIO()
                    self.assertEqual(self.run_reply_command(expected, "The resent reply works."), token)
                    if item_type == new_item.ItemType.BUG:
                        closed = investigation.mark_read(owner.id, saved.id)
                        self.assertEqual(closed.state, investigation.InvestigationState.READ)
                    with redirect_stderr(errors):
                        self.assertEqual(new_item.main(["resend-reply", saved.id]), new_item.Outcome.REFUSED)
                    self.assertEqual(errors.getvalue(), "new_item: The item is not waiting for a reply.\n")
                    send.assert_not_called()
            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertEqual(new_item.main(["resend-reply", "I999999"]), new_item.Outcome.REFUSED)
            self.assertEqual(errors.getvalue(), "new_item: No sent item has that ID.\n")
            send.assert_not_called()

    def test_resend_finds_a_transferred_item_and_holds_its_owner_lock_during_delivery(self) -> None:
        case = test_review_submissions.ReviewSubmissionTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        saved = review_submissions.submit(test_review_submissions.request(new_item_dialog.SubmissionKind.BUG))
        successor = test_review_submissions.PRODUCER
        read_items = review_items.read

        def move_after_read(root: Path, *, kinds: tuple[review_items.ReviewType, ...]) -> list[review_items.ReviewItem]:
            items = read_items(root, kinds=kinds)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(case.tidy_fixture(case.wrap_fixture()), (0, ""))
            return items

        def send(address: str, sender: str, message: str) -> None:
            self.assertEqual((address, sender), (f"session:{successor}", change_page.SENDER))
            self.assertEqual(message.splitlines()[-1], change_page.reply_command(saved.key, saved.reply_token))
            self.assertFalse(saved.path.exists())
            lock = case.root / "state" / "investigation-locks" / (successor + ".lock")
            self.assertEqual(file_lock.probe_existing_exclusive(lock), file_lock.LockAvailability.HELD)

        with (mock.patch.object(review_items, "read", side_effect=move_after_read),
              mock.patch.object(change_page, "send_message", side_effect=send) as delivered):
            self.assertEqual(change_page.resend_submission_reply(saved.id), saved.id)
        delivered.assert_called_once()

    def run_reply_command(self, command: str, words: str) -> str:
        arguments = shlex.split(command)
        self.assertEqual(arguments[:3], ["~/.claude/scripts/lib/py",
                                         "~/.claude/scripts/review_links/change_page.py", "answer-question"])
        self.assertEqual(arguments[-2:], ["--text", "<your answer>"])
        key = arguments[arguments.index("--item") + 1]
        token = arguments[arguments.index("--question") + 1]
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(staff, "is_owner", return_value=True),
              redirect_stdout(output), redirect_stderr(errors)):
            result = change_page.main([*arguments[2:-1], words])
        self.assertEqual((result, output.getvalue(), errors.getvalue()), (0, f"Answered {key}\n", ""))
        return token

    def test_identical_bugs_create_distinct_list_evidence_and_skip_review_queue(self) -> None:
        server = test_serve.ReviewLinksServerTests()
        self.addCleanup(server.doCleanups)
        server.setUp()
        case = server.review_fixture()
        document = server.root.parent / "bob"
        document.mkdir()
        bob = fixture_member("producer", name="bob", doc=staff.LiveDoc(local_machine(), document))
        morty = fixture_member("producer", name="morty")
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={morty.id}\n")
        _ = conf.path("producer_map").write_text("# Producers use their own rows.\n", encoding="utf-8")
        cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
        self.addCleanup(cache.close)
        address = for_you.ConsoleAddress("127.0.0.1", server.port)
        output, errors = io.StringIO(), io.StringIO()
        words = "A waiting row has no drag grip."
        arguments = ["bug", bob.name, words, "--session", "another-session"]
        with (mock.patch.object(server.server, "review_cache", cache),
              mock.patch.object(server.server, "delivery_status", case.status),
              mock.patch.object(server.server.RequestHandlerClass, "log_request"),
              mock.patch.object(for_you, "console_address", return_value=address),
              mock.patch("platform.system", return_value="Linux"),
              redirect_stdout(output), redirect_stderr(errors)):
            for _ in range(2):
                self.assertEqual(new_item.main(arguments), new_item.Outcome.FILED, errors.getvalue())
                self.assertTrue(server.new_item_posts.wait_until_none_run())
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(output.getvalue().splitlines(), ["Bug → bob. List item 1.", "Bug → bob. List item 2."])
        first, second = the_list.records(morty.id, include_closed=True)
        self.assertEqual((first.number, second.number), (1, 2))
        assert isinstance(first.report, the_list.ListReport)
        assert isinstance(second.report, the_list.ListReport)
        self.assertNotEqual(first.report.evidence_key, second.report.evidence_key)
        snapshot = cache.get()
        queued_keys = {entry["key"] for entry in snapshot.entries}
        for item, report in ((first, first.report), (second, second.report)):
            key = report.evidence_key
            self.assertEqual((item.owner, item.producer, item.unit, item.title), (bob.id, bob.id, "", words))
            self.assertEqual((report.kind, report.words), (the_list.ReportKind.BUG, words))
            self.assertEqual(item.reviews, ())
            self.assertNotIn(key, queued_keys)
            self.assertIn(b"Open List item", snapshot.body(key))
            self.assertEqual((snapshot.records[key].owner, snapshot.records[key].title), (bob.id, words))


if __name__ == "__main__":
    _ = unittest.main()
