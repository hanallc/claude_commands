"""Check the sender hook with private Claude session records."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from typing import cast, final, override
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message.process_identity import process_start


HOOK = Path(__file__).resolve().with_name("pre-tool-use-review-hold-send.py")
SESSION = "review-session"
NAME = "review owner"
MESSAGE = "The checks completed."


@final
class ReviewHoldSendTests(unittest.TestCase):
    home: Path = Path()
    config: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.home / "configured"
        self.environment = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.config)}

    def write_session(self, config: Path, *, started: str) -> None:
        pid = os.getpid()
        sessions = config / "sessions"
        sessions.mkdir(parents=True)
        _ = (sessions / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "procStart": started, "sessionId": SESSION, "name": NAME,
            "messagingSocketPath": str(config / "session.sock"), "updatedAt": 1,
        }), encoding="utf-8")

    def write_live_session(self, config: Path) -> None:
        started = process_start(os.getpid())
        assert started
        self.write_session(config, started=started)

    def run_hook(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-S", "-B", str(HOOK)], input=payload,
                              env=self.environment, capture_output=True, text=True, check=False,
                              timeout=TEST_HANG_GUARD_SECS)

    def send(self, target: str, message: str = MESSAGE) -> subprocess.CompletedProcess[str]:
        return self.run_hook(json.dumps({"tool_name": "SendMessage", "tool_input": {"to": target, "message": message}}))

    def assert_denied(self, result: subprocess.CompletedProcess[str], target: str, message: str = MESSAGE) -> None:
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertTrue(result.stdout, f"The hook must refuse {target}.")
        payload = cast(dict[str, dict[str, str]], json.loads(result.stdout))
        decision = payload["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        reason = decision["permissionDecisionReason"]
        self.assertIn(
            f'~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to {shlex.quote(target)} '
            + '--from "<your session name>" --text "<same text>"', reason,
        )
        self.assertIn("Add `--urgent` for an URGENT message.", reason)
        self.assertNotIn(message, reason)

    def test_denies_explicit_addresses_without_registry_records(self) -> None:
        for target in (f"uds:{self.home / 'session.sock'}", f"session:{SESSION}"):
            with self.subTest(target=target):
                self.assert_denied(self.send(target), target)

    def test_denies_a_live_registry_name_with_or_without_a_ref(self) -> None:
        self.write_live_session(self.home / ".claude2")
        for target in (NAME, f"{NAME} [ref]"):
            with self.subTest(target=target):
                self.assert_denied(self.send(target), NAME)

    def test_denies_an_urgent_message_to_a_session(self) -> None:
        self.write_live_session(self.config)
        message = "URGENT: The build needs attention."
        self.assert_denied(self.send(NAME, message), NAME, message)

    def test_denies_a_live_session_beside_an_invalid_session_file(self) -> None:
        sessions = self.home / ".claude2" / "sessions"
        sessions.mkdir(parents=True)
        record = sessions / "42.json"
        self.write_live_session(self.config)
        for invalid in ("{incomplete", "[]", json.dumps({"name": NAME})):
            with self.subTest(invalid=invalid):
                _ = record.write_text(invalid, encoding="utf-8")
                self.assert_denied(self.send(NAME), NAME)

    def test_allows_helper_names_without_a_live_registry_match(self) -> None:
        self.write_session(self.config, started="stale process start")
        for target in ("main", "helper", NAME):
            with self.subTest(target=target):
                result = self.send(target)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_allows_a_bad_payload(self) -> None:
        result = self.run_hook('{"tool_name": "SendMessage",')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


if __name__ == "__main__":
    _ = unittest.main()
