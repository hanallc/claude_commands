"""Test The List against private staff records and state folders."""
from __future__ import annotations

import errno
import fcntl
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Literal, Protocol, cast, final, override
from unittest.mock import Mock, patch
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from scripts.console_text.test_support import accepting_console_text
from scripts.agents import remote
from scripts.agents.remote import local_machine
from scripts.herdr.place import HerdrPlace
from scripts.lib import atomic_file, conf, main_publish
from scripts.message import send_result, session_send
from scripts.notify import review
from scripts.production import change_review, delivery_state, investigation, record_store, repeat_rule, session_pane, staff, the_list, update_registration
from scripts.production.test_investigation import OWNER, OTHER, RUNNER
from scripts.review_links import change_page, list_page, new_item_dialog, question_record, review_items, review_submissions

MORTY = staff.StaffRow(staff.StaffId("536997b5-4415-4cb1-b683-d777e02e9a22"), "producer", "morty", staff.StaffRoot(),
                       current_session=staff.StaffId("20fa30c6-3cad-493a-b74a-0e408a93f865"))
BOB = staff.StaffRow(staff.StaffId("a02a55c8-a2c4-47d9-bf99-fd30ffe75247"), "producer", "bob", staff.StaffRoot(),
                     current_session=staff.StaffId("32c954ea-25b3-44e7-9eed-3218ce9df1d6"))
START = datetime(2026, 11, 16, 18, tzinfo=UTC)
UPKEEP_INTERVAL = timedelta(minutes=2)
LIST_TEXT_LIMITS = {
    "max_chars.latest_status": 80,
    "max_chars.next_step": 72,
    "max_chars.evidence_line": 90,
}


def copy_list_item(identity: str, *, source_keeper: staff.StaffId, destination_keeper: staff.StaffId) -> None:
    """Copy a List fixture into another keeper's folder."""
    filename = f"{identity}.md"
    destination = the_list.folder(destination_keeper)
    destination.mkdir(parents=True, exist_ok=True)
    _ = shutil.copyfile(the_list.folder(source_keeper) / filename, destination / filename)


class ProducerScopedUpdate(Protocol):
    def __call__(self, keeper: staff.StaffId, identity: str, changes: the_list.ItemChanges, *,
                 source: str, assigned_to: staff.StaffRow, schedule_edits: the_list.ScheduleChanges) -> the_list.ListEdit: ...


scoped_update = cast(ProducerScopedUpdate, getattr(the_list, "_update"))


class PreviousLogEntry(Protocol):
    @property
    def what(self) -> object: ...


class PreviousListItem(Protocol):
    @property
    def title(self) -> str: ...

    @property
    def latest_status(self) -> str: ...

    @property
    def next_step(self) -> str: ...

    @property
    def log(self) -> tuple[PreviousLogEntry, ...]: ...

    @property
    def latest_status_time(self) -> object: ...

    @property
    def unread_since(self) -> object: ...


class PreviousListReader(Protocol):
    def load(self, keeper: staff.StaffId, identity: str) -> PreviousListItem: ...

    def update(self, keeper: staff.StaffId, identity: str, changes: dict[str, str], *,
               source: str) -> PreviousListItem: ...


