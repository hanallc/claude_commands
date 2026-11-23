"""Real verify entry point cancellation with private Cargo and Git fixtures."""
from __future__ import annotations

import fcntl
import subprocess
import unittest

from scripts.direct.verify_tree_cancel_test_support import VerifyTreeCancelFixture as VerifyTreeCancelFixture
from scripts.lib.build_folders_test_support import tool_folders
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTreeCancelTests(VerifyTreeCancelFixture):
    def test_source_edit_cancels_once_releases_resources_and_records_no_verdict(self) -> None:
        process = self.start_verify("test")
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        self.edit_source()
        cancellation = self.assert_cancelled(process, "test")
        self.assertEqual(cancellation["path"], "sample/src/lib.rs")
        attempts = [args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]]
        self.assertEqual(len(attempts), 1, "cancelled call restarted its tests")

    def test_external_document_edit_does_not_cancel_test(self) -> None:
        process = self.start_verify("test", TEST_FINISH="1")
        self.wait_for_running(self.control / "running", process)
        _ = (self.workspace / "docs/design.md").write_text("new documentation\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = process.communicate(timeout=1)
        # A Cargo-owned handshake makes this a live span, not an already-ended run.
        _ = (self.control / "release").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotIn("cancelled", output + errors)
        self.assert_no_cache_verdict()
        self.assert_resources_released()

    def test_tree_watch_cleanup_preserves_the_pass_when_the_lock_is_held(self) -> None:
        self.write_tool(self.root / "copy_watch.py", "")
        process = self.start_verify("test", TEST_FINISH="1")
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        watch, = tool_folders(self.root / "tmp", "verify-tree", pid=process.pid)
        lock = watch.with_name(watch.name + ".lock")
        # Give the removal helper a lock it cannot take.
        _ = lock.rename(lock.with_suffix(".old"))
        with lock.open("x") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _ = (self.control / "release").touch()
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertTrue(watch.is_dir(), "cleanup removed a folder with a held lock")
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotIn("cannot remove the tree watch folder", errors)
        cache = self.board / "verify_cache"
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)
        self.assertEqual(list(cache.glob("*.fail")), [])
        self.assert_resources_released()


if __name__ == "__main__":
    _ = unittest.main()
