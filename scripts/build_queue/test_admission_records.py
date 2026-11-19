"""Test process identity reads for admission clients."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.build_queue import admission_records as records


class LandingWaitTextTests(unittest.TestCase):
    def test_head_and_follower_display_the_landing_wait_reason(self) -> None:
        reason = records.LandingWait()
        decision = records.AdmissionDecision("hold", 1, 0, 0, 0)
        head = records.HeadAdmissionStatus(decision, 1, 2, cause=reason)
        text = "waits for natemerge's landing"
        self.assertEqual(records.reason_phrase(reason, ()), text)
        self.assertEqual(records.reason_phrase(head, ()), text)
        self.assertEqual(records.head_wait_text(head, ZoneInfo("UTC")), f"head: {text}")


class ProcessStartTimeTests(unittest.TestCase):
    def test_process_stat_with_invalid_name_keeps_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            directory = proc / "123"
            directory.mkdir()
            _ = (directory / "stat").write_bytes(b"123 (verify\xff) S " + b"0 " * 18 + b"4200\n")

            with patch.object(records, "Path", return_value=proc):
                self.assertEqual(records.process_start_time(123), "4200")
