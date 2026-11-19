"""Test The List against private staff records and state folders."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast, final, override
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from scripts.agents.remote import local_machine
from scripts.lib import atomic_file
from scripts.production import change_review, investigation, record_store, staff, the_list, update_registration
from scripts.production.test_investigation import OWNER, OTHER, RUNNER
from scripts.review_links import change_page, list_page, question_record, review_items

MORTY = staff.StaffRow(staff.StaffId("536997b5-4415-4cb1-b683-d777e02e9a22"), "producer", "morty", staff.StaffRoot(),
                       current_session=staff.StaffId("20fa30c6-3cad-493a-b74a-0e408a93f865"))
BOB = staff.StaffRow(staff.StaffId("a02a55c8-a2c4-47d9-bf99-fd30ffe75247"), "producer", "bob", staff.StaffRoot(),
                     current_session=staff.StaffId("32c954ea-25b3-44e7-9eed-3218ce9df1d6"))
START = datetime(2026, 11, 16, 18, tzinfo=UTC)


class ProducerScopedUpdate(Protocol):
    def __call__(self, keeper: staff.StaffId, identity: str, changes: the_list.ItemChanges, *,
                 source: str, assigned_to: staff.StaffId) -> the_list.ListEdit: ...


scoped_update = cast(ProducerScopedUpdate, getattr(the_list, "_update"))


@final
class TheListTests(unittest.TestCase):
    root: Path = Path()

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.notices: list[tuple[staff.StaffId, str]] = []
        self.send_patch = patch.object(the_list, "_send_assignment_notice", side_effect=self.record_notice)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staff_file = self.root / "staff.json"
        rows = [row.to_json() for row in (MORTY, RUNNER, OWNER, OTHER, BOB)]
        _ = staff_file.write_text(json.dumps({"schema": 1, "rows": rows}))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "STAFF_FILE": str(staff_file),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "CODEX_THREAD_ID": "",
            "CLAUDE_CODE_SESSION_ID": MORTY.current_session}))
        _ = self.enterContext(patch.object(the_list, "now", return_value=START))
        _ = self.send_patch.start()
        self.addCleanup(self.send_patch.stop)

    def record_notice(self, recipient: staff.StaffRow, text: str) -> None:
        self.notices.append((recipient.id, text))

    def command(self, *args: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = the_list.main(list(args))
        return result, output.getvalue(), errors.getvalue()

    def add(self, title: str = "Deliver the page", *, number: int | the_list.NextNumber = the_list.NextNumber.AVAILABLE,
            changes: the_list.ItemChanges | None = None) -> the_list.ListItem:
        return the_list.add(MORTY.id, title, owner=RUNNER.id, source="nate", number=number,
                            changes=changes if changes is not None else {})

    def test_commands_preserve_fields_text_and_history(self) -> None:
        content = "Use the result.\r\n\n\n## Latest status\n\nThis heading stays in the text.\n"
        result = self.command("add", "--title", 'The page: "List"', "--owner", RUNNER.id,
            "--number", "106", "--source", "nate", "--state", "Working", "--unit", "unit-board",
            "--next-check", "2026-11-16T19:00:00+00:00", "--eta", "2026-11-16T20:00:00+00:00/2026-11-16T21:00:00+00:00",
            "--next-step", content, "--latest-status", content, "--evidence", content, "--done-when", content,
            "--requested-by", "dwight")
        self.assertEqual(result, (0, "L106\n", ""))
        item = the_list.load(MORTY.id, "L106")
        self.assertEqual((item.number, item.owner, item.unit, item.state, item.requested_by),
            (106, RUNNER.id, "unit-board", the_list.ItemState.WORKING, the_list.RequestedBy.DWIGHT))
        self.assertEqual((item.next_step, item.latest_status, item.evidence, item.done_when), (content,) * 4)
        self.assertEqual((item.opened, item.updated, item.unread_since), (START,) * 3)
        self.assertIs(item.closed, the_list.NoTimestamp.UNSET)
        self.assertEqual(item.eta, the_list.EtaRange(START + timedelta(hours=2), START + timedelta(hours=3)))
        path = the_list.folder(MORTY.id) / "L106.md"
        initial_log = item.log[0]
        self.assertEqual(initial_log.time, START)
        self.assertIn('"action": "Added"', path.read_text())
        self.assertNotIn("keeper:", path.read_text())
        self.assertEqual(initial_log.what, the_list.FieldChange("Added", {
            "title": item.title, "owner": RUNNER.id, "producer": MORTY.id, "state": the_list.ItemState.WORKING,
            "eta": item.eta, "next_step": content, "latest_status": content, "unit": "unit-board",
            "next_check": START + timedelta(hours=1), "done_when": content, "evidence": content,
            "requested_by": the_list.RequestedBy.DWIGHT}))
        self.assertEqual(self.command("mark-read", "--id", item.id, "--source", "nate"), (0, "", ""))
        self.assertIs(the_list.load(MORTY.id, item.id).unread_since, the_list.NoTimestamp.UNSET)
        with patch.object(the_list, "now", return_value=START + timedelta(minutes=10)):
            self.assertEqual(self.command("update", "--id", item.id, "--state", "Delivered", "--eta", "Delivered",
                "--next-step", "Review the page.", "--latest-status", "The page is available.", "--source", "sdlc report"), (0, "", ""))
        item = the_list.load(MORTY.id, item.id)
        self.assertEqual((item.state, item.eta, item.latest_status),
                         (the_list.ItemState.DELIVERED, the_list.EtaStatus.DELIVERED, "The page is available."))
        self.assertEqual(item.unread_since, START + timedelta(minutes=10))
        self.assertEqual([entry.source for entry in item.log], ["nate", "nate", "sdlc report"])
        self.assertEqual(item.log[-1].what, the_list.FieldChange("Updated", {
            "state": the_list.ItemState.DELIVERED, "eta": the_list.EtaStatus.DELIVERED,
            "next_step": "Review the page.", "latest_status": "The page is available."}))
        self.assertEqual(item.log[0], initial_log)
        self.assertEqual(item.next_step, "Review the page.")
        self.assertIn('"action": "Marked read"', path.read_text())
        self.assertEqual(item.log[1], the_list.ItemChange(START, the_list.MarkedRead(), "nate"))
        self.assertIn("| # | Owner | Producer | Item | State | Next step | ETA | Update |", self.command("list")[1])
        self.assertIn('| 106 | show | morty | The page: "List" | Delivered;', self.command("list")[1])

    def test_producer_takes_a_producer_name_or_staff_id_and_shows_its_name(self) -> None:
        self.assertEqual(self.command("add", "--title", "Plan the year", "--owner", RUNNER.id, "--producer", "dwight",
                                      "--source", "nate"), (0, "L1\n", ""))
        self.assertEqual(the_list.load(MORTY.id, "L1").producer, OTHER.id)
        self.assertEqual(self.command("add", "--title", "Ship it", "--owner", RUNNER.id, "--source", "nate"), (0, "L2\n", ""))
        self.assertEqual(the_list.load(MORTY.id, "L2").producer, MORTY.id)
        self.assertEqual(self.command("update", "--id", "L2", "--producer", OTHER.id, "--source", "morty"), (0, "", ""))
        item = the_list.load(MORTY.id, "L2")
        self.assertEqual(item.producer, OTHER.id)
        self.assertEqual(item.log[-1].what, the_list.FieldChange("Updated", {"producer": OTHER.id}))
        self.assertEqual(self.command("update", "--id", "L2", "--producer", "morty", "--source", "morty"), (0, "", ""))
        self.assertEqual(the_list.load(MORTY.id, "L2").producer, MORTY.id)
        table = self.command("list")[1]
        self.assertIn("| 1 | show | dwight | Plan the year |", table)
        self.assertIn("| 2 | show | morty | Ship it |", table)

    def test_open_saves_refuse_a_retired_producer_even_with_a_supplied_snapshot(self) -> None:
        open_item = self.add("Update this")
        old_item = self.add("Close this", changes={"producer": OTHER.id})
        closed_item = self.add("Keep closed", changes={"producer": OTHER.id})
        closed_item = the_list.close(MORTY.id, closed_item.id, "Done", source="fixture")
        with staff.supplied_snapshot(staff.snapshot()):
            _ = staff.set_producer_retirement(OTHER.id, staff.RetiredProducer("claude:sonnet:high"))
            self.assertEqual(staff.snapshot().by_id(OTHER.id), OTHER)
            for arguments in (
                ("add", "--title", "Add this", "--owner", RUNNER.id, "--producer", OTHER.id),
                ("update", "--id", open_item.id, "--producer", OTHER.id),
                ("reopen", "--id", closed_item.id),
            ):
                with self.subTest(command=arguments[0]):
                    code, _, errors = self.command(*arguments, "--source", "fixture")
                    self.assertEqual(code, 1)
                    self.assertIn(f"{OTHER.name}: retired producer. Choose an active producer.", errors)
            self.assertEqual(the_list.records(MORTY.id, include_closed=True), [open_item, old_item, closed_item])
            closed = the_list.close(MORTY.id, old_item.id, "Done", source="fixture")
            self.assertEqual((closed.state, closed.producer), (the_list.ItemState.CLOSED, OTHER.id))
            self.assertEqual(the_list.load(MORTY.id, old_item.id), closed)

    def test_move_open_producer_preserves_closed_items_and_retries_without_a_log_entry(self) -> None:
        current = self.add("Move this", changes={"producer": OTHER.id})
        closed = self.add("Keep this", changes={"producer": OTHER.id})
        closed = the_list.close(MORTY.id, closed.id, "Closed for this test", source="fixture")
        for _ in range(2):
            moved = the_list.move_open_producer(MORTY.id, current.id, OTHER.id, MORTY.id, source="agents retire")
            self.assertEqual(moved.producer, MORTY.id)
            self.assertEqual(len(moved.log), len(current.log) + 1)
            self.assertEqual(moved.log[-1].source, "agents retire")
            self.assertEqual(the_list.move_open_producer(MORTY.id, closed.id, OTHER.id, MORTY.id,
                                                        source="agents retire"), closed)
        notes = the_list.folder(MORTY.id) / "L-notes.md"
        _ = notes.write_text("# Notes\n")
        with the_list.require_no_open_producer_items(MORTY.id, OTHER.id):
            self.assertEqual(notes.read_text(), "# Notes\n")

    def test_assignment_sends_both_notices_after_save(self) -> None:
        item = self.add("Old title", changes={"latest_status": "The build is complete."})
        _ = the_list.park(MORTY.id, item.id, START + timedelta(days=1), source="morty")
        launcher = "~/.claude/scripts/lib/py ~/.claude/scripts/production/the_list.py"

        def check_saved(recipient: staff.StaffRow, text: str) -> None:
            identity = "L2" if "item 2:" in text else item.id
            saved = the_list.load(MORTY.id, identity)
            self.assertEqual((saved.title, saved.owner, saved.producer), ("Build plan", RUNNER.id, OTHER.id))
            self.record_notice(recipient, text)

        with patch.object(the_list, "_send_assignment_notice", side_effect=check_saved):
            self.assertEqual(self.command("update", "--id", item.id, "--title", "Build plan", "--producer", OTHER.id,
                                          "--source", "morty"), (0, "", ""))
            self.assertEqual(self.command("add", "--title", "Build plan", "--owner", RUNNER.id, "--producer", "dwight",
                                          "--source", "morty"), (0, "L2\n", ""))

        for number in (1, 2):
            with self.subTest(number=number):
                producer, showrunner = self.notices[(number - 1) * 2:number * 2]
                self.assertEqual(producer, (OTHER.id, f"You are the producer of item {number}: Build plan. "
                    + f"Its showrunner is show.\nRun `{launcher} list --all --latest-status` to read it. "
                    + f"Run `{launcher} update --id L{number}` to update it. "
                    + "Use any of `--latest-status`, `--next-step`, `--eta`, `--state`, `--next-check`."))
                self.assertEqual(showrunner, (RUNNER.id, f"dwight is the producer of item {number}: Build plan. "
                    + "Send design questions on this item to dwight."))
        self.assertEqual(len(self.notices), 4)
        read_command = self.notices[0][1].split("`")[1]
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            result, output, error = self.command(*read_command.split()[2:])
        self.assertEqual((result, error), (0, ""))
        self.assertIn("| Latest status |", output)
        self.assertIn("| 1 | show | dwight | Build plan | New; parked until", output)
        self.assertIn("| The build is complete. |", output)

    def test_assignment_sends_no_notice_for_same_producer_or_morty(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        self.assertEqual(self.command("update", "--id", item.id, "--producer", "dwight", "--next-step", "Read the plan.",
                                      "--source", "morty"), (0, "", ""))
        self.assertEqual(self.command("update", "--id", item.id, "--producer", "morty", "--source", "morty"), (0, "", ""))
        self.assertEqual(self.command("add", "--title", "New item", "--owner", RUNNER.id, "--producer", "morty",
                                      "--source", "morty"), (0, "L2\n", ""))
        self.assertEqual(self.notices, [])

    def test_assignment_reports_failed_notice_and_keeps_assignment(self) -> None:
        item = self.add()

        def refuse_producer(recipient: staff.StaffRow, text: str) -> None:
            self.record_notice(recipient, text)
            if recipient.id == OTHER.id:
                raise OSError("Sender failed")

        args = ("update", "--id", item.id, "--producer", "dwight", "--source", "morty")
        with patch.object(the_list, "_send_assignment_notice", side_effect=refuse_producer):
            result, output, error = self.command(*args)
            self.assertEqual((result, output), (1, ""))
            self.assertEqual(error, f"the_list: {item.id} was saved. The producer notice to {OTHER.id} failed. Sender failed\n")
            saved = the_list.load(MORTY.id, item.id)
            self.assertEqual(saved.producer, OTHER.id)
            self.assertEqual([recipient for recipient, _ in self.notices], [OTHER.id, RUNNER.id])
            self.assertEqual(self.command(*args), (0, "", ""))
            self.assertEqual(the_list.load(MORTY.id, item.id), saved)
        self.assertEqual(len(self.notices), 2)

    def test_assignment_reports_queued_notice(self) -> None:
        item = self.add()
        self.send_patch.stop()
        with patch.object(subprocess, "run", side_effect=(
            subprocess.CompletedProcess([], 0, "SENT: delivered\n", ""),
            subprocess.CompletedProcess([], 1, "QUEUED: showrunner is away\n", ""),
        )) as send:
            result, output, error = self.command("update", "--id", item.id, "--producer", "dwight", "--source", "morty")
        self.assertEqual((result, output), (1, ""))
        self.assertIn(f"The showrunner notice to {RUNNER.id} failed. QUEUED: showrunner is away", error)
        self.assertEqual(the_list.load(MORTY.id, item.id).producer, OTHER.id)
        self.assertEqual([call.args[0][2:8] for call in send.call_args_list],
                         [["--to", OTHER.id, "--recipient-kind", "producer", "--from", "morty"],
                          ["--to", RUNNER.id, "--recipient-kind", "showrunner", "--from", "morty"]])

    def test_assignment_notifies_showrunner_if_the_producer_retires_after_save(self) -> None:
        item = self.add()
        members = staff.snapshot()
        retired = replace(members, rows=tuple(row for row in members.rows if row.id != OTHER.id))
        with patch.object(staff, "snapshot", side_effect=(members, retired)):
            result, output, error = self.command("update", "--id", item.id, "--producer", "dwight", "--source", "morty")
        self.assertEqual((result, output), (1, ""))
        self.assertEqual(error, f"the_list: {item.id} was saved. The producer notice to {OTHER.id} failed. "
                         + "The producer has no current producer staff row.\n")
        self.assertEqual(the_list.load(MORTY.id, item.id).producer, OTHER.id)
        self.assertEqual(self.notices, [(RUNNER.id,
            f"The saved assignment for item {item.number}: {item.title} needs a new producer.")])

    def test_assignment_reports_both_unsent_notices_if_staff_refresh_fails(self) -> None:
        item = self.add()
        members = staff.snapshot()
        args = ("update", "--id", item.id, "--producer", "dwight", "--source", "morty")
        with patch.object(staff, "snapshot", side_effect=(members, staff.StaffUnreadable("Staff unavailable."), members)):
            result, output, error = self.command(*args)
            self.assertEqual((result, output), (1, ""))
            self.assertEqual(error, f"the_list: {item.id} was saved. The producer and showrunner notices were not sent. "
                             + "Staff unavailable.\n")
            self.assertEqual(the_list.load(MORTY.id, item.id).producer, OTHER.id)
            self.assertEqual(self.command(*args), (0, "", ""))
        self.assertEqual(self.notices, [])

    def test_assignment_sends_nothing_if_save_fails(self) -> None:
        item = self.add()
        with patch.object(os, "replace", side_effect=OSError("The disk refused replacement")):
            result, _, error = self.command("update", "--id", item.id, "--producer", "dwight", "--source", "morty")
        self.assertEqual(result, 1)
        self.assertIn("The disk refused replacement", error)
        self.assertEqual(the_list.load(MORTY.id, item.id), item)
        self.assertEqual(self.notices, [])

    def test_assignment_reports_missing_showrunner(self) -> None:
        result, output, error = self.command("add", "--title", "Build plan", "--owner", "nate", "--producer", "dwight",
                                            "--source", "morty")
        self.assertEqual((result, output), (1, "L1\n"))
        self.assertIn("The showrunner notice to nate failed. the item has no assigned showrunner", error)
        self.assertEqual(the_list.load(MORTY.id, "L1").producer, OTHER.id)
        self.assertEqual([recipient for recipient, _ in self.notices], [OTHER.id])
        self.assertIn("The item has no assigned showrunner.", self.notices[0][1])

    def test_assignment_refuses_notice_to_an_owner_without_a_current_showrunner_row(self) -> None:
        missing = staff.StaffId("4bd6593c-bd3b-4de7-ae06-bd5186db2f4a")
        for owner in (OWNER.id, missing):
            with self.subTest(owner=owner):
                item = the_list.add(MORTY.id, "Build plan", owner=owner, source="morty", changes={})
                self.notices.clear()
                result, output, error = self.command(
                    "update", "--id", item.id, "--producer", "dwight", "--source", "morty")
                self.assertEqual((result, output), (1, ""))
                self.assertEqual(error, f"the_list: {item.id} was saved. The showrunner notice to {owner} failed. "
                                 + "the owner has no current showrunner staff row\n")
                self.assertEqual(the_list.load(MORTY.id, item.id).producer, OTHER.id)
                self.assertEqual([recipient for recipient, _ in self.notices], [OTHER.id])
                self.assertIn("The item has no assigned showrunner.", self.notices[0][1])

    def test_producer_lists_only_its_items_from_mortys_store(self) -> None:
        own = self.add("Dwight's item", changes={"producer": OTHER.id})
        _ = self.add("Morty's item")
        foreign = self.add("Bob's item", changes={"producer": BOB.id})
        path = the_list.folder(MORTY.id) / f"{foreign.id}.md"
        _ = path.write_text(path.read_text().replace("## Next step", "## Invalid heading"))
        _ = the_list.add(OTHER.id, "Wrong store", owner=RUNNER.id, source="dwight", changes={})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            result, output, error = self.command("list", "--all")
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(output.splitlines()[2:], [f"| {own.number} | show | dwight | Dwight's item | New |  | Not asked | ● |"])

    def test_producer_reports_an_incomplete_list_without_record_details(self) -> None:
        damaged = self.add("Private title", changes={"producer": OTHER.id})
        path = the_list.folder(MORTY.id) / f"{damaged.id}.md"
        _ = path.write_text(path.read_text().replace('title: "Private title"', 'title: invalid private text'))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            for visible in ((), ("| 2 | show | dwight | Visible item | New |  | Not asked | ● |",)):
                with self.subTest(visible=visible):
                    if visible:
                        _ = self.add("Visible item", changes={"producer": OTHER.id})
                    result, output, error = self.command("list", "--all")
                    self.assertEqual(result, 1)
                    self.assertEqual(error, "the_list: The List results are incomplete. Could not read every record.\n")
                    self.assertEqual(tuple(output.splitlines()[2:]), visible)

    def test_producer_reports_an_incomplete_list_if_the_directory_cannot_be_read(self) -> None:
        _ = self.add("Private title", changes={"producer": OTHER.id})
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}),
              patch.object(Path, "iterdir", side_effect=PermissionError("private path"))):
            result, output, error = self.command("list", "--all")
        self.assertEqual(result, 1)
        self.assertEqual(error, "the_list: The List results are incomplete. Could not read every record.\n")
        self.assertEqual(output.splitlines()[2:], [])

    def test_producer_updates_its_fields_with_its_name_as_source(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("update", "--id", str(item.number), "--latest-status", "The plan is ready.",
                                          "--next-step", "Review the plan.", "--eta", "Pending"), (0, "", ""))
            self.assertEqual(self.command("update", "--id", item.id, "--eta", "Delivered", "--source", "morty"), (0, "", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual((saved.latest_status, saved.next_step, saved.eta),
                         ("The plan is ready.", "Review the plan.", the_list.EtaStatus.DELIVERED))
        self.assertEqual([entry.source for entry in saved.log], ["nate", "dwight", "dwight"])
        self.assertEqual(saved.log[-2].what, the_list.FieldChange("Updated", {
            "latest_status": "The plan is ready.", "next_step": "Review the plan.", "eta": the_list.EtaStatus.PENDING}))
        self.assertEqual(self.notices, [])

    def test_producer_can_set_working_but_cannot_set_closed(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            with self.assertRaises(SystemExit) as refused:
                _ = self.command("update", "--id", item.id, "--state", "Closed")
            self.assertEqual(refused.exception.code, 2)
            self.assertEqual(the_list.load(MORTY.id, item.id), item)
            self.assertEqual(self.command("update", "--id", item.id, "--state", "Working"), (0, "", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.state, the_list.ItemState.WORKING)
        with self.assertRaisesRegex(the_list.ListError, "use close"):
            _ = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.CLOSED}, source=OTHER.name)
        with self.assertRaisesRegex(the_list.ListError, "use close"):
            _ = scoped_update(MORTY.id, item.id, {"state": the_list.ItemState.CLOSED},
                              source=OTHER.name, assigned_to=OTHER.id)
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_producer_sets_next_check_with_its_name_as_source(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        check = START + timedelta(days=1)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("update", "--id", item.id, "--next-check", check.isoformat(),
                                          "--source", "morty"), (0, "", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.next_check, check)
        self.assertEqual(saved.log[-1].what, the_list.FieldChange("Updated", {"next_check": check}))
        self.assertEqual(saved.log[-1].source, OTHER.name)

    def test_producer_cannot_update_another_producers_item(self) -> None:
        item = self.add(changes={"producer": BOB.id})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            for option, value in (("--eta", "Pending"), ("--state", "Working"), ("--next-check", START.isoformat())):
                with self.subTest(option=option):
                    self.assertEqual(self.command("update", "--id", item.id, option, value),
                                     (1, "", f"the_list: {item.id} is assigned to another producer\n"))
                    self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_producer_cannot_update_other_fields(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        fields = (("--title", "New title"), ("--owner", "invalid owner"), ("--producer", "invalid producer"),
                  ("--unit", "unit-board"), ("--evidence", "evidence"),
                  ("--done-when", "Done"), ("--requested-by", "nate"),
                  ("--blocked-by",), ("--related-to",))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            for field, *values in fields:
                with self.subTest(field=field):
                    result, output, error = self.command("update", "--id", item.id, "--eta", "Pending", field, *values)
                    self.assertEqual((result, output), (1, ""))
                    self.assertIn("producers can update only --latest-status, --next-step, --eta, --state, --next-check.",
                                  error)
                    self.assertIn(f"Refused options: {field}", error)
                    self.assertEqual(the_list.load(MORTY.id, item.id), item)
        self.assertEqual(self.notices, [])

    def test_producer_scoped_update_cannot_reassign_an_item(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        with self.assertRaisesRegex(the_list.ListError, "Refused options: --producer"):
            _ = scoped_update(MORTY.id, item.id, {"producer": BOB.id}, source=OTHER.name, assigned_to=OTHER.id)
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_producer_scoped_update_logs_its_own_name(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        _ = scoped_update(MORTY.id, item.id, {"latest_status": "The plan is ready."},
                          source=MORTY.name, assigned_to=OTHER.id)
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.latest_status, "The plan is ready.")
        self.assertEqual(saved.log[-1].source, OTHER.name)

    def test_producer_cannot_run_other_commands(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        commands = (
            ("add", "--title", "New item", "--owner", RUNNER.id),
            ("park", "--id", item.id, "--until", START.isoformat()),
            ("close", "--id", item.id, "--words", "Done"),
            ("reopen", "--id", item.id),
            ("mark-read", "--id", item.id),
            ("clean-evidence",),
            ("link", item.id, "--review", "C1"),
            ("unlink", item.id, "--review", "C1"),
        )
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            for name, *args in commands:
                with self.subTest(command=name):
                    self.assertEqual(self.command(name, *args, "--source", "dwight"),
                                     (1, "", f"the_list: only morty can run {name}\n"))
            self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "Answer"),
                             (1, "", "the_list: only morty can run answer\n"))
        self.assertEqual(the_list.records(MORTY.id), [item])
        self.assertEqual(self.notices, [])

    def test_close_reopen_and_repeats_keep_the_record_and_number(self) -> None:
        item = self.add(number=70)
        path = the_list.folder(MORTY.id) / "L70.md"
        self.assertEqual(self.command("close", "--id", item.id, "--words", "Stop tracking this item", "--source", "nate"), (0, "", ""))
        closed = the_list.load(MORTY.id, item.id)
        self.assertEqual((closed.state, closed.closed, closed.closing_words),
                         (the_list.ItemState.CLOSED, START, "Stop tracking this item"))
        self.assertEqual(the_list.records(MORTY.id), [])
        self.assertEqual(the_list.records(MORTY.id, include_closed=True), [closed])
        self.assertNotIn("| 70 |", self.command("list")[1])
        self.assertIn("| 70 |", self.command("list", "--all")[1])
        before = path.read_bytes(), path.stat().st_mtime_ns
        _ = the_list.close(MORTY.id, item.id, "Stop tracking this item", source="nate")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.add().number, 71)
        for args in (("update", "--id", item.id, "--state", "Working", "--source", "morty"),
                     ("park", "--id", item.id, "--until", "", "--source", "morty"),
                     ("close", "--id", item.id, "--words", "Different words", "--source", "nate")):
            with self.subTest(args=args):
                self.assertEqual(self.command(*args)[0], 1)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.command("reopen", "--id", item.id, "--source", "nate"), (0, "", ""))
        reopened = the_list.load(MORTY.id, item.id)
        self.assertEqual(reopened.state, the_list.ItemState.NEW)
        self.assertEqual(reopened.closing_words, "")
        self.assertIs(reopened.closed, the_list.NoTimestamp.UNSET)
        self.assertEqual(reopened.log[:-1], closed.log)
        self.assertEqual(reopened.log[-1].what, the_list.Reopened())
        before = path.read_bytes(), path.stat().st_mtime_ns
        _ = the_list.reopen(MORTY.id, item.id, source="nate")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_duplicate_updates_and_reads_do_not_write(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / "L1.md"
        _ = the_list.update(MORTY.id, item.id, {"title": item.title}, source="morty")
        self.assertEqual(the_list.load(MORTY.id, item.id), item)
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        before = path.read_bytes(), path.stat().st_mtime_ns
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        _ = the_list.update(MORTY.id, item.id, {}, source="morty")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_clean_evidence_removes_only_the_migration_copy_once(self) -> None:
        block = 'Source checkins.json item:\n\n{"owner": "sdlc", "nested": {"state": "Delivered"}}\n'
        cases = (
            ("Keep these words.  \n\n## A heading\n\nKeep this line.\n\n \t\n" + block,
             "Keep these words.  \n\n## A heading\n\nKeep this line.", False),
            (" \t\n\n" + block, "", False),
            ("Closed evidence.\r\n\r\n" + block.replace("\n", "\r\n"), "Closed evidence.", True),
            ("Earlier words.\n\n" + block + "\n\nRecorded identity evidence:\n\nKeep these later words.  \n\n",
             "Earlier words.\n\nRecorded identity evidence:\n\nKeep these later words.  \n\n", False),
            ("Keep every line.\n\n", "Keep every line.\n\n", False),
            ("The Source checkins.json item: words stay.\n", "The Source checkins.json item: words stay.\n", False),
            ("Source checkins.json item:\nA person wrote this.", "Source checkins.json item:\nA person wrote this.", False),
            ('Source checkins.json item:\n{"owner": "sdlc"}}\n', 'Source checkins.json item:\n{"owner": "sdlc"}}\n', False),
            ('Source checkins.json item:\n{"owner": "sdlc"},\n', 'Source checkins.json item:\n{"owner": "sdlc"},\n', False),
        )
        originals: list[tuple[the_list.ListItem, str]] = []
        for evidence, expected, closed in cases:
            item = self.add(changes={"evidence": evidence, "next_step": block,
                                     "latest_status": block, "done_when": block})
            if closed:
                item = the_list.close(MORTY.id, item.id, "The user removed it", source="nate")
            originals.append((item, expected))
        paths = [the_list.folder(MORTY.id) / f"{item.id}.md" for item, _ in originals]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": OWNER.current_session}):
            self.assertEqual(self.command("clean-evidence", "--source", "morty"),
                             (1, "", "the_list: the current session must be a producer\n"))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)
        moment = START + timedelta(minutes=1)
        with patch.object(the_list, "now", return_value=moment):
            self.assertEqual(self.command("clean-evidence", "--source", "morty"), (0, "4 changed; 5 unchanged\n", ""))
        for index, (original, expected) in enumerate(originals):
            with self.subTest(item=original.id):
                cleaned = the_list.load(MORTY.id, original.id)
                self.assertEqual(cleaned.evidence, expected)
                if original.evidence != expected:
                    entry = the_list.ItemChange(moment, the_list.EvidenceCopyRemoved("checkins.json"), "morty")
                    self.assertEqual(cleaned, replace(original, evidence=expected, updated=moment, log=(*original.log, entry)))
                else:
                    self.assertEqual(cleaned, original)
                    self.assertEqual((paths[index].read_bytes(), paths[index].stat().st_mtime_ns), before[index])
        after = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        self.assertEqual(self.command("clean-evidence", "--source", "morty"), (0, "0 changed; 9 unchanged\n", ""))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], after)

    def test_clean_evidence_removes_the_matching_row_copy_once(self) -> None:
        block = ("## {number}. The former title\n\n**List state:** Working\n\n"
                 + "- **Owner:** sdlc\n- **Next:** Check the result\n  on the other machine.\n")
        copied_json = 'Source checkins.json item:\n\n{"owner": "sdlc"}\n'
        cases = (
            (block, "", 0, 1),
            (block + "\nKeep these later words.  \n\n", "Keep these later words.  \n\n", 0, 1),
            ("Keep earlier words.\n\n" + block + "\n## Human evidence\n\nKeep this proof.\n",
             "Keep earlier words.\n\n## Human evidence\n\nKeep this proof.\n", 0, 1),
            (block.replace("{number}", "999"), block.replace("{number}", "999"), 0, 0),
            (block.replace("**List state:** Working\n\n", ""),
             block.replace("**List state:** Working\n\n", ""), 0, 0),
            ("## {number}. Title\n\n**List state:** Working\n\nKeep human evidence.\n",
             "## {number}. Title\n\n**List state:** Working\n\nKeep human evidence.\n", 0, 0),
            (block.replace("\n", "\r\n"), "", 0, 1),
            (copied_json + "\n" + block + "\nKeep the proof.\n", "Keep the proof.\n", 1, 1),
            (block + "\n" + copied_json + "\nKeep the proof.\n", "Keep the proof.\n", 1, 1),
        )
        originals: list[tuple[the_list.ListItem, str, int, int]] = []
        for number, (evidence, expected, json_entries, row_entries) in enumerate(cases, 54):
            item = self.add(number=number, changes={"evidence": evidence.replace("{number}", str(number))})
            if number == 54:
                item = the_list.close(MORTY.id, item.id, "Finished", source="nate")
            originals.append((item, expected.replace("{number}", str(number)), json_entries, row_entries))
        moment = START + timedelta(minutes=1)
        with patch.object(the_list, "now", return_value=moment), patch.object(
                record_store.NumberedRecords, "write", autospec=True, side_effect=record_store.NumberedRecords.write) as save:
            self.assertEqual(self.command("clean-evidence", "--source", "morty"), (0, "6 changed; 3 unchanged\n", ""))
        self.assertEqual(save.call_count, 6)
        for original, expected, json_entries, row_entries in originals:
            with self.subTest(item=original.id):
                entries = (
                    (the_list.ItemChange(moment, the_list.EvidenceCopyRemoved("checkins.json"), "morty"),)
                    * json_entries
                    + (the_list.ItemChange(moment, the_list.EvidenceCopyRemoved("the List row"), "morty"),)
                    * row_entries
                )
                cleaned = the_list.load(MORTY.id, original.id)
                self.assertEqual(cleaned, replace(original, evidence=expected,
                    updated=moment if entries else original.updated, log=(*original.log, *entries)))
        paths = [the_list.folder(MORTY.id) / f"{item.id}.md" for item, _, _, _ in originals]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        self.assertEqual(self.command("clean-evidence", "--source", "morty"), (0, "0 changed; 9 unchanged\n", ""))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_clean_evidence_keeps_a_human_bullet_after_the_row_copy(self) -> None:
        proof = "- Deployment failed. Keep this proof.\n"
        block = "## 54. Former title\n\n**List state:** Working\n\n- **Owner:** sdlc\n"
        item = self.add(number=54, changes={"evidence": block + "\n" + proof})
        self.assertEqual(self.command("clean-evidence", "--source", "morty"), (0, "1 changed; 0 unchanged\n", ""))
        self.assertEqual(the_list.load(MORTY.id, item.id).evidence, proof)
        self.assertEqual(self.command("clean-evidence", "--source", "morty"), (0, "0 changed; 1 unchanged\n", ""))

    def test_clean_evidence_keeps_text_with_one_leading_space_after_the_row_copy(self) -> None:
        proof = " Keep this proof.\n"
        block = "## 54. Former title\n\n**List state:** Working\n\n- **Owner:** sdlc\n"
        item = self.add(number=54, changes={"evidence": block + "\n" + proof})
        self.assertEqual(self.command("clean-evidence", "--source", "morty"), (0, "1 changed; 0 unchanged\n", ""))
        self.assertEqual(the_list.load(MORTY.id, item.id).evidence, proof)
        self.assertEqual(self.command("clean-evidence", "--source", "morty"), (0, "0 changed; 1 unchanged\n", ""))

    def test_review_links_keep_order_history_and_repeat_without_writes(self) -> None:
        first = investigation.file(OWNER.id, "First review", "Read the first result.", waiting_why='The owner must complete the investigation.')
        second = investigation.file(OWNER.id, "Second review", "Read the second result.", waiting_why='The owner must complete the investigation.')
        identities = (f"{OWNER.name}/{first.id}", f"{OWNER.name}/{second.id}")
        keys = tuple(f"investigation/{OWNER.id}/{review.id}" for review in (first, second))
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        with patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(self.root / "reviews")}), patch.object(
                review_items, "read", wraps=review_items.read) as read:
            self.assertEqual(self.command("link", item.id, "--review", identities[1], "--review", identities[0],
                                          "--review", identities[1], "--source", "morty"), (0, "", ""))
        read.assert_called_once()
        linked = the_list.load(MORTY.id, item.id)
        self.assertEqual(linked.reviews, keys[::-1])
        self.assertEqual(linked.log[:-1], item.log)
        self.assertEqual(linked.log[-1],
                         the_list.ItemChange(START, the_list.ReviewLinksChanged("Linked reviews", keys[::-1]), "morty"))
        self.assertEqual(replace(linked, reviews=item.reviews, log=item.log), item)
        before = path.read_bytes(), path.stat().st_mtime_ns
        with patch.object(review_items, "read", return_value=[]) as read:
            self.assertEqual(self.command("link", item.id, "--review-key", keys[0], "--source", "morty"), (0, "", ""))
            read.assert_not_called()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.command("unlink", item.id, "--review-key", keys[1], "--review-key", keys[1],
                                     "--source", "morty"), (0, "", ""))
        unlinked = the_list.load(MORTY.id, item.id)
        self.assertEqual(unlinked.reviews, (keys[0],))
        self.assertEqual(unlinked.log[:-1], linked.log)
        self.assertEqual(unlinked.log[-1].what, the_list.ReviewLinksChanged("Unlinked reviews", (keys[1],)))
        before = path.read_bytes(), path.stat().st_mtime_ns
        with patch.object(review_items, "read", return_value=[]):
            self.assertEqual(self.command("unlink", item.id, "--review", "missing/C9", "--source", "morty"), (0, "", ""))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        with patch.object(review_items, "read", return_value=[]):
            self.assertEqual(self.command("link", item.id, "--review-key", keys[0], "--review", "missing/C9",
                                          "--source", "morty"), (1, "", "the_list: unknown review id: missing/C9\n"))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = the_list.close(MORTY.id, item.id, "Stop tracking", source="nate")
        before = path.read_bytes(), path.stat().st_mtime_ns
        for command in ("link", "unlink"):
            with self.subTest(command=command):
                self.assertEqual(self.command(command, item.id, "--review", identities[0], "--source", "morty"),
                    (1, "", f"the_list: {item.id}: reopen the Closed item before changing it\n"))
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_review_link_ignores_a_producer_directory(self) -> None:
        folder = self.root / "producer"
        folder.mkdir()
        _ = (folder / "README.md").write_text("# Producer\n")
        members = staff.snapshot()
        rows = tuple(replace(row, doc=staff.LiveDoc(local_machine(), folder)) if row.id == MORTY.id
                     else row for row in members.rows)
        _ = staff.record_path().write_text(replace(members, rows=rows).encode())
        report = investigation.file(OWNER.id, "Read the result", "Check the result.", waiting_why="Check the result.")
        item = self.add()
        with (patch.object(change_review, "lists_directory", return_value=self.root),
              self.assertNoLogs(review_items.__name__, level="WARNING")):
            self.assertEqual(self.command("link", item.id, "--review", f"{OWNER.name}/{report.id}",
                                          "--source", "morty"), (0, "", ""))
        self.assertEqual(the_list.load(MORTY.id, item.id).reviews, (f"investigation/{OWNER.id}/{report.id}",))

    def test_review_link_warns_and_skips_an_unreadable_change_list(self) -> None:
        unreadable = self.root / f"{change_review.PREFIX}broken.md"
        _ = unreadable.write_bytes(b"\xff")
        listing = self.root / f"{change_review.PREFIX}{RUNNER.name}.md"
        _ = listing.write_text(
            "## To review\n\n| # | Change | How to check | Producer |\n| --- | --- | --- | --- |\n"
            + f"| C1 | Review the result | Open the result | {RUNNER.id} |\n", encoding="utf-8")
        item = self.add()
        key = f"{RUNNER.name}/C1"
        with (patch.object(change_review, "lists_directory", return_value=self.root),
              self.assertLogs(review_items.__name__, level="WARNING") as warnings):
            self.assertEqual(self.command("link", item.id, "--review", key, "--source", "morty"), (0, "", ""))
        warning, = warnings.output
        self.assertIn(f"Review source {unreadable}:", warning)
        self.assertEqual(the_list.load(MORTY.id, item.id).reviews, (key,))

    def test_old_records_without_reviews_load_without_writes(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        _ = path.write_text(path.read_text().replace('reviews: "[]"\n', "").replace(f'producer: "{MORTY.id}"\n', ""))
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(the_list.load(MORTY.id, item.id), item)
        self.assertEqual((item.reviews, item.producer), ((), MORTY.id))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_review_commands_resolve_keys_and_refuse_ambiguous_shown_ids(self) -> None:
        _ = investigation.file(OWNER.id, "First review", "Read the result.", waiting_why='The owner must complete the investigation.')
        first = review_items.investigation_items(staff.snapshot())[0]
        second = replace(first, key="proposal/other/I1", type=review_items.ReviewType.PROPOSAL,
                         source=review_items.ProposalLedgerSource(self.root / "proposals.md"))
        ambiguous = (1, "", f"the_list: ambiguous review id: {first.id}; "
                     + f"{first.key} (investigation), {second.key} (proposal)\n")
        for option, identity, expected in (
                ("--review", first.id, ambiguous),
                ("--review", first.key, (1, "", "the_list: unknown review id: " + first.key + "\n")),
                ("--review-key", first.id, (1, "", "the_list: unknown review key: " + first.id + "\n")),
                ("--review-key", first.key, (0, "", ""))):
            with self.subTest(option=option, identity=identity):
                item = self.add()
                path = the_list.folder(MORTY.id) / f"{item.id}.md"
                before = path.read_bytes(), path.stat().st_mtime_ns
                with patch.object(review_items, "read", return_value=[first, second]):
                    self.assertEqual(self.command("link", item.id, option, identity, "--source", "morty"), expected)
                if expected[0]:
                    self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
                else:
                    self.assertEqual(the_list.load(MORTY.id, item.id).reviews, (first.key,))
                    with patch.object(review_items, "read", return_value=[first, second]):
                        self.assertEqual(self.command("unlink", item.id, "--review", first.id, "--source", "morty"),
                                         ambiguous)
                    with patch.object(review_items, "read", return_value=[first]):
                        self.assertEqual(self.command("unlink", item.id, "--review", first.id, "--source", "morty"),
                                         (0, "", ""))
                    self.assertEqual(the_list.load(MORTY.id, item.id).reviews, ())

        third = replace(first, id="other/I2", key="investigation/other/I2")
        fourth = replace(first, id="other/I3", key="investigation/other/I3")
        item = self.add()
        with patch.object(review_items, "read", return_value=[first, second, third, fourth]) as read:
            self.assertEqual(self.command("link", item.id, "--review-key", second.key, "--review", third.id,
                "--review-key", first.key, "--review", fourth.id, "--review-key", second.key,
                "--source", "morty"), (0, "", ""))
        read.assert_called_once()
        linked_item = the_list.load(MORTY.id, item.id)
        self.assertEqual(linked_item.reviews, (second.key, third.key, first.key, fourth.key))
        self.assertEqual(linked_item.log[-1].what, the_list.ReviewLinksChanged("Linked reviews", linked_item.reviews))
        with patch.object(review_items, "read", return_value=[first, second, third, fourth]) as read:
            self.assertEqual(self.command("unlink", item.id, "--review", third.id, "--review-key", second.key,
                "--review", third.id, "--review-key", first.key, "--source", "morty"), (0, "", ""))
        read.assert_called_once()
        unlinked_item = the_list.load(MORTY.id, item.id)
        self.assertEqual(unlinked_item.reviews, (fourth.key,))
        self.assertEqual(unlinked_item.log[-1].what,
                         the_list.ReviewLinksChanged("Unlinked reviews", (third.key, second.key, first.key)))

    def test_review_options_keep_shown_ids_separate_from_exact_keys(self) -> None:
        _ = investigation.file(OWNER.id, "Review the result", "Read the result.", waiting_why='The owner must complete the investigation.')
        base = review_items.investigation_items(staff.snapshot())[0]
        first = replace(base, id="natedev/C1", key="natedev/C1")
        second = replace(base, id=first.id, key="hanadocs/C1", type=review_items.ReviewType.PROPOSAL,
                         source=review_items.ProposalLedgerSource(self.root / "proposals.md"))
        ambiguous = (1, "", "the_list: ambiguous review id: natedev/C1; "
                     + "natedev/C1 (investigation), hanadocs/C1 (proposal)\n")
        cases = (
            ("link", "--review", first.id, (), ambiguous, ()),
            ("link", "--review", first.id, (first.key,), ambiguous, (first.key,)),
            ("link", "--review-key", first.key, (), (0, "", ""), (first.key,)),
            ("link", "--review-key", second.key, (), (0, "", ""), (second.key,)),
            ("unlink", "--review-key", second.key, (first.key, second.key), (0, "", ""), (first.key,)),
            ("unlink", "--review", first.id, (first.key, second.key), ambiguous, (first.key, second.key)),
        )
        for command, option, value, linked, expected, keys in cases:
            with self.subTest(command=command, option=option, linked=linked):
                item = self.add()
                if linked:
                    item = the_list.link(MORTY.id, item.id, linked, source="morty")
                path = the_list.folder(MORTY.id) / f"{item.id}.md"
                before = path.read_bytes(), path.stat().st_mtime_ns
                with patch.object(review_items, "read", return_value=[first, second]):
                    self.assertEqual(self.command(command, item.id, option, value, "--source", "morty"), expected)
                self.assertEqual(the_list.load(MORTY.id, item.id).reviews, keys)
                if expected[0]:
                    self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_page_events_only_mark_read_or_append_feedback(self) -> None:
        item = self.add(number=106)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": OWNER.current_session}):
            self.assertEqual(self.command("mark-read", "--id", item.id, "--source", "nate")[0], 1)
            read = the_list.mark_read(MORTY.id, item.id, source=the_list.PAGE_SOURCE)
        self.assertEqual(replace(read, unread_since=item.unread_since, log=item.log), item)
        self.assertEqual(read.log[-1].what, the_list.MarkedRead())
        words = 'Explain this simply.\nKeep "these" words.'
        self.assertEqual(the_list.save_feedback(MORTY.id, item.id, the_list.PageFeedback("request", words)),
                         the_list.PageFeedback("request", words))
        sent = the_list.load(MORTY.id, item.id)
        self.assertEqual(replace(sent, log=read.log), read)
        self.assertEqual(sent.log[-1].what, the_list.PageFeedback("request", words))
        self.assertEqual(the_list.sent_to_morty(sent), START)
        self.assertIs(sent.unread_since, the_list.NoTimestamp.UNSET)
        again = the_list.mark_read(MORTY.id, item.id, source=the_list.PAGE_SOURCE)
        self.assertEqual(the_list.sent_to_morty(again), START)
        with patch.object(the_list, "now", return_value=START + timedelta(minutes=1)):
            changed = the_list.update(MORTY.id, item.id, {"latest_status": "Morty acts."}, source="owner report")
        self.assertIs(the_list.sent_to_morty(changed), the_list.NoTimestamp.UNSET)

    def test_answer_refusals_leave_the_record_unchanged(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        before = path.read_bytes()
        for identity, text, error in (("Q2", "The check passed.", "Unknown question id."),
                                      ("Q1", " \t ", "Enter an answer first.")):
            with self.subTest(identity=identity, text=text):
                self.assertEqual(self.command("answer", item.id, "--question", identity, "--text", text),
                                 (1, "", f"the_list: {error}\n"))
                self.assertEqual(path.read_bytes(), before)
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, item.id, the_list.PageRequest("request-1", "question", "Which check passed?"), status)
        opened = the_list.load(MORTY.id, item.id)
        self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "The check passed."),
                         (0, f"answered {item.id} Q1\n", ""))
        answered = the_list.load(MORTY.id, item.id)
        answer = answered.questions[0].answer
        assert isinstance(answer, question_record.Answer)
        self.assertEqual(answer.text, "The check passed.")
        self.assertEqual(answered.unread_since, START)
        self.assertEqual(len(answered.log), len(opened.log) + 1)
        self.assertIn('"action": "Answered", "question_id": "Q1"', path.read_text())
        self.assertEqual(answered.log[-1].what, the_list.QuestionAnswered("Q1"))
        before = path.read_bytes()
        self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "A second answer."),
                         (1, "", "the_list: This question already has an answer.\n"))
        self.assertEqual(path.read_bytes(), before)

    def test_answer_round_trips_unicode_separators(self) -> None:
        item = self.add()
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, item.id, the_list.PageRequest("ask", "question", "Which checks passed?"), status)
        text = "First result\u2028Second result\u2029Third result"
        self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", text),
                         (0, f"answered {item.id} Q1\n", ""))
        saved = the_list.load(MORTY.id, item.id)
        answer = saved.questions[0].answer
        assert isinstance(answer, question_record.Answer)
        self.assertEqual(answer.text, text)
        self.assertEqual(saved.unread_since, START)

    def test_close_clears_unread_and_reopen_marks_a_new_update(self) -> None:
        item = self.add()
        self.assertIsInstance(the_list.unread_update(item), the_list.UnreadUpdate)
        closed = the_list.close(MORTY.id, item.id, "The report is retired.", source="nate")
        self.assertIs(closed.unread_since, the_list.NoTimestamp.UNSET)
        self.assertIs(the_list.unread_update(closed), the_list.NoUnreadUpdate.ABSENT)
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        self.assertIn('unread_since: ""\n', path.read_text())

        reopened_at = START + timedelta(minutes=1)
        with patch.object(the_list, "now", return_value=reopened_at):
            reopened = the_list.reopen(MORTY.id, item.id, source="nate")
        self.assertEqual(reopened.unread_since, reopened_at)
        self.assertIsInstance(the_list.unread_update(reopened), the_list.UnreadUpdate)

    def test_answer_marks_a_closed_item_unread_until_mark_read(self) -> None:
        item = self.add()
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, item.id, the_list.PageRequest("ask", "question", "Which check passed?"), status)
        _ = the_list.close(MORTY.id, item.id, "The report is retired.", source="nate")
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        answered_at = START + timedelta(minutes=1)
        with patch.object(the_list, "now", return_value=answered_at):
            self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "The page check passed."),
                             (0, f"answered {item.id} Q1\n", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.state, the_list.ItemState.CLOSED)
        self.assertEqual(saved.updated, answered_at)
        self.assertIsInstance(saved.questions[0].answer, question_record.Answer)
        self.assertEqual(saved.unread_since, answered_at)
        self.assertIsInstance(the_list.unread_update(saved), the_list.UnreadUpdate)
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        self.assertIn(f'unread_since: {json.dumps(answered_at.isoformat())}\n', path.read_text())
        with patch.object(the_list, "now", return_value=answered_at):
            _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        read = the_list.load(MORTY.id, item.id)
        self.assertIs(read.state, the_list.ItemState.CLOSED)
        self.assertIs(the_list.unread_update(read), the_list.NoUnreadUpdate.ABSENT)
        self.assertIn('unread_since: ""\n', path.read_text())

    def test_load_reads_a_legacy_closed_record_without_an_unread_update(self) -> None:
        item = self.add(changes={"latest_status": "The report is ready.", "next_step": "Review the report.",
                                 "evidence": "The page check passed.", "done_when": "The report closes."})
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, item.id,
                                  the_list.PageRequest("ask", "question", "Which check passed?"), status)
        _ = the_list.close(MORTY.id, item.id, "The report is retired.", source="nate")
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        field = f'unread_since: {json.dumps(START.isoformat())}\n'
        stored = path.read_text().replace('unread_since: ""\n', field, 1)
        self.assertIn(field, stored)
        _ = path.write_text(stored)
        closed = the_list.load(MORTY.id, item.id)
        self.assertIs(closed.unread_since, the_list.NoTimestamp.UNSET)
        self.assertIs(the_list.unread_update(closed), the_list.NoUnreadUpdate.ABSENT)
        self.assertEqual(path.read_text(), stored)
        answered_at = START + timedelta(minutes=1)
        words = "The page check passed."
        with patch.object(the_list, "now", return_value=answered_at):
            self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", words),
                             (0, f"answered {item.id} Q1\n", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertIn(f'unread_since: {json.dumps(answered_at.isoformat())}\n', path.read_text())
        self.assertEqual(saved.log, (*closed.log, the_list.ItemChange(answered_at, the_list.QuestionAnswered("Q1"), "morty")))
        self.assertEqual(saved.updated, answered_at)
        answer = saved.questions[0].answer
        assert isinstance(answer, question_record.Answer)
        self.assertEqual(answer.text, words)
        self.assertEqual(saved.questions, (replace(closed.questions[0], answer=answer),))
        self.assertEqual(replace(saved, updated=closed.updated, unread_since=closed.unread_since,
                                 log=closed.log, questions=closed.questions), closed)

    def test_list_command_keeps_an_item_while_its_question_waits(self) -> None:
        item = self.add("Check the List")
        status = change_page.DeliveryStatus(self.root / "reviews")
        shown = list_page.render(status.root, include_counts=False).decode()
        self.assertRegex(shown, r'<tbody data-list-item[^>]*data-waiting-for-answer="false"[^>]*">')
        request = the_list.PageRequest("ask", "question", "Which check passed?")
        _ = the_list.ask_question(MORTY.id, item.id, request, status)
        waiting = list_page.render(status.root, include_counts=False, status=status).decode()
        self.assertRegex(waiting, r'<tbody data-list-item[^>]*data-waiting-for-answer="true"[^>]*">')
        self.assertIn('data-open-question="true"', waiting)
        result, output, errors = self.command("list")
        self.assertEqual((result, errors), (0, ""))
        self.assertIn(item.title, output)
        self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "The browser check."),
                         (0, f"answered {item.id} Q1\n", ""))
        answered = the_list.load(MORTY.id, item.id)
        returned = list_page.render(status.root, include_counts=False).decode()
        self.assertRegex(returned, r'<tbody data-list-item[^>]*data-waiting-for-answer="false"[^>]*">')
        self.assertIn('data-open-question="false"', returned)
        self.assertEqual(answered.unread_since, START)

    def test_typed_log_refuses_invalid_fields(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        before = path.read_text()
        activity = {"request": "request", "action": "tell", "words": "Read this.", "state": "pending"}
        cases: tuple[tuple[str, dict[str, object]], ...] = (
            ("added", {"action": "Added", "fields": {"title": "Only a title"}}),
            ("updated", {"action": "Updated", "fields": {}}),
            ("empty title", {"action": "Updated", "fields": {"title": ""}}),
            ("multiline title", {"action": "Updated", "fields": {"title": "First\nSecond"}}),
            ("mismatched ETA kind", {"action": "Updated", "fields":
                {"eta": {"kind": "time or status", "value": "After the check"}}}),
            ("spaced request ID", {"feedback": dict(activity, request="bad id")}),
            ("parked", {"action": "Parked", "fields": {"title": "Only a title"}}),
            ("closed", {"action": "Closed", "fields": {"state": "Closed"}}),
            ("blank closing words", {"action": "Closed", "fields": {"closing_words": " "}}),
            ("blank request", {"feedback": dict(activity, request="")}),
            ("blank feedback", {"feedback": dict(activity, words=" ")}),
            ("missing request", {"feedback": {key: value for key, value in activity.items() if key != "request"}}),
            ("missing feedback", {"feedback": {key: value for key, value in activity.items() if key != "words"}}),
            ("missing state", {"feedback": {key: value for key, value in activity.items() if key != "state"}}),
            ("missing action", {"feedback": {key: value for key, value in activity.items() if key != "action"}}),
            ("blank receipt request", {"receipt": {"request": "", "action": "read", "words": ""}}),
            ("blank question", {"receipt": {"request": "ask", "action": "question", "words": "", "question_id": "Q1"}}),
            ("wrong feedback source", {"source": "morty", "feedback": activity}),
            ("wrong receipt source", {"source": "morty", "receipt": {
                "request": "request", "action": "read", "words": ""}}),
            ("wrong question receipt source", {"source": "morty", "receipt": {
                "request": "ask", "action": "question", "words": "Which check?", "question_id": "Q1"}}),
            ("missing review keys", {"action": "Linked reviews", "keys": []}),
            ("invalid review key", {"action": "Unlinked reviews", "keys": [""]}),
            ("unknown Evidence copy", {"action": "Removed Evidence copy", "copy": "another copy"}),
        )
        for label, entry in cases:
            with self.subTest(label=label):
                row = {"time": START.isoformat(), "source": the_list.PAGE_SOURCE, **entry}
                _ = path.write_text(before.removesuffix("\n") + "\n- " + json.dumps(row) + "\n")
                with self.assertRaises(the_list.ListError):
                    _ = the_list.load(MORTY.id, item.id)

    def test_log_reader_and_item_validator_refuse_the_same_field_values(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        before = path.read_text()
        cases: tuple[tuple[str, object, object], ...] = (
            ("title", "", ""),
            ("owner", "../owner", "../owner"),
            ("producer", "../producer", "../producer"),
            ("unit", "first\nsecond", "first\nsecond"),
            ("state", "Unknown", "Unknown"),
            ("next_check", the_list.CheckCondition("first\nsecond"), "first\nsecond"),
            ("eta", the_list.EtaDescription(""), {"kind": "text", "value": ""}),
            ("blocked_by", (the_list.ItemDependency(0, ""),), [[0, ""]]),
            ("blocked_by", (the_list.ItemDependency(item.number, "ready"),), [[item.number, "ready"]]),
            ("blocked_by", (the_list.ItemDependency(2, "one"), the_list.ItemDependency(2, "two")),
             [[2, "one"], [2, "two"]]),
            ("related_to", (0,), [0]),
            ("related_to", (item.number,), [item.number]),
            ("requested_by", "Unknown", "Unknown"),
            ("next_step", 1, 1),
            ("latest_status", 1, 1),
            ("evidence", 1, 1),
            ("done_when", 1, 1),
            ("parked_until", START.replace(tzinfo=None), START.replace(tzinfo=None).isoformat()),
            ("closing_words", " ", " "),
        )
        self.assertEqual({field for field, _, _ in cases}, set(the_list.LogFields.__annotations__))
        for field, native, stored in cases:
            action = {"parked_until": "Parked", "closing_words": "Closed"}.get(field, "Updated")
            with self.subTest(field=field, value=stored, path="item"):
                if action == "Parked":
                    assert isinstance(native, datetime)
                    with self.assertRaises(the_list.ListError):
                        _ = the_list.park(MORTY.id, item.id, native, source="nate")
                elif action == "Closed":
                    assert isinstance(native, str)
                    with self.assertRaises(the_list.ListError):
                        _ = the_list.close(MORTY.id, item.id, native, source="nate")
                else:
                    changes = cast(the_list.ItemChanges, cast(object, {field: native}))
                    with self.assertRaises(the_list.ListError):
                        _ = the_list.update(MORTY.id, item.id, changes, source="morty")
            with self.subTest(field=field, value=stored, path="log"):
                row = {"time": START.isoformat(), "source": "morty", "action": action, "fields": {field: stored}}
                _ = path.write_text(before.removesuffix("\n") + "\n- " + json.dumps(row) + "\n")
                try:
                    with self.assertRaises(the_list.ListError):
                        _ = the_list.load(MORTY.id, item.id)
                finally:
                    _ = path.write_text(before)

    def test_every_log_writer_saves_a_typed_entry_that_reloads(self) -> None:
        evidence = ('Source checkins.json item:\n\n{"owner": "sdlc"}\n\n'
                    + "## 1. Former title\n\n**List state:** Working\n\n- **Owner:** sdlc\n")
        item = self.add(number=1, changes={"evidence": evidence})
        path = the_list.folder(MORTY.id) / "L1.md"
        seen = 0

        def check(*tags: str) -> None:
            nonlocal seen
            entries = the_list.load(MORTY.id, item.id).log
            _, marker, body = path.read_text().partition("## Log\n\n")
            self.assertTrue(marker)
            rows = [cast(dict[str, object], json.loads(line[2:])) for line in body.split("\n") if line]
            self.assertEqual(len(entries), len(rows))
            self.assertEqual(len(entries) - seen, len(tags))
            first, seen = seen, len(entries)
            for entry, row, tag in zip(entries[first:], rows[first:], tags, strict=True):
                with self.subTest(event=tag):
                    self.assertNotIsInstance(entry.what, str)
                    self.assertNotIn("note", row)
                    self.assertEqual(row.get("action", tag if tag in row else ""), tag)

        check("Added")
        _ = the_list.update(MORTY.id, item.id, {"title": "New title"}, source="morty")
        check("Updated")
        _ = the_list.link(MORTY.id, item.id, ("card-1",), source="morty")
        check("Linked reviews")
        _ = the_list.unlink(MORTY.id, item.id, ("card-1",), source="morty")
        check("Unlinked reviews")
        until = START + timedelta(days=1)
        _ = the_list.park(MORTY.id, item.id, until, source=the_list.PAGE_SOURCE,
                          notice=the_list.PageItemNotice("park", "Park this item.", "park"))
        check("Parked", "feedback")
        _ = the_list.park(MORTY.id, item.id, until, source=the_list.PAGE_SOURCE,
                          notice=the_list.PageItemNotice("park-again", "Keep it parked.", "park"))
        check("receipt")
        _ = the_list.close(MORTY.id, item.id, "Works.", source=the_list.PAGE_SOURCE,
                           notice=the_list.PageItemNotice("remove", "Remove this item.", "remove"))
        check("Closed", "feedback")
        _ = the_list.reopen(MORTY.id, item.id, source="morty")
        check("Reopened")
        _ = the_list.clean_evidence(MORTY.id, source="morty")
        check("Removed Evidence copy", "Removed Evidence copy")
        _ = the_list.mark_read(MORTY.id, item.id, source=the_list.PAGE_SOURCE,
                               page_request=the_list.PageRequest("read", "read", ""))
        check("receipt", "Marked read")
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, item.id, the_list.PageRequest("ask", "question", "Ready?"), status)
        check("receipt")
        _ = the_list.ask_question(MORTY.id, item.id, the_list.PageRequest("ask-again", "question", "Ready?"), status)
        check("receipt")
        self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "Ready."),
                         (0, "answered L1 Q1\n", ""))
        check("Answered")
        _ = the_list.save_feedback(MORTY.id, item.id, the_list.PageFeedback("tell", "Read this."))
        check("feedback")
        the_list.deliver_feedback(MORTY.id, item.id, "tell", lambda _, entry: replace(entry, state="sent"))
        check("feedback")

    def test_legacy_fields_keep_the_existing_unread_update_identity(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        before, marker, _ = path.read_text().partition("## Log\n\n")
        dependencies = ("(ItemDependency(number=105, condition='on main'), "
                        + "ItemDependency(number=106, condition='first page on main'))")
        note = f'Updated blocked_by: {json.dumps(dependencies)}; related_to: "(112, 113)"'
        _ = path.write_text(before + marker + f"- 2026-11-16 18:00:00+00:00 {note} — morty\n")
        previous = the_list.unread_update(replace(item, log=(the_list.ItemChange(START, note, "morty"),)))
        restored = the_list.load(MORTY.id, item.id)
        self.assertEqual(restored.log[0].what, the_list.LegacyFieldChange("Updated", {
            "blocked_by": (the_list.ItemDependency(105, "on main"), the_list.ItemDependency(106, "first page on main")),
            "related_to": (112, 113)}, note))
        self.assertEqual(the_list.unread_update(restored), previous)

        _ = the_list.save_feedback(MORTY.id, item.id, the_list.PageFeedback("legacy-request", "Read the log."))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.log[0], restored.log[0])
        self.assertEqual(the_list.unread_update(saved), previous)

    def test_feedback_refuses_invalid_request_ids_without_writing(self) -> None:
        for request in (" ", "bad id", "bad\tid"):
            with self.subTest(request=request):
                item = self.add()
                path = the_list.folder(MORTY.id) / f"{item.id}.md"
                before = path.read_bytes(), path.stat().st_mtime_ns
                with self.assertRaises(the_list.ListError):
                    _ = the_list.save_feedback(MORTY.id, item.id, the_list.PageFeedback(request, "Keep this result."))
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_feedback_keeps_its_original_update_target_across_delivery(self) -> None:
        item = self.add()
        target = the_list.unread_update(item)
        feedback = the_list.PageFeedback("request", "Keep this result.", read_update=target)
        _ = the_list.save_feedback(MORTY.id, item.id, feedback)
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(the_list.feedback_requests(saved)["request"], feedback)
        self.assertEqual(the_list.unread_update(saved), target)
        send = Mock(return_value=replace(feedback, state="sent"))
        with patch.object(staff, "authority_snapshot", side_effect=staff.StaffUnreadable("fixture staff read failed")) as read:
            the_list.deliver_feedback(MORTY.id, item.id, "request", send)
            read.assert_not_called()
        the_list.deliver_feedback(MORTY.id, item.id, "request", send)
        send.assert_called_once_with(saved, feedback)
        sent = the_list.load(MORTY.id, item.id)
        self.assertEqual(the_list.feedback_requests(sent)["request"].state, "sent")
        self.assertEqual(the_list.feedback_requests(sent)["request"].read_update, target)
        self.assertEqual(the_list.unread_update(sent), target)

    def test_mark_read_checks_the_update_target_inside_the_store_lock(self) -> None:
        item = self.add()
        target = the_list.unread_update(item)
        self.assertIsInstance(target, the_list.UnreadUpdate)
        assert isinstance(target, the_list.UnreadUpdate)
        newer = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        self.assertEqual(newer.unread_since, item.unread_since)
        self.assertNotEqual(the_list.unread_update(newer), target)
        unchanged = the_list.mark_read(MORTY.id, item.id, source=the_list.PAGE_SOURCE, update=target)
        self.assertEqual(unchanged, newer)
        current = the_list.unread_update(newer)
        assert isinstance(current, the_list.UnreadUpdate)
        read = the_list.mark_read(MORTY.id, item.id, source=the_list.PAGE_SOURCE, update=current)
        self.assertIs(read.unread_since, the_list.NoTimestamp.UNSET)
        self.assertEqual(read.log[-1].what, the_list.MarkedRead())

    def test_page_item_actions_save_the_notice_in_the_same_write(self) -> None:
        for action in ("remove", "park"):
            with self.subTest(action=action):
                item = self.add("One write for " + action)
                notice = the_list.PageItemNotice("request-" + action, "The item action is complete.", action)
                with patch.object(record_store, "write_text_atomic", wraps=atomic_file.write_text_atomic) as save:
                    if action == "remove":
                        saved = the_list.close(MORTY.id, item.id, "Removed by nate.", source=the_list.PAGE_SOURCE, notice=notice)
                    else:
                        saved = the_list.park(MORTY.id, item.id, START + timedelta(days=1), source=the_list.PAGE_SOURCE, notice=notice)
                save.assert_called_once()
                feedback = the_list.feedback_requests(saved)[notice.request]
                self.assertEqual((feedback.action, feedback.words, feedback.state), (action, notice.words, "pending"))
                self.assertEqual(saved.log[-1].time, saved.log[-2].time)

    def test_repeated_remove_request_does_not_close_a_reopened_item(self) -> None:
        item = self.add()
        notice = the_list.PageItemNotice("request", "The item was removed.", "remove")
        _ = the_list.close(MORTY.id, item.id, "Removed by nate.", source=the_list.PAGE_SOURCE, notice=notice)
        reopened = the_list.reopen(MORTY.id, item.id, source="morty")
        with patch.object(record_store, "write_text_atomic", wraps=atomic_file.write_text_atomic) as save:
            repeated = the_list.close(MORTY.id, item.id, "Removed by nate.", source=the_list.PAGE_SOURCE, notice=notice)
        save.assert_not_called()
        self.assertEqual(repeated, reopened)

    def test_parking_hides_the_item_until_its_time_without_changing_state(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WORKING})
        with patch.object(the_list, "user_zone", return_value=ZoneInfo("America/New_York")):
            self.assertEqual(self.command("park", "--id", item.id, "--until", "2026-11-17", "--source", "nate"), (0, "", ""))
        item = the_list.load(MORTY.id, item.id)
        self.assertEqual(item.parked_until, datetime(2026, 11, 17, 5, tzinfo=UTC))
        self.assertEqual(item.state, the_list.ItemState.WORKING)
        self.assertNotIn("| 1 |", self.command("list")[1])
        self.assertIn("| 1 |", self.command("list", "--include-parked")[1])
        self.assertIn("parked until 2026-11-17 |", self.command("list", "--include-parked")[1])
        with patch.object(the_list, "now", return_value=datetime(2026, 11, 17, 5, tzinfo=UTC)):
            self.assertIn("| 1 |", self.command("list")[1])
        self.assertEqual(self.command("park", "--id", item.id, "--until", "", "--source", "nate"), (0, "", ""))
        self.assertIs(the_list.load(MORTY.id, item.id).parked_until, the_list.NoTimestamp.UNSET)

    def test_unpark_clears_active_parking_and_saves_one_notice(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WORKING})
        parked = the_list.park(MORTY.id, item.id, START + timedelta(days=1), source="nate")
        words = "List page: nate unparked item 1."
        notice = the_list.PageItemNotice("unpark-1", words, "unpark")
        with patch.object(record_store, "write_text_atomic", wraps=atomic_file.write_text_atomic) as save:
            result = the_list.unpark(MORTY.id, item.id, source=the_list.PAGE_SOURCE, notice=notice)
        save.assert_called_once()
        self.assertEqual(result, the_list.SavedEdit(parked, the_list.load(MORTY.id, item.id)))
        saved = result.item
        self.assertIs(saved.parked_until, the_list.NoTimestamp.UNSET)
        self.assertEqual(saved.state, parked.state)
        self.assertEqual(saved.log[-2].what, the_list.Unparked())
        self.assertEqual(saved.log[-1].what, the_list.PageFeedback("unpark-1", words, action="unpark"))
        self.assertEqual(len(saved.log), len(parked.log) + 2)
        self.assertEqual(the_list.unpark(MORTY.id, item.id, source=the_list.PAGE_SOURCE, notice=notice),
                         the_list.UnchangedEdit(saved))

        parked_again = the_list.park(MORTY.id, item.id, START + timedelta(days=2), source="nate")
        self.assertEqual(the_list.unpark(MORTY.id, item.id, source=the_list.PAGE_SOURCE, notice=notice),
                         the_list.UnchangedEdit(parked_again))

    def test_unpark_without_active_parking_changes_nothing(self) -> None:
        item = self.add()
        expired = the_list.park(MORTY.id, item.id, START, source="nate")
        plain = self.add()
        with patch.object(record_store, "write_text_atomic", wraps=atomic_file.write_text_atomic) as save:
            for current in (expired, plain):
                with self.subTest(item=current.id):
                    self.assertEqual(the_list.unpark(MORTY.id, current.id, source="nate"), the_list.UnchangedEdit(current))
                    self.assertEqual(the_list.load(MORTY.id, current.id), current)
        save.assert_not_called()

    def test_unpark_replays_a_receipt_without_a_saved_reason(self) -> None:
        item = self.add()
        _ = the_list.park(MORTY.id, item.id, START, source="nate")
        notice = the_list.PageItemNotice("unpark-1", "List page: nate unparked item 1.", "unpark")
        _ = the_list.unpark(MORTY.id, item.id, source=the_list.PAGE_SOURCE, notice=notice)
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        text = path.read_text()
        reason = ', "unpark_no_change": "expired"'
        self.assertIn(reason, text)
        _ = path.write_text(text.replace(reason, "", 1))

        parked = the_list.park(MORTY.id, item.id, START + timedelta(days=1), source="nate")
        replayed = the_list.unpark(MORTY.id, item.id, source=the_list.PAGE_SOURCE, notice=notice)
        self.assertEqual(replayed, the_list.UnchangedEdit(parked))
        self.assertEqual(the_list.unpark_no_change_reason(replayed.item, notice.request),
                         the_list.UnparkNoChangeReason.NO_ACTIVE_PARKING)

    def test_default_order_uses_waiting_unread_then_next_check(self) -> None:
        first = self.add("Later check", changes={"next_check": START + timedelta(hours=2)})
        second = self.add("Earlier check", changes={"next_check": START + timedelta(hours=1)})
        waiting = self.add("Needs nate", changes={"state": the_list.ItemState.WAITING_FOR_NATE})
        _ = the_list.mark_read(MORTY.id, waiting.id, source="nate")
        read = self.add("Read check", changes={"next_check": START - timedelta(minutes=1)})
        _ = the_list.mark_read(MORTY.id, read.id, source="nate")
        output = self.command("list")[1]
        numbers = [int(line.split("|")[1].strip()) for line in output.splitlines()[2:]]
        self.assertEqual(numbers, [waiting.number, second.number, first.number, read.number])
        self.assertIn("check overdue", output)
        self.assertIn("●", output)
        self.assertIn("—", output)
        for check in (START - timedelta(minutes=1), START + timedelta(hours=1), the_list.CheckCondition("The review ends")):
            closed = self.add("Closed check", changes={"next_check": check})
            _ = the_list.close(MORTY.id, closed.id, "The user removed it", source="nate")
            row = next(line for line in self.command("list", "--all")[1].splitlines()
                       if line.startswith(f"| {closed.number} |"))
            self.assertEqual(row.split("|")[5].strip(), "Closed")

    def test_item_links_keep_conditions_and_refuse_cycles(self) -> None:
        first, second, third = self.add("First"), self.add("Second"), self.add("Third")
        self.assertEqual(self.command("update", "--id", second.id, "--blocked-by", "L1:Proof is available",
            "--related-to", "L3", "--source", "morty"), (0, "", ""))
        item = the_list.load(MORTY.id, second.id)
        self.assertEqual(item.blocked_by, (the_list.ItemDependency(first.number, "Proof is available"),))
        self.assertEqual(item.related_to, (third.number,))
        _ = the_list.update(MORTY.id, third.id, {"blocked_by": (the_list.ItemDependency(second.number, "Delivered"),)}, source="morty")
        for identity, dependency in ((first.id, "L3:Available"), (second.id, "L2:Available"),
                                     (first.id, "L999:Available"), (first.id, "L3:")):
            with self.subTest(identity=identity, dependency=dependency):
                path = the_list.folder(MORTY.id) / f"{identity}.md"
                before = path.read_bytes()
                self.assertEqual(self.command("update", "--id", identity, "--blocked-by", dependency, "--source", "morty")[0], 1)
                self.assertEqual(path.read_bytes(), before)
        item_path = the_list.folder(MORTY.id) / f"{second.id}.md"
        original_item = item_path.read_bytes()
        for target in (first, third):
            target_path = item_path.with_name(f"{target.id}.md")
            original_target = target_path.read_bytes()
            for data in (b"bad record", b"\xff", None):
                with self.subTest(target=target.id, data=data):
                    if data is None:
                        target_path.unlink()
                    else:
                        _ = target_path.write_bytes(data)
                    with patch.object(the_list, "load", wraps=the_list.load) as reads:
                        self.assertEqual(self.command("mark-read", "--id", second.id, "--source", "nate"), (0, "", ""))
                    self.assertEqual([call.args for call in reads.call_args_list], [(MORTY.id, second.id)])
                    self.assertIs(the_list.load(MORTY.id, second.id).unread_since, the_list.NoTimestamp.UNSET)
                    _ = the_list.update(MORTY.id, second.id, {"latest_status": "References need repair"}, source="morty")
                    links: the_list.ItemChanges = ({"blocked_by": item.blocked_by} if target == first
                                                   else {"related_to": item.related_to})
                    before = item_path.read_bytes()
                    with self.assertRaises(the_list.ListError):
                        _ = the_list.update(MORTY.id, second.id, links, source="morty")
                    self.assertEqual(item_path.read_bytes(), before)
                    with self.assertRaises(the_list.ListError):
                        _ = self.add("Invalid reference", changes=links)
                    self.assertFalse(item_path.with_name("L4.md").exists())
                    _ = target_path.write_bytes(original_target)
                    _ = item_path.write_bytes(original_item)
        _ = the_list.close(MORTY.id, first.id, "Stop tracking", source="nate")
        self.assertEqual(the_list.load(MORTY.id, second.id).blocked_by, item.blocked_by)
        self.assertEqual(self.command("update", "--id", second.id, "--blocked-by", "--related-to", "--source", "morty"), (0, "", ""))
        self.assertEqual(the_list.load(MORTY.id, second.id).blocked_by, ())

    def test_concurrent_writers_keep_numbers_and_all_changes(self) -> None:
        def create(number: int) -> int:
            return self.add(f"Item {number}").number
        with ThreadPoolExecutor(max_workers=4) as writers:
            numbers = list(writers.map(create, range(12)))
        self.assertEqual(set(numbers), set(range(1, 13)))

        def change(number: int) -> None:
            _ = the_list.update(MORTY.id, "L1", {"latest_status": f"Report {number}"}, source=f"owner {number}")
        with ThreadPoolExecutor(max_workers=4) as writers:
            _ = list(writers.map(change, range(12)))
        item = the_list.load(MORTY.id, "L1")
        self.assertEqual(len(item.log), 13)
        self.assertEqual({entry.source for entry in item.log[1:]}, {f"owner {number}" for number in range(12)})
        self.assertFalse(list(the_list.folder(MORTY.id).glob("*.tmp")))

    def test_atomic_failure_keeps_the_previous_record(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        before = path.read_bytes()
        with patch.object(os, "replace", side_effect=OSError("The disk refused replacement")):
            result, output, error = self.command("update", "--id", item.id, "--state", "Working", "--source", "morty")
        self.assertEqual((result, output), (1, ""))
        self.assertEqual(len(error.splitlines()), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_invalid_records_keep_their_numbers_and_cache_sources(self) -> None:
        sources: list[Path] = []
        self.assertEqual(the_list.records(MORTY.id, sources=sources), [])
        self.assertEqual(sources, [the_list.folder(MORTY.id)])
        item = self.add()
        path = the_list.folder(MORTY.id) / "L2.md"
        for data in (b"bad", b"\xff"):
            with self.subTest(data=data):
                _ = path.write_bytes(data)
                sources.clear()
                errors = io.StringIO()
                with redirect_stderr(errors):
                    self.assertEqual(the_list.records(MORTY.id, sources=sources), [item])
                self.assertEqual(set(sources), {path.parent, path, path.parent / "L1.md"})
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn(str(path), errors.getvalue())
                with self.assertRaises(the_list.ListError):
                    _ = the_list.load(MORTY.id, "L2")
                self.assertEqual(self.add().number, 3)
                self.assertEqual(path.read_bytes(), data)
                (path.parent / "L3.md").unlink()

    def test_invalid_fields_and_inputs_refuse_without_writing(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / "L1.md"
        before = path.read_text()
        for field, old, new in (("state", "New", "Parked"), ("number", "1", "2"),
                                ("opened", START.isoformat(), "bad"), ("eta", "Not asked", "Tomorrow\nInvalid"),
                                ("closed", "", START.isoformat()), ("blocked_by", "[]", "[true]"),
                                ("reviews", "[]", "[true]"), ("reviews", "[]", '["C1", "C1"]')):
            with self.subTest(field=field):
                _ = path.write_text(before.replace(f"{field}: {json.dumps(old)}", f"{field}: {json.dumps(new)}"))
                with self.assertRaises(the_list.ListError):
                    _ = the_list.load(MORTY.id, item.id)
                _ = path.write_text(before)
        for fields in ({"eta": 1}, {"state": "Parked"}, {"unknown": "value"}, {"related_to": [True]}):
            with self.subTest(log_fields=fields):
                base, _, _ = before.partition("## Log\n\n")
                entry = {"time": START.isoformat(), "source": "nate", "action": "Updated", "fields": fields}
                _ = path.write_text(base + "## Log\n\n- " + json.dumps(entry) + "\n")
                with self.assertRaises(the_list.ListError):
                    _ = the_list.load(MORTY.id, item.id)
                _ = path.write_text(before)
        for args in (("add", "--title", "Bad\ntitle", "--owner", RUNNER.id, "--source", "nate"),
                     ("add", "--title", "Title", "--owner", OWNER.id, "--source", "nate"),
                     ("add", "--title", "Title", "--owner", RUNNER.id, "--number", "1", "--source", "nate"),
                     ("add", "--title", "Title", "--owner", RUNNER.id, "--producer", RUNNER.id, "--source", "nate"),
                     ("update", "--id", "L1", "--producer", "show", "--source", "morty"),
                     ("update", "--id", "L1", "--producer", "nobody", "--source", "morty"),
                     ("update", "--id", "../L1", "--title", "Title", "--source", "nate"),
                     ("update", "--id", "L1", "--next-check", "2026-11-16T19:00:00", "--source", "morty"),
                     ("update", "--id", "L1", "--eta", "2026-11-17T00:00:00+00:00/2026-11-16T00:00:00+00:00", "--source", "morty"),
                     ("update", "--id", "L1", "--title", "Changed", "--source", ""),
                     ("close", "--id", "L1", "--words", "", "--source", "nate")):
            with self.subTest(args=args):
                result, output, error = self.command(*args)
                self.assertEqual((result, output), (1, ""))
                self.assertEqual(len(error.splitlines()), 1)
                self.assertEqual(path.read_text(), before)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "missing"}):
            self.assertEqual(self.command("list"), (1, "", "the_list: the current session has no staff row\n"))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": RUNNER.current_session}):
            self.assertEqual(self.command("list"), (1, "", "the_list: the current session must be a producer\n"))

    def test_shared_records_preserve_investigation_format_and_separate_folders(self) -> None:
        investigation_item = investigation.file(MORTY.id, "Find the cause", "Why?\n\n## Answer\n\nKeep this text.", waiting_why='The owner must complete the investigation.')
        item = self.add()
        self.assertEqual((investigation_item.id, item.id), ("I1", "L1"))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)
        self.assertEqual(investigation.load(MORTY.id, investigation_item.id), investigation_item)
        path = investigation.folder(MORTY.id) / "I1.md"
        self.assertEqual(path.read_text().split("\n---\n\n", 1)[1], investigation_item.question + "\n\n## Answer\n\n")
        with patch.object(record_store, "write_text_atomic", wraps=atomic_file.write_text_atomic) as writes:
            _ = investigation.answer(MORTY.id, "I1", "Found it.")
            _ = the_list.update(MORTY.id, "L1", {"state": the_list.ItemState.WORKING}, source="morty")
        self.assertEqual({cast(Path, call.args[0]).parent for call in writes.call_args_list},
                         {investigation.folder(MORTY.id), the_list.folder(MORTY.id)})
        typed = replace(item, number=50, owner=the_list.UnassignedOwner.NATE,
                        opened=the_list.NoTimestamp.UNSET, updated=the_list.NoTimestamp.UNSET,
                        eta=the_list.EtaDescription("About November 19; approval required"),
                        next_check=the_list.CheckCondition("After the owner supplies proof"))
        dependent = replace(typed, number=51, owner=the_list.UnassignedOwner.NOT_RECORDED,
                            related_to=(50,), blocked_by=(the_list.ItemDependency(50, "Proof is available"),))
        the_list.create_records(OTHER.id, [dependent, typed])
        self.assertEqual(the_list.load(OTHER.id, "L50"), typed)
        self.assertEqual(the_list.load(OTHER.id, "L51"), dependent)
        text = the_list.table(OTHER.id)
        self.assertIn("| nate |", text)
        self.assertIn("| Not recorded |", text)
        self.assertIn("About November 19; approval required", text)
        self.assertIn("check: After the owner supplies proof", text)
        with self.assertRaises(the_list.ListError):
            the_list.create_records(OTHER.id, [replace(typed, number=52)])
        self.assertFalse((the_list.folder(OTHER.id) / "L52.md").exists())
        with self.assertRaises(the_list.ListError):
            the_list.create_records(OWNER.id, [typed, replace(dependent, related_to=(999,))])
        self.assertEqual(list(the_list.folder(OWNER.id).glob("*.md")), [])
        with self.assertRaises(the_list.ListError):
            the_list.create_records(OWNER.id, [typed, typed])
        self.assertEqual(list(the_list.folder(OWNER.id).glob("*.md")), [])
        _ = staff.set_producer_retirement(OTHER.id, staff.RetiredProducer("claude:sonnet:high"))
        with self.assertRaisesRegex(the_list.ListError, f"{OTHER.name}: retired producer"):
            the_list.create_records(OWNER.id, [typed, replace(dependent, producer=OTHER.id)])
        self.assertEqual(list(the_list.folder(OWNER.id).glob("*.md")), [])
        the_list.create_records(OWNER.id, [typed, dependent])
        self.assertEqual(the_list.records(OWNER.id), [typed, dependent])
        for text_eta in ("2026-11-20/2026-11-19", "2026-11-99/2026-11-19", "Paused"):
            with self.subTest(text_eta=text_eta):
                changed = the_list.update(OTHER.id, "L50", {"eta": the_list.EtaDescription(text_eta)}, source="morty")
                self.assertEqual(the_list.load(OTHER.id, "L50"), changed)
                self.assertIn('eta_kind: "text"', (the_list.folder(OTHER.id) / "L50.md").read_text())
        for eta in ("2026-11-19 (planned)", "Pending (check 18:00 EST)", "Paused"):
            with self.subTest(eta=eta):
                self.assertEqual(the_list.eta_text(the_list.eta_value(eta)), eta)
        with patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": ""}):
            self.assertEqual(the_list.folder(MORTY.id), self.root / ".local/state/showrunner" / MORTY.id / "list")
            self.assertEqual(update_registration.outstanding_path(update_registration.ProducerQuestions(MORTY)),
                             record_store.state_root() / "outstanding" / f"{MORTY.id}.json")
            self.assertEqual(investigation.state_root(), record_store.state_root())


if __name__ == "__main__":
    _ = unittest.main()
