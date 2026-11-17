"""Check the main push lock hold record."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast, override

from scripts.lib import main_push_lock


class MainPushLockRecordTests(unittest.TestCase):
    checkout: Path = Path()
    record: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.checkout = Path(folder.name)
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

    def test_lines_past_the_retention_go(self) -> None:
        old = json.dumps({"holder": "old", "acquired_at": time.time() - 15 * 86400})
        recent = json.dumps({"holder": "recent", "acquired_at": time.time() - 86400})
        _ = self.record.write_text(f"{old}\n{recent}\n")
        with main_push_lock.main_push_lock(self.checkout, "now"):
            pass
        self.assertEqual([line["holder"] for line in self.lines()], ["recent", "now"])


if __name__ == "__main__":
    _ = unittest.main()
