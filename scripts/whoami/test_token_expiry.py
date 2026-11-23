"""Check shared reminder windows for config dates and Mac token metadata."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts.lib import conf
from scripts.whoami import mac_fallback_token, token_expiry


class TokenExpiryTests(unittest.TestCase):
    def test_config_date_uses_the_current_window_in_the_user_zone(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = root / "showrunners.conf"
        _ = config.write_text("token_expiry.lead_days=30 7\n"
                              + "token_expiry.fixture_runner.ends=2027-11-22\n", encoding="utf-8")
        settings = conf.read(config)
        assert isinstance(settings.settings, dict)
        tokens = token_expiry.configured_tokens(settings.settings, settings.values)
        token = next(token for token in tokens if token.name == "fixture-runner")
        self.assertEqual(token.ends, token_expiry.CalendarEnd(date(2027, 11, 22)))
        self.assertTrue(any(isinstance(item.ends, token_expiry.EndUnavailable)
                            for item in tokens))
        leads = token_expiry.expiry_days(settings.values("token_expiry.lead_days").get("token_expiry.lead_days", ""))
        zone = ZoneInfo("America/New_York")
        end = datetime(2027, 11, 22, 5, tzinfo=timezone.utc)
        for days, window in ((31, None), (30, 30), (8, 30), (7, 7), (2, 7), (0, 7), (-1, None)):
            with self.subTest(days=days):
                now = end - timedelta(days=days)
                self.assertEqual(token.end_date(zone), date(2027, 11, 22))
                self.assertEqual(token.days_left(now, zone), days)
                self.assertEqual(token.expiry_lead(now, zone, leads), window)
        first = datetime(2027, 10, 23, tzinfo=zone)
        self.assertIsNone(token.expiry_lead(first - timedelta(seconds=1), zone, leads))
        self.assertEqual(token.expiry_lead(first, zone, leads), 30)

    def test_mac_end_date_uses_the_local_date_and_the_expiry_instant(self) -> None:
        made = datetime(2026, 11, 23, 2, tzinfo=timezone.utc)
        mac = mac_fallback_token.MacFallbackToken(made)
        token = token_expiry.mac_token(mac, mac_fallback_token.Unknown.VALUE)
        self.assertEqual(token.ends, token_expiry.ExpiryInstant(mac.expires))
        zone = ZoneInfo("America/New_York")
        self.assertEqual(token.end_date(zone), date(2027, 11, 22))
        for days, window in ((31, None), (30, 30), (8, 30), (7, 7), (2, 7), (0, None), (-1, None)):
            with self.subTest(days=days):
                now = mac.expires - timedelta(days=days)
                self.assertEqual(token.expiry_lead(now, zone, (7, 30)), window)
        self.assertEqual(token.expiry_lead(mac.expires - timedelta(seconds=1), zone, (7, 30)), 7)
