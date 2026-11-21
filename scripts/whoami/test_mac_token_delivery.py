"""Check approval at the phone sender with local fixtures."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.notify import push_reference, push_texts, pushover, review, silence
from scripts.whoami.mac_fallback_token import ALERT_IDS


class MacTokenDeliveryTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.approvals: Path = Path()
        self.keys: Path = Path()
        self.posts: list[bytes] = []

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.approvals = root / "approvals.json"
        self.keys = root / "fixture-keys"
        _ = self.keys.write_text("PUSHOVER_USER=fixture\nPUSHOVER_TOKEN=fixture\n", encoding="utf-8")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(root / "state")})))
        _ = self.enterContext(patch.object(push_texts, "REVIEWED_FILE", self.approvals))
        _ = self.enterContext(patch.object(pushover, "LOG_FILE", root / "pushes.jsonl"))
        _ = self.enterContext(patch.object(silence, "on_mac", return_value=False))
        _ = self.enterContext(patch.object(review, "switched_off", return_value=True))
        _ = self.enterContext(patch.object(review, "log_review"))
        _ = self.enterContext(redirect_stdout(io.StringIO()))

    def post(self, _url: str, data: bytes) -> tuple[int, bytes]:
        self.posts.append(data)
        return 200, b'{"status":1}'

    def fixture_approval(self) -> None:
        _ = self.approvals.write_text(json.dumps({
            entry.id: {example.name: {"sha256": push_texts.fingerprint(entry), "time": "2026-11-21"}
                       for example in entry.examples}
            for entry in push_texts.catalog() if entry.id in ALERT_IDS
        }), encoding="utf-8")

    def direct(self, entry: push_texts.PushText) -> int:
        args = ["--source", "quota_alert", "--text-id", entry.id]
        for key, value in entry.examples[0].values.items():
            args.extend(["--value", f"{key}={value}"])
        return pushover.main(args, self.keys, self.post)

    def test_direct_cli_requires_approval_when_live_review_is_off(self) -> None:
        entries = [entry for entry in push_texts.catalog() if entry.id in ALERT_IDS]
        self.assertEqual(len(entries), 2)
        with redirect_stderr(io.StringIO()) as errors:
            for entry in entries:
                with self.subTest(text_id=entry.id):
                    self.assertEqual(self.direct(entry), 2)
        self.assertIn("needs current approval", errors.getvalue())
        self.assertEqual(self.posts, [])
        self.fixture_approval()
        for entry in entries:
            self.assertEqual(self.direct(entry), 0)
        self.assertEqual(len(self.posts), 2)

    def test_forwarded_delivery_requires_current_local_approval(self) -> None:
        push: silence.KeptPush = {
            "source": "quota_alert", "priority": "0", "title": "Mac fallback token expires 2027-10-01",
            "message": "Action: Read the Accounts page", "detail": "registered text, approved 2026-11-21",
            "key": "", "text_id": "quota.mac_token_expiry", "due": "", "reference": push_reference.NO_REFERENCE,
        }
        with (patch("sys.stdin", io.StringIO(json.dumps(push))), redirect_stderr(io.StringIO()) as errors):
            code = pushover.main(["deliver"], self.keys, self.post)
        self.assertEqual(code, 2)
        self.assertIn("needs current approval", errors.getvalue())
        self.assertEqual(self.posts, [])

    def test_queued_release_drops_alert_after_approval_disappears(self) -> None:
        self.fixture_approval()
        self.assertEqual(silence.on(None, lambda _argv: 0), 0)
        push: silence.KeptPush = {
            "source": "quota_alert", "priority": "0", "title": "Mac token login failed Sat 11-21 10:07 EST",
            "message": "Action: Read the Accounts page", "detail": "registered text, approved 2026-11-21",
            "key": "", "text_id": "quota.mac_token_login", "due": "", "reference": push_reference.NO_REFERENCE,
        }
        self.assertIsInstance(silence.keep_here(push), silence.Kept)
        self.assertEqual([push["text_id"] for push in silence.read_kept()], ["quota.mac_token_login"])
        self.assertEqual(self.posts, [])
        self.approvals.unlink()
        with redirect_stdout(io.StringIO()) as output:
            code = silence.off(lambda _argv: 0, lambda push: pushover.post_kept(push, self.keys, self.post))
        self.assertEqual(code, 0)
        self.assertIn("0 kept pushes delivered; 1 dropped", output.getvalue())
        self.assertEqual(self.posts, [])
        self.assertEqual(silence.read_kept(), [])

    def test_mac_token_alerts_refuse_instead_of_queueing_during_silence(self) -> None:
        self.fixture_approval()
        self.assertEqual(silence.on(None, lambda _argv: 0), 0)
        entries = [entry for entry in push_texts.catalog() if entry.id in ALERT_IDS]
        self.assertEqual(len(entries), 2)
        with redirect_stderr(io.StringIO()) as errors:
            for entry in entries:
                with self.subTest(text_id=entry.id):
                    self.assertEqual(self.direct(entry), 2)
        self.assertIn("silence is active", errors.getvalue())
        self.assertEqual(silence.read_kept(), [])
        self.assertEqual(silence.off(lambda _argv: 0, lambda push: pushover.post_kept(push, self.keys, self.post)), 0)
        self.assertEqual(self.posts, [])
        for entry in entries:
            self.assertEqual(self.direct(entry), 0)
        self.assertEqual(len(self.posts), 2)
