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

    def test_end_now_releases_saved_messages_without_an_open_hold(self) -> None:
        notice = "No review hold is open.\n"
        self.assertEqual(self.end_now(), notice)
        self.assertFalse(review_hold.held_file(SESSION).parent.exists())

        for mode in ("expired", "failed-release", "disabled"):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": mode}):
                self.open_review(mode, RELEASE_AT - (60 if mode == "disabled" else 600))
                message = review_hold.ReviewMessage("Sender", RELEASE_AT - 1, "Saved message.")
                self.assertTrue(review_hold.keep_native_message(mode, message))
                if mode == "failed-release":
                    trigger = review_hold.ReviewMessage("Sender", RELEASE_AT, "Start an idle release.")
                    with review_hold.route(mode, trigger, urgent=False, include_new=False,
                                           record_keys=review_hold_delivery.record_keys) as release:
                        assert isinstance(release, review_hold.ReleasedMessages)
                        self.assertEqual(release.held, (message,))
                        _ = review_hold.file_release(mode, release, RELEASE_AT, batch_only=True)
                elif mode == "disabled":
                    _ = write_config_fixture(Path(os.environ["CONF_DIR"]) / "send.conf", "review_hold=off")

                held = review_hold.held_file(mode)
                expected = review_hold.held_notice(1, held, "Released at 18:00:00 UTC")
                self.assertEqual(self.end_now(), expected + "\n")
                self.assertEqual(held.read_text(encoding="utf-8").count(message.text), 1)
                self.assertEqual(review_hold.item_count(mode), 3)
                self.assertEqual(self.end_now(), notice)

    def test_a_prompt_preserves_end_now_during_an_urgent_send(self) -> None:
        self.open_review(SESSION, RELEASE_AT - 60)
        ordinary = review_hold.ReviewMessage("Sender", RELEASE_AT - 3, "Ordinary message.")
        old = review_hold.ReviewMessage("Sender", RELEASE_AT - 2, "Old urgent message.", key="urgent")
        urgent = review_hold.ReviewMessage("Sender", RELEASE_AT - 1, "URGENT: New message.", key=old.key)
        for message in (ordinary, old):
            self.assertTrue(review_hold.keep_native_message(SESSION, message))

        record_keys = mock.Mock()
        with review_hold.route(SESSION, urgent, urgent=True, record_keys=record_keys) as sending:
            assert isinstance(sending, review_hold.ReleasedMessages)
            self.assertEqual(sending.held, (old,))
            self.assertEqual(self.end_now(), "A release of held messages is in progress.\n")
            review_hold.record_user_prompt(SESSION, RELEASE_AT + 1, self.fail, record_keys=record_keys)
            sending.accepted_at = RELEASE_AT + 2

        held = review_hold.held_file(SESSION)
        with review_hold.pending_release(SESSION, record_keys=record_keys) as pending:
            self.assertEqual(pending.held, (ordinary,))
            expected = review_hold.held_notice(1, held, "Released at 18:00:02 UTC")
            self.assertEqual(review_hold.file_release(SESSION, pending, RELEASE_AT + 2, batch_only=True), expected)
            pending.accepted_at = RELEASE_AT + 2
        contents = held.read_text(encoding="utf-8")
        self.assertEqual(contents.count(ordinary.text), 1)
        self.assertNotIn(old.text, contents)
        with review_hold.pending_release(SESSION, record_keys=record_keys) as pending:
            self.assertEqual(pending.held, ())

        later = review_hold.ReviewMessage("Sender", RELEASE_AT + 3, "Keep this message.")
        self.assertTrue(review_hold.keep_native_message(SESSION, later))
        self.assertEqual(review_hold.item_count(SESSION), 3)


if __name__ == "__main__":
    _ = unittest.main()
