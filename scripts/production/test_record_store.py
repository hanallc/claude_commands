"""Test numbered records in a private state folder."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.agents.remote import local_machine
from scripts.production import record_store, staff


class NumberedRecordsTests(unittest.TestCase):
    def test_list_sequence_starts_from_records_and_the_migration_log(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(directory)}))
        store = record_store.NumberedRecords(staff.StaffId("keeper"), "list", "L", "list item", ValueError)
        morty = staff.StaffRow(store.owner, "producer", "morty", staff.StaffRoot(), current_session=store.owner)
        self.enterContext(staff.supplied_snapshot(staff.StaffSnapshot((morty,))))
        former = directory / "former-keeper" / "list"
        former.mkdir(parents=True)
        closed = former / "L207.md"
        _ = closed.write_text('state: "Closed"\n\n## Log\n\nRemoved from the List.\n')
        history = store.folder() / ".migration-complete.json"
        with store.locked():
            self.assertEqual(store.next_number(), 208)
            _ = history.write_text(json.dumps({"records": {"L208.md": "fixture hash"}}))
            self.assertEqual(store.next_number(), 209)
            with self.assertRaisesRegex(ValueError, "below the saved sequence"):
                store.reserve_number(208)
            store.reserve_number(209)
            history.unlink()
            closed.unlink()
            self.assertEqual(store.next_number(), 210)
            with self.assertRaisesRegex(ValueError, "below the saved sequence"):
                store.reserve_number(208)
            self.assertEqual(store.next_number(), 210)
            _ = store.counter_path().write_text("invalid\n")
            with self.assertRaisesRegex(ValueError, "invalid next number"):
                _ = store.next_number()

    def test_second_keeper_cannot_allocate_a_list_number(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(directory)}))
        keeper = staff.StaffId("keeper")
        second = staff.StaffId("second")
        members = staff.StaffSnapshot((
            staff.StaffRow(keeper, "producer", "morty", staff.StaffRoot(), current_session=keeper),
            staff.StaffRow(second, "producer", "second", staff.StaffRoot(), current_session=second),
        ))
        self.enterContext(staff.supplied_snapshot(members))
        store = record_store.NumberedRecords(second, "list", "L", "list item", ValueError)
        with self.assertRaisesRegex(ValueError, "only morty's List folder"):
            _ = store.next_number()
        with self.assertRaisesRegex(ValueError, "only morty's List folder"):
            store.reserve_number(1)
        self.assertFalse(store.folder().exists())

    def git_history_fixture(self) -> Path:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}))
        _ = self.git(directory, "init", "-b", "main")
        return directory / "production.md"

    def git(self, directory: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(directory), "-c", "user.name=Fixture",
                               "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *arguments],
                              capture_output=True, check=True, text=True).stdout.strip()

    def commit_folder(self, doc: Path, folder: Path) -> str:
        _ = doc.write_text(f"- **Proposals:** {folder}\n")
        _ = self.git(doc.parent, "add", doc.name)
        _ = self.git(doc.parent, "commit", "-m", "Set the proposals folder.")
        return self.git(doc.parent, "rev-parse", "HEAD")

    def test_history_reuses_one_git_read_until_a_commit_changes(self) -> None:
        doc = self.git_history_fixture()
        old, new = (doc.parent / name for name in ("old", "new"))
        _ = self.commit_folder(doc, old)
        with patch.object(subprocess, "run", wraps=subprocess.run) as git:
            self.assertEqual(tuple(record_store.historical_issue_folders(doc)), (old,))
            self.assertEqual(tuple(record_store.historical_issue_folders(doc)), (old,))
            git.assert_called_once()
        _ = self.commit_folder(doc, new)
        self.assertEqual(set(record_store.historical_issue_folders(doc)), {old, new})

    def test_history_follows_branches_and_detached_worktree_heads(self) -> None:
        doc = self.git_history_fixture()
        current, former = (doc.parent / name for name in ("current", "former"))
        first = self.commit_folder(doc, current)
        linked = doc.parent / "linked"
        _ = self.git(doc.parent, "worktree", "add", "--detach", str(linked))
        self.assertEqual(set(record_store.historical_issue_folders(doc)), {current})
        alternate = self.commit_folder(linked / doc.name, former)
        alias = doc.parent / "old-linked"
        alias.symlink_to(linked, target_is_directory=True)
        self.assertEqual(set(record_store.historical_issue_folders(alias / doc.name)), {current, former})
        self.assertEqual(set(record_store.historical_issue_folders(doc)), {current, former})
        _ = self.git(linked, "switch", "--detach", first)
        self.assertEqual(set(record_store.historical_issue_folders(doc)), {current})

        _ = self.git(doc.parent, "branch", "review/former", alternate)
        self.assertEqual(set(record_store.historical_issue_folders(doc)), {current, former})
        _ = self.git(doc.parent, "pack-refs", "--all")
        self.assertEqual(set(record_store.historical_issue_folders(doc)), {current, former})
        _ = self.git(doc.parent, "branch", "-D", "review/former")
        self.assertEqual(set(record_store.historical_issue_folders(doc)), {current})

    def test_history_tracks_included_git_config(self) -> None:
        doc = self.git_history_fixture()
        folder = doc.parent / "proposals"
        _ = self.commit_folder(doc, folder)
        included = doc.parent / "work settings"
        _ = included.write_text("[diff]\n\trenameLimit = 20\n")
        _ = self.git(doc.parent, "config", "include.path", str(included))
        sources: list[Path] = []
        self.assertEqual(tuple(record_store.historical_issue_folders(doc, sources=sources)), (folder,))
        self.assertIn(included, sources)
        _ = included.write_text("[diff]\n\trenameLimit = 30\n")
        with patch.object(subprocess, "run", wraps=subprocess.run) as git:
            self.assertEqual(tuple(record_store.historical_issue_folders(doc)), (folder,))
            self.assertEqual(tuple(record_store.historical_issue_folders(doc)), (folder,))
            git.assert_called_once()

    def test_history_retries_a_failed_git_read(self) -> None:
        doc = self.git_history_fixture()
        folder = doc.parent / "proposals"
        _ = self.commit_folder(doc, folder)
        failed = subprocess.CompletedProcess(["git"], 1, stdout="", stderr="Git could not read a ref.")
        with patch.object(subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(OSError, "Git could not read a ref"):
                _ = tuple(record_store.historical_issue_folders(doc))
        with patch.object(subprocess, "run", side_effect=OSError("Git could not start.")):
            with self.assertRaisesRegex(OSError, "Git could not start"):
                _ = tuple(record_store.historical_issue_folders(doc))
        with patch.object(subprocess, "run", wraps=subprocess.run) as git:
            self.assertEqual(tuple(record_store.historical_issue_folders(doc)), (folder,))
            self.assertEqual(tuple(record_store.historical_issue_folders(doc)), (folder,))
            git.assert_called_once()

    def test_producer_question_folders_do_not_read_the_producer_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": directory}):
            root = Path(directory)
            owner = staff.StaffId("producer")
            document = root / "producer"
            document.mkdir()
            producer = staff.StaffRow(owner, "producer", "producer", staff.StaffRoot(),
                                      current_session=staff.StaffId(""), doc=staff.LiveDoc(local_machine(), document))
            members = staff.StaffSnapshot((producer,))
            registered = root / "former-questions"
            registered.mkdir()
            registry = root / "review-folders" / owner / "question.json"
            registry.parent.mkdir(parents=True)
            _ = registry.write_text(json.dumps([str(registered)]))

            self.assertCountEqual(
                record_store.staff_store_folders(record_store.RecordKind.QUESTION, members=members),
                [registered, root / owner / "questions"],
            )

    def test_f099_allocation_refuses_a_missing_named_production_document(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(root / "state")}))
        owner = staff.StaffId("fixture")
        document = root / "checkout" / "production.md"
        folder = root / "legacy-proposals"
        folder.mkdir()
        legacy = folder / "legacy.md"
        _ = legacy.write_text("P8")
        row = staff.StaffRow(owner, "showrunner", "fixture", staff.StaffRoot(), current_session=owner,
                             doc=staff.LiveDoc(local_machine(), document))
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))))
        sequence = root / "state" / "record-ids" / "proposal.water"
        with record_store.record_numbers(record_store.RecordKind.PROPOSAL, Path.read_text) as numbers:
            with self.assertRaisesRegex(record_store.RecordAllocationRefused, "production.md"):
                _ = numbers.next_id()
            self.assertFalse(sequence.exists())
            self.assertEqual(legacy.read_text(), "P8")

            document.parent.mkdir()
            _ = document.write_text(f"- **Proposals:** {folder}\n")
            self.assertEqual(numbers.next_id(), "P9")

    def test_unicode_separators_stay_inside_a_header_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": directory}):
            store = record_store.NumberedRecords(staff.StaffId("fixture"), "records", "R", "record", ValueError)
            text = "First\u2028Second\u2029Third"
            fields = {"id": "R1", "description": text, "state": "open"}
            body = "Body text\n"
            with store.locked():
                store.write("R1", fields, body)

            self.assertIn(text.encode("utf-8"), store.path("R1").read_bytes())
            self.assertEqual(store.read("R1"), record_store.RecordText(fields, body))
            self.assertEqual(store.field("R1", "description"), text)
