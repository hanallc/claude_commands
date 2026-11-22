"""Check the main push lock hold record."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast, override

from unittest.mock import patch

from scripts.lib.git_test_support import git_fixture_environment
from scripts.lib import file_lock, main_push_lock
from scripts.lib.process_start import Unreadable
from scripts.production import land_queue, land_runs


class MainPushLockRecordTests(unittest.TestCase):
    checkout: Path = Path()
    record: Path = Path()

    @override
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, git_fixture_environment(), clear=True))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.checkout = Path(folder.name)
        self.enterContext(patch.dict(os.environ, {
            "HOME": folder.name, "CONF_DIR": str(self.checkout / "config"),
            "MODELS_CONFIG_FILE": str(self.checkout / "config/models.conf"),
            "GATE_STATE_DIR": str(self.checkout / "land-state"),
            "XDG_STATE_HOME": str(self.checkout / "state"),
        }))
        _ = subprocess.run(["git", "init", "-q", str(self.checkout)], check=True, timeout=30)
        self.record = self.checkout / ".git" / main_push_lock.RECORD_NAME

    def lines(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in self.record.read_text().splitlines()]

    def test_each_hold_appends_holder_wait_and_hold_seconds(self) -> None:
        with main_push_lock.main_push_lock(self.checkout, "first"):
            time.sleep(0.05)
        with main_push_lock.main_push_lock(self.checkout, "second"):
            pass
        first, second = self.lines()
        self.assertEqual((first["holder"], second["holder"]), ("first", "second"))
        self.assertGreaterEqual(cast(float, first["held_seconds"]), 0.05)
        self.assertGreaterEqual(cast(float, second["waited_seconds"]), 0.0)
        self.assertEqual(sorted(first), ["acquired_at", "held_seconds", "holder", "pid", "waited_seconds"])

    def test_a_nested_hold_writes_one_line(self) -> None:
        with main_push_lock.main_push_lock(self.checkout, "outer"):
            with main_push_lock.main_push_lock(self.checkout, "inner"):
                pass
        self.assertEqual([line["holder"] for line in self.lines()], ["outer"])

    def test_settings_push_leaves_the_line_free_and_nonblocking_admission_returns_busy(self) -> None:
        with patch.dict(os.environ, {"GATE_STATE_DIR": str(self.checkout / "land-state")}):
            with main_push_lock.settings_push(self.checkout) as publisher:
                self.assertIsInstance(publisher, land_queue.SettingsGap)
                path = self.checkout / "land-state/land_queue.json.lock"
                with file_lock.exclusive(path, wait=False) as line:
                    self.assertIs(line, file_lock.LockResult.ACQUIRED)
                with main_push_lock.settings_push(self.checkout) as timer:
                    self.assertIsInstance(timer, land_queue.SettingsGapBusy)

    def test_settings_push_requires_a_readable_land_identity(self) -> None:
        with land_queue.landing(["a" * 40]):
            with patch.object(land_runs, "process_start", return_value=Unreadable("fixture read failed")):
                with main_push_lock.settings_push(self.checkout) as publisher:
                    self.assertIsInstance(publisher, land_queue.SettingsGapBusy)

    def test_settings_push_waits_when_land_records_cannot_be_read(self) -> None:
        folder = land_runs.lands_dir()
        folder.mkdir(parents=True)
        record = folder / "broken.json"
        _ = record.write_text("{")
        with main_push_lock.settings_push(self.checkout) as publisher:
            self.assertIsInstance(publisher, land_queue.SettingsGapBusy)
        record.unlink()
        with patch.object(Path, "iterdir", side_effect=PermissionError("fixture permission error")):
            with main_push_lock.settings_push(self.checkout) as publisher:
                self.assertIsInstance(publisher, land_queue.SettingsGapBusy)
        with main_push_lock.settings_push(self.checkout) as publisher:
            self.assertIsInstance(publisher, land_queue.SettingsGap)

    def test_lines_past_the_retention_go(self) -> None:
        old = json.dumps({"holder": "old", "acquired_at": time.time() - 15 * 86400})
        recent = json.dumps({"holder": "recent", "acquired_at": time.time() - 86400})
        _ = self.record.write_text(f"{old}\n{recent}\n")
        with main_push_lock.main_push_lock(self.checkout, "now"):
            pass
        self.assertEqual([line["holder"] for line in self.lines()], ["recent", "now"])


if __name__ == "__main__":
    _ = unittest.main()
