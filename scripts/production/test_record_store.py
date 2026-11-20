"""Test numbered records in a private state folder."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from scripts.agents.remote import local_machine
from scripts.production import record_store, staff


class NumberedRecordsTests(unittest.TestCase):
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

        with self.subTest(history="unknown account"):
            old.mkdir()
            new.mkdir()
            _ = self.commit_folder(doc, Path(f"~review-test-{uuid4().hex}/proposals"))
            _ = self.commit_folder(doc, new)
            notices: list[Path] = []

            def skipped(source: Path, error: Exception) -> None:
                self.assertIsInstance(error, RuntimeError)
                notices.append(source)

            with (patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": str(doc.parent / "state")}),
                  patch.object(subprocess, "run", wraps=subprocess.run) as git):
                for _ in range(2):
                    notices.clear()
                    folders = record_store.known_folders(record_store.RecordKind.PROPOSAL, staff.StaffId("owner"),
                        document=doc, strict=True, on_source_error=skipped)
                    self.assertEqual(folders, (new.resolve(), old.resolve()))
                    self.assertEqual(notices, [doc])
                git.assert_called_once()

    def test_history_follows_branches_and_detached_worktree_heads(self) -> None:
        doc = self.git_history_fixture()
        current, former = (doc.parent / name for name in ("current", "former"))
        first = self.commit_folder(doc, current)
        linked = doc.parent / "linked"
        _ = self.git(doc.parent, "worktree", "add", "--detach", str(linked))
        self.assertEqual(set(record_store.historical_issue_folders(doc)), {current})
        alternate = self.commit_folder(linked / doc.name, former)
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

    def test_registry_skips_an_empty_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": directory}):
            owner = staff.StaffId("producer")
            registry = Path(directory) / "review-folders" / owner / "proposal.json"
            registry.parent.mkdir(parents=True)
            notices: list[Path] = []

            def skipped(source: Path, error: Exception) -> None:
                self.assertIsInstance(error, ValueError)
                notices.append(source)

            registered = Path(directory).resolve() / "proposals"
            for valid in ((), (registered,)):
                with self.subTest(valid=valid):
                    notices.clear()
                    _ = registry.write_text(json.dumps(["", *map(str, valid)]))
                    folders = record_store.known_folders(record_store.RecordKind.PROPOSAL, owner,
                                                          strict=True, on_source_error=skipped)
                    self.assertEqual(folders, valid)
                    self.assertEqual(notices, [registry])

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
