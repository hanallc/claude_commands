"""Use temporary Git repositories to verify backup and restore."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import subprocess

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from tempfile import TemporaryDirectory
import tarfile
import unittest
from unittest.mock import patch

from scripts.agents import remote
from scripts.copies import copies
from scripts.lib.git_test_support import fixture_git_environment
from scripts.production import log_backup, record_store, staff, tidy_wrapped
from scripts.production.production_doc import Production, production_backup_checkout, production_backup_root, read_production
from scripts.production.production_names import BACKUP_DOCUMENT_DIRECTORY, LEGACY_COPIES
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production.update_registration import QuestionStaff


class LogBackupTests(unittest.TestCase):
    def test_production_check_keeps_backups_until_hourly_migration(self) -> None:
        with (TemporaryDirectory() as folder,
              patch.dict(os.environ, fixture_git_environment(Path(folder)), clear=True)):
            root = Path(folder)
            document = root / "production.md"
            check = Path(__file__).with_name("production_check.sh")
            state = root / "state"
            log = root / "LOG.md"
            with (isolated_staff_file(root / "staff.json", {}),
                  patch.dict(os.environ, {"HOME": str(root), "SHOWRUNNER_STATE_DIR": str(state)})):
                owner = fixture_member("showrunner", name="backup-owner",
                                       doc=staff.LiveDoc(remote.local_machine(), document))
                legacy = state / owner.id / LEGACY_COPIES
                backup_root = production_backup_root()
                migrated = backup_root / owner.id / LEGACY_COPIES

                def no_mirror(identity: staff.StaffId) -> log_backup.BackupMirror:
                    raise AssertionError(f"{identity}: no Git history to publish")

                cases = (("before migration", "running", 0, 1),
                         ("migration conflict", "running", 0, 2),
                         ("after migration", "running", 0, 2),
                         ("after wrap", "wrapped", 2, 2),
                         ("while paused", "paused", 1, 2))
                with self.subTest(stage="wrap while the first copy holds the lock"):
                    _ = document.write_text(f"- **Showrunner checkout:** `{root}`\n"
                                            + "- **Merge branch:** `integration`\n- **Log:** `LOG.md`\n")
                    _ = log.write_bytes(b"LOG during wrap\n")
                    state_file = state / owner.id / "state.json"
                    state_file.parent.mkdir(parents=True)
                    _ = state_file.write_text("{}")

                    def wrap_during_check(member: staff.StaffRow) -> Production:
                        production = read_production(member)
                        self.assertFalse(legacy.exists())
                        _ = staff.set_doc(owner.id, staff.WrappedDoc(remote.local_machine(), document))
                        with self.assertRaises(tidy_wrapped.TidyRefused):
                            _ = tidy_wrapped.tidy(owner.name)
                        self.assertTrue(state_file.exists())
                        return production

                    with (patch.object(tidy_wrapped, "STATE_DIR", state),
                          patch.object(copies, "remove_trees"),
                          patch.object(record_store, "staff_store_folders", return_value=())):
                        with patch.object(log_backup, "read_production", side_effect=wrap_during_check):
                            log_backup.backup_during_check(backup_root, state, owner.id)
                        (copied,) = legacy.iterdir()
                        _ = tidy_wrapped.tidy(owner.name)
                        self.assertEqual((migrated / copied.name).read_bytes(), b"LOG during wrap\n")
                        log_backup.backup_during_check(backup_root, state, owner.id)
                        self.assertFalse(state_file.parent.exists())
                    _ = staff.set_doc(owner.id, staff.LiveDoc(remote.local_machine(), document))
                    (migrated / copied.name).unlink()

                for stage, status, code, count in cases:
                    with self.subTest(stage=stage):
                        if stage == "migration conflict":
                            migrated.mkdir(parents=True, exist_ok=True)
                            (copied,) = legacy.iterdir()
                            conflict = migrated / copied.name
                            _ = conflict.write_bytes(b"Different content")
                            self.assertEqual(log_backup.backup_all(backup_root, state, staff.StaffSnapshot(()),
                                                                  no_mirror, 1, hourly=True), 1)
                            conflict.unlink()
                        elif stage == "after migration":
                            self.assertEqual(log_backup.backup_all(backup_root, state, staff.StaffSnapshot(()),
                                                                  no_mirror, 1, hourly=True), 0)
                            expected = {path.name: path.read_bytes() for path in legacy.iterdir()}
                            self.assertEqual({path.name: path.read_bytes() for path in migrated.iterdir()}, expected)
                        _ = document.write_text(f"> **Status: PRODUCTION — {status}.**\n"
                                                + f"- **Showrunner checkout:** `{root}`\n"
                                                + "- **Merge branch:** `integration`\n- **Log:** `LOG.md`\n")
                        _ = log.write_text(stage)
                        result = subprocess.run(["zsh", str(check), owner.id], capture_output=True, text=True,
                                                env={**os.environ, "XDG_STATE_HOME": str(root)},
                                                check=False, timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual((result.returncode, result.stdout, result.stderr), (code, "", ""))
                        self.assertEqual(len(list(legacy.glob("log-*.md"))), count)

    def test_backup_restores_both_repositories_and_retries_after_wrap(self) -> None:
        with (TemporaryDirectory() as folder,
              patch.dict(os.environ, {**fixture_git_environment(Path(folder)),
                         "GIT_CONFIG_SYSTEM": os.devnull}, clear=True)):
            root = Path(folder)
            state = root / "staff-state"
            checkout = root / "worktree"
            checkout.mkdir()
            document = checkout / "demo-production.md"
            log = checkout / "LOG.md"
            notes = root / "staged-notes"
            notes.mkdir()
            document_bytes = (f"- **Showrunner checkout:** `{checkout}`\r\n"
                              "- **Merge branch:** `integration`\r\n- **Log:** `LOG.md`\r\n").encode()
            _ = document.write_bytes(document_bytes)
            _ = log.write_bytes(b"First entry\r\n")
            note = notes / "review.txt"
            _ = note.write_bytes(b"\x00\xff\r\n")
            _ = (notes / ".gitattributes").write_text("* text eol=lf\n")
            mirror = root / "mac.git"
            mirror.mkdir()
            _ = log_backup.git(mirror, "init", "--bare", "--initial-branch=main")

            def measure(repack: bool) -> str:
                if repack:
                    _ = log_backup.git(mirror, "gc", "--no-prune")
                return log_backup.command("du", "-sk", str(mirror))

            def destination(identity: staff.StaffId) -> log_backup.BackupMirror:
                self.assertEqual(identity, owner.id)
                return log_backup.BackupMirror(str(mirror), measure)

            with isolated_staff_file(root / "staff.json", {}), patch.dict(os.environ, {"HOME": str(root)}):
                owner = fixture_member("showrunner", name="backup-owner",
                                       doc=staff.LiveDoc(remote.local_machine(), document))
                live = staff.local_snapshot()
                legacy = state / owner.id / LEGACY_COPIES
                legacy.mkdir(parents=True)
                _ = (legacy / "log-old.md").write_bytes(b"Original copy\r\n")
                backup_root = production_backup_root()
                repository = production_backup_checkout(owner.id)
                with patch.object(log_backup, "staged_notes_directory", return_value=notes):
                    protected = checkout / "document" / "live.md"
                    protected.parent.mkdir()
                    _ = protected.write_bytes(b"Live document\n")
                    _ = log_backup.git(checkout, "init", "--initial-branch=main")
                    repository.parent.mkdir(parents=True)
                    with self.subTest(destination="link to production checkout"):
                        repository.symlink_to(checkout, target_is_directory=True)
                        self.assertEqual(log_backup.backup_all(backup_root, state, live, destination, 1), 1)
                        self.assertEqual(protected.read_bytes(), b"Live document\n")
                        repository.unlink()
                    displaced = mirror.with_name("unavailable.git")
                    _ = mirror.rename(displaced)
                    self.assertEqual(log_backup.backup_all(backup_root, state, live, destination, 1), 1)
                    first = log_backup.git(repository, "rev-parse", "HEAD")
                    mirror = displaced.rename(mirror)
                    for item in (document, log):
                        item.unlink()
                    with self.subTest(wrap="new legacy copy before the next backup"):
                        late = legacy / "log-before-wrap.md"
                        _ = late.write_bytes(b"Late LOG copy\r\n")
                        wrapped_owner = replace(owner, doc=staff.WrappedDoc(remote.local_machine(), document))
                        identities = QuestionStaff.for_record(staff.StaffSnapshot((wrapped_owner,)))
                        with (patch.object(tidy_wrapped, "STATE_DIR", state),
                              patch.object(copies, "remove_trees"),
                              patch.object(record_store, "staff_store_folders", return_value=())):
                            _ = tidy_wrapped.tidy(owner.name, identities=identities)
                        self.assertFalse(legacy.exists())
                        self.assertEqual((backup_root / owner.id / LEGACY_COPIES / late.name).read_bytes(),
                                         b"Late LOG copy\r\n")
                    wrapped = staff.StaffSnapshot(())
                    self.assertEqual(log_backup.backup_all(backup_root, state, wrapped, destination, 1), 0)
                    self.assertEqual(log_backup.git(mirror, "rev-parse", "HEAD"), first)
                    self.assertEqual((backup_root / owner.id / LEGACY_COPIES / "log-old.md").read_bytes(),
                                     b"Original copy\r\n")

                    _ = document.write_bytes(document_bytes)
                    _ = log.write_bytes(b"Second entry\r\n")
                    self.assertEqual(log_backup.backup_all(backup_root, state, live, destination, 1), 0)
                    self.assertEqual(log_backup.backup_all(backup_root, state, live, destination, 1), 0)
                    second = log_backup.git(repository, "rev-parse", "HEAD")
                    self.assertEqual(log_backup.git(repository, "rev-list", "--count", second), "2")
                    for label, source in (("natedev", repository), ("mac", mirror)):
                        restored = root / label
                        _ = log_backup.command("git", "clone", "--quiet", "--no-checkout", str(source), str(restored))
                        for commit, content in ((second, b"Second entry\r\n"), (first, b"First entry\r\n")):
                            with self.subTest(label=label, commit=commit):
                                archive = root / f"{label}-{commit}.tar"
                                _ = log_backup.git(restored, "archive", f"--output={archive}", commit)
                                destination_folder = root / f"{label}-restore-{commit}"
                                destination_folder.mkdir()
                                with tarfile.open(archive) as files:
                                    files.extractall(destination_folder, filter="data")
                                expected = {f"{BACKUP_DOCUMENT_DIRECTORY}/demo-production.md": document_bytes,
                                            "log/LOG.md": content, "notes/review.txt": note.read_bytes(),
                                            "notes/.gitattributes": b"* text eol=lf\n"}
                                self.assertEqual({str(path.relative_to(destination_folder))
                                                  for path in destination_folder.rglob("*") if path.is_file()},
                                                 set(expected))
                                for name, data in expected.items():
                                    self.assertEqual((destination_folder / name).read_bytes(), data)
