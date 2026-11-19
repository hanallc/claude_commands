"""Check direct calls from sessions with older PostToolUse settings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class PostToolUseBannedWordsTests(unittest.TestCase):
    def test_legacy_paths_exit_silently_without_writing_state(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        guide = home / "rust/nate_style/rust/forbidden-words.md"
        guide.parent.mkdir(parents=True)
        _ = guide.write_text('### "zorblatt"\n\nDo not use it.\n', encoding="utf-8")
        payload = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": str(home / "note.txt"), "content": "zorblatt"},
        })
        for filename in ("post-tool-use-banned-words.py", "post-tool-use-banned-words-block.py"):
            with self.subTest(filename=filename):
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name(filename))],
                    input=payload, env={**os.environ, "HOME": str(home)}, cwd=home,
                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
                )
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
                self.assertFalse((home / ".claude/state").exists())


if __name__ == "__main__":
    _ = unittest.main()
