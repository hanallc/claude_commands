"""Check direct calls from sessions with older Stop settings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class StopBannedWordsHookTests(unittest.TestCase):
    def test_legacy_path_exits_silently_without_writing_state(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        guide = home / "rust/nate_style/rust/forbidden-words.md"
        guide.parent.mkdir(parents=True)
        _ = guide.write_text('### "zorblax"\n\nDo not use it.\n', encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("stop-assistant-prose-banned-words.py"))],
            input=json.dumps({"session_id": "session-1234", "last_assistant_message": "A zorblax reply."}),
            env={**os.environ, "HOME": str(home)}, cwd=home, capture_output=True,
            text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse((home / ".claude/state").exists())


if __name__ == "__main__":
    _ = unittest.main()
