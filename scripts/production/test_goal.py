"""Verify goal commands and card refreshes with private List records."""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime
from functools import cached_property
from io import StringIO
from typing import cast, final, override
from unittest.mock import patch

from scripts.production import goal, record_store, staff, the_list
from scripts.production.staff_test_support import StaffTestCase, fixture_member


@final
class GoalTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(cast(AbstractContextManager[object], patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.root / "state")})))

    @cached_property
    def keeper(self) -> staff.StaffRow:
        return fixture_member("producer", name="morty")

    @cached_property
    def producer(self) -> staff.StaffRow:
        return fixture_member("producer", name="goal-producer")

    def add(self, title: str, producer: staff.StaffId) -> the_list.ListItem:
        return the_list.add(self.keeper.id, title, owner=the_list.UnassignedOwner.NATE, source="nate",
                            changes={"producer": producer})

    def copy_to_keeper(self, item: the_list.ListItem, keeper: staff.StaffId) -> None:
        folder = the_list.folder(keeper)
        folder.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(the_list.folder(self.keeper.id) / f"{item.id}.md", folder / f"{item.id}.md")

    def run_goal(self, *args: str, caller: staff.StaffRow | None = None) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        row = caller if caller is not None else self.producer
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": row.current_session, "CODEX_THREAD_ID": ""}),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            code = the_list.goal_main(list(args))
        return code, stdout.getvalue().strip(), stderr.getvalue().strip()

    def test_set_replaces_the_goal_and_keeps_each_producers_goal_separate(self) -> None:
        first = self.add("First goal", self.producer.id)
        second = self.add("Second goal", self.producer.id)
        other = self.add("Keeper goal", self.keeper.id)
        self.assertEqual(self.run_goal("set", first.id)[0], 0)
        self.assertEqual(self.run_goal("note", "--blocked", "Waiting for review.", "--faster", "Review now.")[0], 0)
        self.assertEqual(self.run_goal("set", first.id)[0], 0)
        self.assertIn("Blocked by: Waiting for review.", goal.read_card(self.producer.id))
        self.assertEqual(self.run_goal("set", other.id, caller=self.keeper)[0], 0)
        self.assertEqual(self.run_goal("set", other.id),
                         (1, "", f"goal: {other.id} is assigned to another producer."))
        self.assertEqual(self.run_goal("set", second.id)[0], 0)
        card = goal.read_card(self.producer.id)
        self.assertIn(f"Main goal: {second.id} Second goal", card)
        self.assertIn("Last note: none recorded", card)
        self.assertIn(f"Main goal: {other.id} Keeper goal", goal.read_card(self.keeper.id))

    def test_card_uses_the_saved_list_state_eta_and_note(self) -> None:
        item = self.add("Deliver the page", self.producer.id)
        self.assertEqual(self.run_goal("card"), (0, "No main goal set.", ""))
        self.assertEqual(self.run_goal("set", item.id)[0], 0)
        self.assertIn("ETA: none recorded", goal.read_card(self.producer.id))
        eta = datetime(2026, 11, 25, 20, 0, tzinfo=UTC)
        _ = the_list.update(self.keeper.id, item.id, {"state": the_list.ItemState.BLOCKED, "eta": eta}, source="morty")
        with patch.object(the_list, "now", return_value=datetime(2026, 11, 24, 19, 0, tzinfo=UTC)):
            self.assertEqual(self.run_goal("note", "--blocked", "The Mac is locked.",
                                           "--faster", "Unlock the Mac.")[0], 0)
        expected = (
            f"Main goal: {item.id} Deliver the page\n"
            "State: Blocked\n"
            "ETA: 2026-11-25T20:00:00+00:00\n"
            "Blocked by: The Mac is locked.\n"
            "Faster if: Unlock the Mac.\n"
            "Last note: 2026-11-24T19:00:00+00:00"
        )
        self.assertEqual(goal.read_card(self.producer.id), expected)
        self.assertEqual(self.run_goal("card"), (0, expected, ""))

    def test_list_writes_refresh_only_the_selected_goal(self) -> None:
        marked = self.add("Publish the page", self.producer.id)
        other = self.add("Change the app", self.producer.id)
        self.assertEqual(self.run_goal("set", marked.id)[0], 0)
        self.assertEqual(self.run_goal("note", "--blocked", "Review waits.", "--faster", "Review now.")[0], 0)
        path = goal.card_path(self.producer.id)
        before = path.stat().st_ino
        _ = the_list.update(self.keeper.id, other.id, {"state": the_list.ItemState.WORKING}, source="morty")
        self.assertEqual(path.stat().st_ino, before)
        _ = the_list.update(self.keeper.id, marked.id, {
            "title": "Publish the panel", "state": the_list.ItemState.WORKING, "eta": date(2026, 11, 25),
        }, source="morty")
        card = goal.read_card(self.producer.id)
        self.assertIn(f"Main goal: {marked.id} Publish the panel\nState: Working\nETA: 2026-11-25", card)
        self.assertIn("Blocked by: Review waits.", card)
        _ = the_list.close(self.keeper.id, marked.id, "The panel is live.", source="nate")
        self.assertIn("State: Closed", goal.read_card(self.producer.id))

    def test_goal_defaults_to_the_callers_store_and_refreshes_for_that_keeper(self) -> None:
        item = self.add("Morty's copy", self.producer.id)
        self.copy_to_keeper(item, self.producer.id)
        _ = the_list.update(self.producer.id, item.id, {"title": "Producer's copy"}, source=self.producer.name)

        with staff.supplied_snapshot(staff.StaffSnapshot((self.producer,))):
            code, card, errors = self.run_goal("set", item.id)
            self.assertEqual((code, errors), (0, ""))
            self.assertIn("Producer's copy", card)
            self.assertEqual(self.run_goal("note", "--blocked", "Review waits.", "--faster", "Review now.")[0], 0)

        before = goal.read_card(self.producer.id)
        _ = the_list.update(self.keeper.id, item.id, {"state": the_list.ItemState.BLOCKED}, source="morty")
        self.assertEqual(goal.read_card(self.producer.id), before)
        _ = the_list.update(self.producer.id, item.id, {"state": the_list.ItemState.WORKING}, source=self.producer.name)
        card = goal.read_card(self.producer.id)
        self.assertIn("State: Working", card)
        self.assertIn("Blocked by: Review waits.", card)
        self.assertEqual(self.run_goal("card"), (0, card, ""))

    def test_goal_keeps_the_explicit_keeper_for_notes_and_card_rebuilds(self) -> None:
        legacy = fixture_member("producer", name="legacy-keeper")
        item = self.add("Morty's copy", self.producer.id)
        other = self.add("Another producer's item", legacy.id)
        self.copy_to_keeper(item, legacy.id)
        self.copy_to_keeper(other, legacy.id)
        self.copy_to_keeper(item, self.producer.id)
        _ = the_list.update(legacy.id, item.id, {"title": "Legacy copy"}, source="morty")

        self.assertEqual(self.run_goal("set", other.id, "--keeper", legacy.id),
                         (1, "", f"goal: {other.id} is assigned to another producer."))
        code, card, errors = self.run_goal("set", item.id, "--keeper", legacy.id)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("Legacy copy", card)
        self.assertEqual(self.run_goal("note", "--blocked", "Review waits.", "--faster", "Review now.")[0], 0)

        _ = the_list.update(legacy.id, item.id, {"state": the_list.ItemState.WORKING}, source="morty")
        card = goal.read_card(self.producer.id)
        self.assertIn("Legacy copy\nState: Working", card)
        self.assertIn("Faster if: Review now.", card)
        self.assertEqual(self.run_goal("card"), (0, card, ""))

    def test_reassignment_removes_the_item_details_from_the_old_producers_card(self) -> None:
        item = self.add("Publish the page", self.producer.id)
        self.assertEqual(self.run_goal("set", item.id)[0], 0)
        _ = the_list.move_open_producer(self.keeper.id, item.id, self.producer.id, self.keeper.id, source="morty")
        card = goal.read_card(self.producer.id)
        self.assertIn(f"Main goal: {item.id}\nState: List item unavailable\nETA: none recorded", card)
        self.assertEqual(self.run_goal("card"), (0, card, ""))
        self.assertEqual(goal.read_card(self.keeper.id), "No main goal set.")

    def test_failed_refresh_leaves_a_stale_card_until_manual_rebuild(self) -> None:
        item = self.add("Publish the panel", self.producer.id)
        self.assertEqual(self.run_goal("set", item.id)[0], 0)
        self.assertEqual(self.run_goal("note", "--blocked", "Review waits.", "--faster", "Review now.")[0], 0)
        errors = StringIO()
        with (patch.object(goal, "save", side_effect=OSError("Refresh write failed.")), redirect_stderr(errors)):
            _ = the_list.update(self.keeper.id, item.id, {"state": the_list.ItemState.WORKING}, source="morty")
        self.assertIn("Refresh write failed.", errors.getvalue())
        self.assertEqual(the_list.load(self.keeper.id, item.id).state, the_list.ItemState.WORKING)
        self.assertEqual(goal.read_card(self.producer.id), "Card is stale: run goal.py card.")
        code, card, stderr = self.run_goal("card")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("State: Working", card)
        self.assertIn("Blocked by: Review waits.\nFaster if: Review now.", card)
        self.assertEqual(goal.read_card(self.producer.id), card)

    def test_goal_read_failure_leaves_the_card_stale_after_the_read_recovers(self) -> None:
        item = self.add("Publish the panel", self.producer.id)
        self.assertEqual(self.run_goal("set", item.id)[0], 0)

        def fail_read(producer: staff.StaffId) -> goal.GoalRecord:
            self.assertEqual(goal.read_card(producer), "Card is stale: run goal.py card.")
            raise OSError("Goal read failed.")

        errors = StringIO()
        with patch.object(goal, "load", side_effect=fail_read) as read, redirect_stderr(errors):
            _ = the_list.update(self.keeper.id, item.id, {"state": the_list.ItemState.WORKING}, source="morty")
        read.assert_called_once_with(self.producer.id)
        self.assertIn("Goal read failed.", errors.getvalue())
        self.assertEqual(the_list.load(self.keeper.id, item.id).state, the_list.ItemState.WORKING)
        self.assertIsInstance(goal.load(self.producer.id), dict)
        self.assertEqual(goal.read_card(self.producer.id), "Card is stale: run goal.py card.")

    def test_card_command_rebuilds_a_malformed_card_from_the_saved_goal(self) -> None:
        item = self.add("Publish the panel", self.producer.id)
        self.assertEqual(self.run_goal("set", item.id)[0], 0)
        self.assertEqual(self.run_goal("note", "--blocked", "Review waits.", "--faster", "Review now.")[0], 0)
        _ = the_list.update(self.keeper.id, item.id, {"state": the_list.ItemState.WORKING}, source="morty")
        expected = goal.read_card(self.producer.id)
        path = goal.goal_path(self.producer.id)
        saved = path.read_bytes()
        for malformed in (b"{", b'{"card": null}', b"\xff"):
            with self.subTest(malformed=malformed):
                _ = goal.card_path(self.producer.id).write_bytes(malformed)
                self.assertEqual(self.run_goal("card"), (0, expected, ""))
                self.assertEqual(goal.read_card(self.producer.id), expected)
                self.assertEqual(path.read_bytes(), saved)

    def test_unpublished_list_write_restores_the_card(self) -> None:
        item = self.add("Publish the panel", self.producer.id)
        self.assertEqual(self.run_goal("set", item.id)[0], 0)
        before = goal.read_card(self.producer.id)
        with (patch.object(record_store.NumberedRecords, "write", side_effect=OSError("List write failed.")),
              self.assertRaisesRegex(OSError, "List write failed")):
            _ = the_list.update(self.keeper.id, item.id, {"state": the_list.ItemState.WORKING}, source="morty")
        self.assertEqual(the_list.load(self.keeper.id, item.id), item)
        self.assertEqual(goal.read_card(self.producer.id), before)

    def test_list_write_stops_if_the_card_cannot_be_marked_stale(self) -> None:
        item = self.add("Publish the panel", self.producer.id)
        self.assertEqual(self.run_goal("set", item.id)[0], 0)
        before = goal.read_card(self.producer.id)
        with (patch.object(goal, "write_text_atomic", side_effect=OSError("Stale card write failed.")),
              self.assertRaisesRegex(OSError, "Stale card write failed")):
            _ = the_list.update(self.keeper.id, item.id, {"state": the_list.ItemState.WORKING}, source="morty")
        self.assertEqual(the_list.load(self.keeper.id, item.id), item)
        self.assertEqual(goal.read_card(self.producer.id), before)

    def test_deep_goal_record_does_not_fail_a_list_write(self) -> None:
        item = self.add("Publish the panel", self.producer.id)
        self.assertEqual(self.run_goal("set", item.id)[0], 0)
        depth = sys.getrecursionlimit() + 1
        _ = goal.goal_path(self.producer.id).write_text("[" * depth + "]" * depth, encoding="utf-8")
        errors = StringIO()
        with redirect_stderr(errors):
            _ = the_list.update(self.keeper.id, item.id, {"state": the_list.ItemState.WORKING}, source="morty")
        self.assertEqual(errors.getvalue(), f"Main goal for {self.producer.id}: Invalid main goal record.\n")
        self.assertEqual(the_list.load(self.keeper.id, item.id).state, the_list.ItemState.WORKING)
        self.assertEqual(goal.read_card(self.producer.id), "Card is stale: run goal.py card.")

    def test_card_command_reports_a_malformed_goal_on_one_line(self) -> None:
        item = self.add("Publish the panel", self.producer.id)
        self.assertEqual(self.run_goal("set", item.id)[0], 0)
        depth = sys.getrecursionlimit() + 1
        path = goal.goal_path(self.producer.id)
        for malformed in (b"{", b'{"keeper": null}', b"\xff", b"[" * depth + b"]" * depth):
            with self.subTest(malformed=malformed[:20]):
                _ = path.write_bytes(malformed)
                self.assertEqual(self.run_goal("card"), (1, "", "goal: Invalid main goal record."))
                self.assertEqual(path.read_bytes(), malformed)
