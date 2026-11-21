"""Test size measurements and notices with a private producer record."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from uuid import uuid4

from scripts.maintenance import folder_size
from scripts.maintenance.schedule import schedule_directory
from scripts.production import staff


class FolderSizeTests(unittest.TestCase):
    root: Path = Path()
    folder: Path = Path()
    config: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        self.root = home / "producer-docs"
        self.folder = self.root / "fixture-docs"
        self.folder.mkdir(parents=True)
        self.config = home / "config/staff.conf"
        self.config.parent.mkdir()
        self.set_limit(2)
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(home), "CONF_DIR": str(self.config.parent), "XDG_STATE_HOME": str(home / "state"),
        }))
        producer = self.member("fixture-producer", self.folder)
        _ = self.enterContext(staff.supplied_snapshot(staff.StaffSnapshot((producer,))))

    def member(self, name: str, document: Path) -> staff.StaffRow:
        return staff.StaffRow(staff.native_id(str(uuid4())), "producer", name, staff.StaffRoot(),
                              doc=staff.LiveDoc("fixture-machine", document),
                              current_session=staff.native_id(str(uuid4())))

    def set_limit(self, limit: int) -> None:
        _ = self.config.write_text(
            f"PRODUCER_FOLDER_ROOT={self.root}\n# whole: 1..\nproducer_folder_limit_mb={limit}\n",
        )

    def resize(self, path: Path, size: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as file:
            _ = file.truncate(size)

    def test_sends_once_until_the_folder_returns_to_the_limit(self) -> None:
        producer = staff.snapshot().rows[0]
        outside = self.root.parent / "elsewhere"
        self.resize(outside / "ignored.mp4", 5_000_000)
        (self.folder / "linked-folder").symlink_to(outside, target_is_directory=True)
        (self.folder / "linked-file").symlink_to(outside / "ignored.mp4")
        self.resize(self.folder / "note.md", 1)
        media = self.folder / "subfolder/recording.mp4"
        self.resize(media, 1_999_999)
        accepted = subprocess.CompletedProcess[str]([], 0, stdout="SENT: fixture\n")
        with patch.object(subprocess, "run", return_value=accepted) as send:
            folder_size.run()
            send.assert_not_called()
            self.resize(media, 2_000_000)
            folder_size.run()
            send.assert_called_once()
            first = cast(list[str], send.call_args.args[0])
            self.assertEqual(first[first.index("--to") + 1], f"staff:{producer.id}")
            message = first[first.index("--text") + 1]
            self.assertIn(str(self.folder), message)
            self.assertIn("uses 2.000001 MB", message)
            self.assertIn("limit is 2 MB", message)
            renamed = replace(producer, name="renamed-fixture")
            with staff.supplied_snapshot(staff.StaffSnapshot((renamed,))):
                folder_size.run()
            send.assert_called_once()
            self.resize(media, 1_999_999)
            folder_size.run()
            self.resize(media, 2_000_000)
            folder_size.run()
            self.assertEqual(send.call_count, 2)
            second = cast(list[str], send.call_args.args[0])
            self.assertNotEqual(first[first.index("--key") + 1], second[second.index("--key") + 1])

    def test_measures_the_parent_of_a_recorded_document(self) -> None:
        document = self.folder / "current.md"
        producer = self.member("fixture-document", document)
        self.resize(document, 1)
        self.resize(self.folder / "subfolder/payload.bin", 2_000_000)
        accepted = subprocess.CompletedProcess[str]([], 0, stdout="SENT: fixture\n")
        with (staff.supplied_snapshot(staff.StaffSnapshot((producer,))),
              patch.object(subprocess, "run", return_value=accepted) as send):
            folder_size.run()
        send.assert_called_once()
        args = cast(list[str], send.call_args.args[0])
        self.assertEqual(args[args.index("--to") + 1], f"staff:{producer.id}")
        self.assertIn(f"{str(self.folder)!r} uses 2.000001 MB", args[args.index("--text") + 1])

    def test_measures_a_folder_deeper_than_the_recursion_limit(self) -> None:
        leaf = self.folder
        for _ in range(180):
            leaf /= "d"
            leaf.mkdir()
        self.resize(leaf / "payload.bin", 3_000_000)
        accepted = subprocess.CompletedProcess[str]([], 0, stdout="SENT: fixture\n")
        with (patch.object(os, "open", wraps=os.open) as open_directory,
              patch.object(subprocess, "run", return_value=accepted) as send):
            original_limit = sys.getrecursionlimit()
            try:
                sys.setrecursionlimit(150)
                folder_size.run()
            finally:
                sys.setrecursionlimit(original_limit)
        directory_opens = sum(bool(cast(int, call.args[1]) & os.O_DIRECTORY)
                              for call in open_directory.call_args_list)
        self.assertEqual(directory_opens, 182)
        send.assert_called_once()
        args = cast(list[str], send.call_args.args[0])
        self.assertIn(f"{str(self.folder)!r} uses 3 MB", args[args.index("--text") + 1])

    def test_keeps_the_open_ancestor_when_its_path_is_replaced(self) -> None:
        self.resize(self.folder / "payload.bin", 1_500_000)
        (self.folder / "child").mkdir()
        replacement = self.root / "replacement"
        self.resize(replacement / "child/payload.bin", 1_500_000)
        original = self.folder.stat()
        list_names = os.listdir
        replaced = False

        def replace_after_listing(directory: int) -> list[str]:
            nonlocal replaced
            names = list_names(directory)
            if os.path.samestat(os.fstat(directory), original):
                _ = self.folder.rename(self.root / "moved")
                _ = replacement.rename(self.folder)
                replaced = True
            return names

        with (patch.object(os, "listdir", side_effect=replace_after_listing),
              patch.object(subprocess, "run") as send, redirect_stdout(io.StringIO()) as output):
            folder_size.run()
        self.assertTrue(replaced)
        self.assertEqual(output.getvalue(), "")
        send.assert_not_called()

    def test_reads_a_changed_limit_without_a_restart(self) -> None:
        self.resize(self.folder / "payload.bin", 2_500_000)
        accepted = subprocess.CompletedProcess[str]([], 0, stdout="SENT: fixture\n")
        with patch.object(subprocess, "run", return_value=accepted) as send:
            self.set_limit(3)
            folder_size.run()
            send.assert_not_called()
            self.set_limit(2)
            folder_size.run()
            send.assert_called_once()
            self.set_limit(3)
            folder_size.run()
            send.assert_called_once()
            self.set_limit(1)
            folder_size.run()
            self.assertEqual(send.call_count, 2)
            args = cast(list[str], send.call_args.args[0])
            self.assertIn("limit is 1 MB", args[args.index("--text") + 1])

    def test_retries_a_failed_send_and_accepts_a_queued_send(self) -> None:
        self.resize(self.folder / "payload.bin", 3_000_000)
        failed = subprocess.CompletedProcess[str]([], 3, stdout="FAILED: fixture\n")
        queued = subprocess.CompletedProcess[str]([], 1, stdout="QUEUED: fixture\n")
        with (patch.object(subprocess, "run", side_effect=[failed, queued]) as send,
              redirect_stdout(io.StringIO()) as output):
            folder_size.run()
            folder_size.run()
            folder_size.run()
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(send.call_count, 2)
        first, second = (cast(list[str], call.args[0]) for call in send.call_args_list)
        self.assertEqual(first[first.index("--key") + 1], second[second.index("--key") + 1])

    def test_missing_and_unreadable_folders_leave_other_producers_eligible(self) -> None:
        producer = staff.snapshot().rows[0]
        missing = self.member("fixture-missing", self.root / "missing")
        unreadable_folder = self.root / "unreadable"
        unreadable = self.member("fixture-unreadable", unreadable_folder)
        self.resize(unreadable_folder / "blocked.bin", 3_000_000)
        self.resize(self.folder / "payload.bin", 3_000_000)
        list_names = os.listdir
        blocked = unreadable_folder.stat()

        def refuse_one_folder(directory: int) -> list[str]:
            if os.path.samestat(os.fstat(directory), blocked):
                raise PermissionError("fixture folder is unreadable")
            return list_names(directory)

        accepted = subprocess.CompletedProcess[str]([], 0, stdout="SENT: fixture\n")
        record = staff.StaffSnapshot((missing, unreadable, producer))
        with (staff.supplied_snapshot(record), patch.object(os, "listdir", side_effect=refuse_one_folder),
              patch.object(subprocess, "run", return_value=accepted) as send,
              redirect_stdout(io.StringIO()) as output):
            folder_size.run()
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(str(missing.id), lines[0])
        self.assertIn(str(unreadable.id), lines[1])
        self.assertIn("fixture folder is unreadable", lines[1])
        send.assert_called_once()
        args = cast(list[str], send.call_args.args[0])
        self.assertEqual(args[args.index("--to") + 1], f"staff:{producer.id}")

    def test_skips_a_link_to_a_directory_that_denies_access(self) -> None:
        self.resize(self.folder / "payload.bin", 3_000_000)
        outside = self.root.parent / "no-access"
        outside.mkdir()
        outside.chmod(0o000)
        self.addCleanup(outside.chmod, 0o700)
        link = self.folder / "linked-directory"
        link.symlink_to(outside, target_is_directory=True)
        accepted = subprocess.CompletedProcess[str]([], 0, stdout="SENT: fixture\n")
        with (patch.object(os, "open", wraps=os.open) as open_directory,
              patch.object(subprocess, "run", return_value=accepted) as send, redirect_stdout(io.StringIO()) as output):
            folder_size.run()
        directory_opens = sum(bool(cast(int, call.args[1]) & os.O_DIRECTORY)
                              for call in open_directory.call_args_list)
        self.assertEqual(directory_opens, 2)
        self.assertEqual(output.getvalue(), "")
        send.assert_called_once()

    def test_measures_every_folder_before_a_send_and_continues_after_a_timeout(self) -> None:
        first = staff.snapshot().rows[0]
        missing = self.member("fixture-missing", self.root / "missing")
        last_folder = self.root / "last"
        last = self.member("fixture-last", last_folder)
        self.resize(self.folder / "payload.bin", 3_000_000)
        self.resize(last_folder / "payload.bin", 4_000_000)
        calls: list[str] = []
        output = io.StringIO()
        state = schedule_directory("job") / "folder-size.json"

        def deliver(argv: list[str], *, timeout: int, **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertIn(str(missing.id), output.getvalue())
            self.assertEqual(argv[argv.index("--timeout") + 1], "5")
            self.assertEqual(timeout, 10)
            address = argv[argv.index("--to") + 1]
            calls.append(address)
            if address == f"staff:{first.id}":
                self.assertEqual(set(folder_size.load_crossings(state)), {first.id, last.id})
                raise subprocess.TimeoutExpired(argv, timeout)
            self.assertIn("send.py exceeded its 10-second process limit", output.getvalue())
            self.assertEqual(folder_size.load_crossings(state)[first.id].attempts, 1)
            return subprocess.CompletedProcess[str](argv, 0, stdout="SENT: fixture\n")

        record = staff.StaffSnapshot((first, missing, last))
        with (staff.supplied_snapshot(record),
              patch.object(subprocess, "run", side_effect=deliver), redirect_stdout(output)):
            folder_size.run()
        self.assertEqual(calls, [f"staff:{first.id}", f"staff:{last.id}"])
        self.assertTrue(folder_size.load_crossings(state)[last.id].accepted)


if __name__ == "__main__":
    _ = unittest.main()
