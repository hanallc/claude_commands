#!/usr/bin/env python3
"""Tests for session-start-temp-root.py, run as Claude Code runs it: JSON on stdin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

HOOK = Path(__file__).resolve().parent / "session-start-temp-root.py"


class SessionTempRootTests(unittest.TestCase):
    def run_hook(self, base: Path, payload: object, env_file: Path | None, **extra: str) -> None:
        environment = {**os.environ, "SESSION_FORMER_ROOT": str(base / "former"),
                       "SESSION_TEMP_ROOT": str(base / "session")}
        for name in ("CLAUDE_ENV_FILE", "CLAUDE_SESSION_FOLDER"):
            _ = environment.pop(name, None)
        environment.update(extra)
        if env_file is not None:
            environment["CLAUDE_ENV_FILE"] = str(env_file)
        _ = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), text=True, env=environment,
                           check=True)

    def assert_exported(self, env_file: Path, root: Path) -> None:
        self.assertEqual(env_file.read_text(), f"export TMPDIR={root}\n")
        shown = subprocess.run(["sh", "-c", f". {env_file}; printf %s \"$TMPDIR\""], capture_output=True,
                               text=True, check=True).stdout
        self.assertEqual(shown, str(root))

    def test_without_the_session_root_the_former_root_is_made_and_exported_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            env_file = base / "env.sh"
            for _ in range(2):
                self.run_hook(base, {"session_id": "abc-123"}, env_file)
            root = base / "former" / "abc-123"
            self.assertTrue(root.is_dir())
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertFalse((base / "session").exists())
            self.assert_exported(env_file, root)

    def test_with_the_session_root_a_session_without_staff_takes_its_working_folder_name(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "session").mkdir()
            env_file = base / "env.sh"
            for _ in range(2):
                self.run_hook(base, {"session_id": "abc123-x", "cwd": "/home/x/worktrees/hana-fix"}, env_file)
            root = base / "session" / "hana-fix"
            self.assertTrue(root.is_dir())
            self.assertEqual(os.readlink(base / "session" / ".id" / "abc123-x"), "hana-fix")
            self.assertFalse((base / "former").exists())
            self.assert_exported(env_file, root)

    def test_a_launcher_folder_is_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            made = base / "session" / "verify"
            made.mkdir(parents=True)
            _ = (made / ".session.json").write_text(json.dumps({"owner": "member-1", "session_id": ""}))
            env_file = base / "env.sh"
            self.run_hook(base, {"session_id": "new-id"}, env_file, TMPDIR=str(made), CLAUDE_SESSION_FOLDER="verify")
            self.assert_exported(env_file, made)
            record = cast(object, json.loads((made / ".session.json").read_text()))
            self.assertEqual(record, {"owner": "member-1", "session_id": "new-id"})

    def test_a_bad_session_id_or_no_env_file_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            self.run_hook(base, {"session_id": "../x"}, base / "env.sh")
            self.run_hook(base, {"session_id": "abc"}, None)
            self.assertFalse((base / "former").exists())
            self.assertFalse((base / "session").exists())
            self.assertFalse((base / "env.sh").exists())


if __name__ == "__main__":
    _ = unittest.main()
