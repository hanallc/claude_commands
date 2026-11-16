"""Test The List against private staff records and state folders."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.lib import atomic_file
from scripts.production import investigation, record_store, staff, the_list, update_registration
from scripts.production.test_investigation import OWNER, OTHER, RUNNER

MORTY = staff.StaffRow(staff.StaffId("536997b5-4415-4cb1-b683-d777e02e9a22"), "producer", "morty", staff.StaffRoot(),
                       current_session=staff.StaffId("20fa30c6-3cad-493a-b74a-0e408a93f865"))
START = datetime(2026, 11, 16, 18, tzinfo=UTC)


class TheListTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staff_file = self.root / "staff.json"
        _ = staff_file.write_text(json.dumps({"schema": 1, "rows": [row.to_json() for row in (MORTY, RUNNER, OWNER, OTHER)]}))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "STAFF_FILE": str(staff_file),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "CODEX_THREAD_ID": "",
            "CLAUDE_CODE_SESSION_ID": MORTY.current_session}))
        _ = self.enterContext(patch.object(the_list, "now", return_value=START))

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
        self.assertIn("- 2026-11-16 18:00:00+00:00 Added; ", path.read_text())
        self.assertNotIn("keeper:", path.read_text())
        self.assertIn('state: "Working"', initial_log.what)
        self.assertIn("eta: " + json.dumps(str(item.eta)), initial_log.what)
        for key in ("next_step", "latest_status"):
            self.assertIn(key + ": " + json.dumps(content), initial_log.what)
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
        self.assertIn("Delivered", item.log[-1].what)
        self.assertEqual(item.log[0], initial_log)
        self.assertEqual(item.next_step, "Review the page.")
        self.assertLess(path.read_text().index("Added; "), path.read_text().index("Marked read — nate"))
        self.assertIn("| # | Owner | Item | State | Next step | ETA | Update |", self.command("list")[1])
        self.assertIn('| 106 | show | The page: "List" | Delivered;', self.command("list")[1])

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
        self.assertEqual(reopened.log[-1].what, "Reopened")
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

    def test_parking_hides_the_item_until_its_time_without_changing_state(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WORKING})
        with patch.object(the_list, "user_zone", return_value=ZoneInfo("America/New_York")):
            self.assertEqual(self.command("park", "--id", item.id, "--until", "2026-11-17", "--source", "nate"), (0, "", ""))
        item = the_list.load(MORTY.id, item.id)
        self.assertEqual(item.parked_until, datetime(2026, 11, 17, 5, tzinfo=UTC))
        self.assertEqual(item.state, the_list.ItemState.WORKING)
        self.assertNotIn("| 1 |", self.command("list")[1])
        self.assertIn("| 1 |", self.command("list", "--include-parked")[1])
        self.assertIn("parked until", self.command("list", "--include-parked")[1])
        with patch.object(the_list, "now", return_value=datetime(2026, 11, 17, 5, tzinfo=UTC)):
            self.assertIn("| 1 |", self.command("list")[1])
        self.assertEqual(self.command("park", "--id", item.id, "--until", "", "--source", "nate"), (0, "", ""))
        self.assertIs(the_list.load(MORTY.id, item.id).parked_until, the_list.NoTimestamp.UNSET)

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
                                ("closed", "", START.isoformat()), ("blocked_by", "[]", "[true]")):
            with self.subTest(field=field):
                _ = path.write_text(before.replace(f"{field}: {json.dumps(old)}", f"{field}: {json.dumps(new)}"))
                with self.assertRaises(the_list.ListError):
                    _ = the_list.load(MORTY.id, item.id)
                _ = path.write_text(before)
        for args in (("add", "--title", "Bad\ntitle", "--owner", RUNNER.id, "--source", "nate"),
                     ("add", "--title", "Title", "--owner", OWNER.id, "--source", "nate"),
                     ("add", "--title", "Title", "--owner", RUNNER.id, "--number", "1", "--source", "nate"),
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
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("list"), (1, "", "the_list: the current session must be the morty producer\n"))

    def test_shared_records_preserve_investigation_format_and_separate_folders(self) -> None:
        investigation_item = investigation.file(MORTY.id, "Find the cause", "Why?\n\n## Answer\n\nKeep this text.")
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
