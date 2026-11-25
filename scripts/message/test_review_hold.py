"""Test the command that ends a review hold."""
from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stdout
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.hooks import review_hold_delivery
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import review_hold

SESSION = "review-session"
RELEASE_AT = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc).timestamp()


class ReviewHoldTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = home / "config"
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "HOME": str(home), "CONF_DIR": str(config), "USER_ZONE": "UTC", "CLAUDE_CODE_SESSION_ID": SESSION,
        })))
        _ = write_config_fixture(config / "send.conf", "review_hold=on\nreview_hold_idle_minutes=10")

    def end_now(self, at: float = RELEASE_AT) -> str:
        output = io.StringIO()
        with mock.patch.object(review_hold, "time", return_value=at), redirect_stdout(output):
            self.assertEqual(review_hold.main(["end-now"]), 0)
        return output.getvalue()

    def open_review(self, session: str, at: float) -> None:
        review_hold.set_count(session, 3, at, self.fail, record_keys=review_hold_delivery.record_keys)

    def test_end_now_uses_the_idle_release(self) -> None:
        idle_session = "idle-review"
        self.open_review(SESSION, RELEASE_AT - 60)
        self.open_review(idle_session, RELEASE_AT - 600)
        for text, offset in (("Later message.", 35), ("Earlier message.", 40)):
            message = review_hold.ReviewMessage("Sender", RELEASE_AT - offset, text)
            for session in (SESSION, idle_session):
                self.assertTrue(review_hold.keep_native_message(session, message))
        idle_notices: list[str] = []
        review_hold.record_user_prompt(idle_session, RELEASE_AT, idle_notices.append,
                                       record_keys=review_hold_delivery.record_keys)
        self.assertEqual(len(idle_notices), 1)

        held = review_hold.held_file(SESSION)
        idle_file = review_hold.held_file(idle_session)
        self.assertEqual(self.end_now(), idle_notices[0].replace(str(idle_file), str(held)) + "\n")
        contents = held.read_text(encoding="utf-8")
        self.assertEqual(contents, idle_file.read_text(encoding="utf-8"))
        self.assertLess(contents.index("Earlier message."), contents.index("Later message."))
        self.assertEqual(review_hold.item_count(SESSION), 3)
        self.assertEqual(self.end_now(), "No review hold is open.\n")
        self.assertEqual(held.read_text(encoding="utf-8"), contents)

    def test_a_prompt_restarts_the_hold_after_end_now(self) -> None:
        self.open_review(SESSION, RELEASE_AT - 60)
        self.assertEqual(self.end_now(), "No messages were held.\n")
        during_break = review_hold.ReviewMessage("Sender", RELEASE_AT + 1, "This message can arrive.")
        self.assertFalse(review_hold.keep_native_message(SESSION, during_break))

        review_hold.record_user_prompt(SESSION, RELEASE_AT + 2, self.fail, record_keys=review_hold_delivery.record_keys)
        after_prompt = review_hold.ReviewMessage("Sender", RELEASE_AT + 3, "Keep this message.")
        self.assertTrue(review_hold.keep_native_message(SESSION, after_prompt))
        self.assertFalse(review_hold.held_file(SESSION).exists())

    def test_end_now_says_when_no_hold_is_open(self) -> None:
        notice = "No review hold is open.\n"
        held = review_hold.held_file(SESSION)
        self.assertEqual(self.end_now(), notice)
        self.assertFalse(held.parent.exists())

        self.open_review(SESSION, RELEASE_AT - 600)
        message = review_hold.ReviewMessage("Sender", RELEASE_AT - 1, "Release this after the idle end.")
        self.assertTrue(review_hold.keep_native_message(SESSION, message))
        self.assertEqual(self.end_now(), notice)
        self.assertFalse(held.exists())
        self.assertEqual(review_hold.item_count(SESSION), 3)


if __name__ == "__main__":
    _ = unittest.main()
