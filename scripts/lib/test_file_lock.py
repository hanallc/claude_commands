"""Keep exclusive locks across concurrent operations and exceptions."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.lib import file_lock
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS, LauncherHangGuard


class FileLockTests(unittest.TestCase):
    def test_probe_checks_an_existing_lock_without_creating_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operation.lock"
            self.assertIs(file_lock.probe_existing_exclusive(path), file_lock.LockAvailability.AVAILABLE)
            self.assertFalse(path.exists())
            with file_lock.exclusive(path, wait=False) as acquired:
                self.assertIs(acquired, file_lock.LockResult.ACQUIRED)
                self.assertIs(file_lock.probe_existing_exclusive(path), file_lock.LockAvailability.HELD)
            self.assertIs(file_lock.probe_existing_exclusive(path), file_lock.LockAvailability.AVAILABLE)

    def test_closing_a_parent_handle_keeps_the_inherited_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operation.lock"
            parent = file_lock.take_exclusive(path)
            assert isinstance(parent, file_lock.ExclusiveLock)
            inherited = file_lock.ExclusiveLock(os.dup(parent.descriptor))
            parent.close()
            try:
                self.assertIs(file_lock.take_exclusive(path), file_lock.LockResult.BUSY)
                with file_lock.exclusive(path, wait=False) as held:
                    self.assertIs(held, file_lock.LockResult.BUSY)
            finally:
                inherited.release()
            with file_lock.exclusive(path, wait=False) as released:
                self.assertIs(released, file_lock.LockResult.ACQUIRED)

    def test_blocking_acquire_continues_after_the_holder_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=1) as executor:
            path = Path(directory) / "operation.lock"
            entered, acquired = threading.Event(), threading.Event()

            def acquire() -> file_lock.LockResult:
                entered.set()
                with file_lock.exclusive(path) as result:
                    acquired.set()
                    return result

            with file_lock.exclusive(path) as held:
                self.assertIs(held, file_lock.LockResult.ACQUIRED)
                contender = executor.submit(acquire)
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS), "The lock contender did not start.")
                self.assertFalse(acquired.is_set())
            guard = LauncherHangGuard("The lock contender did not return after the holder released the lock")
            self.assertIs(guard.result(contender), file_lock.LockResult.ACQUIRED)
            self.assertTrue(acquired.is_set())

    def test_nonblocking_acquire_reports_a_busy_lock_without_entering_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operation.lock"
            with file_lock.exclusive(path):
                with file_lock.exclusive(path, wait=False) as result:
                    self.assertIs(result, file_lock.LockResult.BUSY)
            with file_lock.exclusive(path, wait=False) as result:
                self.assertIs(result, file_lock.LockResult.ACQUIRED)

    def test_exclusive_acquires_a_write_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.lock"
            path.touch()
            path.chmod(0o200)
            with file_lock.exclusive(path) as result:
                self.assertIs(result, file_lock.LockResult.ACQUIRED)

    def test_an_exception_releases_the_lock_for_the_next_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operation.lock"
            with self.assertRaisesRegex(RuntimeError, "operation failed"):
                with file_lock.exclusive(path):
                    raise RuntimeError("operation failed")
            with file_lock.exclusive(path, wait=False) as result:
                self.assertIs(result, file_lock.LockResult.ACQUIRED)
