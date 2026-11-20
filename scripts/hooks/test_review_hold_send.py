"""Check the sender hook with private Claude session records and review state."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import cast, final, override
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


HOOK = Path(__file__).resolve().with_name("pre-tool-use-review-hold-send.py")
SESSION = "review-session"
NAME = "review owner"
MESSAGE = "The checks completed."


@final
class ReviewHoldSendTests(unittest.TestCase):
    home: Path = Path()
    config: Path = Path()
    socket: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.home / "configured"
        self.socket = self.home / "review.sock"
        conf = self.home / "config"
        conf.mkdir()
        _ = (conf / "send.conf").write_text(
            "# range: 0.001..\nreview_hold_idle_minutes=60\n", encoding="utf-8")
        self.environment = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.config), "CONF_DIR": str(conf)}

    def write_session(self, config: Path) -> Path:
        sessions = config / "sessions"
        sessions.mkdir(parents=True)
        record = sessions / "42.json"
        _ = record.write_text(json.dumps({"pid": 42, "sessionId": SESSION, "name": NAME,
                                          "messagingSocketPath": str(self.socket), "updatedAt": 1}), encoding="utf-8")
        return record

    def open_hold(self) -> Path:
        directory = self.home / ".local/state/adhoc_review"
        holds = directory / ".holds"
        holds.mkdir(parents=True)
        _ = (directory / SESSION).write_text("1\n", encoding="utf-8")
        state = holds / f"{SESSION}.json"
        _ = state.write_text(json.dumps({"last_activity": time.time(), "messages": []}), encoding="utf-8")
        return state

    def run_hook(self, target: str, message: str = MESSAGE) -> subprocess.CompletedProcess[str]:
        payload = {"tool_name": "SendMessage", "tool_input": {"to": target, "message": message}}
        return subprocess.run([sys.executable, "-S", "-B", str(HOOK)], input=json.dumps(payload),
                              env=self.environment, capture_output=True, text=True, check=False,
                              timeout=TEST_HANG_GUARD_SECS)

    def assert_allowed(self, target: str, message: str = MESSAGE) -> None:
        result = self.run_hook(target, message)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def assert_denied(self, target: str) -> None:
        result = self.run_hook(target)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        payload = cast(dict[str, dict[str, str]], json.loads(result.stdout))
        decision = payload["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        reason = decision["permissionDecisionReason"]
        self.assertIn("The session is in a review.", reason)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to 'review owner'", reason)
        self.assertIn("Send the same text", reason)
        self.assertIn("KEPT", reason)
        self.assertNotIn(MESSAGE, reason)

    def test_denies_a_held_session_by_socket_in_the_config_override(self) -> None:
        _ = self.write_session(self.config)
        _ = self.open_hold()
        self.assert_denied(f"uds:{self.socket}")

    def test_denies_a_held_session_by_name_in_a_home_config_dir(self) -> None:
        _ = self.write_session(self.home / ".claude2")
        _ = self.open_hold()
        self.assert_denied(NAME)

    def test_allows_a_session_without_a_hold(self) -> None:
        _ = self.write_session(self.config)
        self.assert_allowed(NAME)

    def test_allows_an_unknown_target(self) -> None:
        _ = self.write_session(self.config)
        _ = self.open_hold()
        self.assert_allowed("unknown")

    def test_allows_an_urgent_message(self) -> None:
        _ = self.write_session(self.config)
        _ = self.open_hold()
        self.assert_allowed(NAME, "URGENT: The build needs attention.")

    def test_allows_a_send_when_a_session_file_is_invalid(self) -> None:
        record = self.write_session(self.config)
        _ = self.open_hold()
        _ = record.with_name("43.json").write_text("{unreadable", encoding="utf-8")
        self.assert_allowed(NAME)

    def test_allows_a_send_when_the_hold_file_is_invalid(self) -> None:
        _ = self.write_session(self.config)
        state = self.open_hold()
        _ = state.write_text("{unreadable", encoding="utf-8")
        self.assert_allowed(NAME)


if __name__ == "__main__":
    _ = unittest.main()
