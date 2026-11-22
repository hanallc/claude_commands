"""Test action storage and filing with fixture folders."""
from __future__ import annotations

import errno
import fcntl
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.for_you import action


class ActionTests(unittest.TestCase):
    """Keep action records and filer output inside fixtures."""

    folder: Path = Path()
    store: action.ActionStore = action.ActionStore(Path())

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "actions"
        self.store = action.ActionStore(self.folder)
        self.enterContext(patch.dict(os.environ, {"FOR_YOU_ACTIONS_DIR": str(self.folder)}))

    def add(self, requester: str = "staff-a", dedupe: str = "access") -> action.UserAction:
        return self.store.add(title="Restore access", command="github-warmup", machine="natedev",
                              cwd="/fixture/repo", requested_by=requester, reason="The login requires the user.",
                              waits="The release check", concerns="admin-console", dedupe_key=dedupe)

    def cli(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            result = action.main(list(arguments))
        return result, output.getvalue()

    def test_add_and_list_retained_records(self) -> None:
        self.assertEqual(self.store.list(), ())
        self.assertFalse(self.folder.exists())
        first = self.add()
        second = self.add(dedupe="release")
        self.assertEqual(action.ActionStore().folder, self.folder)
        self.assertEqual(self.store.list(), (first, second))
        self.assertEqual(first.status, "open")
        self.assertEqual(first.completion, "manual")
        self.assertEqual(first.created_at, first.updated_at)
        self.assertEqual((self.folder / (action.action_id(first.key) + ".json")).stat().st_mode & 0o777, 0o600)

    def test_dedupe_preserves_existing_open_action(self) -> None:
        first = self.add()
        path = self.folder / (action.action_id(first.key) + ".json")
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(self.add(), first)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertNotEqual(self.add("staff-b").key, first.key)

    def test_concurrent_adds_share_one_open_action(self) -> None:
        def add_one(_: int) -> action.UserAction:
            return self.add()

        with ThreadPoolExecutor(max_workers=4) as workers:
            records = tuple(workers.map(add_one, range(8)))
        self.assertEqual(len({record.key for record in records}), 1)
        self.assertEqual(len(self.store.list()), 1)

    def test_withdraw_records_reason_and_allows_new_request(self) -> None:
        first = self.add()
        result = self.store.withdraw(first.key, requested_by="staff-a", reason="The release was cancelled.")
        self.assertEqual(result.status, "withdrawn")
        self.assertEqual(result.withdraw_reason, "The release was cancelled.")
        self.assertEqual(result.completed_at, "")
        self.assertNotEqual(self.add().key, first.key)
        self.assertEqual(len(self.store.list()), 2)

    def test_cross_requester_withdraw_refuses_before_write(self) -> None:
        first = self.add()
        with self.assertRaisesRegex(action.ActionStoreError, "Another requester"):
            _ = self.store.withdraw(first.key, requested_by="staff-b", reason="Cancel.")
        self.assertEqual(self.store.list(), (first,))

    def test_manual_done_preserves_evidence_and_is_idempotent(self) -> None:
        first = self.add()
        result = self.store.mark_done(first.key, evidence="The user confirmed access.")
        self.assertEqual(result.status, "done")
        self.assertEqual(result.completed_at, result.updated_at)
        self.assertEqual(result.evidence, "The user confirmed access.")
        self.assertEqual(self.store.mark_done(first.key, evidence="A repeated confirmation."), result)
        self.assertNotEqual(self.add().key, first.key)

    def test_terminal_states_refuse_other_transitions(self) -> None:
        first = self.add()
        _ = self.store.withdraw(first.key, requested_by="staff-a", reason="Cancel.")
        with self.assertRaises(action.ActionStoreError):
            _ = self.store.mark_done(first.key, evidence="Confirmed.")
        second = self.add()
        _ = self.store.mark_done(second.key, evidence="Confirmed.")
        with self.assertRaises(action.ActionStoreError):
            _ = self.store.withdraw(second.key, requested_by="staff-a", reason="Cancel.")

    def test_atomic_replace_holds_the_shared_write_lock(self) -> None:
        first = self.add()
        path = self.folder / (action.action_id(first.key) + ".json")
        original_replace = os.replace
        calls: list[str] = []

        def replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            self.assertEqual(Path(target), path)
            self.assertEqual(action.UserAction.decode(path.read_text()).status, "open")
            self.assertEqual(action.UserAction.decode(Path(source).read_text()).status, "done")
            self.assertEqual(Path(source).parent, path.parent)
            with (self.folder / ".lock").open("a") as lock:
                with self.assertRaises(OSError) as caught:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIn(caught.exception.errno, (errno.EAGAIN, errno.EACCES))
            calls.append(str(target))
            original_replace(source, target)

        with patch.object(os, "replace", side_effect=replace):
            _ = self.store.mark_done(first.key, evidence="Confirmed.")
        self.assertEqual(calls, [str(path)])
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_failed_replace_retains_previous_record(self) -> None:
        first = self.add()
        with patch.object(os, "replace", side_effect=OSError("Fixture replacement failure.")):
            with self.assertRaises(OSError):
                _ = self.store.mark_done(first.key, evidence="Confirmed.")
        self.assertEqual(self.store.list(), (first,))
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_damaged_record_refuses_read_and_dedupe(self) -> None:
        first = self.add()
        path = self.folder / (action.action_id(first.key) + ".json")
        _ = path.write_text("{damaged", encoding="utf-8")
        with self.assertRaisesRegex(action.ActionStoreError, path.name):
            _ = self.store.list()
        with self.assertRaises(action.ActionStoreError):
            _ = self.add()

    def test_invalid_fields_and_filename_refuse_read(self) -> None:
        first = self.add()
        path = self.folder / (action.action_id(first.key) + ".json")
        for field, value in (("version", 2), ("title", 3), ("status", "bad"), ("completed_at", "bad"),
                             ("created_at", "2026-11-17T00:00:00"), ("cwd", "relative")):
            with self.subTest(field=field):
                _ = path.write_text(json.dumps({**asdict(first), field: value}))
                with self.assertRaises(action.ActionStoreError):
                    _ = self.store.list()
        _ = path.write_text(json.dumps(asdict(first)))
        _ = path.rename(self.folder / "wrong.json")
        with self.assertRaisesRegex(action.ActionStoreError, "filename"):
            _ = self.store.list()

    def test_empty_evidence_reason_and_path_traversal_are_refused(self) -> None:
        first = self.add()
        with self.assertRaises(action.ActionStoreError):
            _ = self.store.mark_done(first.key, evidence=" ")
        with self.assertRaises(action.ActionStoreError):
            _ = self.store.withdraw(first.key, requested_by="staff-a", reason=" ")
        with self.assertRaises(action.ActionStoreError):
            _ = self.store.mark_done("../other", evidence="Confirmed.")

    def test_cli_add_list_and_withdraw_never_execute_command(self) -> None:
        def card_url(key: str) -> str:
            return "http://fixture/review?item=" + key

        with (patch.object(action, "card_url", side_effect=card_url),
              patch("subprocess.run", side_effect=AssertionError("The filer executed a command."))):
            result, output = self.cli("add", "--title", "Restore access", "--command", "never-run-this",
                                      "--machine", "mac", "--cwd", "/fixture/repo", "--requested-by", "staff-a",
                                      "--reason", "User access", "--waits", "Release", "--concerns", "console",
                                      "--dedupe-key", "access")
            self.assertEqual(result, 0)
            key, url = output.splitlines()
            self.assertIn(key, url)
            result, output = self.cli("list", "--json")
            self.assertEqual(result, 0)
            records = cast(list[dict[str, object]], json.loads(output))
            self.assertEqual(records[0]["key"], key)
            self.assertEqual(records[0]["command"], "never-run-this")
            self.assertEqual(self.cli("withdraw", "--id", key, "--requested-by", "staff-a", "--reason", "Cancel."),
                             (0, key + "\n"))
        errors = io.StringIO()
        with redirect_stderr(errors):
            result, _ = self.cli("withdraw", "--id", key, "--requested-by", "staff-b", "--reason", "Cancel.")
        self.assertEqual(result, 2)
        self.assertIn("Another requester", errors.getvalue())

    def test_card_url_uses_existing_review_path_and_encoded_key(self) -> None:
        from scripts.review_links import review_link

        key = "action/00000000-0000-0000-0000-000000000001"
        with patch.object(review_link, "review_root", return_value=self.folder), \
                patch.dict(os.environ, {"REVIEW_LINKS_HOST": "fixture.invalid"}):
            url = action.card_url(key)
        self.assertIn("/console/review.html?type=for-you&show=open&item=action%2F", url)
        self.assertFalse(self.folder.exists())


if __name__ == "__main__":
    _ = unittest.main()
