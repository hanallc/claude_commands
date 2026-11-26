"""Check the records and decisions for push text approval requests."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, final, override
from unittest.mock import MagicMock, patch

from scripts.herdr import herdr
from scripts.herdr.herdr_types import HerdrFailure
from scripts.lib import conf, script_prompts
from scripts.notify import text_asks


@final
class TextAskTests(unittest.TestCase):
    pane = "w1:p2"
    session = "session-1"
    now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.ask_path = Path()
        self.approvals = Path()
        self.focused: dict[str, object] = {"focused": True, "agent_session": {"value": self.session}}
        self.reader = MagicMock(return_value=self.focused)

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ask_path = root / "text-asks" / f"{self.pane}.json"
        self.approvals = root / "text-approvals"
        config = root / "config"
        config.mkdir()
        _ = (config / "notify.conf").write_text("# range: 1..60\ntext_ask_minutes=7\n", encoding="utf-8")
        _ = (config / "script_prompts.conf").write_text("# whole: 1..\nmax_records=4096\n", encoding="utf-8")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "NOTIFY_STATE_DIR": str(root),
            "CONVERSATION_PAUSE_STATE_DIR": str(root / "pause"),
            "CONF_DIR": str(config),
        })))
        _ = cast(object, self.enterContext(patch.object(text_asks, "read_pane", self.reader)))

    def open_ask(self, at: datetime | None = None, *, text_id: str = "text", fingerprint: str = "digest") -> None:
        text_asks.open_ask(text_id, fingerprint, self.session, self.pane,
                           self.now - timedelta(minutes=6) if at is None else at)

    def test_no_ask_requires_only_one_stat(self) -> None:
        with (patch.object(Path, "stat", side_effect=FileNotFoundError) as stat,
              patch.object(Path, "open", side_effect=AssertionError("file read")),
              patch.object(conf, "read", side_effect=AssertionError("config read")),
              patch.object(script_prompts, "typed_by_script", side_effect=AssertionError("script read")),
              patch.object(script_prompts, "keys_since", side_effect=AssertionError("key mark read"))):
            result = text_asks.answer(self.session, self.pane, "yes", True, self.now)
        self.assertIsInstance(result, text_asks.NoAsk)
        stat.assert_called_once_with()
        self.reader.assert_not_called()

    def test_keys_after_the_ask_refuse_approval_until_the_mark_ages_out(self) -> None:
        asked_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.open_ask(asked_at)
        with (patch.object(herdr, "herdr_binary", return_value="fixture-herdr"),
              patch.object(herdr.Herdr, "pane_row", return_value=herdr.NoPane(self.pane)),
              patch.object(herdr, "check_target", return_value=True),
              patch.object(herdr.Herdr, "run", return_value="") as transport):
            client = herdr.Herdr("fixture")
            client.send_keys(self.pane, "o", target=self.session)
            client.send_keys(self.pane, "k", "Enter", target=self.session)
        self.assertEqual(transport.call_count, 2)
        self.assertFalse(script_prompts.typed_by_script(self.pane, "ok"))
        result = text_asks.answer(self.session, self.pane, "ok", True, datetime.now(timezone.utc))
        assert isinstance(result, text_asks.Refused)
        self.assertIn("printable keys", result.reason)
        self.assertTrue(self.ask_path.exists())
        self.assertFalse(self.approvals.exists())
        self.reader.assert_not_called()

        new_ask_time = datetime.now(timezone.utc) + timedelta(minutes=7, seconds=1)
        self.open_ask(new_ask_time)
        result = text_asks.answer(self.session, self.pane, "ok", True, new_ask_time + timedelta(seconds=1))
        self.assertIsInstance(result, text_asks.Approved)

    def test_recent_keys_before_the_ask_refuse_approval_after_enter(self) -> None:
        with (patch.object(herdr, "herdr_binary", return_value="fixture-herdr"),
              patch.object(herdr.Herdr, "pane_row", return_value=herdr.NoPane(self.pane)),
              patch.object(herdr, "check_target", return_value=True),
              patch.object(herdr.Herdr, "run", return_value="")):
            client = herdr.Herdr("fixture")
            client.send_keys(self.pane, "y", target=self.session)
            client.send_keys(self.pane, "e", "s", target=self.session)
            asked_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            self.open_ask(asked_at)
            client.send_keys(self.pane, "Enter", target=self.session)
        self.assertFalse(script_prompts.typed_by_script(self.pane, "yes"))
        self.assertFalse(script_prompts.keys_since(self.pane, asked_at))
        result = text_asks.answer(self.session, self.pane, "yes", True, asked_at + timedelta(seconds=1))
        assert isinstance(result, text_asks.Refused)
        self.assertIn("printable keys", result.reason)
        self.assertTrue(self.ask_path.exists())
        self.assertFalse(self.approvals.exists())
        self.reader.assert_not_called()

    def test_refuses_before_reading_herdr_when_source_session_time_or_script_fails(self) -> None:
        script_prompts.record(self.pane, "approve")
        for session, source_typed, age, prompt, reason in (
            (self.session, False, 6, "yes", "source"),
            ("other", True, 6, "yes", "another session"),
            (self.session, True, 7, "yes", "time limit"),
            (self.session, True, -1, "yes", "time limit"),
            (self.session, True, 6, "approve", "script"),
        ):
            with self.subTest(reason=reason, age=age):
                self.open_ask(self.now - timedelta(minutes=age))
                result = text_asks.answer(session, self.pane, prompt, source_typed, self.now)
                assert isinstance(result, text_asks.Refused)
                self.assertIn(reason, result.reason)
                self.assertTrue(self.ask_path.exists())
                self.assertFalse(self.approvals.exists())
        self.reader.assert_not_called()

    def test_refuses_when_herdr_fails_or_the_pane_has_wrong_focus_or_session(self) -> None:
        self.open_ask()
        for row, error, reason in (
            (self.focused, HerdrFailure("no server"), "no server"),
            ({**self.focused, "focused": False}, None, "focus"),
            ({**self.focused, "focused": 1}, None, "focus"),
            ({"focused": True, "agent_session": {"value": "other"}}, None, "report this session"),
            ({"focused": True}, None, "report this session"),
        ):
            with self.subTest(row=row, error=error):
                self.reader.return_value = row
                self.reader.side_effect = error
                result = text_asks.answer(self.session, self.pane, "yes", True, self.now)
                assert isinstance(result, text_asks.Refused)
                self.assertIn(reason, result.reason)
                self.assertTrue(self.ask_path.exists())
                self.assertFalse(self.approvals.exists())

    def test_approves_user_words_and_keeps_them_exactly(self) -> None:
        for prompt in ("yes i approve it", "yes", "go", "  (OK!), use it. ", "Approved."):
            with self.subTest(prompt=prompt):
                self.open_ask()
                result = text_asks.answer(self.session, self.pane, prompt, True, self.now)
                expected = text_asks.Approved("text", "digest", prompt, self.session, self.pane,
                                              self.now.isoformat(), "user-prompt")
                self.assertEqual(result, expected)
                self.assertEqual(text_asks.approval_for("text", "digest"), expected)
                self.assertFalse(self.ask_path.exists())
                self.assertTrue((self.approvals / "text-digest.json").is_file())

    def test_other_answers_leave_the_ask_open(self) -> None:
        self.open_ask()
        for prompt in ("no", "don't approve", "yes but not that one", "yes, I don’t approve",
                       "ok, never use it", "go, no", "yes but I can't", "absolutely yes", ""):
            with self.subTest(prompt=prompt):
                self.assertIsInstance(text_asks.answer(self.session, self.pane, prompt, True, self.now),
                                      text_asks.LeftOpen)
                self.assertTrue(self.ask_path.exists())
                self.assertFalse(self.approvals.exists())

    def test_failed_claim_leaves_the_ask_open_without_an_approval(self) -> None:
        self.open_ask()
        unlink = Path.unlink

        def refuse_claim(path: Path, missing_ok: bool = False) -> None:
            if path == self.ask_path:
                raise PermissionError("cannot claim the ask")
            unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", refuse_claim):
            result = text_asks.answer(self.session, self.pane, "yes", True, self.now)
        assert isinstance(result, text_asks.Refused)
        self.assertIn("cannot claim the ask", result.reason)
        self.assertTrue(self.ask_path.exists())
        self.assertFalse(self.approvals.exists())
        self.assertIsInstance(text_asks.approval_for("text", "digest"), text_asks.NoUsableApproval)

    def test_new_request_replaces_the_request_for_one_pane(self) -> None:
        self.open_ask()
        self.open_ask(text_id="next", fingerprint="new-digest")
        result = text_asks.answer(self.session, self.pane, "go", True, self.now)
        assert isinstance(result, text_asks.Approved)
        self.assertEqual((result.text_id, result.fingerprint), ("next", "new-digest"))


if __name__ == "__main__":
    _ = unittest.main()
