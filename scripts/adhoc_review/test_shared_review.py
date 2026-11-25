"""Test shared reviews with private files and session registry results."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from threading import Barrier
from typing import cast, final, override
from unittest.mock import patch
from uuid import uuid4

from scripts.adhoc_review import shared_review
from scripts.copies.copies_test_support import copies_environment
from scripts.lib import scratch
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message import sessions
from scripts.production import staff


OWNER = "10000000-0000-4000-8000-000000000001"
CLONE = "10000000-0000-4000-8000-000000000002"
OTHER = "10000000-0000-4000-8000-000000000003"
PIDS = {OWNER: 123, CLONE: 124, OTHER: 125}


@final
class SharedReviewTests(unittest.TestCase):
    home: Path = Path()
    liveness: dict[str, shared_review.SessionLiveness] = {}
    processes: dict[int, shared_review.SessionLiveness] = {}

    @override
    def setUp(self) -> None:
        fixture = Path(tempfile.gettempdir()) / f"adhoc-review-tests-{uuid4().hex}"
        self.addCleanup(shutil.rmtree, fixture, ignore_errors=True)
        environment = copies_environment(fixture)
        with (patch.dict(os.environ, {**environment, "SESSION_TEMP_ROOT": str(fixture / "session"),
                                     "SESSION_FORMER_ROOT": str(fixture / "former"),
                                     "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": ""}),
              patch.object(scratch, "maker", return_value="")):
            self.home = scratch.new("gate:adhoc-review-test", "review")
        self.liveness = {
            identity: sessions.LiveSessionProcess(pid) for identity, pid in PIDS.items()
        }
        self.processes = {pid: sessions.LiveSessionProcess(pid) for pid in PIDS.values()}
        named_owner = staff.StaffRow(staff.StaffId(OWNER), "showrunner", "bob", staff.StaffRoot(),
                                     current_session=staff.StaffId(OWNER))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)})))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((named_owner,))))
        _ = self.enterContext(patch.object(sessions, "read_registry", return_value=sessions.RegistryRead((), ())))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.observe_registry))
        _ = self.enterContext(patch.object(sessions, "process_start", side_effect=self.process_start))
        _ = self.enterContext(patch.object(sessions, "process_liveness", side_effect=self.observe_process))

    def process_start(self, pid: int) -> str:
        self.assertIn(pid, PIDS.values())
        return f"start-{pid}"

    def observe_process(self, pid: int, started: str) -> shared_review.SessionLiveness:
        self.assertEqual(started, self.process_start(pid))
        return self.processes[pid]

    def observe(self, session: str) -> shared_review.SessionLiveness:
        return self.liveness[session]

    def observe_registry(self, session: str, registry: sessions.RegistryRead) -> shared_review.SessionLiveness:
        self.assertEqual(registry, sessions.RegistryRead((), ()))
        return self.observe(session)

    def cli(self, actor: str, *args: str, expected: int = 0) -> str:
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": actor}),
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as error):
            self.assertEqual(shared_review.main(list(args)), expected, error.getvalue())
        return output.getvalue() if expected == 0 else error.getvalue()

    def start(self, *items: str) -> shared_review.ReviewHandle:
        path = self.home / "items.json"
        _ = path.write_text(json.dumps(items), encoding="utf-8")
        return cast(shared_review.ReviewHandle, json.loads(self.cli(OWNER, "start", "--source", "Fixture", "--items", str(path))))

    def test_simultaneous_claims_take_different_items(self) -> None:
        review = self.start("first", "second")
        _ = self.cli(CLONE, "join", "bob")
        root = Path(review["document"]).parent
        ready = Barrier(2, timeout=TEST_HANG_GUARD_SECS)

        def claim(actor: str) -> shared_review.ClaimedItem | shared_review.NoAvailableItem:
            store = shared_review.ReviewStore(root, self.observe)
            _ = ready.wait()
            return store.claim(actor, review["owner"], review["review"], shared_review.NextItem())

        with ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(claim, actor) for actor in (OWNER, CLONE)]
            results = [call.result(timeout=TEST_HANG_GUARD_SECS) for call in calls]

        self.assertCountEqual([row["item"] for row in results if row["state"] == "claimed"], [1, 2])
        for actor, result in zip((OWNER, CLONE), results, strict=True):
            verb = "claim" if actor == OWNER else "next"
            self.assertEqual(json.loads(self.cli(actor, verb, OWNER, review["review"])), result)

    def test_join_by_staff_name_reads_both_sessions_decisions_from_one_document(self) -> None:
        review = self.start("First choice\nDetails", "Second choice")
        handle = (review["owner"], review["review"])
        self.assertEqual(json.loads(self.cli(CLONE, "join", "bob")), review)
        self.assertEqual(json.loads(self.cli(OWNER, "resume")), review)
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["text"], "First choice\nDetails")
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 2)
        self.assertIn("current claim", self.cli(CLONE, "record", *handle, "1", "Wrong item", expected=2))
        _ = self.cli(CLONE, "record", *handle, "2", "Defer")
        _ = self.cli(OWNER, "decide", *handle, "1", "Accept", "--actions", "Pending: update the plan")
        document = self.cli(CLONE, "show", *handle)
        self.assertEqual(document, self.cli(OWNER, "show", *handle))
        self.assertIn("1. First choice → Accept\n   Pending: update the plan", document)
        self.assertIn("2. Second choice → Defer", document)
        self.assertTrue(Path(review["document"]).read_text().endswith(document))
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle)), {"state": "complete", "remaining": 0})

    def test_next_keeps_a_live_claim_missing_from_the_registry(self) -> None:
        review = self.start("first")
        handle = (review["owner"], review["review"])
        _ = self.cli(CLONE, "join", "bob")
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 1)
        self.liveness[CLONE] = sessions.SessionLivenessUnknown("fixture registry is unreadable")
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle)), {"state": "busy", "remaining": 1})
        self.liveness[CLONE] = sessions.SessionNotLive()
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle)), {"state": "busy", "remaining": 1})
        self.processes[PIDS[CLONE]] = sessions.SessionNotLive()
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["item"], 1)
        self.assertIn("current claim", self.cli(CLONE, "record", *handle, "1", "Late decision", expected=2))

    def test_leave_releases_the_item_and_owner_end_invalidates_the_review_handle(self) -> None:
        review = self.start("first")
        handle = (review["owner"], review["review"])
        _ = self.cli(CLONE, "join", "bob")
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 1)
        self.assertIn("Only the owner", self.cli(CLONE, "end", *handle, expected=2))
        _ = self.cli(CLONE, "leave", *handle)
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["item"], 1)
        _ = self.cli(OWNER, "decide", *handle, "1", "Accept")
        self.assertIn("1. first → Accept", self.cli(OWNER, "end", *handle))
        self.assertFalse(Path(review["document"]).exists())
        self.assertIn("review has ended", self.cli(CLONE, "next", *handle, expected=2))
        replacement = self.start("replacement")
        self.assertNotEqual(replacement["review"], review["review"])
        self.assertIn("review has ended", self.cli(CLONE, "next", *handle, expected=2))

    def test_revisit_releases_the_old_claim_and_keeps_the_decision_until_recorded(self) -> None:
        review = self.start("first")
        handle = (review["owner"], review["review"])
        _ = self.cli(OWNER, "claim", *handle)
        _ = self.cli(OWNER, "decide", *handle, "1", "Accept")
        _ = self.cli(CLONE, "join", "bob")
        _ = self.cli(CLONE, "add", *handle, "second")
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["item"], 2)
        revisit = cast(shared_review.ClaimedItem, json.loads(self.cli(OWNER, "claim", *handle, "--item", "1")))
        self.assertEqual((revisit["item"], revisit["remaining"]), (1, 2))
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 2)
        self.assertIn("first → Accept", self.cli(CLONE, "show", *handle))
        _ = self.cli(OWNER, "decide", *handle, "1", "Defer")
        self.assertIn("first → Defer", self.cli(CLONE, "show", *handle))

    def test_join_keeps_a_live_owners_review_when_the_registry_loses_the_record(self) -> None:
        review = self.start("first")
        path = Path(review["document"])
        self.liveness[OWNER] = sessions.SessionNotLive()
        self.assertEqual(json.loads(self.cli(CLONE, "join", "bob")), review)
        self.processes[PIDS[OWNER]] = sessions.SessionLivenessUnknown("process unavailable")
        self.assertIn("Cannot confirm", self.cli(CLONE, "join", "bob", expected=2))
        self.assertTrue(path.exists())
        self.liveness[OWNER] = sessions.SessionLivenessUnknown("registry unavailable")
        self.processes[PIDS[OWNER]] = sessions.SessionNotLive()
        self.assertIn("Cannot confirm", self.cli(CLONE, "join", "bob", expected=2))
        self.assertTrue(path.exists())
        self.liveness[OWNER] = sessions.SessionNotLive()
        self.assertIn("ended their session", self.cli(CLONE, "join", "bob", expected=2))
        self.assertFalse(path.exists())

    def test_solo_command_keeps_its_document_announcement_todos_and_wrap_up(self) -> None:
        command = Path(__file__).resolve().parents[2] / "commands/adhoc_review.md"
        text = command.read_text(encoding="utf-8")
        for line in (
            "Create a transient document at the repository root by default.",
            "Exclude it from Git with `bash ~/.claude/scripts/exclude/exclude.sh <path>`.",
            "Before the first item's choice line, add `N items. Decisions go in <path>. I'll delete this file when the review ends.`",
            "Never write \"existing path\": name the doc.",
            "If a todo tool is available (e.g. `TaskCreate`), add one task per item",
            "1. Mark the task `in_progress`.",
            "Mark the task `completed` and continue to the next item.",
            "If they want to revisit an earlier item, jump back. Don't insist on linear order.",
            "List all pending actions, or say `none pending`. Then delete the document.",
            "`Wrote decisions for N completed items to <path>`. It stays.",
            "Ensure every task has a terminal status.",
        ):
            with self.subTest(line=line):
                self.assertIn(line, text)

    def test_owner_mirrors_a_solo_decision_in_a_separate_private_file(self) -> None:
        document = self.home / "decisions.md"
        _ = document.write_text("# Review\n", encoding="utf-8")
        with patch("sys.stdin", StringIO('["First choice"]')):
            review = cast(shared_review.ReviewHandle, json.loads(
                self.cli(OWNER, "start", "--source", "Fixture", "--items", "-")))
        shared = Path(review["document"])
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o600)
        handle = (review["owner"], review["review"])
        self.assertIn("owner uses claim", self.cli(OWNER, "next", *handle, expected=2))
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["item"], 1)
        _ = document.write_text("# Review\nFirst choice → Accept\n", encoding="utf-8")
        _ = self.cli(OWNER, "decide", *handle, "1", "Accept")
        self.assertIn("First choice → Accept", self.cli(OWNER, "end", *handle))
        self.assertFalse(shared.exists())
        self.assertEqual(document.read_text(encoding="utf-8"), "# Review\nFirst choice → Accept\n")

    def test_split_preserves_other_claims_and_offers_subitems_in_order(self) -> None:
        review = self.start("A: combined", "B: independent", "C: later")
        handle = (review["owner"], review["review"])
        _ = self.cli(CLONE, "join", "bob")
        _ = self.cli(OWNER, "claim", *handle)
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 2)
        self.assertEqual(json.loads(self.cli(OWNER, "split", *handle, "1", "A.1: first", "A.2: second")),
                         {"remaining": 4})
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["text"], "A.1: first")
        _ = self.cli(OWNER, "decide", *handle, "1", "Accept")
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["text"], "A.2: second")
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["text"], "B: independent")

    def test_join_counts_a_live_revisit_and_omits_it_after_the_holder_exits(self) -> None:
        review = self.start("first")
        handle = (review["owner"], review["review"])
        _ = self.cli(OWNER, "claim", *handle)
        _ = self.cli(OWNER, "decide", *handle, "1", "Accept")
        _ = self.cli(CLONE, "join", "bob")
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle, "--item", "1"))["remaining"], 1)
        self.assertEqual(json.loads(self.cli(OTHER, "join", "bob"))["remaining"], 1)
        self.liveness[CLONE] = sessions.SessionNotLive()
        self.processes[PIDS[CLONE]] = sessions.SessionNotLive()
        self.assertEqual(json.loads(self.cli(OTHER, "join", "bob"))["remaining"], 0)
        self.assertIn("first → Accept", self.cli(OTHER, "show", *handle))

    def test_joined_skip_records_a_decision_and_releases_the_item(self) -> None:
        review = self.start("first", "second")
        handle = (review["owner"], review["review"])
        self.assertIn("Join this review", self.cli(CLONE, "next", *handle, expected=2))
        _ = self.cli(CLONE, "join", "bob")
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 1)
        self.assertEqual(json.loads(self.cli(CLONE, "record", *handle, "1", "Skipped")), {"remaining": 1})
        self.assertIn("first → Skipped", self.cli(OWNER, "show", *handle))
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 2)

    def test_added_item_follows_its_prerequisite(self) -> None:
        review = self.start("prerequisite", "later")
        handle = (review["owner"], review["review"])
        _ = self.cli(OWNER, "add", *handle, "new", "--after", "1")
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["text"], "prerequisite")
        _ = self.cli(OWNER, "decide", *handle, "1", "Accept")
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["text"], "new")
