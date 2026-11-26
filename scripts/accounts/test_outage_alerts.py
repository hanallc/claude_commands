"""Test outage alert ledger operations."""
from __future__ import annotations

from pathlib import Path
import tempfile
import time
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
                assert isinstance(claim, outage_alerts.AlertClaim)
                if delivered:
                    outage_alerts.delivered(claim)

            self.assertEqual(outage_alerts.clear_prefix("source:", 12.0), 2)
            self.assertEqual(outage_alerts.clear_prefix("source:", 12.0), 0)
            for key in ("source:old-claim", "source:old-delivery"):
                self.assertIsInstance(outage_alerts.claim(key), outage_alerts.AlertClaim)
            for key in ("source:equal", "source:new", "source-other:old"):
                self.assertIsInstance(outage_alerts.claim(key), outage_alerts.AlertDelivered)
