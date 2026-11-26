"""Test outage alert ledger operations."""
from __future__ import annotations

from pathlib import Path
import tempfile
import time
from typing import cast
import unittest
from unittest.mock import patch

from scripts.accounts import outage_alerts


class OutageAlertsTests(unittest.TestCase):
    def test_clear_prefix_removes_only_older_matching_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.accounts.outage_alerts._path", return_value=Path(directory) / "alerts.json"
        ):
            for key, stamp, delivered in (
                ("source:old-claim", 10.0, False),
                ("source:old-delivery", 11.0, True),
                ("source:equal", 12.0, True),
                ("source:new", 13.0, True),
                ("source-other:old", 10.0, True),
            ):
                with patch.object(time, "time", return_value=stamp):
                    claim = outage_alerts.claim(key)
                self.assertIsInstance(claim, outage_alerts.AlertClaim)
                if delivered:
                    outage_alerts.delivered(cast(outage_alerts.AlertClaim, claim))

            self.assertEqual(outage_alerts.clear_prefix("source:", 12.0), 2)
            self.assertEqual(outage_alerts.clear_prefix("source:", 12.0), 0)
            for key in ("source:old-claim", "source:old-delivery"):
                self.assertIsInstance(outage_alerts.claim(key), outage_alerts.AlertClaim)
            for key in ("source:equal", "source:new", "source-other:old"):
                self.assertIsInstance(outage_alerts.claim(key), outage_alerts.AlertDelivered)

    def test_clear_prefix_can_use_delivery_time_and_keep_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.accounts.outage_alerts._path", return_value=Path(directory) / "alerts.json"
        ):
            with patch.object(time, "time", return_value=10.0):
                pending = outage_alerts.claim("source:pending")
                delivered_early = outage_alerts.claim("source:early")
                delivered_late = outage_alerts.claim("source:late")
            assert isinstance(pending, outage_alerts.AlertClaim)
            assert isinstance(delivered_early, outage_alerts.AlertClaim)
            assert isinstance(delivered_late, outage_alerts.AlertClaim)
            outage_alerts.delivered(delivered_early, sent_at=11.0)
            outage_alerts.delivered(delivered_late, sent_at=13.0)
            self.assertEqual(outage_alerts.clear_prefix("source:", 12.0, delivered_only=True), 1)
            with patch.object(time, "time", return_value=12.0):
                self.assertIsInstance(outage_alerts.claim(pending.session_id), outage_alerts.AlertInFlight)
                self.assertIsInstance(outage_alerts.claim(delivered_early.session_id), outage_alerts.AlertClaim)
                self.assertIsInstance(outage_alerts.claim(delivered_late.session_id), outage_alerts.AlertDelivered)
