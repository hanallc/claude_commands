"""Check mirror setup and lease order with private state."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment
from scripts.lib import build_folders
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import tmp_sweep
from scripts.mac_test import floor_claims


class MirrorLeaseOrderTests(unittest.TestCase):
    root: Path = Path()
    tree: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, copies_environment(self.root))))
        self.tree = build_folders.build_root() / "task" / "mirror"

    def remote_mirror(self, session: str) -> copies.RemoteMirror:
        return copies.decode_remote_mirror(json.dumps(
            {"host": "fixture", "path": "/private/mirror", "owner": "unit",
             "repo": "fixture", "session": session}))

    def test_remote_probe_marker_prevents_setup_worker(self) -> None:
        """Remote mirror probe setup row 1."""
        self.tree.mkdir(parents=True)
        (self.tree / floor_claims.MIRROR_MARK).touch()
        worker = self.root / ".claude/scripts/lib/py"
        worker.parent.mkdir(parents=True)
        called = self.root / "called"
        _ = worker.write_text(f'#!/bin/sh\ntouch "{called}"\nexit 42\n')
        worker.chmod(0o700)
        script = copies.mirror_probe_setup(str(self.tree))
        done = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                              timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(called.exists())

    def test_existing_remote_mirror_skips_local_lease_and_setup(self) -> None:
        """Remote mirror probe setup row 2."""
        self.tree.mkdir(parents=True)
        with (mock.patch.object(copies, "local_mirror_lease", wraps=copies.local_mirror_lease) as lock,
              mock.patch.object(copies, "mark_mirror", wraps=copies.mark_mirror) as setup):
            copies.probe_remote_folder(str(self.tree))
        lock.assert_not_called()
        setup.assert_not_called()
        self.assertFalse((self.tree / floor_claims.MIRROR_MARK).exists())

    def test_remote_lease_creates_host_path_before_flock(self) -> None:
        """Mirror lease helpers row 1."""
        mirror = self.remote_mirror("caller")
        registry = copies.remote_registry()
        host = registry / (copies.remote_key(mirror.host, "") + ".host-lease")
        self.assertFalse(host.exists())
        flock = fcntl.flock
        calls: list[tuple[int, int]] = []

        def lock(fd: int, operation: int) -> None:
            calls.append((os.fstat(fd).st_ino, operation))
            self.assertTrue(host.is_file())
            flock(fd, operation)

        with mock.patch.object(fcntl, "flock", side_effect=lock), copies.remote_lease(mirror):
            self.assertEqual(calls[0], (host.stat().st_ino, fcntl.LOCK_SH | fcntl.LOCK_NB))

    def test_busy_remote_lease_reads_the_saved_session(self) -> None:
        """Mirror lease helpers row 3."""
        holder = self.remote_mirror("saved-holder")
        caller = self.remote_mirror("caller")
        copies.register_remote(holder)
        with (copies.remote_lease(holder),
              mock.patch.object(copies, "read_remote_mirror", wraps=copies.read_remote_mirror) as read):
            with self.assertRaisesRegex(copies.BusyRemoteMirror, "held by session saved-holder"):
                with copies.remote_lease(caller):
                    self.fail("The caller acquired the held lease")
        read.assert_called_once_with(copies.remote_record_path(holder))

    def test_process_scan_runs_while_both_mirror_leases_are_held(self) -> None:
        """Mirror lease helpers row 6."""
        def scan(paths: tuple[Path, ...]) -> copies.MirrorProcessScan:
            self.assertEqual(paths, (self.tree,))
            for path in (floor_claims.mirror_parent_lock_path(self.tree), floor_claims.mirror_lock_path(self.tree)):
                with path.open("a+") as stream, self.assertRaises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return copies.MirrorProcessScan(tmp_sweep.ProcessUsesKnown([]), frozenset())

        with mock.patch.object(copies, "scan_mirror_processes", side_effect=scan) as observed:
            with copies.local_mirror_lease(self.tree, "fixture"):
                self.assertTrue(self.tree.is_dir())
        observed.assert_called_once_with((self.tree,))

    def test_unreadable_process_scan_reports_holder_before_setup(self) -> None:
        """Mirror lease helpers row 7."""
        self.tree.mkdir(parents=True)
        session = self.tree / floor_claims.MIRROR_SESSION
        _ = session.write_text("saved-holder\n")
        with (mock.patch.object(copies, "scan_mirror_processes",
                               return_value=copies.MirrorProcessScan(tmp_sweep.ProcessUsesIncomplete(), frozenset())),
              self.assertRaisesRegex(copies.CopyError, "held by session saved-holder")):
            with copies.local_mirror_lease(self.tree, "caller"):
                self.fail("The caller acquired a mirror with an unreadable process scan")
        self.assertEqual(session.read_text(), "saved-holder\n")

    def test_probe_setup_sees_mirror_and_session_before_publishing_metadata(self) -> None:
        """Mirror lease helpers row 8."""
        mark = copies.mark_mirror

        def publish(tree: Path, owner: str, repo: str, session: str, **extra: object) -> None:
            self.assertTrue(tree.is_dir())
            self.assertEqual((tree / floor_claims.MIRROR_SESSION).read_text(), "probe\n")
            mark(tree, owner, repo, session, **extra)

        with (mock.patch.object(copies, "scan_mirror_processes",
                                return_value=copies.MirrorProcessScan(tmp_sweep.ProcessUsesKnown([]), frozenset())),
              mock.patch.object(copies, "mark_mirror", side_effect=publish) as setup):
            copies.probe_remote_folder(str(self.tree))
        setup.assert_called_once_with(self.tree, "probe", "probe", "probe")
        self.assertTrue((self.tree / floor_claims.MIRROR_MARK).is_file())

    def test_mirror_lease_clears_context_and_closes_descriptors_on_exit(self) -> None:
        """Mirror lease helpers row 9."""
        before = copies.COMMAND_LOCKS.get()
        held: tuple[int, ...] = ()
        with (mock.patch.object(copies, "scan_mirror_processes",
                                return_value=copies.MirrorProcessScan(tmp_sweep.ProcessUsesKnown([]), frozenset())),
              self.assertRaisesRegex(RuntimeError, "fixture")):
            with copies.local_mirror_lease(self.tree, "fixture"):
                held = copies.COMMAND_LOCKS.get()[len(before):]
                self.assertGreaterEqual(len(held), 2)
                self.assertEqual(os.fstat(held[-1]).st_ino, floor_claims.mirror_lock_path(self.tree).stat().st_ino)
                raise RuntimeError("fixture")
        self.assertEqual(copies.COMMAND_LOCKS.get(), before)
        for descriptor in held:
            with self.assertRaises(OSError):
                _ = os.fstat(descriptor)
