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
from scripts.message import session_records, sessions
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
            identity: sessions.LiveSessionProcess(pid, self.process_start(pid)) for identity, pid in PIDS.items()
        }
        self.processes = {pid: sessions.LiveSessionProcess(pid, self.process_start(pid)) for pid in PIDS.values()}
        named_owner = staff.StaffRow(staff.StaffId(OWNER), "showrunner", "bob", staff.StaffRoot(),
                                     current_session=staff.StaffId(OWNER))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)})))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((named_owner,))))
        _ = self.enterContext(patch.object(sessions, "read_registry", return_value=sessions.RegistryRead((), ())))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.observe_registry))
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
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle)), {
            "state": "complete", "remaining": 0, "items": [
                {"item": 1, "text": "First choice\nDetails",
                 "decision": {"words": "Accept", "actions": "Pending: update the plan"}},
                {"item": 2, "text": "Second choice", "decision": {"words": "Defer", "actions": ""}},
            ],
        })

    def test_next_keeps_a_live_claim_missing_from_the_registry(self) -> None:
        review = self.start("first")
        handle = (review["owner"], review["review"])
        _ = self.cli(CLONE, "join", "bob")
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 1)
        busy = {"state": "busy", "remaining": 1, "items": [{"item": 1, "text": "first"}]}
        self.liveness[CLONE] = sessions.SessionLivenessUnknown("fixture registry is unreadable")
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle)), busy)
        self.liveness[CLONE] = sessions.SessionNotLive()
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle)), busy)
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
        self.assertIn("review has ended", self.cli(CLONE, "next", *handle, expected=2))
        _ = self.cli(OWNER, "finish", *handle)
        self.assertFalse(Path(review["document"]).exists())
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
        self.assertTrue(path.exists())

    def test_owner_exit_preserves_joined_decisions_for_resume(self) -> None:
        review = self.start("first", "second")
        handle = (review["owner"], review["review"])
        _ = self.cli(CLONE, "join", "bob")
        _ = self.cli(CLONE, "next", *handle)
        _ = self.cli(CLONE, "record", *handle, "1", "Accept")
        record = Path(review["document"])
        before = record.read_text()
        self.liveness[OWNER] = sessions.SessionNotLive()
        self.processes[PIDS[OWNER]] = sessions.SessionNotLive()
        for verb in ("show", "next"):
            self.assertIn("ended their session", self.cli(CLONE, verb, *handle, expected=2))
        self.assertEqual(record.read_text(), before)
        self.liveness[OWNER] = sessions.LiveSessionProcess(126, "resumed-owner")
        self.assertEqual(json.loads(self.cli(OWNER, "resume")), {**review, "remaining": 1})
        self.assertIn("1. first → Accept", self.cli(OWNER, "show", *handle))
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 2)

    def test_owner_claim_checks_the_saved_start_when_the_registry_omits_it(self) -> None:
        review = self.start("first")
        handle = (review["owner"], review["review"])
        self.liveness[OWNER] = sessions.LiveSessionProcess(PIDS[OWNER])
        self.processes[PIDS[OWNER]] = sessions.SessionNotLive()
        self.assertIn("Cannot confirm your process", self.cli(OWNER, "claim", *handle, expected=2))
        self.processes[PIDS[OWNER]] = sessions.LiveSessionProcess(PIDS[OWNER], self.process_start(PIDS[OWNER]))
        self.assertEqual(json.loads(self.cli(OWNER, "claim", *handle))["item"], 1)

    def test_returning_owner_without_a_verified_start_allows_joined_claims(self) -> None:
        review = self.start("first", "second")
        handle = (review["owner"], review["review"])
        _ = self.cli(OWNER, "claim", *handle)
        _ = self.cli(OWNER, "decide", *handle, "1", "Accept")
        self.liveness[OWNER] = sessions.LiveSessionProcess(126)
        self.processes[PIDS[OWNER]] = sessions.SessionNotLive()
        self.assertEqual(json.loads(self.cli(CLONE, "join", "bob"))["remaining"], 1)
        self.assertIn("1. first → Accept", self.cli(CLONE, "show", *handle))
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["item"], 2)
        self.processes[PIDS[OWNER]] = sessions.LiveSessionProcess(PIDS[OWNER], self.process_start(PIDS[OWNER]))
        self.assertIn("Cannot confirm your process", self.cli(OWNER, "resume", expected=2))

    def test_solo_command_keeps_its_document_announcement_todos_and_wrap_up(self) -> None:
        text = self.command_text()
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
        _ = self.cli(OWNER, "finish", *handle)
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

    def command_text(self) -> str:
        return (Path(__file__).resolve().parents[2] / "commands/adhoc_review.md").read_text(encoding="utf-8")

    def test_interrupted_end_preserves_final_decisions_until_the_owner_saves_them(self) -> None:
        review = self.start("First choice", "Second choice")
        handle = (review["owner"], review["review"])
        document = self.home / "decisions.md"
        _ = document.write_text("# Review\n")
        _ = self.cli(CLONE, "join", "bob")
        _ = self.cli(CLONE, "next", *handle)
        _ = self.cli(CLONE, "record", *handle, "1", "Accept")
        _ = self.cli(CLONE, "next", *handle)
        self.assertIn("save the final decisions", self.cli(OWNER, "finish", *handle, expected=2))
        self.assertIn("First choice → Accept", self.cli(OWNER, "end", *handle))
        self.assertEqual(document.read_text(), "# Review\n")
        self.liveness[OWNER] = sessions.SessionNotLive()
        self.processes[PIDS[OWNER]] = sessions.SessionNotLive()
        self.assertIn("review has ended", self.cli(CLONE, "record", *handle, "2", "Late", expected=2))
        self.assertTrue(json.loads(self.cli(OWNER, "resume"))["closing"])
        _ = document.write_text(self.cli(OWNER, "end", *handle))
        _ = self.cli(OWNER, "finish", *handle)
        self.assertIn("First choice → Accept", document.read_text())
        self.assertFalse(Path(review["document"]).exists())

    def test_failed_sharing_setup_leaves_the_solo_steps_available(self) -> None:
        text = self.command_text()
        self.assertIn("Enable sharing only after `start` returns the review IDs.", text)
        self.assertIn("If `start` fails, continue the solo review silently.", text)
        self.assertIn("Use shared commands only when sharing is active.", text)
        document = self.home / "decisions.md"
        _ = document.write_text("# Review\n")
        self.liveness[OWNER] = sessions.SessionLivenessUnknown("fixture registry is unreadable")
        with patch("sys.stdin", StringIO('["First choice"]')):
            self.assertIn("Cannot confirm your process", self.cli(
                OWNER, "start", "--source", "Fixture", "--items", "-", expected=2))
        self.liveness[OWNER] = sessions.LiveSessionProcess(PIDS[OWNER], self.process_start(PIDS[OWNER]))
        old_review = self.start("First choice")
        old_record = Path(old_review["document"])
        before = old_record.read_text()
        with patch("sys.stdin", StringIO('["First choice"]')):
            self.assertIn("already has a review record", self.cli(
                OWNER, "start", "--source", "Fixture", "--items", "-", expected=2))
        self.assertEqual(old_record.read_text(), before)
        self.assertEqual(document.read_text(), "# Review\n")

    def test_claim_saves_the_start_that_the_registry_verified_before_pid_reuse(self) -> None:
        registry = sessions.RegistryRead(tuple(
            sessions.RegistryObservation(sessions.SessionRecord(
                sessionId=identity, pid=pid, name="", messagingSocketPath="", updatedAt=1,
                tmux="", formerNames=[], procStart=self.process_start(pid)), {})
            for identity, pid in PIDS.items()), ())
        with (patch.object(sessions, "read_registry", return_value=registry),
              patch.object(sessions, "session_liveness", new=session_records.session_liveness),
              patch.object(session_records, "headless", return_value=False),
              patch.object(os, "kill"),
              patch.object(session_records, "process_start", side_effect=self.process_start) as read_start,
              patch.object(sessions, "process_start", new=read_start)):
            review = self.start("First choice")
            _ = self.cli(CLONE, "join", "bob")
            starts = iter([self.process_start(PIDS[CLONE]), "reused-pid"])

            def start_during_claim(pid: int) -> str:
                return next(starts) if pid == PIDS[CLONE] else self.process_start(pid)

            read_start.side_effect = start_during_claim
            _ = self.cli(CLONE, "next", OWNER, review["review"])
        self.assertEqual(next(starts), "reused-pid")
        record = Path(review["document"]).read_text().splitlines()[0]
        parsed = cast(shared_review.ActiveReview, json.loads(
            record.removeprefix(shared_review.RECORD_PREFIX).removesuffix(shared_review.RECORD_SUFFIX)))
        self.assertEqual(parsed["participants"][CLONE], {"pid": PIDS[CLONE], "started": self.process_start(PIDS[CLONE])})

    def test_owner_claim_returns_new_joined_tasks_in_shared_order(self) -> None:
        review = self.start("First choice", "Second choice")
        handle = (review["owner"], review["review"])
        _ = self.cli(CLONE, "join", "bob")
        _ = self.cli(CLONE, "next", *handle)
        _ = self.cli(CLONE, "split", *handle, "1", "First part", "Second part")
        _ = self.cli(CLONE, "record", *handle, "1", "Accept")
        _ = self.cli(CLONE, "add", *handle, "New choice", "--after", "3")
        claim = cast(shared_review.ClaimedItem, json.loads(self.cli(OWNER, "claim", *handle)))
        self.assertEqual(claim["item"], 3)
        self.assertEqual(claim.get("items"), [
            {"item": 1, "text": "First part", "decision": {"words": "Accept", "actions": ""}},
            {"item": 3, "text": "Second part"},
            {"item": 4, "text": "New choice"},
            {"item": 2, "text": "Second choice"},
        ])

    def test_join_sets_the_hold_before_the_first_item_and_refreshes_it_on_busy(self) -> None:
        review = self.start("First choice", "Second choice")
        handle = (review["owner"], review["review"])
        self.assertEqual(json.loads(self.cli(CLONE, "join", "bob"))["remaining"], 2)
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle))["remaining"], 2)
        self.assertEqual(json.loads(self.cli(CLONE, "record", *handle, "1", "Accept")), {"remaining": 1})
        _ = self.cli(OWNER, "claim", *handle)
        self.assertEqual(json.loads(self.cli(CLONE, "next", *handle)), {
            "state": "busy", "remaining": 1, "items": [
                {"item": 1, "text": "First choice", "decision": {"words": "Accept", "actions": ""}},
                {"item": 2, "text": "Second choice"},
            ],
        })
        flow = self.command_text().split("## Join a review\n", 1)[1].split("## Step 1", 1)[0]
        self.assertNotIn("`show OWNER REVIEW`", flow)
        contract = (
            "Apply `remaining` from `join` with the Step 3 status command.",
            "Run `next OWNER REVIEW` to claim an item.",
            "Apply `remaining` from `next` before you handle the result, including `busy` or `complete`.",
            "Read the decisions in the returned `items`.",
            "On `busy`,",
            "On `complete`,",
            "Set this session's count to zero. Use the held message steps in Step 5.",
        )
        positions = [flow.index(line) for line in contract]
        self.assertEqual(positions, sorted(positions))
