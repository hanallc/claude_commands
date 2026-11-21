"""Use temporary Git repositories to verify backup and restore."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from tempfile import TemporaryDirectory
import tarfile
import unittest
from unittest.mock import patch

from scripts.agents import remote
from scripts.production import log_backup, staff
from scripts.production.production_names import BACKUP_CHECKOUT, BACKUP_ROOT, LEGACY_COPIES
from scripts.production.staff_test_support import fixture_member, isolated_staff_file


class LogBackupTests(unittest.TestCase):
    def test_production_check_reports_status_without_starting_backup(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            document = root / "production.md"
            check = Path(__file__).with_name("production_check.sh")
            with isolated_staff_file(root / "staff.json", {}):
                owner = fixture_member("showrunner", name="backup-owner",
                                       doc=staff.LiveDoc(remote.local_machine(), document))
                for status, code in (("running", 0), ("wrapped", 2), ("paused", 1)):
                    with self.subTest(status=status):
                        _ = document.write_text(f"> **Status: PRODUCTION — {status}.**\n")
                        result = subprocess.run(["zsh", str(check), owner.id], capture_output=True, text=True,
                                                env={**os.environ, "HOME": str(root), "XDG_STATE_HOME": str(root)},
                                                check=False, timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual((result.returncode, result.stdout, result.stderr), (code, "", ""))
                        self.assertFalse((root / BACKUP_ROOT).exists())

    def test_backup_restores_both_repositories_and_retries_after_wrap(self) -> None:
        with (TemporaryDirectory() as folder,
              patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})):
            root = Path(folder)
            state = root / "staff-state"
            backup_root = root / "backups"
            checkout = root / "worktree"
            checkout.mkdir()
            document = checkout / "demo-production.md"
            log = checkout / "LOG.md"
            notes = checkout / "notes"
            notes.mkdir()
            _ = document.write_bytes(b"Production\r\n")
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

            def destination(identity: str) -> log_backup.BackupMirror:
                self.assertEqual(identity, owner.id)
                return log_backup.BackupMirror(str(mirror), measure)

            with isolated_staff_file(root / "staff.json", {}):
                owner = fixture_member("showrunner", name="backup-owner",
                                       doc=staff.LiveDoc(remote.local_machine(), document))
                sources = log_backup.BackupSources(document, log, notes)
                live = staff.local_snapshot()
                legacy = state / owner.id / LEGACY_COPIES
                legacy.mkdir(parents=True)
                _ = (legacy / "log-old.md").write_bytes(b"Original copy\r\n")
                repository = backup_root / owner.id / BACKUP_CHECKOUT
                with patch.object(log_backup, "production_sources", return_value=sources):
                    displaced = mirror.with_name("unavailable.git")
                    _ = mirror.rename(displaced)
                    self.assertEqual(log_backup.backup_all(backup_root, state, live, destination, 1), 1)
                    first = log_backup.git(repository, "rev-parse", "HEAD")
                    mirror = displaced.rename(mirror)
                    for item in (document, log):
                        item.unlink()
                    wrapped = staff.StaffSnapshot(())
                    self.assertEqual(log_backup.backup_all(backup_root, state, wrapped, destination, 1), 0)
                    self.assertEqual(log_backup.git(mirror, "rev-parse", "HEAD"), first)
                    self.assertEqual((backup_root / owner.id / LEGACY_COPIES / "log-old.md").read_bytes(),
                                     b"Original copy\r\n")

                    _ = document.write_bytes(b"Production\r\n")
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
                                expected = {"document/demo-production.md": b"Production\r\n",
                                            "log/LOG.md": content, "notes/review.txt": note.read_bytes(),
                                            "notes/.gitattributes": b"* text eol=lf\n"}
                                self.assertEqual({str(path.relative_to(destination_folder))
                                                  for path in destination_folder.rglob("*") if path.is_file()},
                                                 set(expected))
                                for name, data in expected.items():
                                    self.assertEqual((destination_folder / name).read_bytes(), data)