@final
class TheListTests(unittest.TestCase):
    root: Path = Path()
    list_config: Path = Path()

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
            "CLAUDE_CODE_SESSION_ID": MORTY.current_session,
            # Each date this file expects is a New York date; the live zone setting must not move it.
            "USER_ZONE": "America/New_York"}))
        _ = os.environ.pop("CONF_DIR", None)
        self.list_config = self.root / ".claude/config/the_list.conf"
        self.list_config.parent.mkdir(parents=True)
        settings = {**LIST_TEXT_LIMITS, "series_run_limit": 730, "confirm_wait_minutes": 15}
        _ = self.list_config.write_text("".join(f"# whole: 1..\n{key}={limit}\n" for key, limit in settings.items()))
        _ = self.enterContext(patch.object(the_list, "now", return_value=START))
        self.enterContext(accepting_console_text())
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

    def test_image_answer_retry_keeps_the_original_producer_and_a_later_state(self) -> None:
        item = self.add(changes={"producer": BOB.id, "state": the_list.ItemState.WAITING_FOR_NATE,
                                 "eta": the_list.EtaStatus.WAITING_FOR_NATE, "next_step": "Check the results."})
        key = "review:" + "a" * 64
        words = "Image 000000000001 approved.\nKeep the first row."
        self.assertEqual(the_list.save_image_answer(
            MORTY.id, item.id, key, words, image_id="000000000001", approved=True), BOB.id)
        saved = the_list.load(MORTY.id, item.id)
        self.assertIs(saved.state, the_list.ItemState.WORKING)
        self.assertIs(saved.eta, the_list.EtaStatus.NO_TIME_YET)
        self.assertEqual(saved.next_step, "bob: act on nate's answer to picture 000000000001")
        self.assertIs(the_list.unread_update(saved), the_list.NoUnreadUpdate.ABSENT)
        self.assertEqual(saved.log[-1].what, words)
        later = the_list.update(MORTY.id, item.id, {"producer": MORTY.id,
                                "state": the_list.ItemState.WAITING_FOR_NATE}, source="morty")
        self.assertEqual(the_list.save_image_answer(
            MORTY.id, item.id, key, words, image_id="000000000001", approved=True), BOB.id)
        self.assertEqual(the_list.load(MORTY.id, item.id), later)

    def test_image_answer_replaces_the_next_step(self) -> None:
        image = "000000000001"
        item = self.add(changes={"producer": BOB.id, "next_step": f"nate reviews picture {image}."})
        _ = the_list.save_image_answer(MORTY.id, item.id, "review:next", f"Image {image}: Approved.",
                                       image_id=image, approved=True)
        self.assertEqual(the_list.load(MORTY.id, item.id).next_step,
                         f"bob: act on nate's answer to picture {image}")

    def test_image_answer_marks_read_and_keeps_open_questions(self) -> None:
        image = "000000000001"
        item = self.add()
        question = question_record.Question("Q1", START.isoformat(), "Which check passed?")
        self.write_legacy_question(item, question)
        self.assertIsInstance(the_list.unread_update(the_list.load(MORTY.id, item.id)), the_list.UnreadUpdate)
        _ = the_list.save_image_answer(MORTY.id, item.id, "review:read", f"Image {image}: Feedback.",
                                       image_id=image, approved=False)
        saved = the_list.load(MORTY.id, item.id)
        self.assertIs(saved.unread_since, the_list.NoTimestamp.UNSET)
        self.assertEqual(saved.questions, (question,))
        self.assertTrue(any(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log))

    def test_image_answer_marks_read_and_closes_only_the_matching_ask(self) -> None:
        image = "000000000001"
        item = self.add()
        picture_ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.APPROVAL, target=OWNER.id,
            what=f"Review picture {image}.", why="The layout changed.", config=False, requester=MORTY.id, source="morty")
        other_ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id,
            what=f"Check picture f{image}.", why="Another layout changed.", config=False, requester=MORTY.id, source="morty")
        _ = the_list.save_image_answer(MORTY.id, item.id, "review:read", f"Image {image}: Feedback.",
                                       image_id=image, approved=False)
        saved = the_list.load(MORTY.id, item.id)
        self.assertIs(saved.unread_since, the_list.NoTimestamp.UNSET)
        self.assertEqual(the_list.open_asks(saved), (other_ask,))
        self.assertEqual(saved.asks[0].ask_id, picture_ask.ask_id)
        self.assertTrue(any(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log))

    def test_image_answer_uses_the_state_and_verdict_for_the_eta(self) -> None:
        image = "000000000001"
        for state in (the_list.ItemState.WAITING_FOR_NATE, the_list.ItemState.DELIVERED, the_list.ItemState.WORKING):
            for approved in (True, False):
                with self.subTest(state=state, approved=approved):
                    item = self.add(changes={"state": state, "eta": the_list.EtaStatus.WAITING_FOR_NATE})
                    words = f"Image {image}: " + ("Approved." if approved else "Feedback.")
                    _ = the_list.save_image_answer(MORTY.id, item.id, "review:state", words,
                                                   image_id=image, approved=approved)
                    saved = the_list.load(MORTY.id, item.id)
                    keeps_delivery = state is the_list.ItemState.DELIVERED and approved
                    self.assertEqual(saved.state, state if keeps_delivery else the_list.ItemState.WORKING)
                    self.assertEqual(saved.eta, the_list.EtaStatus.WAITING_FOR_NATE if keeps_delivery
                                     else the_list.EtaStatus.NO_TIME_YET)
                    self.assertEqual(saved.log[-1].what, words)

    def test_tag_normalizes_one_line_and_refuses_invalid_text(self) -> None:
        self.assertEqual(the_list.Tag(" Review  Flow ").text, "review flow")
        for text in ("", "  ", "review,flow", "review\nflow", "review\r", "review\u2028flow"):
            with self.subTest(text=text), self.assertRaises(the_list.ListError):
                _ = the_list.Tag(text)

    def test_tag_and_page_action_refuse_control_characters(self) -> None:
        error = "^a tag cannot contain a control character$"
        for text in ("a\u0000b", "a\tb", "a\x7fb", "a\x80b", "a\x9fb"):
            with self.subTest(text=text), self.assertRaisesRegex(the_list.ListError, error):
                _ = the_list.Tag(text)
        for text in ("a\ud800b", "a\udfffb"):
            with self.subTest(text=text), self.assertRaisesRegex(
                    the_list.ListError, "^a tag cannot contain a lone surrogate$"):
                _ = the_list.Tag(text)
        self.assertEqual(the_list.Tag("a\ufffdb").text, "a\ufffdb")
        self.assertEqual(the_list.Tag("a\U0001f642b").text, "a\U0001f642b")

        item = self.add()
        signed = list_page.signed_tag_request(self.root, MORTY.id, item.id)
        body = urlencode({**signed, "keeper": MORTY.id, "id": item.id,
                          "action": "add-tag", "words": "a\u0000b", "until": ""})
        with self.assertRaisesRegex(the_list.ListError, error):
            _ = list_page.save_action(self.root, body)
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_tag_refuses_the_reserved_empty_list_text(self) -> None:
        for text in ("none", "None", "  NONE  "):
            with self.subTest(text=text), self.assertRaisesRegex(
                    the_list.ListError, "'none' is reserved for an empty tag list"):
                _ = the_list.Tag(text)
        self.assertEqual(the_list.Tag("Review none").text, "review none")

    def test_tag_changes_keep_order_and_log_once_for_each_write(self) -> None:
        item = self.add()
        flow, build = the_list.Tag("Review flow"), the_list.Tag("build")
        change = the_list.add_tag(MORTY.id, item.id, flow, source="morty")
        self.assertEqual(change.log, (*item.log, the_list.ItemChange(
            START, the_list.TagsChanged((flow,)), "morty")))
        change = the_list.add_tag(MORTY.id, item.id, build, source="morty")
        self.assertEqual(change.tags, (flow, build))
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertIn(("tags: " + json.dumps(json.dumps(["review flow", "build"])) + "\n").encode(), before[0])
        self.assertEqual(the_list.add_tag(MORTY.id, item.id, the_list.Tag("Review  Flow "), source="morty"), change)
        self.assertEqual(the_list.remove_tag(MORTY.id, item.id, the_list.Tag("missing"), source="morty"), change)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

        removed = the_list.remove_tag(MORTY.id, item.id, flow, source="morty")
        self.assertEqual(removed.tags, (build,))
        self.assertEqual(removed.log, (*change.log, the_list.ItemChange(
            START, the_list.TagsChanged((build,)), "morty")))
        restored = the_list.add_tag(MORTY.id, item.id, flow, source="morty")
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved, restored)
        self.assertEqual(saved.tags, (build, flow))
        self.assertEqual(list(the_list.log_field_changes(saved, include_tags=True))[-1], {"tags": (build, flow)})
        self.assertEqual(list(the_list.log_field_changes(saved))[-1], {})

    def previous_list_reader(self) -> PreviousListReader:
        revision = "9f48ce4ef2:scripts/production/the_list.py"
        old_source = subprocess.check_output(
            ["git", "show", revision], cwd=Path(__file__).resolve().parents[2], text=True)
        module = ModuleType("scripts.production.previous_the_list")
        module.__file__ = str(Path(the_list.__file__).with_name("previous_the_list.py"))
        self.enterContext(patch.dict(sys.modules, {module.__name__: module}))
        exec(compile(old_source, revision, "exec"), module.__dict__)
        _ = self.enterContext(patch.object(module, "now", return_value=START))
        return cast(PreviousListReader, cast(object, module))

    def test_previous_reader_accepts_tag_add_and_last_removal(self) -> None:
        previous = self.previous_list_reader()
        item = self.add(changes={"latest_status": "The page is ready."})
        original = previous.load(MORTY.id, item.id)
        tag = the_list.Tag('Review "flow"')
        added = the_list.add_tag(MORTY.id, item.id, tag, source="morty")
        with self.subTest(operation="add"):
            saved = previous.load(MORTY.id, item.id)
            self.assertEqual((saved.title, saved.latest_status), (original.title, original.latest_status))
            self.assertEqual(saved.log[:-1], original.log)
            self.assertEqual(len(saved.log), len(added.log))
            self.assertEqual(saved.log[-1].what, 'Tags: review "flow"')

        with self.subTest(operation="rewrite with the previous writer"):
            saved = previous.update(MORTY.id, item.id, {"next_step": "Run checks."}, source="morty")
            path = the_list.folder(MORTY.id) / f"{item.id}.md"
            self.assertNotIn(b"\ntags:", path.read_bytes())
            replayed = the_list.load(MORTY.id, item.id)
            self.assertEqual(replayed.tags, (tag,))
            self.assertEqual(replayed.log[:-1], added.log)
            self.assertEqual(replayed.next_step, saved.next_step)

        removed = the_list.remove_tag(MORTY.id, item.id, tag, source="morty")
        with self.subTest(operation="remove last"):
            saved = previous.load(MORTY.id, item.id)
            self.assertEqual((saved.title, saved.latest_status), (original.title, original.latest_status))
            self.assertEqual(saved.log[:len(original.log)], original.log)
            self.assertEqual(len(saved.log), len(removed.log))
            self.assertEqual(saved.log[-1].what, "Tags: none")

        current = the_list.load(MORTY.id, item.id)
        self.assertEqual(current, removed)
        self.assertEqual(current.tags, ())
        self.assertEqual(list(the_list.log_field_changes(current, include_tags=True))[-3:],
                         [{"tags": (tag,)}, {"next_step": "Run checks."}, {"tags": ()}])

    def test_tag_edits_leave_unread_and_mark_read_target_unchanged(self) -> None:
        item = self.add()
        target = the_list.unread_update(item)
        assert isinstance(target, the_list.UnreadUpdate)
        tag = the_list.Tag("build")
        with patch.object(the_list, "now", return_value=START + timedelta(minutes=1)):
            added = the_list.add_tag(MORTY.id, item.id, tag, source="morty")
            with self.subTest(operation="add to unread item"):
                self.assertEqual(added.unread_since, item.unread_since)
                self.assertEqual(the_list.unread_update(the_list.load(MORTY.id, item.id)), target)
            removed = the_list.remove_tag(MORTY.id, item.id, tag, source="morty")
            with self.subTest(operation="remove from unread item"):
                self.assertEqual(removed.unread_since, item.unread_since)
                self.assertEqual(the_list.unread_update(removed), target)
        read = the_list.mark_read(MORTY.id, item.id, source=the_list.PAGE_SOURCE, update=target)
        self.assertIs(read.unread_since, the_list.NoTimestamp.UNSET)
        previous = self.previous_list_reader()
        updated = the_list.update(MORTY.id, item.id, {"tags": (tag,), "title": read.title}, source="morty")
        self.assertIs(the_list.unread_update(updated), the_list.NoUnreadUpdate.ABSENT)
        self.assertEqual(previous.load(MORTY.id, item.id).unread_since, "")
        self.assertIs(the_list.unread_update(the_list.remove_tag(MORTY.id, item.id, tag, source="morty")),
                      the_list.NoUnreadUpdate.ABSENT)

    def test_previous_reader_keeps_added_fields_and_status_time_with_tags(self) -> None:
        previous = self.previous_list_reader()
        original = self.add(changes={"latest_status": "The page is ready."})
        tag = the_list.Tag("review flow")
        tagged = self.add(changes={"latest_status": original.latest_status, "tags": (tag,)})
        original_saved = previous.load(MORTY.id, original.id)
        tagged_saved = previous.load(MORTY.id, tagged.id)
        with self.subTest(operation="add"):
            self.assertEqual(tagged_saved.log[0], original_saved.log[0])
            self.assertEqual(tagged_saved.latest_status_time, START)
            self.assertEqual(tagged_saved.log[1].what, "Tags: review flow")

        moment = START + timedelta(minutes=1)
        with patch.object(the_list, "now", return_value=moment):
            updated = the_list.update(MORTY.id, tagged.id, {
                "latest_status": "The check passed.", "tags": (tag, the_list.Tag("build"))}, source="morty")
        with self.subTest(operation="update"):
            saved = previous.load(MORTY.id, tagged.id)
            self.assertEqual(saved.latest_status, "The check passed.")
            self.assertEqual(saved.latest_status_time, moment)
            self.assertEqual(saved.log[-1].what, "Tags: review flow, build")
            self.assertEqual(len(saved.log), len(tagged_saved.log) + 2)
        self.assertEqual(the_list.load(MORTY.id, tagged.id), updated)
        self.assertEqual(list(the_list.log_field_changes(updated, include_tags=True))[-2:],
                         [{"latest_status": "The check passed."}, {"tags": (tag, the_list.Tag("build"))}])

    def test_unrelated_and_invalid_tag_notes_stay_plain(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        before = path.read_text().rstrip("\n") + "\n"
        for note in ("List tags: review later", "Tags: see later,", "Tags: build, build",
                     "Tags: "):
            with self.subTest(note=note):
                row = {"time": START.isoformat(), "source": "morty", "note": note}
                _ = path.write_text(before + "- " + json.dumps(row) + "\n")
                current = the_list.load(MORTY.id, item.id)
                self.assertEqual(current.log[-1].what, note)
                self.assertEqual(current.tags, ())

        note = "Tags: see later"
        _ = path.write_text(before + f"- 2026-11-16 18:00:00+00:00 {note} — morty\n")
        current = the_list.load(MORTY.id, item.id)
        self.assertEqual(current.log[-1].what, note)
        self.assertEqual(current.tags, ())
        _ = the_list.update(MORTY.id, item.id, {"next_step": "Review the page."}, source="morty")
        reread = the_list.load(MORTY.id, item.id)
        self.assertEqual(reread.log[-2].what, note)
        self.assertEqual(reread.tags, ())

    def test_duplicate_tags_are_refused_in_edits_and_headers(self) -> None:
        item = self.add()
        tags = (the_list.Tag("build"), the_list.Tag("BUILD"))
        with self.assertRaises(the_list.ListError):
            _ = the_list.update(MORTY.id, item.id, {"tags": tags}, source="morty")
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        duplicates = json.dumps(json.dumps(["build", "BUILD"]))
        _ = path.write_text(path.read_text().replace('tags: "[]"', f"tags: {duplicates}"))
        with self.assertRaises(the_list.ListError):
            _ = the_list.load(MORTY.id, item.id)

    def test_old_record_without_tags_reads_without_a_write(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        _ = path.write_text(path.read_text().replace('tags: "[]"\n', ""))
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertNotIn(b"\ntags:", before[0])
        self.assertEqual(the_list.load(MORTY.id, item.id), item)
        self.assertEqual(the_list.load(MORTY.id, item.id).tags, ())
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_tags_count_open_items_and_list_requires_every_tag(self) -> None:
        flow, build, archive = the_list.Tag("review flow"), the_list.Tag("build"), the_list.Tag("archive")
        both = self.add("Both tags", changes={"tags": (flow, build), "producer": OTHER.id})
        one = self.add("One tag", changes={"tags": (flow,), "producer": BOB.id})
        _ = self.add("No tags")
        closed = self.add("Closed item", changes={"tags": (flow, build)})
        _ = the_list.close(MORTY.id, closed.id, "Delivered.", source="morty")
        self.assertEqual(self.command("tag", "--id", closed.id, "--add", "Archive", "--source", "morty"), (0, "", ""))
        items = the_list.records(MORTY.id, include_closed=True)
        closed = next(item for item in items if item.id == closed.id)
        self.assertEqual(closed.tags, (flow, build, archive))
        self.assertEqual(closed.unread_since, the_list.NoTimestamp.UNSET)
        self.assertEqual(the_list.tag_counts(items), {flow: 2, build: 1})
        self.assertEqual(the_list.select_by_tags(items, (flow, build, flow)), [both, closed])
        self.assertEqual(the_list.select_by_tags(items, ()), items)
        self.assertEqual(self.command("tags"), (0, "build\t1\nreview flow\t2\n", ""))
        code, output, error = self.command("list", "--tag", "Review  Flow ", "--tag", "Build")
        self.assertEqual((code, error), (0, ""))
        self.assertIn(both.title, output)
        for title in (one.title, "No tags", closed.title):
            self.assertNotIn(title, output)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": BOB.current_session}):
            self.assertEqual(self.command("tags"), (0, "review flow\t1\n", ""))
            code, output, error = self.command("list", "--tag", "Review Flow", "--tag", "Build")
            self.assertEqual((code, error), (0, ""))
            self.assertNotIn(both.title, output)

    def test_producer_tags_only_its_own_items_under_the_write_lock(self) -> None:
        own = self.add(changes={"producer": OTHER.id})
        other = self.add(changes={"producer": BOB.id, "tags": (the_list.Tag("build"),)})
        path = the_list.folder(MORTY.id) / f"{other.id}.md"
        before = path.read_bytes(), path.stat().st_mtime_ns
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("tag", "--id", own.id, "--add", " Build "), (0, "", ""))
            tagged = the_list.load(MORTY.id, own.id)
            self.assertEqual(tagged.tags, (the_list.Tag("build"),))
            self.assertEqual(tagged.log[-1].source, OTHER.name)
            self.assertEqual(self.command("tag", "--id", own.id, "--remove", "BUILD", "--source", "morty"), (0, "", ""))
            for option in ("--add", "--remove"):
                with self.subTest(option=option):
                    self.assertEqual(self.command("tag", "--id", other.id, option, "build"), (1, "", (
                        f"the_list: {other.id} is assigned to another producer. "
                        + "A producer can change only its own items.\n")))
        saved = the_list.load(MORTY.id, own.id)
        self.assertEqual(saved.tags, ())
        self.assertEqual(saved.log[-1].source, OTHER.name)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.command("tag", "--id", other.id, "--remove", "build", "--source", "morty"), (0, "", ""))
        self.assertEqual(the_list.load(MORTY.id, other.id).tags, ())

    def test_log_field_changes_replays_linked_delivery(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WORKING})
        delivered = the_list.UnparkedForDelivery(the_list.LinkedChangeDelivered("sdlc/C1"))
        update = the_list.FieldChange("Updated", {"state": the_list.ItemState.DELIVERED, "next_step": "Read the page."})
        item = replace(item, log=(*item.log, the_list.ItemChange(START, delivered, "the List"),
                                  the_list.ItemChange(START, update, "morty")))
        self.assertEqual(list(the_list.log_field_changes(item))[-2:], [{}, {"next_step": "Read the page."}])

    def test_log_field_changes_replays_legacy_reopened(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WORKING})
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        _ = path.write_text(path.read_text().rstrip("\n")
            + '\n- 2026-11-16 18:10:00+00:00 Closed: Done. — nate\n'
            + '- 2026-11-16 18:20:00+00:00 Reopened — nate\n'
            + '- 2026-11-16 18:30:00+00:00 Updated state: "New"; next_step: "Read the page." — morty\n')
        restored = the_list.load(MORTY.id, item.id)
        self.assertEqual(list(the_list.log_field_changes(restored))[-1], {"next_step": "Read the page."})

    def test_log_field_changes_replays_the_saved_requester(self) -> None:
        for requester in the_list.RequestedBy:
            with self.subTest(requester=requester):
                item = the_list.add(MORTY.id, "Track the requester", owner=RUNNER.id, source="nate",
                                    requested_by=requester, changes={})
                _ = the_list.update(MORTY.id, item.id, {"requested_by": requester, "next_step": "Read the page."},
                                    source="morty")
                added, updated = the_list.log_field_changes(the_list.load(MORTY.id, item.id))
                self.assertEqual(added["requested_by"], requester)
                self.assertEqual(updated, {"next_step": "Read the page."})

    def test_log_field_changes_uses_defaults_for_fields_that_legacy_added_omits(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        header, marker, _ = path.read_text().partition("## Log\n\n")
        _ = path.write_text(header + marker
            + '- 2026-11-16 18:00:00+00:00 Added; title: "Deliver the page" — nate\n'
            + '- 2026-11-16 18:10:00+00:00 Updated evidence: "" — morty\n')
        added, updated = the_list.log_field_changes(the_list.load(MORTY.id, item.id))
        self.assertEqual(added, {"title": item.title})
        self.assertEqual(updated, {})

    def test_log_field_changes_shows_only_the_append_after_legacy_evidence_cleanup(self) -> None:
        item = self.add(changes={"evidence": "Keep this line.\nMigration copy."})
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        _ = path.write_text(path.read_text().rstrip("\n")
            + "\n- 2026-11-16 18:10:00+00:00 Removed the migration copy of checkins.json from Evidence — morty\n"
            + r'- 2026-11-16 18:20:00+00:00 Updated evidence: "Keep this line.\nAppended line." — morty' + "\n")
        changes = list(the_list.log_field_changes(the_list.load(MORTY.id, item.id)))
        self.assertEqual(changes[1:], [{}, {"evidence": "Appended line."}])

    def test_log_field_changes_shows_only_added_evidence_lines_in_order(self) -> None:
        item = self.add(changes={"evidence": "Keep this line.\nRemove this line."})
        _ = the_list.update(MORTY.id, item.id,
                            {"evidence": "First addition.\nKeep this line.\nSecond addition."}, source="morty")
        updated = the_list.update(MORTY.id, item.id, {"evidence": "Keep this line."}, source="morty")
        changes = list(the_list.log_field_changes(updated))
        self.assertEqual(changes[1:], [{"evidence": "First addition.\nSecond addition."}, {}])

    def test_log_field_changes_ignores_evidence_line_ending_changes(self) -> None:
        item = self.add(changes={"evidence": "First line.\r\nSecond line.\r\n"})
        _ = the_list.update(MORTY.id, item.id, {"evidence": "First line.\nSecond line.\n"}, source="morty")
        added, updated = the_list.log_field_changes(the_list.load(MORTY.id, item.id))
        self.assertEqual(added["evidence"], "First line.\r\nSecond line.\r\n")
        self.assertEqual(updated, {})

    def test_log_field_changes_shows_the_first_requester_after_legacy_added_omits_it(self) -> None:
        item = self.add(changes={"requested_by": the_list.RequestedBy.DWIGHT})
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        header, marker, _ = path.read_text().partition("## Log\n\n")
        _ = path.write_text(header + marker
            + '- 2026-11-16 18:00:00+00:00 Added; title: "Deliver the page" — nate\n'
            + '- 2026-11-16 18:10:00+00:00 Updated requested_by: "nate" — morty\n'
            + '- 2026-11-16 18:20:00+00:00 Updated requested_by: "nate" — morty\n')
        changes = list(the_list.log_field_changes(the_list.load(MORTY.id, item.id)))
        self.assertEqual(changes[1:], [{"requested_by": the_list.RequestedBy.NATE}, {}])

    def test_commands_preserve_fields_text_and_history(self) -> None:
        content = "Use the result.\r\n\n\n## Latest status\n\nThis heading stays in the text.\n"
        result = self.command("add", "--title", 'The page: "List"', "--owner", RUNNER.id,
            "--number", "106", "--source", "nate", "--state", "Working", "--unit", "unit-board",
            "--next-check", "2026-11-16T19:00:00+00:00", "--eta", "Nov 16 15:00 EST",
            "--next-step", content, "--latest-status", content, "--evidence", content, "--done-when", content,
            "--requested-by", "dwight")
        self.assertEqual(result, (0, "L106\n", ""))
        item = the_list.load(MORTY.id, "L106")
        self.assertEqual((item.number, item.owner, item.unit, item.state, item.requested_by),
            (106, RUNNER.id, "unit-board", the_list.ItemState.WORKING, the_list.RequestedBy.DWIGHT))
        self.assertEqual((item.next_step, item.latest_status, item.evidence, item.done_when), (content,) * 4)
        self.assertEqual((item.opened, item.updated, item.unread_since), (START,) * 3)
        self.assertIs(item.closed, the_list.NoTimestamp.UNSET)
        self.assertEqual(item.eta, START + timedelta(hours=2))
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

    def test_add_and_update_check_visible_fields_before_writing(self) -> None:
        add = ("add", "--source", "morty", "--owner", RUNNER.id, "--title", "Keep the display usable")
        before = the_list.records(MORTY.id)
        code, _, errors = self.command(*add, "--next-step", "Read /tmp/proof.txt.", "--latest-status", "Saved at 9ab82dea.")
        self.assertEqual(code, 1)
        self.assertIn("omit-file-path: /tmp/proof.txt", errors)
        self.assertIn("omit-commit-hash: 9ab82dea", errors)
        self.assertIn("Revise the list field draft.", errors)
        self.assertEqual(the_list.records(MORTY.id), before)

        code, output, errors = self.command(*add, "--next-step", "Check both labels.")
        self.assertEqual(code, 0, errors)
        identity = output.strip()
        item = the_list.load(MORTY.id, identity)
        self.assertEqual(item.next_step, "Check both labels.")
        update = ("update", "--source", "morty", "--id", identity)
        code, _, errors = self.command(*update, "--add-evidence", "Read /tmp/screen.png.")
        self.assertEqual(code, 1)
        self.assertIn("omit-file-path: /tmp/screen.png", errors)
        self.assertEqual(the_list.load(MORTY.id, identity), item)

        code, _, errors = self.command(*update, "--add-evidence", "Both labels are visible.")
        self.assertEqual(code, 0, errors)
        self.assertEqual(the_list.load(MORTY.id, identity).evidence, "Both labels are visible.")

    def add_scheduled(self, start: str, *options: str) -> tuple[int, str, str]:
        return self.command("add", "--title", "Check the predictions", "--owner", RUNNER.id,
                            "--state", "scheduled", "--start", start, "--do", "Read the figures.\nSend the total.",
                            "--source", "nate", *options)

    def save_series_progress(self, identity: str, *, finished: range | frozenset[int]) -> None:
        store = record_store.NumberedRecords(MORTY.id, "list", "L", "list item", the_list.ListError)
        fields, body = store.read(identity)
        schedule = cast(dict[str, object], json.loads(fields["schedule"]))
        schedule["runs_used"] = len(finished)
        schedule["finished_runs"] = sorted(finished)
        fields["schedule"] = json.dumps(schedule)
        store.write(identity, fields, body)

    def test_scheduled_items_round_trip_and_list_their_next_run(self) -> None:
        start = "2026-11-30T09:00[America/Los_Angeles]"
        stamp = "2026-11-30T09:00:00-08:00[America/Los_Angeles]"
        draft = review_submissions.NewItemDraft(new_item_dialog.ScheduledDraftId("s1-" + "a" * 32), review_submissions.ListDraftClaim(
            "b" * 64, MORTY.id, "Assign to the runner.", ("Starts on Monday.", "Does not repeat"),
            (review_submissions.ListAssignmentNotice(RUNNER.id, "showrunner", "Read the figures.", "morty"),)))
        for options, detail, origin in (
            ((), "Does not repeat", review_submissions.NoNewItemDraft.ABSENT),
            (("--repeat", "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH"), "Repeats: Every 2 weeks on Monday and Thursday",
             review_submissions.NoNewItemDraft.ABSENT),
            ((), "Does not repeat", draft),
        ):
            with self.subTest(detail=detail, origin=origin):
                if isinstance(origin, review_submissions.NewItemDraft):
                    start_time = datetime(2026, 11, 30, 9, tzinfo=ZoneInfo("America/Los_Angeles"))
                    chosen: list[int] = []
                    saved = the_list.add(MORTY.id, "Check the predictions", owner=RUNNER.id, source="nate",
                                         changes={"state": the_list.ItemState.SCHEDULED,
                                                  "schedule": the_list.ScheduledRun(start_time, "Read the figures.\nSend the total."),
                                                  "evidence": "Read the source report."},
                                         new_item_draft_id=origin, before_save=chosen.append)
                    self.assertEqual(chosen, [saved.number])
                    identity = saved.id
                else:
                    code, output, errors = self.add_scheduled(start, *options)
                    self.assertEqual((code, errors), (0, ""))
                    description, identity = output.splitlines()
                    self.assertEqual(description, detail)
                item = the_list.load(MORTY.id, identity)
                self.assertEqual(item.new_item_draft_id, origin)
                schedule = item.schedule
                self.assertIs(item.state, the_list.ItemState.SCHEDULED)
                self.assertIs(item.parked_until, the_list.NoTimestamp.UNSET)
                if isinstance(schedule, the_list.ScheduledSeries):
                    run = schedule.first
                    self.assertEqual(schedule.rule.rrule, "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH")
                else:
                    assert isinstance(schedule, the_list.ScheduledRun)
                    run = schedule
                self.assertEqual(run.start.tzinfo, ZoneInfo("America/Los_Angeles"))
                self.assertEqual(run.words, "Read the figures.\nSend the total.")
                shown = self.command("list")[1]
                self.assertIn(f"Scheduled · Starts {stamp}", shown)
                if options:
                    self.assertIn(f"Every 2 weeks on Monday and Thursday · next {stamp}", shown)
                event = item.log[-1].what
                assert isinstance(event, the_list.FieldChange)
                self.assertEqual(event.fields.get("schedule"), schedule)
                page = list_page.render(self.root, include_counts=False).decode()
                self.assertIn("Check the predictions", page)
                if isinstance(origin, review_submissions.NewItemDraft):
                    self.assertEqual(item.evidence, "Read the source report.")
                    self.assertNotIn(origin.id.value, shown)
                    self.assertNotIn(origin.id.value, page)

        second = datetime(2026, 11, 30, 17, tzinfo=UTC) + timedelta(days=1)
        moved = second + timedelta(hours=1)
        third = second + timedelta(days=1)
        for label, finished, expected in (
            ("run 2 comes before a delayed run 1", frozenset[int](), ((2, second), (1, moved), (3, third))),
            ("finished run 2 leaves run 1 pending", frozenset({2}), ((1, moved), (3, third))),
            ("finishing both runs leaves run 3 pending", frozenset({1, 2}), ((3, third),)),
        ):
            with self.subTest(label=label):
                code, output, errors = self.add_scheduled((second - timedelta(days=1)).isoformat(), "--repeat", "FREQ=DAILY;COUNT=3")
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                self.assertEqual(self.command("update", "--id", identity, "--scope", "this", "--source", "nate",
                                              "--start", moved.isoformat()), (0, "Repeats: Daily for 3 runs\n", ""))
                series = the_list.load(MORTY.id, identity).schedule
                assert isinstance(series, the_list.ScheduledSeries)
                progress = series.finish_runs(finished)
                self.assertEqual(progress.runs_used, len(finished))
                self.assertEqual(progress.finished_runs, finished)
                self.save_series_progress(identity, finished=progress.finished_runs)
                saved = the_list.load(MORTY.id, identity).schedule
                self.assertEqual(saved, progress)
                assert isinstance(saved, the_list.ScheduledSeries)
                self.assertEqual(tuple((run.number, run.run.start) for run in saved.runs()),
                                 expected)
                rows = self.command("list")[1].splitlines()
                row = next(line for line in rows if line.startswith(f"| {identity.removeprefix('L')} |"))
                self.assertIn(f"· next {expected[0][1].isoformat()}", row)

        settings = self.list_config.read_text()
        for label, offset, old_cursor, expected_finished in (
            ("6de0503120 split", 1, {}, frozenset({1})),
            ("9eb8ed1201 cursor", 0, {"runs_used": 2}, frozenset({1})),
        ):
            with self.subTest(label=label):
                _ = self.list_config.write_text(settings.replace("series_run_limit=730", "series_run_limit=4"))
                code, output, errors = self.add_scheduled(start, "--repeat", "FREQ=DAILY;COUNT=4")
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                store = record_store.NumberedRecords(MORTY.id, "list", "L", "list item", the_list.ListError)
                fields, body = store.read(identity)
                legacy = {"start": stamp, "do": "Check the figures.", "repeat": "FREQ=DAILY;COUNT=4",
                          "runs_before": offset, "exceptions": [{"number": 2, "removed": True}], **old_cursor}
                current = fields["schedule"]
                fields["schedule"] = json.dumps(legacy)
                body = body.replace('"schedule": ' + current, '"schedule": ' + fields["schedule"])
                store.write(identity, fields, body)
                saved = the_list.load(MORTY.id, identity)
                series = saved.schedule
                assert isinstance(series, the_list.ScheduledSeries)
                self.assertEqual((series.finished_runs, series.skipped_runs, series.runs_used),
                                 (expected_finished, frozenset({2}), 2))
                self.assertEqual(series.run_number_offset, offset)
                event = saved.log[-1].what
                assert isinstance(event, the_list.FieldChange)
                self.assertEqual(event.fields.get("schedule"), series)
                self.assertEqual(series.run_allowance, 4)
                _ = the_list.update(MORTY.id, identity, {"title": "Check the totals"}, source="nate")
                _ = self.list_config.write_text(settings.replace("series_run_limit=730", "series_run_limit=2"))
                migrated = the_list.load(MORTY.id, identity)
                self.assertEqual(migrated.schedule, series)
                self.assertEqual(migrated.log[-2], saved.log[-1])

        parked = self.add("A parked item")
        _ = the_list.park(MORTY.id, parked.id, START + timedelta(days=1), source="nate")
        self.assertEqual(self.command("update", "--id", parked.id, "--state", "Scheduled", "--start", start,
                                     "--do", "Check the total.", "--source", "nate"), (0, "Does not repeat\n", ""))
        saved = the_list.load(MORTY.id, parked.id)
        self.assertEqual(saved.parked_until, the_list.NoTimestamp.UNSET)
        self.assertEqual(saved.schedule, the_list.ScheduledRun(
            datetime(2026, 11, 30, 9, tzinfo=ZoneInfo("America/Los_Angeles")), "Check the total."))

    def test_scheduled_run_cycle(self) -> None:
        sender = self.root / "send.py"
        sent = self.root / "sent.jsonl"
        attempted = self.root / "attempted.jsonl"
        _ = sender.write_text(r"""from pathlib import Path
import json
import os
import subprocess
import sys
import time

args = sys.argv[1:]
log = Path(__file__).with_name("sent.jsonl")
attempted = log.with_name("attempted.jsonl")
key = args[args.index("--key") + 1]
to = args[args.index("--to") + 1]
with attempted.open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if ("Slow notice" in args[-1] or "Accepted notice" in args[-1]) and key.endswith(":owner_start"):
    lock = log.with_name("sender-child.lock")
    program = (
        "import fcntl, sys, time\n"
        "with open(sys.argv[1], 'w') as lock:\n"
        "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "    print('ready', flush=True)\n"
        "    time.sleep(10)\n"
    )
    command = [sys.executable, "-c", program, str(lock)]
    child = subprocess.Popen(command, stdout=subprocess.PIPE,
                             start_new_session=os.environ.get("SEND_REPEAT_NOTICE_JOB") != "1")
    assert child.stdout.readline() == b"ready\n"
    child.stdout.close()
    if "Slow notice" in args[-1]:
        child.wait()
if ("Orphan sender" in args[-1] or "Exited sender" in args[-1]) and key.endswith(":owner_start"):
    program = (
        "import os, signal, socket, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=8) as connection:\n"
        "    connection.sendall(b'R')\n"
        "    if connection.recv(1) == b'X':\n"
        "        print('release', flush=True)\n"
        "        os.close(1)\n"
        "        connection.recv(1)\n"
    )
    command = [sys.executable, "-c", program, os.environ["SCHEDFIRE_TEST_READY_PORT"]]
    if "Exited sender" in args[-1]:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert child.stdout.readline() == b"release\n"
        child.stdout.close()
    else:
        with subprocess.Popen(command, stdout=subprocess.DEVNULL) as child:
            child.wait()
if "Scan resume" in args[-1] and key.endswith("_start"):
    time.sleep(30)
rows = log.read_text().splitlines() if log.exists() else []
if any(key in json.loads(row) for row in rows):
    print("SKIPPED: key already accepted")
    sys.exit(0)
with log.open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if "Lose receipt" in args[-1] and key.endswith(":producer_start"):
    sys.exit(3)
if to == "user":
    print("KEPT: held during silence")
elif key.endswith(":owner_start") and "Accepted notice" not in args[-1]:
    print("QUEUED: owner is offline")
    sys.exit(1)
else:
    print("SENT: delivered")
""")
        cases = (
            ("confirmed", "", 730, 0, RUNNER),
            ("timeout", "", 730, 0, MORTY),
            ("count", "FREQ=DAILY;COUNT=2", 730, 0, MORTY),
            ("until", "FREQ=DAILY;UNTIL=20261117T180000Z", 730, 0, RUNNER),
            ("cap", "FREQ=DAILY", 2, 0, RUNNER),
            ("missed", "FREQ=DAILY;COUNT=4", 730, 2, RUNNER),
            ("Lose receipt", "", 730, 0, RUNNER),
            ("owner changed", "", 730, 0, RUNNER),
        )
        config = self.list_config.read_text().replace("confirm_wait_minutes=15", "confirm_wait_minutes=2")
        with (patch.object(session_send, "SEND_SCRIPT", sender),
              patch.object(remote, "local_machine", return_value="natedev"),
              patch.object(sys, "platform", "linux")):
            for case, rule, limit, missed_days, caller in cases:
                with self.subTest(case=case):
                    _ = self.list_config.write_text(config.replace("series_run_limit=730", f"series_run_limit={limit}"))
                    for path in (sent, attempted):
                        _ = path.write_text("")
                    scheduled = the_list.ScheduledRun(START, "Compare the account predictions.")
                    schedule = the_list.ScheduledSeries(scheduled, repeat_rule.parse(rule), limit) if rule else scheduled
                    item = self.add(case, changes={"state": the_list.ItemState.SCHEDULED,
                                                   "schedule": schedule, "producer": BOB.id})
                    if case == "cap":
                        _ = self.list_config.write_text(config.replace("series_run_limit=730", "series_run_limit=1"))
                    due = START + timedelta(days=missed_days)
                    tick = due + timedelta(minutes=1) if missed_days else due
                    if case in {"confirmed", "cap"}:
                        path = the_list.folder(MORTY.id) / f"{item.id}.md"
                        _ = path.write_text("".join(line for line in path.read_text().splitlines(keepends=True)
                                                    if not line.startswith("scheduled_check:")))
                    with (patch.object(the_list, "now", return_value=START - timedelta(seconds=1)),
                          patch.object(the_list, "load", side_effect=AssertionError("the future record was loaded"))):
                        the_list.fire_scheduled(UPKEEP_INTERVAL)
                    self.assertEqual(sent.read_text(), "")
                    extra_logs = missed_days + int(case == "Lose receipt")
                    with patch.object(the_list, "now", return_value=tick), redirect_stderr(io.StringIO()):
                        the_list.fire_scheduled(UPKEEP_INTERVAL)
                        working = the_list.load(MORTY.id, item.id)
                        self.assertIs(working.state, the_list.ItemState.WORKING)
                        self.assertEqual(len(working.log), len(item.log) + 1 + extra_logs)
                        self.assertIn("Instruction sent", str(working.log[len(item.log) + missed_days].what))
                        if missed_days:
                            expected = [f"Scheduled run due {(START + timedelta(days=day)).isoformat()}. Missed."
                                        for day in range(missed_days)]
                            self.assertEqual([entry.what for entry in working.log[len(item.log):-1]], expected)
                        starts = [cast(list[str], json.loads(row)) for row in sent.read_text().splitlines()]
                        self.assertEqual([args[args.index("--to") + 1] for args in starts],
                                         [f"staff:{RUNNER.id}", f"staff:{BOB.id}"])
                        for args in starts:
                            message = args[args.index("--text") + 1]
                            self.assertIn(f"Scheduled List item {item.number} starts now: {case}", message)
                            self.assertIn(scheduled.words, message)
                            self.assertIn(due.isoformat(), message)
                        self.assertEqual(len(working.run_deliveries), 1)
                        run_key = working.run_deliveries[0].key
                        if case == "Lose receipt":
                            the_list.fire_scheduled(UPKEEP_INTERVAL)
                            sends = [cast(list[str], json.loads(row)) for row in attempted.read_text().splitlines()]
                            self.assertEqual(sends, starts)
                            self.assertIn("Delivery uncertain for producer start", str(working.log[-1].what))
                            self.assertEqual(the_list.load(MORTY.id, item.id).log, working.log)
                        if case == "owner changed":
                            _ = the_list.update(MORTY.id, item.id, {"owner": OTHER.id}, source="nate")
                            extra_logs += 1
                            with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": OTHER.current_session}):
                                code, _, error = self.command("confirm", "--id", item.id, "--run", run_key)
                            self.assertEqual(code, 1)
                            self.assertIn("only the owner or morty", error)
                        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": BOB.current_session}):
                            code, _, error = self.command("confirm", "--id", item.id)
                        self.assertEqual(code, 1)
                        self.assertIn("only the owner or morty", error)
                        if case != "timeout":
                            with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": caller.current_session}):
                                result = self.command("confirm", "--id", item.id, "--run", run_key)
                            self.assertEqual(result, (0, f"confirmed {item.id}\n", ""))
                    with patch.object(the_list, "now", return_value=tick + timedelta(minutes=2, seconds=-1)):
                        the_list.fire_scheduled(UPKEEP_INTERVAL)
                    self.assertEqual(len(sent.read_text().splitlines()), 2)
                    with patch.object(the_list, "now", return_value=tick + timedelta(minutes=2)):
                        if case == "timeout":
                            code, _, error = self.command("confirm", "--id", item.id)
                            self.assertEqual(code, 1)
                            self.assertIn("the confirmation time has passed", error)
                        the_list.fire_scheduled(UPKEEP_INTERVAL)
                        the_list.fire_scheduled(UPKEEP_INTERVAL)
                    changed = the_list.load(MORTY.id, item.id)
                    self.assertEqual(len(changed.log), len(item.log) + 1 + extra_logs)
                    result_entry = changed.log[len(item.log) + missed_days]
                    rows = [cast(list[str], json.loads(row)) for row in sent.read_text().splitlines()]
                    if case == "timeout":
                        self.assertIn("No confirmation", str(result_entry.what))
                        self.assertEqual([args[args.index("--to") + 1] for args in rows],
                                         [f"staff:{RUNNER.id}", f"staff:{BOB.id}", "user", f"staff:{BOB.id}"])
                        self.assertEqual(rows[2][rows[2].index("--text-id") + 1], "nate.adhoc")
                        self.assertIn("has no confirmation", rows[3][-1])
                    else:
                        self.assertIn("Confirmed", str(result_entry.what))
                        self.assertEqual(rows, starts)
                    if rule:
                        self.assertIs(changed.state, the_list.ItemState.SCHEDULED)
                        next_time = due + timedelta(days=1)
                        self.assertIn(f"next {next_time.isoformat()}", the_list.table(MORTY.id))
                        with patch.object(the_list, "now", return_value=next_time):
                            the_list.fire_scheduled(UPKEEP_INTERVAL)
                            code, _, error = self.command("confirm", "--id", item.id, "--run", run_key)
                            self.assertEqual(code, 1)
                            self.assertIn("no matching scheduled run", error)
                            self.assertEqual(self.command("confirm", "--id", item.id),
                                             (0, f"confirmed {item.id}\n", ""))
                        changed = the_list.load(MORTY.id, item.id)
                        self.assertEqual(len(changed.log), len(item.log) + 2 + extra_logs)
                        self.assertIn(next_time.isoformat(), str(changed.log[-1].what))
                        self.assertEqual(len(sent.read_text().splitlines()), 4)
                        with patch.object(the_list, "now", return_value=next_time + timedelta(days=1)):
                            the_list.fire_scheduled(UPKEEP_INTERVAL)
                        self.assertEqual(len(sent.read_text().splitlines()), 4)
                    self.assertIs(changed.state, the_list.ItemState.WORKING)
                    self.assertIs(changed.schedule, the_list.NoSchedule.UNSET)

            for case in ("interrupted owner", "slow notice", "accepted notice", "scan resume", "overlapping runs",
                         "same check", "state changed", "all confirmations", "pending instruction",
                         "reopened", "capture allocation fails", "Mac", "orphan sender", "exited sender"):
                with self.subTest(case=case):
                    for path in (sent, attempted):
                        _ = path.write_text("")
                    _ = self.list_config.write_text(config)
                    match case:
                        case "interrupted owner":
                            self.check_scheduled_instruction_interrupt(attempted)
                        case "slow notice" | "accepted notice":
                            self.check_slow_scheduled_notice(sent, attempted, accepted=case == "accepted notice")
                        case "scan resume":
                            self.check_scheduled_scan_resumes(sent)
                        case "overlapping runs" | "same check" | "state changed":
                            self.check_overlapping_scheduled_runs(sent, attempted, case)
                        case "all confirmations" | "pending instruction":
                            self.check_manual_run_settlement(attempted, pending_instruction=case == "pending instruction")
                        case "reopened":
                            self.check_reopened_run_settlement(attempted)
                        case "capture allocation fails":
                            self.check_scheduled_capture_failure(sent, attempted)
                        case "Mac":
                            self.check_mac_scheduled_run(attempted)
                        case "orphan sender" | "exited sender":
                            self.check_scheduled_sender_stops_after_upkeep_dies(sender, sender_exits=case == "exited sender")

    def check_scheduled_instruction_interrupt(self, attempted: Path) -> None:
        schedule = the_list.ScheduledRun(START, "Check the figures.")
        item = self.add("Interrupted", changes={"state": the_list.ItemState.SCHEDULED, "schedule": schedule})
        with (patch.object(the_list, "_send_scheduled_notice", side_effect=KeyboardInterrupt),
              self.assertRaises(KeyboardInterrupt)):
            the_list.fire_scheduled(UPKEEP_INTERVAL)
        interrupted = the_list.load(MORTY.id, item.id)
        active = interrupted.run_deliveries[0]
        self.assertEqual(active.notices[0], the_list.ScheduledNotice(the_list.RunNotice.OWNER_START,
                                                                    the_list.NoticeState.SENDING))
        code, _, error = self.command("confirm", "--id", item.id, "--run", active.key)
        self.assertEqual(code, 1)
        self.assertIn("waiting for its owner instruction", error)
        recovered_at = START + timedelta(minutes=3)
        with patch.object(the_list, "now", return_value=recovered_at):
            the_list.fire_scheduled(UPKEEP_INTERVAL)
            the_list.fire_scheduled(UPKEEP_INTERVAL)
        recovered = the_list.load(MORTY.id, item.id)
        self.assertEqual(recovered.run_deliveries[0].confirmation,
                         the_list.AwaitingConfirmation(recovered_at + timedelta(minutes=2)))
        self.assertEqual([entry.what for entry in recovered.log[len(item.log):]],
                         [f"Scheduled run due {START.isoformat()}. Instruction uncertain. Waiting for confirmation.",
                          f"Scheduled run due {START.isoformat()}. Delivery uncertain for owner start."])
        rows = [cast(list[str], json.loads(row)) for row in attempted.read_text().splitlines()]
        self.assertEqual([args[args.index("--key") + 1].split(":")[-1] for args in rows], ["producer_start"])
        with patch.object(the_list, "now", return_value=recovered_at + timedelta(minutes=2)):
            the_list.fire_scheduled(UPKEEP_INTERVAL)
            the_list.fire_scheduled(UPKEEP_INTERVAL)
        rows = [cast(list[str], json.loads(row)) for row in attempted.read_text().splitlines()]
        self.assertEqual([args[args.index("--key") + 1].split(":")[-1] for args in rows],
                         ["producer_start", "user_timeout", "producer_timeout"])

    def check_slow_scheduled_notice(self, sent: Path, attempted: Path, *, accepted: bool) -> None:
        schedule = the_list.ScheduledRun(START, "Check the figures.")
        changes: the_list.ItemChanges = {"state": the_list.ItemState.SCHEDULED, "schedule": schedule}
        slow = self.add("Accepted notice" if accepted else "Slow notice", changes=changes)
        later = self.add("Later item", changes=changes)
        with redirect_stderr(io.StringIO()) as errors:
            the_list.fire_scheduled(UPKEEP_INTERVAL)
        self.assertIn("timed out", errors.getvalue())
        with (self.root / "sender-child.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows = [cast(list[str], json.loads(row)) for row in sent.read_text().splitlines()]
        self.assertEqual(sum(later.title in args[-1] for args in rows), 2)
        done = the_list.load(MORTY.id, slow.id)
        if accepted:
            self.assertIn("sender group timed out after the sender exited", errors.getvalue())
            run = done.run_deliveries[0]
            self.assertEqual(run.notices[0], the_list.ScheduledNotice(the_list.RunNotice.OWNER_START,
                                                                     the_list.NoticeState.ACCEPTED))
            self.assertEqual(done.log[run.log_index].what,
                             f"Scheduled run due {START.isoformat()}. Instruction sent. Waiting for confirmation.")
            self.assertNotIn("Delivery uncertain", "\n".join(str(entry.what) for entry in done.log))
        else:
            self.assertIn("Delivery uncertain for owner start", str(done.log[-1].what))
        for item in (slow, later):
            self.assertEqual(self.command("confirm", "--id", item.id), (0, f"confirmed {item.id}\n", ""))
        the_list.fire_scheduled(UPKEEP_INTERVAL)
        rows = [cast(list[str], json.loads(row)) for row in attempted.read_text().splitlines()]
        self.assertEqual(sum(slow.title in args[-1] and args[args.index("--key") + 1].endswith(":owner_start")
                             for args in rows), 1)

    def check_scheduled_sender_stops_after_upkeep_dies(self, sender: Path, *, sender_exits: bool) -> None:
        schedule = the_list.ScheduledRun(START, "Check the figures.")
        title = "Exited sender" if sender_exits else "Orphan sender"
        _ = self.add(title, changes={"state": the_list.ItemState.SCHEDULED, "schedule": schedule})
        program = """import sys
from datetime import datetime, timedelta
from pathlib import Path
from scripts.agents import remote
from scripts.message import session_send
from scripts.production import the_list

session_send.SEND_SCRIPT = Path(sys.argv[1])
the_list.now = lambda: datetime.fromisoformat(sys.argv[2])
remote.local_machine = lambda: "natedev"
the_list.fire_scheduled(timedelta(minutes=2))
"""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)
            address = cast(tuple[str, int], listener.getsockname())
            env = {**os.environ, "SCHEDFIRE_TEST_READY_PORT": str(address[1])}
            with subprocess.Popen([sys.executable, "-c", program, str(sender), START.isoformat()],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as upkeep:
                try:
                    connection = listener.accept()[0]
                    with connection:
                        connection.settimeout(5)
                        self.assertEqual(connection.recv(1), b"R")
                        upkeep.kill()
                        if sender_exits:
                            _ = upkeep.wait(timeout=5)
                            connection.sendall(b"X")
                        self.assertEqual(connection.recv(1), b"")
                finally:
                    upkeep.kill()

    def check_scheduled_scan_resumes(self, sent: Path) -> None:
        schedule = the_list.ScheduledRun(START, "Read the figures.")
        changes: the_list.ItemChanges = {"state": the_list.ItemState.SCHEDULED, "schedule": schedule}
        slow = self.add("Scan resume", changes=changes)
        later = self.add("Later scan item", changes=changes)
        with patch.object(the_list, "_SCHEDULED_CHECK_SECONDS", 1.5), redirect_stderr(io.StringIO()):
            the_list.fire_scheduled(UPKEEP_INTERVAL)
            first_check = the_list.load(MORTY.id, slow.id)
            self.assertIn("Delivery uncertain for owner start", str(first_check.log[-1].what))
            self.assertEqual(the_list.load(MORTY.id, later.id), later)
            the_list.fire_scheduled(UPKEEP_INTERVAL)
        rows = [cast(list[str], json.loads(row)) for row in sent.read_text().splitlines()]
        self.assertEqual(sum(later.title in args[-1] for args in rows), 2)
        for item in (slow, later):
            self.assertEqual(self.command("confirm", "--id", item.id), (0, f"confirmed {item.id}\n", ""))

    def check_overlapping_scheduled_runs(self, sent: Path, attempted: Path, case: str) -> None:
        first = the_list.ScheduledRun(START.replace(hour=17), "First words.")
        schedule = the_list.ScheduledSeries(first, repeat_rule.parse("FREQ=DAILY;COUNT=2"), 730)
        item = self.add("Lose receipt in moved run", changes={"state": the_list.ItemState.SCHEDULED, "schedule": schedule})
        second = first.start + timedelta(days=1)
        moved = second + timedelta(minutes=1)
        self.assertEqual(self.command("update", "--id", item.id, "--scope", "this", "--start", moved.isoformat(),
                                      "--source", "nate"), (0, "Repeats: Daily for 2 runs\n", ""))
        if case != "same check":
            with patch.object(the_list, "now", return_value=second), redirect_stderr(io.StringIO()):
                the_list.fire_scheduled(UPKEEP_INTERVAL)
                after_second = the_list.load(MORTY.id, item.id)
                active = after_second.run_deliveries[0]
                self.assertEqual(active.run.start, second)
                self.assertIn("Delivery uncertain for producer start", str(after_second.log[-1].what))
                remaining = after_second.schedule
                assert isinstance(remaining, the_list.ScheduledSeries)
                self.assertEqual(remaining.finished_runs, frozenset({2}))
                self.assertEqual(remaining.runs_used, 1)
                self.assertEqual([(run.number, run.run.start) for run in remaining.runs()], [(1, moved)])
                if case == "state changed":
                    self.assertEqual(self.command("update", "--id", item.id, "--state", "Delivered",
                                                  "--source", "nate"), (0, "", ""))
                    delivered = the_list.load(MORTY.id, item.id)
                    self.assertIs(delivered.state, the_list.ItemState.DELIVERED)
                    self.assertEqual(delivered.schedule, remaining)
                    self.assertEqual(delivered.run_deliveries, ())
                    self.assertEqual(delivered.log[active.log_index].what,
                                     f"Scheduled run due {second.isoformat()}. Settled by a manual state change.")
        tick = moved + timedelta(seconds=30) if case == "same check" else moved
        with patch.object(the_list, "now", return_value=tick), redirect_stderr(io.StringIO()):
            the_list.fire_scheduled(UPKEEP_INTERVAL)
            if case == "same check":
                first_check = the_list.load(MORTY.id, item.id)
                self.assertEqual([run.run.start for run in first_check.run_deliveries], [second, moved])
                self.assertNotIn("Missed.", "\n".join(str(entry.what) for entry in first_check.log))
            the_list.fire_scheduled(UPKEEP_INTERVAL)
            started = the_list.load(MORTY.id, item.id)
            expected = [moved] if case == "state changed" else [second, moved]
            self.assertEqual([run.run.start for run in started.run_deliveries], expected)
            if case == "state changed":
                sends = attempted.read_text()
                with patch.object(the_list, "now", return_value=second + timedelta(minutes=2)):
                    the_list.fire_scheduled(UPKEEP_INTERVAL)
                self.assertEqual(attempted.read_text(), sends)
            for run in started.run_deliveries:
                self.assertEqual(self.command("confirm", "--id", item.id, "--run", run.key),
                                 (0, f"confirmed {item.id}\n", ""))
        rows = [cast(list[str], json.loads(row)) for row in sent.read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        for args, due in zip(rows, (second, second, moved, moved), strict=True):
            self.assertIn(due.isoformat(), args[-1])
        self.assertIs(the_list.load(MORTY.id, item.id).schedule, the_list.NoSchedule.UNSET)

    def check_manual_run_settlement(self, attempted: Path, *, pending_instruction: bool) -> None:
        wait = timedelta(days=3)
        settings = self.list_config.read_text().replace("confirm_wait_minutes=2", "confirm_wait_minutes=4320")
        _ = self.list_config.write_text(settings)
        first = the_list.ScheduledRun(START, "Read the figures.")
        days = "MO,TU,WE" if pending_instruction else "MO,TU"
        rule = repeat_rule.parse(f"FREQ=WEEKLY;BYDAY={days};COUNT={4 if pending_instruction else 3}")
        item = self.add("Manual settlement", changes={"state": the_list.ItemState.SCHEDULED,
                                                     "schedule": the_list.ScheduledSeries(first, rule, 730)})
        due = (START, START + timedelta(days=1), START + timedelta(days=2))
        for tick in due[:2]:
            with patch.object(the_list, "now", return_value=tick):
                the_list.fire_scheduled(UPKEEP_INTERVAL)
        if pending_instruction:
            with (patch.object(the_list, "now", return_value=due[2]),
                  patch.object(the_list, "_deliver_scheduled_notice", side_effect=KeyboardInterrupt),
                  self.assertRaises(KeyboardInterrupt)):
                the_list.fire_scheduled(UPKEEP_INTERVAL)

        active = the_list.load(MORTY.id, item.id)
        first_run, second_run, *pending = active.run_deliveries
        for run, tick in zip((first_run, second_run), due[:2], strict=True):
            self.assertEqual(run.confirmation, the_list.AwaitingConfirmation(tick + wait))
        if pending_instruction:
            self.assertEqual([(run.run.start, run.confirmation) for run in pending],
                             [(due[2], the_list.AwaitingInstruction.OWNER)])

        with patch.object(the_list, "now", return_value=due[2]):
            self.assertEqual(self.command("update", "--id", item.id, "--state", "Delivered", "--source", "nate"),
                             (0, "", ""))
            delivered = the_list.load(MORTY.id, item.id)
            self.assertIs(delivered.state, the_list.ItemState.DELIVERED)
            self.assertEqual(delivered.schedule, active.schedule)
            self.assertEqual(delivered.run_deliveries, tuple(pending))
            for run in (first_run, second_run):
                self.assertEqual(delivered.log[run.log_index].what,
                                 f"Scheduled run due {run.run.start.isoformat()}. Settled by a manual state change.")
            if pending_instruction:
                the_list.fire_scheduled(UPKEEP_INTERVAL)
                resumed = the_list.load(MORTY.id, item.id)
                self.assertEqual([(run.key, run.confirmation) for run in resumed.run_deliveries],
                                 [(pending[0].key, the_list.AwaitingConfirmation(due[2] + wait))])

        sends = attempted.read_text()
        for tick in due[:2]:
            with patch.object(the_list, "now", return_value=tick + wait):
                the_list.fire_scheduled(UPKEEP_INTERVAL)
        self.assertEqual(attempted.read_text(), sends)

    def check_reopened_run_settlement(self, attempted: Path) -> None:
        schedule = the_list.ScheduledRun(START, "Read the figures.")
        item = self.add("Reopened run", changes={"state": the_list.ItemState.SCHEDULED, "schedule": schedule})
        with (patch.object(the_list, "_deliver_scheduled_notice", side_effect=KeyboardInterrupt),
              self.assertRaises(KeyboardInterrupt)):
            the_list.fire_scheduled(UPKEEP_INTERVAL)
        self.assertEqual(self.command("close", "--id", item.id, "--words", "Close this item.", "--source", "nate"),
                         (0, "", ""))
        closed = the_list.load(MORTY.id, item.id)
        self.assertIs(closed.state, the_list.ItemState.CLOSED)
        self.assertIs(closed.run_deliveries[0].confirmation, the_list.AwaitingInstruction.OWNER)

        the_list.fire_scheduled(UPKEEP_INTERVAL)
        instructed = the_list.load(MORTY.id, item.id)
        run = instructed.run_deliveries[0]
        deadline = START + timedelta(minutes=2)
        self.assertEqual(run.confirmation, the_list.AwaitingConfirmation(deadline))
        self.assertEqual(self.command("reopen", "--id", item.id, "--source", "nate"), (0, "", ""))
        reopened = the_list.load(MORTY.id, item.id)
        self.assertIs(reopened.state, the_list.ItemState.NEW)
        self.assertEqual(reopened.run_deliveries, ())
        self.assertEqual(reopened.log[run.log_index].what,
                         f"Scheduled run due {START.isoformat()}. Settled by a manual state change.")
        self.assertEqual(reopened.log[-1].what, the_list.Reopened())

        sends = attempted.read_text()
        with patch.object(the_list, "now", return_value=deadline):
            the_list.fire_scheduled(UPKEEP_INTERVAL)
        self.assertEqual(attempted.read_text(), sends)
        self.assertEqual(the_list.load(MORTY.id, item.id), reopened)

    def check_scheduled_capture_failure(self, sent: Path, attempted: Path) -> None:
        schedule = the_list.ScheduledRun(START, "Read the figures.")
        item = self.add("Capture allocation", changes={"state": the_list.ItemState.SCHEDULED, "schedule": schedule})
        with (tempfile.TemporaryFile() as report, redirect_stderr(io.StringIO()) as errors,
              patch.object(the_list, "TemporaryFile", side_effect=(report, OSError(errno.ENOSPC, "No space left")))):
            the_list.fire_scheduled(UPKEEP_INTERVAL)
            self.assertTrue(report.closed)
        self.assertIn("No space left", errors.getvalue())
        self.assertEqual(attempted.read_text(), "")

        pending = the_list.load(MORTY.id, item.id)
        run = pending.run_deliveries[0]
        self.assertIs(run.confirmation, the_list.AwaitingInstruction.OWNER)
        self.assertEqual(run.notices, (the_list.ScheduledNotice(the_list.RunNotice.OWNER_START),
                                       the_list.ScheduledNotice(the_list.RunNotice.PRODUCER_START)))
        self.assertEqual([entry.what for entry in pending.log[len(item.log):]],
                         [f"Scheduled run due {START.isoformat()}. Sending the instruction."])

        the_list.fire_scheduled(UPKEEP_INTERVAL)
        the_list.fire_scheduled(UPKEEP_INTERVAL)
        self.assertEqual(attempted.read_text(), sent.read_text())
        rows = [cast(list[str], json.loads(row)) for row in sent.read_text().splitlines()]
        self.assertEqual([args[args.index("--key") + 1] for args in rows],
                         [f"list-run:{item.id}:{run.key}:owner_start", f"list-run:{item.id}:{run.key}:producer_start"])
        delivered = the_list.load(MORTY.id, item.id).run_deliveries[0]
        self.assertEqual(delivered.confirmation, the_list.AwaitingConfirmation(START + timedelta(minutes=2)))
        self.assertEqual([notice.state for notice in delivered.notices], [the_list.NoticeState.ACCEPTED] * 2)

    def check_mac_scheduled_run(self, attempted: Path) -> None:
        schedule = the_list.ScheduledRun(START, "Check the figures.")
        item = self.add("Mac check", changes={"state": the_list.ItemState.SCHEDULED, "schedule": schedule})
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        before = path.read_bytes()
        settings = self.list_config.with_name("live_settings.conf")
        _ = settings.write_text("# whole: 1..\nnatedev_forward_seconds=45\n")
        run_key = "fixture run"
        receipt = subprocess.CompletedProcess(["ssh"], 0, f"confirmed {item.id}\n", "")
        with (patch.object(remote, "local_machine", return_value="mac"),
              patch.object(sys, "platform", "darwin"),
              patch.object(main_publish, "ssh", return_value=receipt) as ssh):
            the_list.fire_scheduled(UPKEEP_INTERVAL)
            self.assertEqual(attempted.read_text(), "")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.command("confirm", "--id", item.id, "--run", run_key),
                             (0, f"confirmed {item.id}\n", ""))
            args = cast(tuple[str, ...], ssh.call_args.args)
            self.assertEqual(shlex.split(args[0]), ["CODEX_THREAD_ID=", f"CLAUDE_CODE_SESSION_ID={MORTY.current_session}",
                             "$HOME/.claude/scripts/lib/py", "$HOME/.claude/scripts/production/the_list.py",
                             "confirm", "--id", item.id, "--run", run_key])
            ssh.assert_called_once_with(args[0], ssh_program=os.environ.get("ZONE_SSH") or "ssh", host="natedev",
                                        time_limit=main_publish.CommandTimeLimit(45))

    def test_repeat_series_limit_uses_the_live_setting(self) -> None:
        start = "2026-11-30T17:00:00Z"
        original_settings = self.list_config.read_text()
        for limit in (730, 2):
            with self.subTest(limit=limit):
                _ = self.list_config.write_text(original_settings.replace("series_run_limit=730", f"series_run_limit={limit}"))
                code, output, errors = self.add_scheduled(start, "--repeat", "FREQ=DAILY")
                self.assertEqual((code, errors), (0, ""))
                saved = the_list.load(MORTY.id, output.splitlines()[-1])
                series = saved.schedule
                assert isinstance(series, the_list.ScheduledSeries)
                last = series.first.start + timedelta(days=limit - 1)
                self.assertEqual(series.rule.next_after(series.first.start, last - timedelta(days=1),
                                                       the_list.series_run_limit()), last)
                self.assertIs(series.rule.next_after(series.first.start, last, the_list.series_run_limit()),
                              repeat_rule.NoNextRun.REMAINING)
                refused, message, errors = self.add_scheduled(start, "--repeat", f"FREQ=DAILY;COUNT={limit + 1}")
                self.assertEqual((refused, message), (1, ""))
                self.assertIn(f"COUNT exceeds the series limit of {limit} runs", errors)

                for numbers, unissued in (((1, 999), 999), ((1,), 0)):
                    with self.subTest(finish=numbers):
                        if unissued:
                            with self.assertRaisesRegex(the_list.ListError, f"the series did not issue run {unissued}"):
                                _ = series.finish_runs(numbers)
                        else:
                            completed = series.finish_runs(numbers)
                            self.assertEqual((completed.finished_runs, completed.runs_used), (frozenset({1}), 1))
                            self.assertIs(completed.finish_runs(numbers), completed)

                if limit == 2:
                    with self.subTest(finish="the live limit changes after selection"):
                        selected = tuple(series.runs())[-1]
                        settings = self.list_config.read_text()
                        _ = self.list_config.write_text(settings.replace("series_run_limit=2", "series_run_limit=1"))
                        try:
                            self.assertEqual(the_list.series_run_limit(), 1)
                            completed = series.finish_runs((selected.number,))
                            self.assertEqual((completed.finished_runs, completed.runs_used), (frozenset({2}), 1))
                        finally:
                            _ = self.list_config.write_text(settings)

                for progress in ("exhausted", "skipped"):
                    with self.subTest(progress=progress):
                        if progress == "skipped":
                            self.save_series_progress(saved.id, finished=range(1, limit - 1))
                            moment = last - timedelta(days=1)
                            with patch.object(the_list, "now", return_value=moment):
                                self.assertEqual(self.command("close", "--id", saved.id, "--scope", "this",
                                                              "--source", "nate", "--words", "Skip once."), (0, "", ""))
                        else:
                            self.save_series_progress(saved.id, finished=range(1, limit + 1))
                            moment = last + timedelta(microseconds=1)
                        path = the_list.folder(MORTY.id) / f"{saved.id}.md"
                        before = path.read_bytes()
                        new_start = last + timedelta(days=1)
                        with patch.object(the_list, "now", return_value=moment):
                            code, output, errors = self.command("update", "--id", saved.id, "--scope", "all",
                                                                "--start", new_start.isoformat(), "--source", "nate")
                        if progress == "exhausted":
                            self.assertEqual((code, output), (1, ""))
                            self.assertIn("no runs left under the limit", errors)
                            self.assertEqual(path.read_bytes(), before)
                        else:
                            self.assertEqual((code, errors), (0, ""))
                            updated = the_list.load(MORTY.id, saved.id).schedule
                            assert isinstance(updated, the_list.ScheduledSeries)
                            self.assertEqual([(run.number, run.run.start) for run in updated.runs()],
                                             [(limit, new_start + timedelta(days=limit - 1))])

        for end, next_start, expected in (
            ("", "2026-12-01T09:00:00+00:00", (2, 3, 4)),
            (";UNTIL=20261201T090000Z", "2026-12-04T10:00:00+00:00", (2, 6, 7)),
        ):
            with self.subTest(end=end):
                initial_limit = 2 if end else 4
                _ = self.list_config.write_text(original_settings.replace("series_run_limit=730", f"series_run_limit={initial_limit}"))
                code, output, errors = self.add_scheduled("2026-11-30T09:00+00:00", "--repeat", f"FREQ=DAILY{end}")
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                self.save_series_progress(identity, finished=frozenset({1}))
                if end:
                    self.assertEqual(self.command("update", "--id", identity, "--scope", "this", "--source", "nate",
                                                  "--start", next_start), (0, "Repeats: Daily until 2026-12-01 09:00:00 UTC\n", ""))
                    _ = self.list_config.write_text(original_settings.replace("series_run_limit=730", "series_run_limit=4"))
                    self.assertEqual(self.command("update", "--id", identity, "--scope", "following", "--source", "nate",
                                                  "--repeat", "FREQ=DAILY"), (0, "Repeats: Daily\n", ""))
                for live_limit in (4, 2, 1, 9):
                    with self.subTest(live_limit=live_limit):
                        _ = self.list_config.write_text(original_settings.replace("series_run_limit=730",
                                                                                f"series_run_limit={live_limit}"))
                        reloaded = the_list.load(MORTY.id, identity).schedule
                        assert isinstance(reloaded, the_list.ScheduledSeries)
                        self.assertEqual(tuple(run.number for run in reloaded.runs()), expected)
                        self.assertEqual(reloaded.finish_runs(expected).finished_runs, frozenset((1, *expected)))
                        code, output, errors = self.command("list")
                        self.assertEqual((code, errors), (0, ""))
                        row = next(line for line in output.splitlines() if line.startswith(f"| {identity.removeprefix('L')} |"))
                        self.assertIn(f"· next {next_start}", row)
                        if live_limit == 1 and end:
                            code, _, errors = self.command("update", "--id", identity, "--scope", "following",
                                                           "--source", "nate", "--repeat", "FREQ=DAILY")
                            self.assertEqual((code, errors), (0, ""))
                            self.assertEqual(the_list.load(MORTY.id, identity).schedule, reloaded)
                            code, output, errors = self.command("update", "--id", identity, "--scope", "following",
                                                                "--source", "nate", "--repeat", "FREQ=DAILY;COUNT=1")
                            self.assertEqual((code, output), (1, ""))
                            self.assertIn("no runs left under the limit of 1", errors)
                            self.assertEqual(the_list.load(MORTY.id, identity).schedule, reloaded)

        moved_start = "2026-12-01T18:00:00+00:00"
        moved_words = "Read the figures at the new time."
        for rule, words, start_flags, expected_exceptions in (
            ("FREQ=DAILY", "Read the figures.\nSend the total.", (), ()),
            ("FREQ=DAILY;UNTIL=20261203T170000Z", "Read the December figures.", (), ()),
            ("FREQ=DAILY;COUNT=4", "Send the December total.", (), ()),
            ("FREQ=DAILY", moved_words, ("--start", moved_start),
             (the_list.RunException(2, the_list.ScheduledRun(datetime.fromisoformat(moved_start), moved_words)),)),
        ):
            with self.subTest(words_only=rule, start_flags=start_flags):
                _ = self.list_config.write_text(original_settings.replace("series_run_limit=730", "series_run_limit=4"))
                code, output, errors = self.add_scheduled(start, "--repeat", rule)
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                self.save_series_progress(identity, finished=frozenset({1}))
                if start_flags:
                    code, _, errors = self.command("update", "--id", identity, "--scope", "this", "--source", "nate",
                                                   *start_flags)
                    self.assertEqual((code, errors), (0, ""))
                before = the_list.load(MORTY.id, identity).schedule
                assert isinstance(before, the_list.ScheduledSeries)
                _ = self.list_config.write_text(original_settings.replace("series_run_limit=730", "series_run_limit=2"))
                code, _, errors = self.command("update", "--id", identity, "--scope", "following", "--source", "nate",
                                               "--do", words, *start_flags)
                self.assertEqual((code, errors), (0, ""))
                after = the_list.load(MORTY.id, identity).schedule
                assert isinstance(after, the_list.ScheduledSeries)
                self.assertEqual(after, replace(before, first=replace(before.first, words=words),
                                                exceptions=expected_exceptions))
                self.assertEqual(tuple((run.number, run.run.start) for run in after.runs()),
                                 tuple((run.number, run.run.start) for run in before.runs()))
                self.assertEqual(tuple(run.run.words for run in after.runs()), (words, words, words))

        with self.subTest(allowance="above sys.maxsize"):
            allowance = sys.maxsize + 1
            _ = self.list_config.write_text(original_settings.replace("series_run_limit=730", f"series_run_limit={allowance}"))
            code, output, errors = self.add_scheduled(start, "--repeat", "FREQ=DAILY")
            self.assertEqual((code, errors), (0, ""))
            identity = output.splitlines()[-1]
            _ = self.list_config.write_text(original_settings.replace("series_run_limit=730", "series_run_limit=4"))
            code, output, errors = self.command("list")
            self.assertEqual((code, errors), (0, ""))
            row = next(line for line in output.splitlines() if line.startswith(f"| {identity.removeprefix('L')} |"))
            self.assertIn("· next 2026-11-30T17:00:00+00:00", row)
            after = the_list.load(MORTY.id, identity).schedule
            assert isinstance(after, the_list.ScheduledSeries)
            self.assertEqual(after.run_allowance, allowance)

    def test_repeated_item_updates_use_the_selected_scope(self) -> None:
        start = datetime(2026, 11, 30, 17, tzinfo=UTC)
        second = start + timedelta(days=1)
        moved = second + timedelta(hours=1)
        for scope, now_at, first, run_number_offset, next_words in (
            ("this", second, start, 0, "Read the figures.\nSend the total."),
            ("following", second, moved, 1, "Read new figures."),
            ("all", START, moved, 0, "Read new figures."),
        ):
            with self.subTest(scope=scope):
                code, output, errors = self.add_scheduled(start.isoformat(), "--repeat", "FREQ=DAILY;COUNT=4")
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                if now_at == second:
                    self.save_series_progress(identity, finished=frozenset({1}))
                before = the_list.load(MORTY.id, identity)
                with patch.object(the_list, "now", return_value=now_at):
                    code, output, errors = self.command("update", "--id", identity, "--source", "nate",
                        "--scope", scope, "--start", moved.isoformat(), "--do", "Read new figures.")
                self.assertEqual((code, errors), (0, ""))
                self.assertIn("Repeats: Daily", output)
                item = the_list.load(MORTY.id, identity)
                series = item.schedule
                assert isinstance(series, the_list.ScheduledSeries)
                self.assertEqual((series.first.start, series.run_number_offset), (first, run_number_offset))
                next_run = series.next_run()
                assert isinstance(next_run, the_list.SeriesRun)
                self.assertEqual(next_run.run, the_list.ScheduledRun(moved, "Read new figures."))
                after = series.finish_runs((next_run.number,)).next_run()
                assert isinstance(after, the_list.SeriesRun)
                self.assertEqual(after.run.words, next_words)
                self.assertEqual(len(tuple(series.rule.runs(series.first.start, the_list.series_run_limit()))),
                                 3 if scope == "following" else 4)
                self.assertEqual(item.log[:-1], before.log)
                self.assertEqual(item.log[-1].what, the_list.FieldChange("Updated", {"schedule": series}))

        original_words = "Read the figures.\nSend the total."
        moved_first = start + timedelta(hours=1)
        reanchored_start = moved + timedelta(hours=1)
        for label, now_at, finished, prepare, scope, change, number, expected in (
            ("following keeps the pending move", second, frozenset({1}), ("update", "--start", moved.isoformat()),
             "following", ("--repeat", "FREQ=DAILY;INTERVAL=2"), 2, the_list.ScheduledRun(moved, original_words)),
            ("all keeps a skip after a time edit", second, frozenset({1}), ("close", "--words", "Skip this check."),
             "all", ("--start", moved_first.isoformat()), 3, the_list.ScheduledRun(moved + timedelta(days=1), original_words)),
            ("all keeps the pending run when repeat ends", second, frozenset({1}), (), "all",
             ("--repeat", ""), 2, the_list.ScheduledRun(second, original_words)),
            ("this edits run 2 before a delayed run 1", second, frozenset[int](), ("update", "--start", moved.isoformat()),
             "this", ("--do", "Read new figures."), 2, the_list.ScheduledRun(second, "Read new figures.")),
            ("repeat removal keeps run 2 before a delayed run 1", second, frozenset[int](),
             ("update", "--start", moved.isoformat()), "all", ("--repeat", ""), 2,
             the_list.ScheduledRun(second, original_words)),
            ("following uses the moved Monday for a new weekday rule", start, frozenset({1}),
             ("update", "--start", moved_first.isoformat()), "following", ("--repeat", "FREQ=WEEKLY;BYDAY=MO"), 2,
             the_list.ScheduledRun(moved_first, original_words)),
            ("following changes the start of a delayed run after run 2 finishes", second, frozenset({2}),
             ("update", "--start", moved.isoformat()), "following", ("--start", reanchored_start.isoformat()), 3,
             the_list.ScheduledRun(reanchored_start, original_words)),
        ):
            with self.subTest(label=label):
                code, output, errors = self.add_scheduled(start.isoformat(), "--repeat", "FREQ=DAILY;COUNT=4")
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                self.save_series_progress(identity, finished=finished)
                with patch.object(the_list, "now", return_value=now_at):
                    if prepare:
                        verb, *flags = prepare
                        code, _, errors = self.command(verb, "--id", identity, "--source", "nate",
                                                       "--scope", "this", *flags)
                        self.assertEqual((code, errors), (0, ""))
                    log = the_list.load(MORTY.id, identity).log
                    code, _, errors = self.command("update", "--id", identity, "--source", "nate",
                                                   "--scope", scope, *change)
                self.assertEqual((code, errors), (0, ""))
                item = the_list.load(MORTY.id, identity)
                self.assertEqual(item.log[:-1], log)
                if change == ("--repeat", ""):
                    self.assertEqual(item.schedule, expected)
                else:
                    assert isinstance(item.schedule, the_list.ScheduledSeries)
                    pending = item.schedule.next_run()
                    assert isinstance(pending, the_list.SeriesRun)
                    self.assertEqual((pending.number, pending.run), (number, expected))

        settings = self.list_config.read_text()
        for rule, old_end, moves, end, limits, description, expected in (
            ("FREQ=DAILY", "", ("2026-12-01T10:00+00:00",), "COUNT=3", (730, 730), "Daily for 3 runs",
             ((2, "2026-12-01T10:00+00:00"), (3, "2026-12-02T09:00+00:00"), (4, "2026-12-03T09:00+00:00"))),
            ("FREQ=WEEKLY;BYDAY=MO", "", ("2026-12-08T09:00+00:00",), "COUNT=3", (730, 730), "Weekly on Monday for 3 runs",
             ((2, "2026-12-08T09:00+00:00"), (3, "2026-12-14T09:00+00:00"), (4, "2026-12-21T09:00+00:00"))),
            ("FREQ=DAILY", "", ("2026-12-01T10:00+00:00",), "UNTIL=20261203T090000Z", (730, 730),
             "Daily until 2026-12-03 09:00:00 UTC",
             ((2, "2026-12-01T10:00+00:00"), (3, "2026-12-02T09:00+00:00"), (4, "2026-12-03T09:00+00:00"))),
            ("FREQ=DAILY", "", ("2026-12-04T10:00+00:00",), "COUNT=1", (730, 730), "Daily for 1 run",
             ((3, "2026-12-02T09:00+00:00"),)),
            ("FREQ=DAILY", "", ("2026-12-04T10:00+00:00", "2026-12-01T08:00+00:00"),
             "UNTIL=20261201T120000Z", (730, 730), "Daily until 2026-12-01 12:00:00 UTC",
             ((3, "2026-12-01T08:00+00:00"),)),
            ("FREQ=DAILY", "COUNT=3", ("2026-12-10T10:00+00:00",), "COUNT=2", (730, 730), "Daily for 2 runs",
             ((3, "2026-12-02T09:00+00:00"), (2, "2026-12-10T10:00+00:00"))),
            ("FREQ=DAILY", "COUNT=3", ("2026-12-10T10:00+00:00",), "COUNT=3", (730, 730), "Daily for 3 runs",
             ((3, "2026-12-02T09:00+00:00"), (4, "2026-12-03T09:00+00:00"), (2, "2026-12-10T10:00+00:00"))),
            ("FREQ=DAILY", "COUNT=2", ("2026-12-10T10:00+00:00",), "COUNT=2", (3, 3), "Daily for 2 runs",
             ((2, "2026-12-10T10:00+00:00"), (12, "2026-12-11T09:00+00:00"))),
            ("FREQ=DAILY", "COUNT=2", ("2026-12-10T10:00+00:00",), "UNTIL=20261211T090000Z", (3, 3),
             "Daily until 2026-12-11 09:00:00 UTC",
             ((2, "2026-12-10T10:00+00:00"), (12, "2026-12-11T09:00+00:00"))),
            ("FREQ=DAILY", "UNTIL=20261201T090000Z", ("2026-12-04T10:00+00:00",), "", (4, 4), "Daily",
             ((2, "2026-12-04T10:00+00:00"), (6, "2026-12-05T09:00+00:00"), (7, "2026-12-06T09:00+00:00"))),
            ("FREQ=DAILY", "UNTIL=20261202T090000Z", ("2026-12-04T10:00+00:00",),
             "UNTIL=20261202T090000Z", (4, 2), "Daily until 2026-12-02 09:00:00 UTC",
             ((3, "2026-12-02T09:00+00:00"),)),
        ):
            saved_limit, live_limit = limits
            with self.subTest(rule=rule, old_end=old_end, moves=moves, end=end, limits=limits):
                _ = self.list_config.write_text(settings.replace("series_run_limit=730", f"series_run_limit={saved_limit}"))
                anchor = "2026-11-30T09:00+00:00"
                code, output, errors = self.add_scheduled(anchor, "--repeat", f"{rule};{old_end}" if old_end else rule)
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                self.save_series_progress(identity, finished=frozenset({1}))
                for moved_at in moves:
                    code, _, errors = self.command("update", "--id", identity, "--source", "nate",
                                                   "--scope", "this", "--start", moved_at)
                    self.assertEqual((code, errors), (0, ""))
                before = the_list.load(MORTY.id, identity).schedule
                assert isinstance(before, the_list.ScheduledSeries)
                _ = self.list_config.write_text(settings.replace("series_run_limit=730", f"series_run_limit={live_limit}"))
                now_at = START if end else datetime(2026, 12, 3, 9, tzinfo=UTC)
                with patch.object(the_list, "now", return_value=now_at):
                    code, output, errors = self.command("update", "--id", identity, "--source", "nate",
                                                          "--scope", "following", "--repeat", f"{rule};{end}" if end else rule)
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(output, f"Repeats: {description}\n")
                item = the_list.load(MORTY.id, identity)
                changed = item.schedule
                assert isinstance(changed, the_list.ScheduledSeries)
                self.assertEqual(changed.first.start, datetime.fromisoformat(anchor))
                self.assertEqual(changed.run_allowance, saved_limit)
                self.assertEqual(changed.exceptions, before.exceptions)
                self.assertEqual(item.log[-1].what, the_list.FieldChange("Updated", {"schedule": changed}))
                self.assertEqual(tuple((run.number, run.run.start.isoformat(timespec="minutes"))
                                       for run in changed.runs()), expected)
                rows = self.command("list")[1].splitlines()
                row = next(line for line in rows if line.startswith(f"| {identity.removeprefix('L')} |"))
                self.assertIn(f"· {description} · next ", row)
        _ = self.list_config.write_text(settings)

        old_words, new_words = "Read the figures.", "Read new figures."
        fourth = start + timedelta(days=3, hours=2)
        early = second - timedelta(hours=1)
        daily = the_list.ScheduledSeries(the_list.ScheduledRun(start, old_words),
                                        repeat_rule.parse("FREQ=DAILY;COUNT=5"), the_list.series_run_limit(),
                                         runs_used=1, finished_runs=frozenset({1}))
        tokyo_first = datetime(2026, 11, 24, 0, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
        tokyo_second = tokyo_first + timedelta(days=7)
        later_in_tokyo = tokyo_second.replace(hour=1, minute=0)
        ny_start = "2026-11-30T11:00:00-05:00[America/New_York]"
        between_runs = tokyo_first + timedelta(days=1)
        new_repeat = ("--repeat", "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU")
        weekly = the_list.ScheduledSeries(the_list.ScheduledRun(tokyo_first, old_words),
                                         repeat_rule.parse("FREQ=WEEKLY;BYDAY=TU"), the_list.series_run_limit(),
                                          runs_used=1, finished_runs=frozenset({1}))
        moved_run = the_list.RunException(2, the_list.ScheduledRun(moved, old_words))
        for schedule, now_at, repeat_options, expected, repeat_label in (
            (replace(daily, runs_used=2, exceptions=(moved_run, the_list.RunException(3, the_list.SkippedRun("")),
                                        the_list.RunException(4, the_list.ScheduledRun(fourth, old_words)))),
             second, (), ((2, moved, new_words), (4, fourth, new_words), (5, start + timedelta(days=4), new_words)),
             "Repeats: Daily"),
            (replace(daily, exceptions=(the_list.RunException(3, the_list.ScheduledRun(early, old_words)),)),
             early, (), ((3, early, new_words), (2, second, old_words), (4, start + timedelta(days=3), new_words)),
             "Repeats: Daily"),
            (replace(daily, exceptions=(moved_run,)), second + timedelta(minutes=30),
             ("--repeat", ""), the_list.ScheduledRun(moved, new_words), "Does not repeat"),
            (weekly, between_runs, new_repeat,
             ((2, tokyo_second, new_words), (3, tokyo_second + timedelta(days=14), new_words)),
             "Repeats: Every 2 weeks on Tuesday"),
            (weekly, between_runs, (*new_repeat, "--start", ny_start),
             ((2, later_in_tokyo, new_words), (3, later_in_tokyo + timedelta(days=14), new_words)),
             "Repeats: Every 2 weeks on Tuesday"),
            (weekly, between_runs, (),
             ((2, tokyo_second, new_words), (3, tokyo_second + timedelta(days=7), new_words)),
             "Repeats: Weekly on Tuesday"),
            (weekly, between_runs, ("--start", ny_start),
             ((2, later_in_tokyo, new_words), (3, later_in_tokyo + timedelta(days=7), new_words)),
             "Repeats: Weekly on Tuesday"),
        ):
            with self.subTest(scope="following", exceptions=schedule.exceptions, repeat_options=repeat_options):
                item = self.add(changes={"state": the_list.ItemState.SCHEDULED, "schedule": schedule})
                with patch.object(the_list, "now", return_value=now_at):
                    code, output, errors = self.command("update", "--id", item.id, "--source", "nate",
                                                        "--scope", "following", "--do", new_words, *repeat_options)
                self.assertEqual((code, errors), (0, ""))
                self.assertIn(repeat_label, output)
                saved = the_list.load(MORTY.id, item.id).schedule
                if isinstance(expected, the_list.ScheduledRun):
                    self.assertEqual(saved, expected)
                    continue
                assert isinstance(saved, the_list.ScheduledSeries)
                pending = saved.runs()
                for number, date, words in expected:
                    next_run = next(pending)
                    self.assertEqual((next_run.number, next_run.run), (number, the_list.ScheduledRun(date, words)))
                    self.assertEqual(next_run.run.start.tzinfo, date.tzinfo)

    def test_repeated_item_removals_use_the_selected_scope(self) -> None:
        start = datetime(2026, 11, 30, 17, tzinfo=UTC)
        second = start + timedelta(days=1)
        third = start + timedelta(days=2)
        delayed = second + timedelta(hours=1)
        for scope, now_at, operation, options, state, reorder in (
            ("this", second, "close", ("--words", "Skip this check"), the_list.ItemState.SCHEDULED, False),
            ("this", second, "close", ("--words", "Skip this check"), the_list.ItemState.SCHEDULED, True),
            ("following", second, "park", ("--until", third.isoformat()), the_list.ItemState.WORKING, False),
            ("all", START, "close", ("--words", "Stop these checks"), the_list.ItemState.CLOSED, False),
            ("all", START, "update", ("--state", "Working"), the_list.ItemState.WORKING, False),
        ):
            with self.subTest(scope=scope, operation=operation, reorder=reorder):
                code, output, errors = self.add_scheduled(start.isoformat(), "--repeat", "FREQ=DAILY")
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                if reorder:
                    self.assertEqual(self.command("update", "--id", identity, "--scope", "this", "--source", "nate",
                                                  "--start", delayed.isoformat()), (0, "Repeats: Daily\n", ""))
                elif now_at == second:
                    self.save_series_progress(identity, finished=frozenset({1}))
                before = the_list.load(MORTY.id, identity)
                with patch.object(the_list, "now", return_value=now_at):
                    self.assertEqual(self.command(operation, "--id", identity, "--source", "nate",
                                                  "--scope", scope, *options), (0, "", ""))
                item = the_list.load(MORTY.id, identity)
                self.assertIs(item.state, state)
                if scope == "this":
                    series = item.schedule
                    assert isinstance(series, the_list.ScheduledSeries)
                    next_run = series.next_run()
                    assert isinstance(next_run, the_list.SeriesRun)
                    self.assertEqual((next_run.number, next_run.run.start), (1, delayed) if reorder else (3, third))
                    skip = the_list.RunException(2, the_list.SkippedRun("Skip this check"))
                    if reorder:
                        self.assertEqual(series.skipped_runs, frozenset({2}))
                        self.assertIn(skip, series.exceptions)
                    else:
                        self.assertEqual(series.exceptions, (skip,))
                else:
                    self.assertIs(item.schedule, the_list.NoSchedule.UNSET)
                if operation == "park":
                    self.assertEqual(item.parked_until, third)
                self.assertEqual(item.log[:-1], before.log)
                event = item.log[-1].what
                assert isinstance(event, the_list.FieldChange)
                self.assertEqual(event.fields.get("schedule"), item.schedule)

    def test_repeated_item_refuses_an_invalid_scope_without_writing(self) -> None:
        start = datetime(2026, 11, 30, 17, tzinfo=UTC)
        second = start + timedelta(days=1)
        moved = second + timedelta(hours=1)
        for options, moment, runs_used, prepare, reason, rule in (
            (("update", "--do", "Read new figures."), START, 0, (), "use --scope", "FREQ=DAILY"),
            (("update", "--do", "Read new figures.", "--scope", "following"), START, 0, (), "from the second run", "FREQ=DAILY"),
            (("update", "--repeat", "FREQ=WEEKLY", "--scope", "this"), START, 0, (), "change a repeat rule", "FREQ=DAILY"),
            (("close", "--words", "Stop"), START, 0, (), "use --scope", "FREQ=DAILY"),
            (("close", "--words", "Stop", "--scope", "following"), START, 0, (), "from the second run", "FREQ=DAILY"),
            (("update", "--scope", "this", "--start", "2026-12-01T09:00-08:00"),
             START, 0, (), "another run already uses this time", "FREQ=DAILY"),
            (("update", "--scope", "following", "--do", "Read new figures."),
             START, 0, ("update", "--start", moved.isoformat()), "from the second run", "FREQ=DAILY"),
            (("update", "--scope", "following", "--do", "Read new figures."),
             second, 0, ("close", "--words", "Skip the first run."), "from the second run", "FREQ=DAILY"),
            (("update", "--scope", "following", "--start", start.isoformat()),
             second, 1, (), "choose a time on or after now", "FREQ=DAILY"),
            (("update", "--scope", "all", "--repeat", ""),
             second, 0, (), "choose a time on or after now", "FREQ=DAILY"),
            (("update", "--scope", "all", "--repeat", "FREQ=DAILY;COUNT=3"),
             START, 0, ("update", "--start", (second + timedelta(days=1)).isoformat()),
             "another run already uses this time", "FREQ=DAILY;COUNT=2"),
            (("update", "--scope", "following", "--repeat", "FREQ=DAILY;UNTIL=20261130T170000Z"),
             START, 1, (), "UNTIL must be on or after the selected run", "FREQ=DAILY"),
            (("update", "--scope", "following", "--repeat", "FREQ=DAILY;UNTIL=20261201T170000Z"),
             START, 1, ("update", "--start", "2026-12-04T17:00:00+00:00"), "UNTIL must be on or after the selected run",
             "FREQ=DAILY;UNTIL=20261201T170000Z"),
            (("update", "--scope", "all", "--start", "9999-12-30T23:00-05:00"),
             START, 0, (), "COUNT crosses the supported date limit", "FREQ=DAILY;COUNT=2"),
        ):
            with self.subTest(options=options, moment=moment, prepare=prepare):
                code, output, errors = self.add_scheduled(start.isoformat(), "--repeat", rule)
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                if runs_used:
                    self.save_series_progress(identity, finished=range(1, runs_used + 1))
                if prepare:
                    verb, *flags = prepare
                    code, _, errors = self.command(verb, "--id", identity, "--source", "nate", "--scope", "this", *flags)
                    self.assertEqual((code, errors), (0, ""))
                path = the_list.folder(MORTY.id) / f"{identity}.md"
                before = path.read_bytes()
                verb, *flags = options
                with patch.object(the_list, "now", return_value=moment):
                    code, output, errors = self.command(verb, "--id", identity, "--source", "nate", *flags)
                self.assertEqual((code, output), (1, ""))
                self.assertIn(reason, errors)
                self.assertEqual(path.read_bytes(), before)

        for scope, moment, finished, run_number in (
            (the_list.ScheduleScope.THIS, start + timedelta(days=2), frozenset({1, 2}), 2),
            (the_list.ScheduleScope.FOLLOWING, start + timedelta(days=3), frozenset({1, 2, 3}), 2),
            (the_list.ScheduleScope.ALL, start + timedelta(days=2), frozenset({1, 2}), 2),
            (the_list.ScheduleScope.ALL, start, frozenset[int](), repeat_rule.NoNextRun.REMAINING),
        ):
            with self.subTest(scope=scope, moment=moment, run_number=run_number):
                code, output, errors = self.add_scheduled(start.isoformat(), "--repeat", "FREQ=DAILY;COUNT=3")
                self.assertEqual((code, errors), (0, ""))
                identity = output.splitlines()[-1]
                self.save_series_progress(identity, finished=finished)
                path = the_list.folder(MORTY.id) / f"{identity}.md"
                before = path.read_bytes()
                saved = the_list.load(MORTY.id, identity)
                expected = the_list.ScheduleExpectation(the_list.schedule_revision(saved.schedule), run_number)
                with (patch.object(the_list, "now", return_value=moment),
                      self.assertRaisesRegex(the_list.ScheduleSelectionChanged, "The next run changed")):
                    if scope is the_list.ScheduleScope.ALL:
                        _ = the_list.save_page_schedule(
                            MORTY.id, identity, the_list.ScheduleOperation.EDIT,
                            {"words": "Read new figures.", "repeat": repeat_rule.parse("FREQ=DAILY;INTERVAL=2")},
                            scope=scope, expected=expected, repeat_zone=ZoneInfo("America/New_York"),
                            receipt=the_list.PageRequest("repeat", "park", "Repeat"),
                        )
                    else:
                        _ = the_list.close(MORTY.id, identity, "Stop", source="nate", schedule_scope=scope,
                                           expected_schedule=expected)
                self.assertEqual(path.read_bytes(), before)

    def test_text_limits_on_add_and_update(self) -> None:
        cases = (
            ("latest-status", "latest_status", "max_chars.latest_status"),
            ("next-step", "next_step", "max_chars.next_step"),
            ("add-evidence", "evidence", "max_chars.evidence_line"),
            ("evidence", "evidence", "max_chars.evidence_line"),
        )
        for option, field, key in cases:
            limit = LIST_TEXT_LIMITS[key]
            at_limit = "é" * limit
            over_limit = at_limit + "é"
            unit = "characters per line" if field == "evidence" else "characters"
            if option == "evidence":
                at_limit += "\nA second line."
                over_limit = "A first line.\n" + over_limit
            for action in ("add", "update"):
                with self.subTest(action=action, option=option):
                    if action == "add":
                        command = ("add", "--source", "nate", "--title", "Check the limit", "--owner", RUNNER.id)
                    else:
                        item = self.add()
                        command = ("update", "--source", "nate", "--id", item.id)
                    before = the_list.records(MORTY.id)
                    result = self.command(*command, "--" + option, over_limit)
                    self.assertEqual(result, (1, "", f"the_list: --{option} limit is {limit} {unit}. "
                                                     + "write it for nate: two short sentences\n"))
                    self.assertEqual(the_list.records(MORTY.id), before)
                    code, output, error = self.command(*command, "--" + option, at_limit)
                    self.assertEqual((code, error), (0, ""))
                    identity = output.strip() if action == "add" else command[-1]
                    saved = the_list.load(MORTY.id, identity)
                    self.assertEqual(getattr(saved, field), at_limit)

    def test_done_when_saves_every_result_on_add_and_update(self) -> None:
        done = ("The operator can open the List, find the correct item, and see who owns the next step. "
                "The producer can update that step and confirm that the List shows the new text after a reload.")
        code, output, error = self.command("add", "--source", "nate", "--title", "Deliver the List",
                                           "--owner", RUNNER.id, "--done-when", done)
        self.assertEqual((code, error), (0, ""))
        identity = output.strip()
        self.assertEqual(the_list.load(MORTY.id, identity).done_when, done)
        updated = done + " The change record includes the review result."
        self.assertEqual(self.command("update", "--source", "nate", "--id", identity, "--done-when", updated),
                         (0, "", ""))
        self.assertEqual(the_list.load(MORTY.id, identity).done_when, updated)

    def test_old_long_text_remains_readable_and_survives_other_updates(self) -> None:
        status = "s" * (LIST_TEXT_LIMITS["max_chars.latest_status"] + 1)
        step = "n" * (LIST_TEXT_LIMITS["max_chars.next_step"] + 1)
        done = ("The producer can find the release checklist in the List and confirm that the review, "
                "the browser check, and the final approval are complete before closing the item.")
        evidence = "e" * (LIST_TEXT_LIMITS["max_chars.evidence_line"] + 1)
        item = self.add(changes={"latest_status": status, "next_step": step, "done_when": done, "evidence": evidence})
        self.assertEqual(the_list.load(MORTY.id, item.id), item)
        code, output, error = self.command("list", "--latest-status")
        self.assertEqual((code, error), (0, ""))
        self.assertIn(status, output)
        self.assertIn(step, output)
        self.assertEqual(self.command("update", "--id", item.id, "--source", "nate", "--title", "New title",
                                      "--add-evidence", "New fact."), (0, "", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual((saved.latest_status, saved.next_step, saved.evidence),
                         (status, step, evidence + "\nNew fact."))
        self.assertEqual(saved.done_when, done)

    def test_text_limit_uses_the_current_config_and_refuses_bad_settings(self) -> None:
        self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.list_config.parent)}))
        item = self.add()
        for setting in ("", "# whole: 1..\nmax_chars.latest_status=0\n"):
            with self.subTest(setting=setting):
                _ = self.list_config.write_text(setting)
                self.assertEqual(self.command("update", "--id", item.id, "--source", "nate", "--latest-status", "ok"),
                                 (1, "", f"the_list: --latest-status needs a valid max_chars.latest_status in "
                                         + f"{self.list_config}\n"))
                self.assertEqual(the_list.load(MORTY.id, item.id), item)
        _ = self.list_config.write_text("# whole: 1..\nmax_chars.latest_status=2\n")
        self.assertEqual(self.command("update", "--id", item.id, "--source", "nate", "--latest-status", "ok"),
                         (0, "", ""))
        self.assertEqual(the_list.load(MORTY.id, item.id).latest_status, "ok")

    def test_eta_accepts_the_agreed_forms_on_add_and_update(self) -> None:
        zone_name = "America/New_York"
        zone = ZoneInfo(zone_name)
        cases = (
            ("Nov 19", date(2026, 11, 19)),
            ("Nov 19 14:30 EST", datetime(2026, 11, 19, 14, 30, tzinfo=zone)),
            ("Jan 2", date(2027, 1, 2)),
            ("Jul 4 14:30 EDT", datetime(2026, 7, 4, 14, 30, tzinfo=zone)),
            ("No time yet", "No time yet"),
            ("Waiting for nate", "Waiting for nate"),
            ("Paused", "Paused"),
            ("Delivered", "Delivered"),
        )
        with patch.dict(os.environ, {"USER_ZONE": zone_name}):
            for action in ("add", "update"):
                for text, expected in cases:
                    with self.subTest(action=action, eta=text):
                        if action == "add":
                            command = ("add", "--title", "Check the ETA", "--owner", RUNNER.id)
                            code, output, error = self.command(*command, "--source", "nate", "--eta", text)
                            self.assertEqual((code, error), (0, ""))
                            identity = output.strip()
                        else:
                            identity = self.add().id
                            command = ("update", "--id", identity)
                            self.assertEqual(self.command(*command, "--source", "nate", "--eta", text), (0, "", ""))
                        saved = the_list.load(MORTY.id, identity)
                        self.assertEqual(saved.eta, expected)
                        if text in ("Paused", "Delivered"):
                            result, output, error = self.command(*command, "--source", "nate", "--eta", text.lower())
                            self.assertEqual((result, output), (1, ""), error)
                            self.assertIn("put the reason in --latest-status", error)

    def test_date_only_eta_stores_a_plain_date_in_the_record_and_log(self) -> None:
        with patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            result = self.command("add", "--title", "Check the ETA", "--owner", RUNNER.id,
                                  "--source", "nate", "--eta", "Nov 20")
        self.assertEqual(result, (0, "L1\n", ""))
        record = (the_list.folder(MORTY.id) / "L1.md").read_text()
        with self.subTest(where="record"):
            self.assertIn('\neta: "2026-11-20"\neta_kind: "time or status"\n', record)
        with self.subTest(where="log"):
            self.assertIn('"eta": {"value": "2026-11-20", "kind": "time or status"}', record)
        saved = the_list.load(MORTY.id, "L1")
        with self.subTest(where="read"):
            self.assertEqual(saved.eta, date(2026, 11, 20))
            self.assertIsInstance(saved.log[0].what, the_list.FieldChange)
            assert isinstance(saved.log[0].what, the_list.FieldChange)
            self.assertEqual(saved.log[0].what.fields.get("eta"), saved.eta)

    def test_eta_due_uses_the_next_local_day_for_a_date_and_the_end_of_a_range(self) -> None:
        cases = (
            (date(2026, 3, 8), datetime(2026, 3, 9, 4, tzinfo=UTC)),
            (date.max, None),
            (START, START),
            (the_list.EtaRange(START, START + timedelta(hours=1)), START + timedelta(hours=1)),
            (the_list.EtaStatus.PAUSED, None),
            (the_list.EtaDescription("After the merge"), None),
        )
        with patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            for eta, expected in cases:
                with self.subTest(eta=eta):
                    self.assertEqual(the_list.eta_due(eta), expected)

    def test_eta_refuses_other_forms_on_add_and_update(self) -> None:
        item = self.add()
        commands = (
            ("add", "--title", "Check the ETA", "--owner", RUNNER.id),
            ("update", "--id", item.id),
        )
        with patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            for command in commands:
                for text in ("about Nov 19", "2026-11-19T14:30-05:00/2026-11-20T14:30-05:00",
                             "After the merge", "Pending", "Nov 19 14:30 EDT"):
                    with self.subTest(action=command[0], eta=text):
                        result, output, error = self.command(*command, "--source", "nate", "--eta", text)
                        self.assertEqual((result, output), (1, ""))
                        for form in ("Mon D", "Mon D HH:MM ZONE", "No time yet", "Waiting for nate",
                                     "Paused", "Delivered", "put the reason in --latest-status"):
                            self.assertIn(form, error)
                        self.assertEqual(the_list.records(MORTY.id), [item])

    def test_old_eta_values_survive_an_update(self) -> None:
        old_values = (the_list.EtaRange(START, START + timedelta(hours=1)), START,
                      the_list.EtaDescription("After the merge"), the_list.EtaStatus.NOT_ASKED, the_list.EtaStatus.PENDING)
        for eta in old_values:
            with self.subTest(eta=eta):
                item = self.add(changes={"eta": eta})
                self.assertEqual(self.command("update", "--id", item.id, "--source", "nate", "--state", "Working"),
                                 (0, "", ""))
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(saved.eta, eta)
                self.assertEqual(saved.log[0], item.log[0])
                result, output, error = self.command("update", "--id", item.id, "--source", "nate",
                                                     "--eta", the_list.eta_text(eta))
                self.assertEqual((result, output), (1, ""), error)
                self.assertIn("put the reason in --latest-status", error)

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

    def test_add_refuses_a_producer_without_a_staff_row(self) -> None:
        path = staff.record_path()
        members = staff.snapshot()
        for record_exists in (False, True):
            with self.subTest(record_exists=record_exists), staff.supplied_snapshot(members):
                if record_exists:
                    _ = path.write_text(staff.StaffSnapshot((MORTY, RUNNER)).encode(), encoding="utf-8")
                    with self.assertRaisesRegex(the_list.ListError, f"{OTHER.id}:.*no current producer staff row"):
                        _ = self.add(changes={"producer": OTHER.id})
                else:
                    path.unlink()
                    with self.assertRaises(staff.StaffAbsent):
                        _ = self.add(changes={"producer": OTHER.id})
                self.assertEqual(the_list.records(MORTY.id, include_closed=True), [])

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
                    + "Use any of `--title`, `--done-when`, `--unit`, `--latest-status`, `--next-step`, "
                    + "`--eta`, `--state`, `--next-check`, `--owner`, `--related-to`, `--blocked-by`, "
                    + "`--evidence`, `--add-evidence`."))
                self.assertEqual(showrunner, (RUNNER.id, f"dwight is the producer of item {number}: Build plan. "
                    + "Send design questions on this item to dwight."))
        self.assertEqual(len(self.notices), 4)
        read_command = self.notices[0][1].split("`")[1]
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session, "USER_ZONE": "America/New_York"}):
            result, output, error = self.command(*read_command.split()[2:])
        self.assertEqual((result, error), (0, ""))
        self.assertIn("| Latest status |", output)
        self.assertIn("| 1 | show | dwight | Build plan | New; parked until", output)
        self.assertIn("| The build is complete. · 13:00 EST |", output)
        with self.subTest(owner="producer"):
            owned = the_list.add(MORTY.id, "Producer's plan", owner=BOB.id, source="nate",
                                 changes={"producer": OTHER.id})
            errors = io.StringIO()
            with redirect_stderr(errors):
                result = the_list.notify_producer_assignment(owned, MORTY.id, MORTY.id, caller=MORTY.id)
            self.assertEqual((result, errors.getvalue()), (0, ""))
            (notice,) = self.notices[4:]
            self.assertEqual(notice[0], OTHER.id)
            self.assertIn("You are the producer of item 3: Producer's plan.", notice[1])

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

    def test_assignment_accepts_a_showrunner_notice_held_for_review(self) -> None:
        item = self.add()
        self.send_patch.stop()
        with patch.object(subprocess, "run", side_effect=(
            subprocess.CompletedProcess([], 0, "SENT: delivered\n", ""),
            subprocess.CompletedProcess([], 0, "KEPT: showrunner is in a review; delivered at its end\n", ""),
        )) as send:
            result = self.command("update", "--id", item.id, "--producer", "dwight", "--source", "morty")
        self.assertEqual(result, (0, "", ""))
        self.assertEqual(the_list.load(MORTY.id, item.id).producer, OTHER.id)
        self.assertEqual([call.args[0][3] for call in send.call_args_list], [OTHER.id, RUNNER.id])

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
        legacy = the_list.folder(OTHER.id)
        legacy.mkdir(parents=True)
        words = (the_list.folder(MORTY.id) / f"{own.id}.md").read_text()
        _ = (legacy / f"{own.id}.md").write_text(words.replace("Dwight's item", "Wrong store"))
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
                                          "--next-step", "Review the plan.", "--eta", "No time yet",
                                          "--title", "Publish the plan", "--done-when", "The plan is public."),
                             (0, "", ""))
            self.assertEqual(self.command("update", "--id", item.id, "--eta", "Delivered", "--source", "morty"), (0, "", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual((saved.latest_status, saved.next_step, saved.eta),
                         ("The plan is ready.", "Review the plan.", the_list.EtaStatus.DELIVERED))
        self.assertEqual((saved.title, saved.done_when), ("Publish the plan", "The plan is public."))
        self.assertEqual([entry.source for entry in saved.log], ["nate", "dwight", "dwight"])
        self.assertEqual(saved.log[-2].what, the_list.FieldChange("Updated", {
            "latest_status": "The plan is ready.", "next_step": "Review the plan.", "eta": the_list.EtaStatus.NO_TIME_YET,
            "title": "Publish the plan", "done_when": "The plan is public."}))
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
                              source=OTHER.name, assigned_to=OTHER, schedule_edits={})
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
            for option, value in (("--eta", "Paused"), ("--state", "Working"), ("--next-check", START.isoformat()),
                                  ("--title", "New title"), ("--done-when", "The plan is public."),
                                  ("--unit", "unit-board"), ("--evidence", "An updated result."),
                                  ("--add-evidence", "An additional result."), ("--owner", RUNNER.id),
                                  ("--blocked-by", "L2:ready"), ("--related-to", "L2")):
                with self.subTest(option=option):
                    self.assertEqual(self.command("update", "--id", item.id, option, value), (1, "", (
                        f"the_list: {item.id} is assigned to another producer. "
                        + "A producer can change only its own items.\n")))
                    self.assertEqual(the_list.load(MORTY.id, item.id), item)

        code, output, errors = self.add_scheduled("2026-11-30T17:00Z", "--producer", BOB.name,
                                                   "--repeat", "FREQ=DAILY")
        self.assertEqual((code, errors), (0, ""))
        identity = output.splitlines()[-1]
        saved = the_list.load(MORTY.id, identity)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("update", "--id", identity, "--do", "Change the check.", "--scope", "all"),
                             (1, "", f"the_list: {identity} is assigned to another producer. "
                              + "A producer can change only its own items.\n"))
        self.assertEqual(the_list.load(MORTY.id, identity), saved)

    def test_producer_updates_owner_and_links_without_writing_the_target(self) -> None:
        own = self.add(changes={"producer": OTHER.id, "owner": the_list.UnassignedOwner.NATE})
        target = self.add(changes={"producer": BOB.id})
        target_path = the_list.folder(MORTY.id) / f"{target.id}.md"
        before = target_path.read_bytes(), target_path.stat().st_mtime_ns
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("update", "--id", own.id, "--owner", RUNNER.id,
                                          "--related-to", target.id, "--blocked-by", f"{target.id}:ready"), (0, "", ""))
            saved = the_list.load(MORTY.id, own.id)
            self.assertEqual(saved.log[-1].what, the_list.FieldChange("Updated", {
                "owner": RUNNER.id, "related_to": (target.number,),
                "blocked_by": (the_list.ItemDependency(target.number, "ready"),),
            }))
            self.assertEqual((saved.owner, saved.producer), (RUNNER.id, OTHER.id))
            self.assertEqual(saved.related_to, (target.number,))
            self.assertEqual(saved.blocked_by, (the_list.ItemDependency(target.number, "ready"),))
            self.assertEqual(saved.log[-1].source, OTHER.name)
            result, output, error = self.command("update", "--id", target.id, "--related-to", own.id)
            self.assertEqual((result, output), (1, ""))
            self.assertIn("A producer can change only its own items.", error)
        self.assertEqual((target_path.read_bytes(), target_path.stat().st_mtime_ns), before)
        self.assertEqual(self.notices, [])

    def test_producer_updates_and_appends_its_evidence(self) -> None:
        cases = (
            ("--evidence", "An updated result.\nAnother result.", "An updated result.\nAnother result."),
            ("--add-evidence", "An additional result.", "An old result.\nAn additional result."),
        )
        for option, text, expected in cases:
            with self.subTest(option=option):
                item = self.add(changes={"producer": OTHER.id, "evidence": "An old result."})
                with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
                    self.assertEqual(self.command("update", "--id", item.id, option, text), (0, "", ""))
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(saved.evidence, expected)
                self.assertEqual(saved.log[-1].what, the_list.FieldChange("Updated", {"evidence": expected}))
                self.assertEqual(saved.log[-1].source, OTHER.name)

    def test_producer_updates_the_title_done_when_and_unit_of_its_item(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("update", "--id", item.id, "--title", "Ship the page",
                                          "--done-when", "The page is live.", "--unit", "unit-board"), (0, "", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual((saved.title, saved.done_when, saved.unit), ("Ship the page", "The page is live.", "unit-board"))
        self.assertEqual(saved.log[-1].what, the_list.FieldChange("Updated", {
            "title": "Ship the page", "unit": "unit-board", "done_when": "The page is live."}))
        self.assertEqual(saved.log[-1].source, OTHER.name)

    def test_producer_cannot_update_other_fields(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        fields = (("--producer", BOB.id), ("--producer", "invalid producer"), ("--requested-by", "nate"))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            for field, *values in fields:
                with self.subTest(field=field):
                    result, output, error = self.command("update", "--id", item.id, "--eta", "Paused", field, *values)
                    self.assertEqual((result, output), (1, ""))
                    self.assertIn("producers can update only --title, --done-when, --unit, --latest-status, "
                                  + "--next-step, --eta, --state, --next-check, --owner, --related-to, --blocked-by, "
                                  + "--evidence, --add-evidence.", error)
                    self.assertIn(f"Refused options: {field}", error)
                    self.assertEqual(the_list.load(MORTY.id, item.id), item)
        self.assertEqual(self.notices, [])

    def test_producer_update_rejects_an_invalid_owner(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("update", "--id", item.id, "--owner", OWNER.id),
                             (1, "", "the_list: owner must name a showrunner staff id\n"))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_producer_scoped_update_cannot_reassign_an_item(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        with self.assertRaisesRegex(the_list.ListError, "Refused options: --producer"):
            _ = scoped_update(MORTY.id, item.id, {"producer": BOB.id}, source=OTHER.name, assigned_to=OTHER, schedule_edits={})
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_producer_scoped_update_logs_its_own_name(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        _ = scoped_update(MORTY.id, item.id, {"latest_status": "The plan is ready."},
                          source=MORTY.name, assigned_to=OTHER, schedule_edits={})
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.latest_status, "The plan is ready.")
        self.assertEqual(saved.log[-1].source, OTHER.name)

    def test_producer_scoped_update_refuses_a_retired_producer(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        retired = staff.set_producer_retirement(OTHER.id, staff.RetiredProducer("claude:sonnet:high"))
        with self.assertRaisesRegex(the_list.ListError, "dwight: retired producer"):
            _ = scoped_update(MORTY.id, item.id, {"state": the_list.ItemState.WORKING},
                              source=retired.name, assigned_to=retired, schedule_edits={})
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def top_items(self) -> tuple[staff.TopItem | staff.NoTopItem, ...]:
        record = staff.snapshot()
        return tuple(row.top_item for row in (record.by_id(OTHER.id), record.by_id(BOB.id)) if row is not None)

    def test_make_top_priority_replaces_the_producers_top_item(self) -> None:
        first = self.add(changes={"producer": OTHER.id})
        second = self.add("Second item", changes={"producer": OTHER.id})
        self.assertEqual(the_list.make_top_priority(MORTY.id, first.id, assigned_to=OTHER.id), first)
        _ = the_list.make_top_priority(MORTY.id, second.id)
        self.assertEqual(self.top_items(), (staff.TopItem(second.number), staff.NoTopItem()))
        self.assertEqual(the_list.load(MORTY.id, second.id), second)

    def test_make_top_priority_refuses_a_closed_unknown_or_other_producer_item(self) -> None:
        closed = self.add(changes={"producer": OTHER.id})
        _ = the_list.close(MORTY.id, closed.id, "Done", source="fixture")
        other = self.add("Bob item", changes={"producer": BOB.id})
        cases: tuple[tuple[str, staff.StaffId | the_list.AllItems, str], ...] = (
            (closed.id, the_list.AllItems.INCLUDED, "is Closed"),
            (other.id, OTHER.id, "assigned to another producer"),
            ("L999", the_list.AllItems.INCLUDED, "L999: no list item"))
        for identity, assigned_to, message in cases:
            with self.subTest(identity=identity), self.assertRaisesRegex(the_list.ListError, message):
                _ = the_list.make_top_priority(MORTY.id, identity, assigned_to=assigned_to)
        self.assertEqual(self.top_items(), (staff.NoTopItem(), staff.NoTopItem()))

    def test_top_priority_command_names_a_producers_own_item_and_morty_names_any(self) -> None:
        own = self.add(changes={"producer": OTHER.id})
        other = self.add("Bob item", changes={"producer": BOB.id})
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("top-priority", "--id", str(own.number)),
                             (0, f"{own.id} is the top priority item of dwight\n", ""))
            refused, _, errors = self.command("top-priority", "--id", other.id)
        self.assertEqual((refused, errors), (1, f"the_list: {other.id} is assigned to another producer\n"))
        self.assertEqual(self.command("top-priority", "--id", other.id),
                         (0, f"{other.id} is the top priority item of bob\n", ""))
        self.assertEqual(self.top_items(), (staff.TopItem(own.number), staff.TopItem(other.number)))

    def test_producer_cli_update_reads_staff_once(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        record = staff.snapshot()
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}),
              patch.object(staff, "snapshot", side_effect=[record, OSError("staff read failed")]) as read_staff):
            self.assertEqual(self.command("update", "--id", item.id, "--state", "Working"), (0, "", ""))
        read_staff.assert_called_once_with()
        self.assertEqual(the_list.load(MORTY.id, item.id).state, the_list.ItemState.WORKING)

    def test_list_shows_the_saved_update_time_beside_latest_status(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        updated = START + timedelta(minutes=20)
        with (patch.dict(os.environ, {"USER_ZONE": "America/New_York", "CODEX_THREAD_ID": OTHER.current_session}),
              patch.object(the_list, "now", return_value=updated)):
            self.assertEqual(self.command("update", "--id", item.id, "--latest-status", "The report is ready."), (0, "", ""))
            saved = the_list.load(MORTY.id, item.id)
            self.assertEqual((saved.updated, saved.log[-1].time), (updated, updated))
            self.assertIn("The report is ready. · 13:20 EST", self.command("list", "--latest-status")[1])

        read_at = START + timedelta(hours=11, minutes=5)
        with (patch.dict(os.environ, {"USER_ZONE": "America/New_York", "CODEX_THREAD_ID": OTHER.current_session}),
              patch.object(the_list, "now", return_value=read_at)):
            read = the_list.mark_read(MORTY.id, item.id, source="nate")
            self.assertEqual(read.updated, read_at)
            self.assertIn("The report is ready. · 2026-11-16 13:20 EST", self.command("list", "--latest-status")[1])

    def test_update_refuses_a_clock_time_at_the_end_of_source(self) -> None:
        suffixes = (
            " 13:20", " 13:20 EST", " 13:20+01:00", " 2026-11-18T23:55:00-05:00",
            " 2026-11-18t23:55:00-05:00", " 20261118T235500-0500",
            " 20261118T235500,25-0500", "\t20261118T235500-0500",
        )
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                item = self.add()
                result, output, error = self.command("update", "--id", item.id, "--state", "Working",
                                                     "--source", "morty: report," + suffix)
                self.assertEqual((result, output), (1, ""))
                self.assertIn("the tool adds the time", error)
                self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_update_accepts_source_words_and_a_bare_date(self) -> None:
        for source in ("morty: report", "morty: report, 2026-11-18", "morty: report,20261118"):
            with self.subTest(source=source):
                item = self.add()
                self.assertEqual(self.command("update", "--id", item.id, "--state", "Working", "--source", source),
                                 (0, "", ""))
                self.assertEqual(the_list.load(MORTY.id, item.id).log[-1].source, source)

    def test_producer_can_add_an_item_for_themselves(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": BOB.current_session}):
            for number, reference in enumerate((BOB.name, BOB.id), 1):
                with self.subTest(reference=reference):
                    identity = f"L{number}"
                    self.assertEqual(self.command("add", "--title", "New item", "--owner", RUNNER.id,
                                                  "--source", "bob", "--producer", reference), (0, f"{identity}\n", ""))
                    item = the_list.load(MORTY.id, identity)
                    self.assertEqual((item.title, item.owner, item.producer), ("New item", RUNNER.id, BOB.id))
        self.assertEqual(self.notices, [
            (RUNNER.id, f"bob is the producer of item {number}: New item. Send design questions on this item to bob.")
            for number in (1, 2)
        ])

    def test_producer_add_refuses_another_producer(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": BOB.current_session}):
            for options in (("--producer", OTHER.name), ("--producer", OTHER.id)):
                with self.subTest(options=options):
                    self.assertEqual(self.command("add", "--title", "New item", "--owner", RUNNER.id,
                                                  "--source", "bob", *options),
                                     (1, "", "the_list: To add an item, use --producer bob or your staff ID.\n"))
        self.assertEqual(the_list.records(MORTY.id), [])
        self.assertEqual(self.notices, [])

    def test_producer_parks_and_closes_its_own_item(self) -> None:
        item = self.add(changes={"producer": BOB.id})
        other = self.add(changes={"producer": OTHER.id})
        until = START + timedelta(days=1)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": BOB.current_session}):
            for command, options in (("park", ("--until", until.isoformat())), ("close", ("--words", "Accepted"))):
                with self.subTest(command=command):
                    self.assertEqual(self.command(command, "--id", other.id, *options, "--source", "nate"),
                                     (1, "", f"the_list: {other.id} is assigned to another producer. "
                                      + "A producer can change only its own items.\n"))
            self.assertEqual(self.command("park", "--id", item.id, "--until", until.isoformat(),
                                          "--source", BOB.name), (0, "", ""))
            parked = the_list.load(MORTY.id, item.id)
            self.assertEqual((parked.parked_until, parked.log[-1].source), (until, BOB.name))
            self.assertEqual(self.command("close", "--id", item.id, "--words", "Accepted", "--source", "nate"),
                             (0, "", ""))
        closed = the_list.load(MORTY.id, item.id)
        self.assertEqual((closed.state, closed.closing_words, closed.log[-1].source),
                         (the_list.ItemState.CLOSED, "Accepted", "nate"))
        self.assertEqual(the_list.load(MORTY.id, other.id), other)

    def test_producer_parks_closes_and_reopens_its_own_item(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        until = START + timedelta(days=1)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            for name, *args in (("park", "--until", until.isoformat()), ("unpark",), ("close", "--words", "Done")):
                with self.subTest(command=name), self.assertRaises(SystemExit) as refused:
                    _ = self.command(name, "--id", item.id, *args)
                self.assertEqual(refused.exception.code, 2)
            self.assertEqual(self.command("park", "--id", item.id, "--until", until.isoformat(),
                                          "--source", "nate: park it till tomorrow"), (0, "", ""))
            self.assertEqual(the_list.load(MORTY.id, item.id).parked_until, until)
            self.assertEqual(self.command("unpark", "--id", item.id, "--source", "nate: unpark it"), (0, "", ""))
            self.assertIs(the_list.load(MORTY.id, item.id).parked_until, the_list.NoTimestamp.UNSET)
            self.assertEqual(self.command("close", "--id", item.id, "--words", "Done",
                                          "--source", "nate: close it"), (0, "", ""))
            closed = the_list.load(MORTY.id, item.id)
            self.assertEqual((closed.state, closed.closing_words), (the_list.ItemState.CLOSED, "Done"))
            self.assertEqual(self.command("reopen", "--id", item.id, "--source", "nate: reopen it"), (0, "", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.state, the_list.ItemState.NEW)
        self.assertEqual([entry.source for entry in saved.log[1:]],
                         ["nate: park it till tomorrow", "nate: unpark it", "nate: close it", "nate: reopen it"])

    def test_producer_cannot_park_close_or_reopen_another_producers_item(self) -> None:
        item = self.add(changes={"producer": BOB.id})
        item = the_list.park(MORTY.id, item.id, START + timedelta(days=1), source="nate")
        closed = the_list.close(MORTY.id, self.add(changes={"producer": BOB.id}).id, "Done", source="nate")
        commands = (
            ("park", "--id", item.id, "--until", START.isoformat()),
            ("unpark", "--id", item.id),
            ("close", "--id", item.id, "--words", "Done"),
            ("reopen", "--id", closed.id),
        )
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            for name, _, identity, *args in commands:
                with self.subTest(command=name):
                    self.assertEqual(self.command(name, "--id", identity, *args, "--source", "nate: do it"), (1, "", (
                        f"the_list: {identity} is assigned to another producer. "
                        + "A producer can change only its own items.\n")))
        self.assertEqual(the_list.records(MORTY.id, include_closed=True), [item, closed])
        self.assertEqual(self.notices, [])

    def test_producer_cannot_run_other_commands(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        commands = (
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
            self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "Answer",
                                          "--keeper", BOB.id),
                             (1, "", f"the_list: no List folder for keeper {BOB.id}\n"))
        legacy = self.add(changes={"producer": OTHER.id})
        self.write_legacy_question(legacy, question_record.Question("Q1", START.isoformat(), "Which check passed?"))
        folder = the_list.folder(BOB.id)
        folder.mkdir(parents=True)
        path = folder / f"{legacy.id}.md"
        _ = (the_list.folder(MORTY.id) / path.name).rename(path)
        before = path.read_bytes()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("answer", legacy.id, "--question", "Q1", "--text", "Answer",
                                          "--keeper", BOB.id),
                             (1, "", "the_list: Only the question recipient or morty can answer.\n"))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(the_list.records(MORTY.id), [item])
        legacy = self.legacy_item(BOB, the_list.ItemState.WORKING)
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(BOB.id, legacy.id, the_list.PageRequest("ask", "question", "Ready?"), status)
        opened = the_list.load(BOB.id, legacy.id)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("answer", legacy.id, "--question", "Q1", "--text", "Answer",
                                          "--keeper", BOB.id),
                             (1, "", "the_list: Only the question recipient or morty can answer.\n"))
        self.assertEqual(the_list.load(BOB.id, legacy.id), opened)
        self.assertEqual(self.notices, [])

    def test_two_producers_add_to_mortys_sequence(self) -> None:
        first = self.add()
        for number, producer in enumerate((OTHER, BOB), start=2):
            with patch.dict(os.environ, {"CODEX_THREAD_ID": producer.current_session}):
                result = self.command("add", "--title", "Check the deployment", "--owner", RUNNER.id,
                                      "--source", "another name")
                self.assertEqual(result, (0, f"L{number}\n", ""))
                item = the_list.load(MORTY.id, f"L{number}")
                self.assertEqual((item.producer, item.log[0].source), (producer.id, producer.name))
                self.assertFalse(the_list.folder(producer.id).exists())
                for option, value in (("--producer", MORTY.id), ("--number", "100")):
                    result, output, _ = self.command("add", "--title", "Check the deployment",
                                                      "--owner", RUNNER.id, "--source", producer.name, option, value)
                    self.assertEqual((result, output), (1, ""))
        self.assertEqual([item.number for item in the_list.records(MORTY.id)], [first.number, 2, 3])

    def test_unpublished_reservation_does_not_reuse_its_number(self) -> None:
        first = self.add()
        with patch.object(record_store.NumberedRecords, "write", side_effect=OSError("Cannot publish the item")):
            with self.assertRaisesRegex(OSError, "Cannot publish the item"):
                _ = self.add()
        self.assertEqual(the_list.records(MORTY.id), [first])
        with self.assertRaisesRegex(the_list.ListError, "the next available number is L3"):
            _ = self.add(number=2)
        self.assertEqual(self.add().number, 3)

    def test_producer_add_resolves_related_items_across_producers(self) -> None:
        foreign = self.add("Private prerequisite", changes={"producer": BOB.id})
        own = self.add("Visible prerequisite", changes={"producer": OTHER.id})
        command = ("add", "--title", "Track the result", "--owner", RUNNER.id, "--source", OTHER.name)
        refused = (1, "", "the_list: the referenced List item does not exist\n")
        cases = (
            ("--related-to", "L99", refused),
            ("--blocked-by", "L99:Complete", refused),
            ("--blocked-by", foreign.id + ":Complete", refused),
            ("--related-to", foreign.id, (0, "L3\n", "")),
        )
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            for option, reference, expected in cases:
                with self.subTest(option=option, reference=reference):
                    self.assertEqual(self.command(*command, option, reference), expected)
            self.assertEqual(self.command(*command, "--related-to", own.id, "--blocked-by", own.id + ":Complete"),
                             (0, "L4\n", ""))
            result, output, error = self.command("list", "--all")
            self.assertEqual((result, error), (0, ""))
            self.assertIn("Track the result", output)
            self.assertNotIn(foreign.title, output)
        self.assertEqual(the_list.load(MORTY.id, "L3").related_to, (foreign.number,))
        saved = the_list.load(MORTY.id, "L4")
        self.assertEqual(saved.related_to, (own.number,))
        self.assertEqual(saved.blocked_by, (the_list.ItemDependency(own.number, "Complete"),))

    def test_close_reopen_and_repeats_keep_the_record_and_number(self) -> None:
        item = self.add(number=70)
        _ = the_list.park(MORTY.id, item.id, START + timedelta(days=1), source="nate")
        path = the_list.folder(MORTY.id) / "L70.md"
        self.assertEqual(self.command("close", "--id", item.id, "--words", "Stop tracking this item", "--source", "nate"), (0, "", ""))
        closed = the_list.load(MORTY.id, item.id)
        self.assertEqual((closed.state, closed.closed, closed.closing_words),
                         (the_list.ItemState.CLOSED, START, "Stop tracking this item"))
        self.assertIs(closed.parked_until, the_list.NoTimestamp.UNSET)
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
        self.assertIs(the_list.load(MORTY.id, item.id).schedule, the_list.NoSchedule.UNSET)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_ask_cli_opens_an_ask_and_makes_the_item_unread(self) -> None:
        item = the_list.mark_read(MORTY.id, self.add().id, source="morty")
        self.assertNotIsInstance(item.unread_since, datetime)
        result = self.command("ask", item.id, "--kind", "go", "--target", OWNER.id, "--what", "Merge sdlc to main.",
                              "--why", "The merge needs nate's go.", "--config")
        self.assertEqual(result, (0, f"{item.id} A1\n", ""))
        saved = the_list.load(MORTY.id, item.id)
        ask, = saved.asks
        self.assertEqual((ask.ask_id, ask.kind, ask.target, ask.what, ask.why, ask.config, ask.opened, ask.is_open),
                         ("A1", the_list.AskKind.GO, OWNER.id, "Merge sdlc to main.", "The merge needs nate's go.",
                          True, START, True))
        self.assertEqual(the_list.open_asks(saved), (ask,))
        self.assertIsInstance(saved.unread_since, datetime)
        self.assertEqual(saved.log[-1].what, the_list.AskOpened(ask))

    def test_ask_cli_accepts_only_the_item_producer_or_morty(self) -> None:
        item = self.add()
        args = ("ask", item.id, "--kind", "approval", "--target", OWNER.id, "--what", "Publish the key.",
                "--why", "A config change needs nate.")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": BOB.current_session}):
            refused = self.command(*args)
        self.assertEqual(refused, (1, "", "the_list: only the item's producer or morty can open an ask\n"))
        self.assertEqual(the_list.load(MORTY.id, item.id).asks, ())
        _ = the_list.move_open_producer(MORTY.id, item.id, MORTY.id, BOB.id, source="morty")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": BOB.current_session}):
            self.assertEqual(self.command(*args), (0, f"{item.id} A1\n", ""))
        self.assertEqual(self.command(*args), (0, f"{item.id} A2\n", ""))
        self.assertEqual([ask.ask_id for ask in the_list.load(MORTY.id, item.id).asks], ["A1", "A2"])

    def test_withdraw_ask_requires_the_producer_or_morty(self) -> None:
        item = self.add(changes={"producer": BOB.id})
        args = ("withdraw-ask", item.id, "--reason", "The merge is complete.", "--ask")
        for number, session in enumerate((BOB.current_session, MORTY.current_session), 1):
            ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge.",
                                   why="The merge needs nate's go.", config=False, requester=MORTY.id, source="morty")
            self.assertEqual(ask.ask_id, f"A{number}")
            with patch.object(staff, "runtime_id", return_value=OWNER.current_session):
                code, output, error = self.command(*args, ask.ask_id)
                self.assertEqual((code, output), (1, ""))
                self.assertEqual(len(error.splitlines()), 1)
            with patch.object(staff, "runtime_id", return_value=""):
                self.assertEqual(self.command(*args, ask.ask_id),
                                 (1, "", "the_list: the current session has no staff row\n"))
            self.assertEqual(the_list.open_asks(the_list.load(MORTY.id, item.id)), (ask,))
            with patch.object(staff, "runtime_id", return_value=session):
                self.assertEqual(self.command(*args, ask.ask_id), (0, f"withdrew {item.id} {ask.ask_id}\n", ""))
            self.assertEqual(the_list.open_asks(the_list.load(MORTY.id, item.id)), ())

    def test_withdraw_ask_appends_one_log_entry(self) -> None:
        item = self.add(changes={"producer": BOB.id})
        ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.APPROVAL, target=OWNER.id, what="Publish.",
                               why="The release needs approval.", config=False, requester=BOB.id, source=BOB.name)
        before = the_list.load(MORTY.id, item.id)
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        earlier_log = path.read_bytes().split(b"## Log\n\n", 1)[1].splitlines()
        reason = "The release is complete."
        with patch.object(staff, "runtime_id", return_value=BOB.current_session):
            self.assertEqual(self.command("withdraw-ask", item.id, "--ask", ask.ask_id, "--reason", reason),
                             (0, f"withdrew {item.id} {ask.ask_id}\n", ""))
        saved = the_list.load(MORTY.id, item.id)
        closed = replace(ask, closed=START, withdrawn=True)
        self.assertEqual(saved.asks, (closed,))
        self.assertEqual(saved.log, (*before.log, the_list.ItemChange(START, the_list.AskWithdrawn(closed, reason), BOB.name)))
        self.assertEqual(path.read_bytes().split(b"## Log\n\n", 1)[1].splitlines()[:-1], earlier_log)

    def test_withdraw_ask_on_a_closed_item_keeps_it_closed_and_removes_its_go(self) -> None:
        item = self.add()
        ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge.",
                               why="The merge needs nate's go.", config=False, requester=MORTY.id, source="morty")
        closed = the_list.close(MORTY.id, item.id, "The merge is complete.", source="morty")
        self.assertEqual(the_list.open_asks(closed), (ask,))
        with patch.object(staff, "runtime_id", return_value=MORTY.current_session):
            self.assertEqual(self.command("withdraw-ask", item.id, "--ask", ask.ask_id, "--reason", "The merge is complete."),
                             (0, f"withdrew {item.id} {ask.ask_id}\n", ""))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual((saved.state, saved.closed, saved.closing_words),
                         (the_list.ItemState.CLOSED, closed.closed, closed.closing_words))
        self.assertEqual(the_list.open_asks(saved), ())
        reopened = the_list.reopen(MORTY.id, item.id, source="morty")
        self.assertEqual(the_list.answerable_asks(reopened, staff.snapshot()), ())

    def test_withdraw_ask_refuses_closed_unknown_and_empty_reason(self) -> None:
        item = self.add()
        ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge.",
                               why="The merge needs nate's go.", config=False, requester=MORTY.id, source="morty")
        closed = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge.",
                                  why="The merge needs nate's go.", config=False, requester=MORTY.id, source="morty")
        _ = the_list.give_go(MORTY.id, item.id, closed.ask_id, "0" * 32, source="nate")
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        before = path.read_bytes()
        with patch.object(staff, "runtime_id", return_value=MORTY.current_session):
            for ask_id, reason, expected in (
                (closed.ask_id, "The merge is complete.", f"{item.id} has no open ask {closed.ask_id}"),
                ("A3", "The merge is complete.", f"{item.id} has no open ask A3"),
                (ask.ask_id, "  ", "--reason must have one nonempty line"),
            ):
                with self.subTest(ask_id=ask_id):
                    self.assertEqual(self.command("withdraw-ask", item.id, "--ask", ask_id, "--reason", reason),
                                     (1, "", f"the_list: {expected}\n"))
                    self.assertEqual(path.read_bytes(), before)

    def test_go_keeps_other_unread_changes_unread(self) -> None:
        def go(item: the_list.ListItem) -> the_list.ListItem:
            ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge.",
                                   why="It needs nate.", config=False, requester=MORTY.id, source="morty")
            return the_list.give_go(MORTY.id, item.id, ask.ask_id, "0" * 32, source="nate").item

        def chip(item: the_list.ListItem) -> list_page.ListChip:
            return list_page.list_chip(item.state, parked=False, open_question=the_list.has_open_question(item),
                                       unread_delivery=the_list.unread_delivery(item),
                                       unread_answer=bool(the_list.unread_answer_ids(item)),
                                       open_ask=bool(the_list.answerable_asks(item, staff.snapshot())))
        read = go(the_list.mark_read(MORTY.id, self.add().id, source="morty"))
        self.assertNotIsInstance(read.unread_since, datetime)
        told = the_list.mark_read(MORTY.id, self.add().id, source="morty")
        _ = the_list.save_feedback(MORTY.id, told.id, the_list.PageFeedback("tell-1", "My own words."))
        told = go(the_list.load(MORTY.id, told.id))
        self.assertTrue(any(isinstance(change.what, the_list.PageFeedback) for change in told.log))
        self.assertNotIsInstance(told.unread_since, datetime)
        self.assertEqual(chip(told), list_page.ListChip.WORKING)
        answered = the_list.mark_read(MORTY.id, self.add().id, source="morty")
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, answered.id, the_list.PageRequest("ask", "question", "Ready?"), status)
        self.assertEqual(self.command("answer", answered.id, "--question", "Q1", "--text", "Ready."),
                         (0, f"answered {answered.id} Q1\n", ""))
        answered = go(the_list.load(MORTY.id, answered.id))
        self.assertEqual(the_list.open_asks(answered), ())
        self.assertIsInstance(answered.unread_since, datetime)
        self.assertEqual(the_list.unread_answer_ids(answered), frozenset({"Q1"}))
        self.assertEqual(chip(answered), list_page.ListChip.NEEDS_ME)
        updated = the_list.mark_read(MORTY.id, self.add().id, source="morty")
        _ = the_list.update(MORTY.id, updated.id, {"latest_status": "The gate passed."}, source="morty")
        updated = go(the_list.load(MORTY.id, updated.id))
        self.assertIsInstance(updated.unread_since, datetime)
        self.assertEqual(chip(updated), list_page.ListChip.WORKING)

    def read_with_go_asks(self, state: the_list.ItemState, *targets: staff.StaffId) -> tuple[the_list.ListItem, tuple[the_list.Ask, ...]]:
        """Add a read item in this state with one open Go ask for each target."""
        item = the_list.mark_read(MORTY.id, self.add(changes={"state": state, "producer": BOB.id}).id, source="morty")
        asks = tuple(the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=target, what="Merge.",
                                      why="It needs nate.", config=False, requester=MORTY.id, source="morty")
                     for target in targets)
        return the_list.load(MORTY.id, item.id), asks

    def test_go_keeps_a_waiting_item_waiting_while_another_ask_stays_open(self) -> None:
        item, (first, second) = self.read_with_go_asks(the_list.ItemState.WAITING_FOR_NATE, OWNER.id, OWNER.id)
        given = the_list.give_go(MORTY.id, item.id, first.ask_id, "0" * 32, source="nate").item
        self.assertEqual(given.state, the_list.ItemState.WAITING_FOR_NATE)
        self.assertEqual(the_list.answerable_asks(given, staff.snapshot()), (second,))
        self.assertIsInstance(given.unread_since, datetime)
        self.assertEqual(given.log[-1].what, the_list.GoGiven(given.asks[0]))
        self.assertEqual(the_list.feedback_requests(given), {})

    def test_go_keeps_the_state_of_an_item_in_another_state(self) -> None:
        for state in (the_list.ItemState.WORKING, the_list.ItemState.BLOCKED, the_list.ItemState.DELIVERED):
            with self.subTest(state=state.value):
                item, (ask,) = self.read_with_go_asks(state, OWNER.id)
                given = the_list.give_go(MORTY.id, item.id, ask.ask_id, "0" * 32, source="nate").item
                self.assertEqual(given.state, state)
                self.assertNotIsInstance(given.unread_since, datetime)
                self.assertEqual(given.log[-1].what, the_list.GoGiven(given.asks[0]))
                self.assertEqual(the_list.feedback_requests(given), {})

    def test_a_go_after_a_go_that_set_working_leaves_the_item_read(self) -> None:
        item, (first,) = self.read_with_go_asks(the_list.ItemState.WAITING_FOR_NATE, OWNER.id)
        working = the_list.give_go(MORTY.id, item.id, first.ask_id, "0" * 32, source="nate").item
        self.assertEqual((working.state, working.unread_since), (the_list.ItemState.WORKING, the_list.NoTimestamp.UNSET))
        self.assertEqual([type(change.what) for change in working.log[len(item.log):]],
                         [the_list.GoGiven, the_list.FieldChange, the_list.MarkedRead, the_list.PageFeedback])
        second = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Publish.",
                                  why="It needs nate.", config=False, requester=MORTY.id, source="morty")
        self.assertIsInstance(the_list.load(MORTY.id, item.id).unread_since, datetime)
        again = the_list.give_go(MORTY.id, item.id, second.ask_id, "1" * 32, source="nate").item
        self.assertEqual((again.state, again.unread_since), (the_list.ItemState.WORKING, the_list.NoTimestamp.UNSET))
        chip = list_page.list_chip(again.state, parked=False, open_question=the_list.has_open_question(again),
                                   unread_delivery=the_list.unread_delivery(again),
                                   unread_answer=bool(the_list.unread_answer_ids(again)),
                                   open_ask=bool(the_list.answerable_asks(again, staff.snapshot())))
        self.assertEqual(chip, list_page.ListChip.WORKING)

    def test_go_clears_the_unread_ask_after_tell_moves_the_item(self) -> None:
        item, _ = self.read_with_go_asks(the_list.ItemState.WAITING_FOR_NATE)
        old_update = the_list.unread_update(item)
        ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge.",
                               why="It needs nate.", config=False, requester=MORTY.id, source="morty")
        before_tell = the_list.load(MORTY.id, item.id)
        _ = the_list.save_feedback(MORTY.id, item.id, the_list.PageFeedback("tell-1", "My own words."))
        told = the_list.mark_read(MORTY.id, item.id, source=the_list.PAGE_SOURCE, update=old_update)
        self.assertEqual(told.state, the_list.ItemState.WORKING)
        self.assertIsInstance(told.unread_since, datetime)
        self.assertEqual(the_list.unread_update(told), the_list.unread_update(before_tell))
        self.assertEqual(the_list.open_asks(told), (ask,))

        given = the_list.give_go(MORTY.id, item.id, ask.ask_id, "0" * 32, source="nate").item
        self.assertEqual(the_list.open_asks(given), ())
        self.assertEqual(given.unread_since, the_list.NoTimestamp.UNSET)

    def test_an_open_ask_whose_target_has_no_staff_row_does_not_hold_a_waiting_item(self) -> None:
        gone = staff.StaffId("gone-session")
        item, _ = self.read_with_go_asks(the_list.ItemState.WAITING_FOR_NATE, gone)
        self.assertEqual((len(the_list.open_asks(item)), the_list.answerable_asks(item, staff.snapshot())), (1, ()))
        self.assertIsInstance(item.unread_since, datetime)
        read = self.page_read(item).item
        self.assertEqual(read.state, the_list.ItemState.WORKING)
        self.assertEqual(the_list.feedback_requests(read)["read"].recipient, BOB.id)
        item, (ask, _) = self.read_with_go_asks(the_list.ItemState.WAITING_FOR_NATE, OWNER.id, gone)
        given = the_list.give_go(MORTY.id, item.id, ask.ask_id, "0" * 32, source="nate").item
        self.assertEqual((given.state, given.unread_since), (the_list.ItemState.WORKING, the_list.NoTimestamp.UNSET))
        self.assertEqual(len(the_list.open_asks(given)), 1)
        notice = the_list.feedback_requests(given)[the_list.go_next_step_request("0" * 32)]
        self.assertEqual((notice.recipient, notice.state), (BOB.id, "pending"))
        self.assertIn(f"update --id {item.id} --source bob --next-step", notice.words)

    def test_old_records_without_asks_load_without_writes(self) -> None:
        item = self.add()
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        self.assertNotIn("asks:", path.read_text())
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(the_list.load(MORTY.id, item.id).asks, ())
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge.",
                             why="It needs nate.", config=False, requester=MORTY.id, source="morty")
        self.assertIn("asks:", path.read_text())
        self.assertEqual(len(the_list.load(MORTY.id, item.id).asks), 1)

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
        self.assertEqual(the_list.feedback_notice_time(sent), START)
        self.assertIs(sent.unread_since, the_list.NoTimestamp.UNSET)
        again = the_list.mark_read(MORTY.id, item.id, source=the_list.PAGE_SOURCE)
        self.assertEqual(the_list.feedback_notice_time(again), START)
        with patch.object(the_list, "now", return_value=START + timedelta(minutes=1)):
            changed = the_list.update(MORTY.id, item.id, {"latest_status": "Morty acts."}, source="owner report")
        self.assertIs(the_list.feedback_notice_time(changed), the_list.NoTimestamp.UNSET)

    def test_list_reply_review_runs_before_the_write_lock(self) -> None:
        item = self.add()
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, item.id, the_list.PageRequest("request", "question", "Which check passed?"), status)
        before = the_list.load(MORTY.id, item.id)
        reply = "Step B passed."
        failure = review.RuleFailure("console-standard-5", "Step B")
        original_lock = record_store.NumberedRecords.locked
        with (patch.object(record_store.NumberedRecords, "locked", autospec=True, side_effect=original_lock) as locked,
              patch.object(review, "review_question") as reader):
            def reject(*_args: object, **_kwargs: object) -> review.QuestionReviewFailed:
                locked.assert_not_called()
                return review.QuestionReviewFailed(reply, (), (failure,))

            reader.side_effect = reject
            result = self.command("answer", item.id, "--question", "Q1", "--text", reply)
            self.assertEqual(result[0], 1)
            self.assertIn("console-standard-5: Step B", result[2])
            key = review.QuestionKey(f"reply/list/{MORTY.id}/{item.id}/Q1", "")
            reader.assert_called_once_with(key, reply, console_kind="reply")
            self.assertEqual(the_list.load(MORTY.id, item.id), before)

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

        cases = ((MORTY.id, False, OTHER), (MORTY.id, False, MORTY), (MORTY.id, True, MORTY),
                 (BOB.id, False, OTHER))
        for keeper, legacy_question, answerer in cases:
            with self.subTest(keeper=keeper, legacy_question=legacy_question, answerer=answerer.name):
                assigned = (self.add(changes={"producer": OTHER.id}) if keeper == MORTY.id
                            else self.legacy_item(OTHER, the_list.ItemState.WORKING))
                words = "Which check passed?"
                args = ("answer", assigned.id, "--question", "Q1", "--text", "The check passed.")
                if legacy_question:
                    self.write_legacy_question(assigned, question_record.Question("Q1", START.isoformat(), words))
                else:
                    with patch.object(session_pane, "session_place", return_value=HerdrPlace("native", "w1:p1")) as place:
                        delivery = the_list.ask_question(keeper, assigned.id,
                            the_list.PageRequest("ask", "question", words), status)
                    place.assert_called_once_with(OTHER.current_session)
                    if keeper != MORTY.id:
                        assert isinstance(delivery, change_page.MessageDelivery)
                        self.assertEqual(delivery.recipients, (answerer.id,))
                        _, _, *command = shlex.split(delivery.message.splitlines()[-1].removeprefix(
                            "Answer with this command: "))
                        self.assertEqual(command[2:4], ["--keeper", keeper])
                        args = tuple("The check passed." if value == "<answer>" else value for value in command)
                reassigned = the_list.update(keeper, assigned.id, {"producer": BOB.id}, source="morty")
                refused_producers = ((BOB, OTHER) if legacy_question else (BOB,)) if keeper == MORTY.id else ()
                for refused in refused_producers:
                    with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": refused.current_session}):
                        self.assertEqual(self.command(*args),
                            (1, "", "the_list: Only the question recipient or morty can answer.\n"))
                self.assertEqual(the_list.load(keeper, assigned.id), reassigned)
                morty_item = the_list.load(MORTY.id, assigned.id)
                with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": answerer.current_session}):
                    self.assertEqual(self.command(*args), (0, f"answered {assigned.id} Q1\n", ""))
                saved = the_list.load(keeper, assigned.id)
                self.assertEqual(saved.log[-1].source, answerer.name)
                self.assertEqual(saved.questions[0].recipient_id(keeper), keeper if legacy_question else OTHER.id)
                self.assertIsInstance(saved.questions[0].answer, question_record.Answer)
                if keeper != MORTY.id:
                    self.assertEqual(the_list.load(MORTY.id, assigned.id), morty_item)

    def test_legacy_keeper_and_saved_recipient_can_answer_without_morty(self) -> None:
        members = staff.snapshot()
        without_morty = replace(members, rows=tuple(row for row in members.rows if row.id != MORTY.id))
        status = change_page.DeliveryStatus(self.root / "reviews")
        for caller, route in ((BOB, ()), (OTHER, ("--keeper", BOB.id))):
            with self.subTest(caller=caller.name):
                item = self.legacy_item(OTHER, the_list.ItemState.WORKING)
                morty_item = the_list.load(MORTY.id, item.id)
                with patch.object(session_pane, "session_place", return_value=HerdrPlace("native", "w1:p1")):
                    _ = the_list.ask_question(BOB.id, item.id,
                        the_list.PageRequest("ask", "question", "Which check passed?"), status)
                with staff.supplied_snapshot(without_morty), patch.dict(
                        os.environ, {"CLAUDE_CODE_SESSION_ID": caller.current_session}):
                    self.assertEqual(self.command("answer", item.id, "--question", "Q1",
                                                  "--text", "The check passed.", *route),
                                     (0, f"answered {item.id} Q1\n", ""))
                saved = the_list.load(BOB.id, item.id)
                self.assertEqual(saved.questions[0].recipient_id(BOB.id), OTHER.id)
                answer = saved.questions[0].answer
                assert isinstance(answer, question_record.Answer)
                self.assertEqual(answer.text, "The check passed.")
                self.assertEqual(saved.log[-1].source, caller.name)
                self.assertEqual(the_list.load(MORTY.id, item.id), morty_item)

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

    def test_morty_can_select_the_keeper_when_list_ids_match(self) -> None:
        first = self.add(number=1)
        # Only morty's folder allocates numbers, so a legacy keeper's L1 is a copy of morty's record.
        copy_list_item(first.id, source_keeper=MORTY.id, destination_keeper=OTHER.id)
        second = the_list.load(OTHER.id, first.id)
        status = change_page.DeliveryStatus(self.root / "reviews")
        for keeper, item in ((MORTY.id, first), (OTHER.id, second)):
            _ = the_list.ask_question(keeper, item.id,
                                      the_list.PageRequest("ask", "question", "Which check passed?"), status)
        first_open = the_list.load(MORTY.id, first.id)
        self.assertEqual(self.command("answer", second.id, "--question", "Q1", "--text", "The second check.",
                                      "--keeper", OTHER.id),
                         (0, f"answered {second.id} Q1\n", ""))
        self.assertEqual(the_list.load(MORTY.id, first.id), first_open)
        saved = the_list.load(OTHER.id, second.id)
        answer = saved.questions[0].answer
        assert isinstance(answer, question_record.Answer)
        self.assertEqual(answer.text, "The second check.")
        self.assertEqual(saved.log[-1].source, MORTY.name)

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

    def close_with_legacy_question(self, item: the_list.ListItem) -> None:
        _ = the_list.close(MORTY.id, item.id, "The report is retired.", source="nate")
        self.write_legacy_question(item, question_record.Question("Q1", START.isoformat(), "Which check passed?"))

    def write_legacy_question(self, item: the_list.ListItem, question: question_record.Question) -> None:
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        field = json.dumps(json.dumps([question_record.stored(question)]))
        _ = path.write_text(path.read_text().replace("\n---\n", f"\nquestions: {field}\n---\n", 1))

    def test_close_archives_open_questions_without_requesting_an_answer(self) -> None:
        item = self.add()
        status = change_page.DeliveryStatus(self.root / "reviews")
        request = the_list.PageRequest("ask", "question", "Which check passed?")
        sent = the_list.ask_question(MORTY.id, item.id, request, status)
        self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "The page check."),
                         (0, f"answered {item.id} Q1\n", ""))
        pending = the_list.PageRequest("next", "question", "Which file changed?")
        claimed = the_list.ask_question(MORTY.id, item.id, pending, status)
        asked = the_list.load(MORTY.id, item.id)
        closed = the_list.close(MORTY.id, item.id, "The report is retired.", source="nate")
        self.assertEqual(the_list.load(MORTY.id, item.id), closed)
        self.assertEqual(closed.questions, asked.questions[:1])
        self.assertEqual(closed.log[-2].what, the_list.QuestionClosed(asked.questions[1]))
        self.assertEqual(the_list.close(MORTY.id, item.id, closed.closing_words, source="nate"), closed)
        self.assertEqual(self.command("answer", item.id, "--question", "Q2", "--text", "The report."),
                         (1, "", "the_list: Unknown question id.\n"))
        self.assertIsInstance(the_list.ask_question(MORTY.id, item.id, pending, status),
                              change_page.NoMessageDelivery)
        with self.assertRaisesRegex(question_record.AnswerRefused, "This item is Closed"):
            _ = the_list.ask_question(MORTY.id, item.id, replace(pending, request="unsaved"), status)
        assert isinstance(sent, change_page.MessageDelivery) and isinstance(claimed, change_page.PageQuestionSend)
        with patch.object(change_page, "send_question") as send:
            list_page.deliver(list_page.ListQuestionDelivery(sent, status))
            change_page.deliver(claimed, status)
            path = status.path(claimed.page, claimed.row)
            closed_attempt = change_page.read_question_attempt(path)
            self.assertIs(closed_attempt.state, delivery_state.QuestionSendState.UNKNOWN)
            self.assertIsInstance(closed_attempt.owner, delivery_state.NoQuestionSendOwner)
            self.assertEqual(closed_attempt.error, "")
            change_page.recover_page_question(status, path, closed_attempt)
            send.assert_not_called()
        _ = the_list.reopen(MORTY.id, item.id, source="nate")
        _ = the_list.ask_question(MORTY.id, item.id, replace(pending, request="new"), status)
        self.assertEqual([question.question_id for question in the_list.load(MORTY.id, item.id).questions],
                         ["Q1", "Q3"])

    def test_close_stops_question_retries_and_keeps_confirmed_acceptance(self) -> None:
        status = change_page.DeliveryStatus(self.root / "reviews")
        state = delivery_state.QuestionSendState
        cases: tuple[tuple[send_result.Outcome, delivery_state.QuestionSendState], ...] = (
            ("sent", state.SENT), ("failed", state.UNKNOWN), ("pending", state.UNKNOWN),
        )
        for outcome, expected in cases:
            with self.subTest(outcome=outcome):
                item = self.add()
                delivery = the_list.ask_question(MORTY.id, item.id,
                    the_list.PageRequest("ask", "question", "Which check passed?"), status)
                assert isinstance(delivery, change_page.PageQuestionSend)
                header = send_result.OutcomeHeader(outcome, "Test result", "")

                def close_during_send(*_args: object) -> tuple[send_result.ConfirmedSendOutcome, str]:
                    _ = the_list.close(MORTY.id, item.id, "The report is retired.", source="nate")
                    return send_result.ConfirmedSendOutcome(header), header.detail

                settings = Mock(whole=Mock(return_value=2), number=Mock(return_value=0))
                with (patch.object(conf, "read", return_value=settings),
                      patch.object(change_page, "send_question", side_effect=close_during_send) as send,
                      patch.object(change_page, "alert_delivery_failures") as alert):
                    change_page.deliver(delivery, status)
                send.assert_called_once()
                alert.assert_not_called()
                path = status.path(delivery.page, delivery.row)
                saved = change_page.read_question_attempt(path)
                self.assertEqual(saved.state, expected)
                self.assertIsInstance(saved.owner, delivery_state.NoQuestionSendOwner)
                self.assertFalse(saved.earlier_unconfirmed)
                self.assertEqual(saved.error, "")
                self.assertFalse(status.has_deferred_questions())

    def test_retry_read_error_preserves_the_confirmed_failure(self) -> None:
        status = change_page.DeliveryStatus(self.root / "reviews")
        header = send_result.OutcomeHeader("failed", "The transport refused delivery.", "")
        settings = Mock(whole=Mock(return_value=2), number=Mock(return_value=0))
        for error in (PermissionError("The List file cannot be read."), ValueError("Invalid List record.")):
            with self.subTest(error=type(error).__name__):
                item = self.add()
                delivery = the_list.ask_question(MORTY.id, item.id,
                    the_list.PageRequest("ask", "question", "Which check passed?"), status)
                assert isinstance(delivery, change_page.PageQuestionSend)
                listing = record_store.NumberedRecords(MORTY.id, "list", "L", "list item", ValueError)
                fields = listing.read(item.id)
                with (patch.object(conf, "read", return_value=settings),
                      patch.object(record_store.NumberedRecords, "read", side_effect=[fields, error]),
                      patch.object(change_page, "send_question",
                                   return_value=(send_result.ConfirmedSendOutcome(header), header.detail)) as send,
                      patch.object(change_page, "alert_delivery_failures") as alert,
                      redirect_stderr(io.StringIO()) as log):
                    change_page.deliver(delivery, status)
                saved = change_page.read_question_attempt(status.path(delivery.page, delivery.row))
                self.assertEqual(saved.state, delivery_state.QuestionSendState.FAILED)
                self.assertEqual(saved.error, header.detail)
                self.assertIn(str(error), log.getvalue())
                send.assert_called_once()
                alert.assert_not_called()

    def test_question_recovery_skips_a_legacy_closed_item(self) -> None:
        item = self.add()
        status = change_page.DeliveryStatus(self.root / "reviews")
        delivery = the_list.ask_question(MORTY.id, item.id,
            the_list.PageRequest("ask", "question", "Which check passed?"), status)
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        question, = the_list.load(MORTY.id, item.id).questions
        _ = the_list.close(MORTY.id, item.id, "The report is retired.", source="nate")
        self.write_legacy_question(item, question)
        delivery.job.release()
        path = status.path(delivery.page, delivery.row)
        with patch.object(change_page, "send_question") as send:
            change_page.recover_page_question(status, path, change_page.read_question_attempt(path))
        send.assert_not_called()
        self.assertEqual(the_list.load(MORTY.id, item.id).questions, (question,))

    def test_answer_marks_a_legacy_closed_item_unread_until_mark_read(self) -> None:
        item = self.add()
        self.close_with_legacy_question(item)
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
        self.close_with_legacy_question(item)
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
        gap = datetime(2026, 3, 8, 2, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
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
            ("schedule", "Invalid", {"start": START.isoformat(), "do": ""}),
            ("schedule", the_list.ScheduledRun(gap, "Check the figures."),
             {"start": "2026-03-08T02:30:00-08:00[America/Los_Angeles]", "do": "Check the figures."}),
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
                elif isinstance(native, the_list.ScheduledRun):
                    with self.assertRaisesRegex(the_list.ListError, "start does not exist"):
                        _ = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.SCHEDULED, "schedule": native},
                                            source="morty")
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
        ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.APPROVAL, target=OWNER.id, what="Publish.",
                               why="It needs nate.", config=True, requester=MORTY.id, source="morty")
        check("Ask opened")
        _ = the_list.give_go(MORTY.id, item.id, ask.ask_id, "0" * 32, source="nate")
        check("Go given")
        _ = the_list.log_go_not_delivered(MORTY.id, item.id, "0" * 32, OWNER.id)
        check("Go not delivered")

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

    def test_mark_read_keeps_the_answer_read_after_the_clock_moves_back(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WORKING})
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, item.id, the_list.PageRequest("ask", "question", "Is it ready?"), status)
        with patch.object(the_list, "now", return_value=START + timedelta(minutes=1)):
            self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "Yes.")[0], 0)
        self.assertEqual(the_list.unread_answer_ids(the_list.load(MORTY.id, item.id)), {"Q1"})

        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "The next check passed."}, source="morty")
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.unread_since, START)
        self.assertEqual(the_list.unread_answer_ids(saved), set())

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

    def page_read(self, item: the_list.ListItem, request: str = "read") -> the_list.ListEdit:
        return the_list.mark_read_edit(MORTY.id, item.id, source=the_list.PAGE_SOURCE,
                                       update=the_list.ReadCurrentUpdate.CURRENT,
                                       page_request=the_list.PageRequest(request, "read", ""))

    def test_page_mark_read_moves_a_waiting_item_to_working_and_asks_its_producer_for_the_next_step(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WAITING_FOR_NATE, "producer": BOB.id})
        read = self.page_read(item).item
        self.assertEqual(read.state, the_list.ItemState.WORKING)
        self.assertIs(read.unread_since, the_list.NoTimestamp.UNSET)
        self.assertEqual([entry.what for entry in read.log[len(item.log):-1]],
                         [the_list.FieldChange("Updated", {"state": the_list.ItemState.WORKING}), the_list.MarkedRead()])
        notices = the_list.feedback_requests(read)
        self.assertEqual(list(notices), ["read"])
        notice = notices["read"]
        self.assertEqual((notice.action, notice.state), ("read", "pending"))
        self.assertEqual(notice.words.count("\n"), 0)
        self.assertIn(f"Write its next step: `~/.claude/scripts/lib/py ~/.claude/scripts/production/the_list.py "
                      + f"update --id {item.id} --source bob --next-step", notice.words)
        self.assertEqual((notice.recipient, notice.recipient_id(MORTY.id)), (BOB.id, BOB.id))
        self.assertEqual(the_list.load(MORTY.id, item.id), read)
        retry = self.page_read(item)
        self.assertIsInstance(retry, the_list.UnchangedEdit)
        self.assertEqual(the_list.load(MORTY.id, item.id), read)

    def test_page_mark_read_notice_command_writes_the_next_step_for_each_producer(self) -> None:
        for producer in (MORTY, BOB):
            with self.subTest(producer=producer.name):
                item = self.add(changes={"state": the_list.ItemState.WAITING_FOR_NATE, "producer": producer.id})
                notice = the_list.feedback_requests(self.page_read(item).item)["read"]
                launcher, script, *args = shlex.split(notice.words.split("`")[1].replace("<step>", "Check the page."))
                self.assertEqual((launcher, script), ("~/.claude/scripts/lib/py",
                                                      "~/.claude/scripts/production/the_list.py"))
                with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": producer.current_session}):
                    self.assertEqual(self.command(*args)[0], 0)
                self.assertEqual(the_list.load(MORTY.id, item.id).next_step, "Check the page.")

    def legacy_item(self, producer: staff.StaffRow, state: the_list.ItemState) -> the_list.ListItem:
        """Add an item to morty's folder and copy it into bob's legacy folder."""
        # Only morty's folder allocates numbers, so morty keeps an item with the same number.
        item = self.add(changes={"state": state, "producer": producer.id})
        copy_list_item(item.id, source_keeper=MORTY.id, destination_keeper=BOB.id)
        return the_list.load(BOB.id, item.id)

    def test_page_mark_read_notice_command_names_a_legacy_keeper(self) -> None:
        # bob is the legacy keeper. bob or dwight produces the item in bob's folder.
        for producer in (BOB, OTHER):
            with self.subTest(producer=producer.name):
                item = self.legacy_item(producer, the_list.ItemState.WAITING_FOR_NATE)
                morty_item = the_list.load(MORTY.id, item.id)
                read = the_list.mark_read_edit(BOB.id, item.id, source=the_list.PAGE_SOURCE,
                                               update=the_list.ReadCurrentUpdate.CURRENT,
                                               page_request=the_list.PageRequest("read", "read", "")).item
                self.assertEqual(read.state, the_list.ItemState.WORKING)
                notice = the_list.feedback_requests(read)["read"]
                self.assertEqual(notice.recipient, producer.id)
                self.assertIn(f"update --id {item.id} --keeper {BOB.id} --source {producer.name} --next-step",
                              notice.words)
                _, _, *args = shlex.split(notice.words.split("`")[1].replace("<step>", "Check the page."))
                with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": producer.current_session}):
                    self.assertEqual(self.command(*args), (0, "", ""))
                self.assertEqual(the_list.load(BOB.id, item.id).next_step, "Check the page.")
                self.assertEqual(the_list.load(MORTY.id, item.id), morty_item)

    def test_update_keeper_refuses_an_item_that_the_producer_does_not_produce(self) -> None:
        item = self.legacy_item(BOB, the_list.ItemState.WORKING)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": OTHER.current_session}):
            self.assertEqual(self.command("update", "--id", item.id, "--keeper", BOB.id, "--next-step", "Check it."),
                             (1, "", f"the_list: {item.id} is assigned to another producer. "
                              + "A producer can change only its own items.\n"))
        self.assertEqual(the_list.load(BOB.id, item.id), item)
        self.assertEqual(self.command("update", "--id", item.id, "--keeper", BOB.id, "--next-step", "Check it.",
                                      "--source", "morty"), (0, "", ""))
        self.assertEqual(the_list.load(BOB.id, item.id).next_step, "Check it.")
        self.assertEqual(the_list.load(MORTY.id, item.id).next_step, item.next_step)

    def test_update_refuses_a_new_producer_or_owner_in_a_legacy_folder(self) -> None:
        item = self.legacy_item(BOB, the_list.ItemState.WORKING)
        refusal = (1, "", "the_list: move the item to morty's List before you change its producer or owner\n")
        for options in (("--producer", "dwight"), ("--owner", RUNNER.id)):
            with self.subTest(options=options):
                self.assertEqual(self.command("update", "--id", item.id, "--keeper", BOB.id, *options,
                                              "--source", "morty"), refusal)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": BOB.current_session}):
            self.assertEqual(self.command("update", "--id", item.id, "--keeper", BOB.id, "--owner", RUNNER.id),
                             refusal)
            status, output, errors = self.command("update", "--id", item.id, "--keeper", BOB.id, "--producer", "dwight")
        self.assertEqual((status, output), (1, ""))
        self.assertIn("producers can update only", errors)
        self.assertIn("Refused options: --producer", errors)
        self.assertEqual(the_list.load(BOB.id, item.id), item)
        self.assertEqual(self.notices, [])

    def test_update_without_keeper_edits_mortys_item(self) -> None:
        # bob's own legacy folder holds a copy with the same number, and update still edits morty's item.
        item = self.legacy_item(BOB, the_list.ItemState.WORKING)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": BOB.current_session}):
            self.assertEqual(self.command("update", "--id", item.id, "--next-step", "Check it."), (0, "", ""))
            self.assertEqual(self.command("update", "--id", item.id, "--unit", "example"), (0, "", ""))
            updated = the_list.load(MORTY.id, item.id)
            self.assertEqual(updated.next_step, "Check it.")
            self.assertEqual(updated.unit, "example")
            self.assertEqual(self.command("update", "--id", item.id, "--owner", "nate"), (0, "", ""))
        self.assertEqual(the_list.load(MORTY.id, item.id).owner, the_list.UnassignedOwner.NATE)
        self.assertEqual(the_list.load(BOB.id, item.id), item)

    def test_page_mark_read_notice_for_mortys_item_skips_the_producers_legacy_copy(self) -> None:
        item = self.legacy_item(BOB, the_list.ItemState.WAITING_FOR_NATE)
        read = self.page_read(the_list.load(MORTY.id, item.id)).item
        notice = the_list.feedback_requests(read)["read"]
        self.assertIn(f"update --id {item.id} --source bob --next-step", notice.words)
        _, _, *args = shlex.split(notice.words.split("`")[1].replace("<step>", "Check the page."))
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": BOB.current_session}):
            self.assertEqual(self.command(*args), (0, "", ""))
        self.assertEqual(the_list.load(MORTY.id, item.id).next_step, "Check the page.")
        self.assertEqual(the_list.load(BOB.id, item.id), item)

    def test_keeper_without_a_list_folder_is_refused_and_creates_no_folder(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        missing = staff.StaffId("no-such-keeper")
        cases = ((OTHER, ("update", "--id", item.id, "--keeper", missing, "--next-step", "Check it."), "update"),
                 (MORTY, ("answer", item.id, "--keeper", missing, "--question", "Q1", "--text", "Done."), "answer"))
        for caller, args, name in cases:
            with (self.subTest(command=name),
                  patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": caller.current_session})):
                self.assertEqual(self.command(*args), (1, "", f"the_list: no List folder for keeper {missing}\n"))
                self.assertFalse(the_list.folder(missing).parent.exists())

    def test_page_mark_read_keeps_a_waiting_item_with_an_open_question(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WAITING_FOR_NATE})
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, item.id, the_list.PageRequest("ask", "question", "Ready?"), status)
        asked = the_list.load(MORTY.id, item.id)
        self.assertTrue(the_list.has_open_question(asked))
        read = self.page_read(asked).item
        self.assertEqual(read.state, the_list.ItemState.WAITING_FOR_NATE)
        self.assertEqual(read.log[-1].what, the_list.MarkedRead())
        self.assertEqual(the_list.feedback_requests(read), {})

    def test_page_mark_read_keeps_a_waiting_item_with_an_open_ask(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WAITING_FOR_NATE})
        _ = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge.",
                             why="It needs nate.", config=False, requester=MORTY.id, source="morty")
        asked = the_list.load(MORTY.id, item.id)
        self.assertTrue(the_list.open_asks(asked))
        read = self.page_read(asked).item
        self.assertEqual(read.state, the_list.ItemState.WAITING_FOR_NATE)
        self.assertEqual(read.log[-1].what, the_list.MarkedRead())
        self.assertEqual(the_list.feedback_requests(read), {})
        self.assertEqual(read.asks, asked.asks)

    def test_mark_read_leaves_the_state_of_other_items_unchanged(self) -> None:
        cases = (("working page read", the_list.ItemState.WORKING, False, True),
                 ("delivered page read", the_list.ItemState.DELIVERED, False, True),
                 ("parked waiting page read", the_list.ItemState.WAITING_FOR_NATE, True, True),
                 ("waiting read outside the page", the_list.ItemState.WAITING_FOR_NATE, False, False))
        for name, state, parked, page in cases:
            with self.subTest(name):
                item = self.add(changes={"state": state})
                if parked:
                    item = the_list.park(MORTY.id, item.id, START + timedelta(days=1), source="nate")
                read = (self.page_read(item).item if page
                        else the_list.mark_read(MORTY.id, item.id, source=the_list.PAGE_SOURCE))
                self.assertEqual(read.state, state)
                self.assertIs(read.unread_since, the_list.NoTimestamp.UNSET)
                self.assertEqual(read.log[-1].what, the_list.MarkedRead())
                self.assertEqual(the_list.feedback_requests(read), {})
                if page:
                    receipt = the_list.load(MORTY.id, item.id).log[-2].what
                    self.assertEqual(receipt, the_list.PageRequest("read", "read", ""))

    def test_page_item_actions_save_the_notice_in_the_same_write(self) -> None:
        cases: tuple[tuple[Literal["remove", "park"], the_list.Parking], ...] = (
            ("remove", the_list.NoTimestamp.UNSET),
            ("remove", the_list.ParkUntilDelivered.DELIVERED),
            ("park", the_list.NoTimestamp.UNSET),
        )
        for action, parking in cases:
            with self.subTest(action=action, parking=parking):
                item = self.add("One write for " + action)
                if parking is not the_list.NoTimestamp.UNSET:
                    _ = the_list.park(MORTY.id, item.id, parking, source="nate")
                notice = the_list.PageItemNotice("request-" + action, "The item action is complete.", action)
                with patch.object(record_store, "write_text_atomic", wraps=atomic_file.write_text_atomic) as save:
                    if action == "remove":
                        saved = the_list.close(MORTY.id, item.id, "Removed by nate.", source=the_list.PAGE_SOURCE, notice=notice)
                    else:
                        saved = the_list.park(MORTY.id, item.id, START + timedelta(days=1), source=the_list.PAGE_SOURCE, notice=notice)
                save.assert_called_once()
                if action == "remove":
                    self.assertIs(the_list.load(MORTY.id, item.id).parked_until, the_list.NoTimestamp.UNSET)
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
        item = self.add(changes={"state": the_list.ItemState.WORKING, "producer": BOB.id})
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
        _ = the_list.park(MORTY.id, item.id, the_list.ParkUntilDelivered.DELIVERED, source="morty")
        self.assertEqual(self.command("unpark", "--id", item.id, "--source", "morty"), (0, "", ""))
        self.assertIn(item.title, self.command("list")[1])

    def test_park_until_delivered_through_the_cli(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WORKING})
        self.assertEqual(self.command("park", "--id", item.id, "--until", "delivered", "--source", "morty"),
                         (0, "", ""))
        parked = the_list.load(MORTY.id, item.id)
        self.assertIs(parked.parked_until, the_list.ParkUntilDelivered.DELIVERED)
        self.assertEqual(parked.log[-1].what, the_list.FieldChange("Parked", {"parked_until": parked.parked_until}))
        with patch.object(the_list, "now", return_value=START + timedelta(days=30)):
            self.assertNotIn(item.title, self.command("list")[1])
            self.assertIn("parked until delivered", self.command("list", "--include-parked")[1])
            self.assertEqual(self.command("update", "--id", item.id, "--state", "Delivered", "--source", "morty"),
                             (0, "", ""))
            delivered = the_list.load(MORTY.id, item.id)
            self.assertIs(delivered.state, the_list.ItemState.DELIVERED)
            self.assertIs(delivered.parked_until, the_list.NoTimestamp.UNSET)
            self.assertEqual(delivered.unread_since, START + timedelta(days=30))
            self.assertEqual(delivered.log[-1].what, the_list.UnparkedForDelivery(the_list.ItemDelivered.RECORDED))
            self.assertIn(item.title, self.command("list")[1])
            self.assertEqual(self.command("update", "--id", item.id, "--state", "Delivered", "--source", "morty"),
                             (0, "", ""))
            self.assertEqual(the_list.load(MORTY.id, item.id), delivered)

    def test_parking_reads_the_old_answer_and_a_new_answer_returns_to_needs_me(self) -> None:
        status = change_page.DeliveryStatus(self.root / "reviews")

        def answer(item_id: str, question_id: str, moment: datetime) -> None:
            with patch.object(the_list, "now", return_value=moment):
                request = the_list.PageRequest(f"ask-{question_id}", "question", "Is it ready?")
                _ = the_list.ask_question(MORTY.id, item_id, request, status)
                self.assertEqual(self.command("answer", item_id, "--question", question_id, "--text", "Yes.")[0], 0)

        def check(item_id: str, answers: set[str], chip: list_page.ListChip) -> None:
            saved = the_list.load(MORTY.id, item_id)
            unread = the_list.unread_answer_ids(saved)
            self.assertEqual(unread, answers)
            self.assertIs(list_page.list_chip(saved.state, parked=the_list.parking_active(saved.parked_until, START),
                                              open_question=the_list.has_open_question(saved),
                                              unread_answer=bool(unread)), chip)

        for until in (the_list.ParkUntilDelivered.DELIVERED, START + timedelta(days=1)):
            with self.subTest(until=until):
                item = self.add(changes={"state": the_list.ItemState.WORKING})
                answer(item.id, "Q1", START)
                check(item.id, {"Q1"}, list_page.ListChip.NEEDS_ME)
                with patch.object(the_list, "now", return_value=START + timedelta(minutes=1)):
                    _ = the_list.park(MORTY.id, item.id, until, source="nate")
                check(item.id, set(), list_page.ListChip.PARKED)
                answer(item.id, "Q2", START + timedelta(minutes=2))
                check(item.id, {"Q2"}, list_page.ListChip.NEEDS_ME)

    def test_legacy_park_receipt_reads_an_older_answer(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WORKING})
        until = START + timedelta(days=1)
        _ = the_list.park(MORTY.id, item.id, until, source="nate")
        status = change_page.DeliveryStatus(self.root / "reviews")
        _ = the_list.ask_question(MORTY.id, item.id, the_list.PageRequest("ask", "question", "Is it ready?"), status)
        with patch.object(the_list, "now", return_value=START + timedelta(minutes=1)):
            self.assertEqual(self.command("answer", item.id, "--question", "Q1", "--text", "Yes.")[0], 0)
        self.assertEqual(the_list.unread_answer_ids(the_list.load(MORTY.id, item.id)), {"Q1"})

        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        receipt = {"time": (START + timedelta(minutes=2)).isoformat(), "source": the_list.PAGE_SOURCE,
                   "receipt": {"request": "park-again", "action": "park", "words": "Parked until tomorrow."}}
        with path.open("a", encoding="utf-8") as log:
            _ = log.write(f"\n- {json.dumps(receipt)}\n")
        saved = the_list.load(MORTY.id, item.id)
        self.assertTrue(the_list.has_park_receipt(saved, "park-again"))
        self.assertEqual(saved.parked_until, until)
        self.assertEqual(the_list.unread_answer_ids(saved), set())

    def test_delivery_does_not_clear_a_saved_date(self) -> None:
        item = self.add(changes={"state": the_list.ItemState.WORKING})
        date = "2026-11-17"
        zone = ZoneInfo("America/New_York")
        with patch.object(the_list, "user_zone", return_value=zone):
            moment = datetime(2026, 11, 17, tzinfo=zone)
            parked = the_list.park(MORTY.id, item.id, moment, source="morty")
            path = the_list.folder(MORTY.id) / f"{item.id}.md"
            encoded = path.read_text()
            old = encoded.replace(moment.isoformat(), date)
            self.assertNotEqual(encoded, old)
            _ = path.write_text(old)
            self.assertEqual(the_list.load(MORTY.id, item.id), parked)
            delivered = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.DELIVERED}, source="morty")
            self.assertEqual(delivered.parked_until, moment)
            self.assertNotIn(item.title, self.command("list")[1])
            self.assertFalse(any(isinstance(entry.what, the_list.Unparked) for entry in delivered.log))

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
        for next_number, data in enumerate((b"bad", b"\xff"), start=3):
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
                self.assertEqual(self.add().number, next_number)
                self.assertEqual(path.read_bytes(), data)
                (path.parent / f"L{next_number}.md").unlink()

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

    def test_create_records_refuses_an_absent_producer_on_a_closed_item(self) -> None:
        item = self.add(changes={"producer": OTHER.id})
        closed = the_list.close(MORTY.id, item.id, "Done", source="fixture")
        _ = staff.record_path().write_text(staff.StaffSnapshot((MORTY, RUNNER)).encode(), encoding="utf-8")
        with patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.root / "migration")}):
            with self.assertRaisesRegex(the_list.ListError, f"{OTHER.id}:.*no current producer staff row"):
                the_list.create_records(MORTY.id, [closed])
            self.assertEqual(the_list.records(MORTY.id, include_closed=True), [])

    def test_create_records_validates_and_reserves_mortys_batch(self) -> None:
        item = self.add()
        self.enterContext(patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(self.root / "migration")}))
        typed = replace(item, number=50, owner=the_list.UnassignedOwner.NATE,
                        opened=the_list.NoTimestamp.UNSET, updated=the_list.NoTimestamp.UNSET,
                        eta=the_list.EtaDescription("About November 19; approval required"),
                        next_check=the_list.CheckCondition("After the owner supplies proof"))
        dependent = replace(typed, number=51, owner=the_list.UnassignedOwner.NOT_RECORDED,
                            related_to=(50,), blocked_by=(the_list.ItemDependency(50, "Proof is available"),))
        with self.assertRaisesRegex(the_list.ListError, "only morty's List folder"):
            the_list.create_records(OTHER.id, [dependent, typed])
        self.assertFalse(the_list.folder(OTHER.id).exists())
        with self.assertRaisesRegex(the_list.ListError, "only morty's List folder"):
            _ = the_list.add(OTHER.id, "Wrong keeper", owner=RUNNER.id, source="fixture", changes={})
        self.assertFalse(the_list.folder(OTHER.id).exists())
        with self.assertRaises(the_list.ListError):
            the_list.create_records(MORTY.id, [typed, replace(dependent, related_to=(999,))])
        self.assertEqual(the_list.records(MORTY.id), [])
        with self.assertRaisesRegex(the_list.ListError, "item numbers must be unique"):
            the_list.create_records(MORTY.id, [typed, typed])
        self.assertEqual(the_list.records(MORTY.id), [])
        _ = staff.set_producer_retirement(OTHER.id, staff.RetiredProducer("claude:sonnet:high"))
        with self.assertRaisesRegex(the_list.ListError, f"{OTHER.name}: retired producer"):
            the_list.create_records(MORTY.id, [typed, replace(dependent, producer=OTHER.id)])
        self.assertEqual(the_list.records(MORTY.id), [])
        the_list.create_records(MORTY.id, [dependent, typed])
        self.assertEqual(the_list.records(MORTY.id), [typed, dependent])
        self.assertEqual(self.add().number, 52)
        with self.assertRaisesRegex(the_list.ListError, "the next available number is L53"):
            the_list.create_records(MORTY.id, [replace(typed, number=49)])
        self.assertFalse((the_list.folder(MORTY.id) / "L49.md").exists())
        text = the_list.table(MORTY.id)
        self.assertIn("| nate |", text)
        self.assertIn("| Not recorded |", text)
        self.assertIn("About November 19; approval required", text)
        self.assertIn("check: After the owner supplies proof", text)
        for text_eta in ("2026-11-20/2026-11-19", "2026-11-99/2026-11-19", "Paused"):
            with self.subTest(text_eta=text_eta):
                changed = the_list.update(MORTY.id, "L50", {"eta": the_list.EtaDescription(text_eta)}, source="morty")
                self.assertEqual(the_list.load(MORTY.id, "L50"), changed)
                self.assertIn('eta_kind: "text"', (the_list.folder(MORTY.id) / "L50.md").read_text())
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
