"""Check remote mirror setup and lease order in private folders."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment
from scripts.lib import build_folders, file_lock, scratch
from scripts.lint.tmp_sweep import ProcessUsesKnown
from scripts.mac_test import floor_claims


class StepOneRows(unittest.TestCase):
    root: Path = Path()
    tree: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, copies_environment(self.root))))
        self.tree = build_folders.build_root() / "task" / "mirror"
        _ = self.enterContext(mock.patch.object(copies, "scan_mirror_processes",
                                                return_value=copies.MirrorProcessScan(ProcessUsesKnown([]), frozenset())))

    def test_probe_setup_takes_and_releases_the_mirror_lease(self) -> None:
        """Remote mirror probe setup row 3."""
        self.tree.parent.mkdir(parents=True)
        lock_path = floor_claims.mirror_lock_path(self.tree)
        with lock_path.open("a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(copies.CopyError, "busy"):
                copies.probe_remote_folder(str(self.tree))
            self.assertFalse(self.tree.exists())
            fcntl.flock(holder, fcntl.LOCK_UN)
        copies.probe_remote_folder(str(self.tree))
        with file_lock.exclusive(lock_path, wait=False) as lock:
            self.assertIs(lock, file_lock.LockResult.ACQUIRED)
        self.assertTrue((self.tree / floor_claims.MIRROR_MARK).is_file())

    def test_probe_setup_publishes_copy_metadata(self) -> None:
        """Remote mirror probe setup row 4."""
        with mock.patch.object(scratch, "exclude_copy_metadata", wraps=scratch.exclude_copy_metadata) as exclude:
            copies.probe_remote_folder(str(self.tree))
        exclude.assert_called_once_with(self.tree, additional=floor_claims.MIRROR_METADATA)
        self.assertEqual(floor_claims.read_mirror_record(self.tree / floor_claims.MIRROR_RECORD),
                         {"kind": "remote-mirror", "owner": "probe", "repo": "probe", "session": "probe"})
        self.assertEqual((self.tree / floor_claims.MIRROR_SESSION).read_text(), "probe\n")
        self.assertEqual((self.tree / floor_claims.MIRROR_MARK).read_text(), "registered remote mirror\n")

    def test_remote_host_contention_precedes_the_mirror_lock(self) -> None:
        """Mirror lease helpers row 2."""
        host = "fixture-host"
        lease = copies.remote_registry() / f"{copies.remote_key(host, '/fixture')}.lease"
        with copies.remote_host_lease(host, exclusive=True):
            with self.assertRaises(copies.BusyRemoteHost), copies.remote_mirror_lease(host, lease):
                self.fail("The host lease is held")
            self.assertFalse(lease.exists())
        with file_lock.exclusive(lease, wait=False) as lock:
            self.assertIs(lock, file_lock.LockResult.ACQUIRED)
            with self.assertRaisesRegex(copies.BusyRemoteMirror, "mirror lease is busy"):
                with copies.remote_mirror_lease(host, lease):
                    self.fail("The mirror lease is held")

    def test_local_lease_checks_the_parent_lock_inode(self) -> None:
        """Mirror lease helpers row 4."""
        lock_path = floor_claims.mirror_parent_lock_path(self.tree)
        flock = fcntl.flock

        def replace_lock(descriptor: int, operation: int) -> None:
            flock(descriptor, operation)
            if lock_path.is_file() and os.fstat(descriptor).st_ino == lock_path.stat().st_ino:
                _ = lock_path.rename(lock_path.with_suffix(".old"))
                lock_path.touch()

        with (mock.patch.object(fcntl, "flock", side_effect=replace_lock),
              mock.patch.object(build_folders, "build_folder_creation",
                                wraps=build_folders.build_folder_creation) as guard,
              self.assertRaisesRegex(copies.CopyError, "mirror lease"),
              copies.local_mirror_lease(self.tree, "fixture")):
            self.fail("The lock file changed")
        guard.assert_called_once_with(self.tree)
        self.assertFalse(floor_claims.mirror_lock_path(self.tree).exists())

    def test_local_lease_creates_parent_and_holds_mirror_lock(self) -> None:
        """Mirror lease helpers row 5."""
        self.assertFalse(self.tree.parent.exists())
        with copies.local_mirror_lease(self.tree, "fixture"):
            self.assertTrue(self.tree.is_dir())
            with file_lock.exclusive(floor_claims.mirror_lock_path(self.tree), wait=False) as lock:
                self.assertIs(lock, file_lock.LockResult.BUSY)
        with file_lock.exclusive(floor_claims.mirror_lock_path(self.tree), wait=False) as lock:
            self.assertIs(lock, file_lock.LockResult.ACQUIRED)
