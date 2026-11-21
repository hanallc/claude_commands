"""Check phone alerts from saved Mac token observations."""
from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
import json
import os
import subprocess
from typing import cast, override
from unittest.mock import patch

from scripts.lib.user_zone import user_zone
from scripts.notify import push_texts, silence
from scripts.whoami import mac_fallback_token, quota_alert, quota_alert_test_support as support

setUpModule = support.setUpModule


class MacTokenAlertTests(support.SeededQuotaReminderFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.root / "notify")})))
        with quota_alert.CONFIG.open("a", encoding="utf-8") as settings:
            _ = settings.write("mac_token.expiry_days=30 7\n")

    def save_token(self, made: datetime, *, failure: str = "unknown", good: str = "unknown",
                   checked_at: str | None = None) -> None:
        observed = self.now.isoformat() if checked_at is None else checked_at
        _ = (self.root / "Mac fallback token.md").write_text(
            f"---\nmade: {made.isoformat()}\nchecked_at: {observed}\nlast_login_failure: {failure}\n"
            + f"last_good_use: {good}\n---\n", encoding="utf-8")

    def test_expiry_alert_fires_once_at_each_lead_time(self) -> None:
        made = self.now - timedelta(days=334)
        self.save_token(made)
        first = self.now + timedelta(days=1)
        second = first + timedelta(days=23)
        _ = self.alert([], self.now)
        self.assertEqual(self.noted, [])
        for at, count in ((first, 1), (first + timedelta(days=1), 1), (second, 2),
                          (second + timedelta(days=1), 2)):
            self.save_token(made, checked_at=at.isoformat())
            _ = self.alert([], at)
            self.assertEqual([push.id for push in self.noted], ["quota.mac_token_expiry"] * count)
        expected_date = (self.now + timedelta(days=31)).astimezone(user_zone()).date().isoformat()
        for push in self.noted:
            self.assertEqual(push.values, {"date": expected_date})

    def test_first_observation_inside_seven_days_uses_only_the_current_window(self) -> None:
        self.save_token(self.now - timedelta(days=360))
        _ = self.alert([], self.now)
        _ = self.alert([], self.now + timedelta(days=1))
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_expiry"])

    def test_login_alert_fires_once_for_a_failed_relay(self) -> None:
        self.save_token(self.now - timedelta(days=1), failure=self.now.isoformat())
        _ = self.alert([], self.now)
        _ = self.alert([], self.now + timedelta(minutes=2))
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"])

    def login_claim(self) -> quota_alert.MacTokenLoginClaim | None:
        with quota_alert.state_file() as state:
            return state.get("mac_token_login_claim")

    def assert_login_claim(self, made: datetime, failure: datetime, *,
                           attempted_at: datetime | None = None, delivered: bool = True) -> None:
        self.assertEqual(self.login_claim(), {
            "made": made.isoformat(),
            "failure": failure.isoformat(),
            "attempted_at": (attempted_at if attempted_at is not None else failure).isoformat(),
            "delivered": delivered,
        })

    def test_late_success_before_the_claim_does_not_send_another_push(self) -> None:
        made = self.now - timedelta(days=1)
        failure = self.now
        self.save_token(made, failure=failure.isoformat())
        _ = self.alert([], failure)
        claim = self.login_claim()
        good = failure - timedelta(minutes=1)
        later = failure + timedelta(minutes=1)
        self.save_token(made, failure=failure.isoformat(), good=good.isoformat())
        _ = self.alert([], later)
        self.save_token(made, failure=later.isoformat(), good=good.isoformat())
        _ = self.alert([], later)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"])
        self.assert_login_claim(made, failure)
        self.assertEqual(self.login_claim(), claim)

    def test_success_after_the_claim_allows_one_push_for_the_next_failure(self) -> None:
        made = self.now - timedelta(days=1)
        self.save_token(made, failure=self.now.isoformat())
        _ = self.alert([], self.now)
        good = self.now + timedelta(minutes=1)
        failure = good + timedelta(minutes=1)
        self.save_token(made, failure=self.now.isoformat(), good=good.isoformat())
        _ = self.alert([], good)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"])
        self.save_token(made, failure=failure.isoformat(), good=good.isoformat())
        _ = self.alert([], failure)
        later = failure + timedelta(minutes=1)
        self.save_token(made, failure=later.isoformat(), good=good.isoformat())
        _ = self.alert([], later)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"] * 2)
        self.assert_login_claim(made, failure)

    def test_new_token_allows_one_push_without_a_success(self) -> None:
        made = self.now - timedelta(days=1)
        self.save_token(made, failure=self.now.isoformat())
        _ = self.alert([], self.now)
        replaced = made + timedelta(minutes=1)
        self.save_token(replaced, failure=self.now.isoformat())
        _ = self.alert([], self.now)
        later = self.now + timedelta(minutes=1)
        self.save_token(replaced, failure=later.isoformat())
        _ = self.alert([], later)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"] * 2)
        self.assert_login_claim(replaced, self.now)

    def test_an_older_check_cannot_replace_a_newer_claim(self) -> None:
        made = self.now - timedelta(days=1)
        self.save_token(made, failure=self.now.isoformat())
        _ = self.alert([], self.now)

        old_good = self.now + timedelta(minutes=1)
        old_failure = old_good + timedelta(minutes=1)
        self.save_token(made, failure=old_failure.isoformat(), good=old_good.isoformat())
        older_reading = mac_fallback_token.read()

        good = old_failure + timedelta(minutes=1)
        failure = good + timedelta(minutes=1)
        self.save_token(made, failure=failure.isoformat(), good=good.isoformat())
        _ = self.alert([], failure)
        with patch.object(mac_fallback_token, "read", return_value=older_reading):
            _ = self.alert([], old_failure)
        _ = self.alert([], failure)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"] * 2)
        self.assert_login_claim(made, failure)

    def test_an_older_token_cannot_replace_a_newer_token_claim(self) -> None:
        made = self.now - timedelta(days=2)
        self.save_token(made, failure=self.now.isoformat())
        old_token = mac_fallback_token.read()
        _ = self.alert([], self.now)

        newer_made = made + timedelta(days=1)
        failure = self.now + timedelta(minutes=1)
        self.save_token(newer_made, failure=failure.isoformat())
        _ = self.alert([], failure)
        with patch.object(mac_fallback_token, "read", return_value=old_token):
            _ = self.alert([], failure)
        self.assert_login_claim(newer_made, failure)
        _ = self.alert([], failure)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"] * 2)

    def test_confirmed_login_send_failure_leaves_the_push_due(self) -> None:
        made = self.now - timedelta(days=1)
        self.save_token(made, failure=self.now.isoformat())
        self.push_errors.append(quota_alert.PushFailed("fixture refusal"))
        _ = self.alert([], self.now)
        self.assertIsNone(self.login_claim())
        _ = self.alert([], self.now + timedelta(minutes=1))
        _ = self.alert([], self.now + timedelta(minutes=2))
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"] * 2)
        self.assert_login_claim(made, self.now, attempted_at=self.now + timedelta(minutes=1))

    def test_unconfirmed_login_retries_the_claimed_failure_after_24_hours(self) -> None:
        made = self.now - timedelta(days=1)
        failure = self.now
        self.save_token(made, failure=failure.isoformat())
        self.push_errors.append(quota_alert.PushUnconfirmed("fixture timeout"))
        _ = self.alert([], failure)
        self.assert_login_claim(made, failure, delivered=False)

        retry_at = failure + timedelta(days=1)
        repeated = failure + timedelta(minutes=1)
        early = retry_at - timedelta(seconds=1)
        self.save_token(made, failure=repeated.isoformat(), checked_at=early.isoformat())
        _ = self.alert([], early)
        self.assertEqual(len(self.noted), 1)
        self.assert_login_claim(made, failure, delivered=False)

        self.save_token(made, failure=repeated.isoformat(), checked_at=retry_at.isoformat())
        _ = self.alert([], retry_at)
        self.assert_login_claim(made, failure, attempted_at=retry_at)
        later = retry_at + timedelta(days=1)
        self.save_token(made, failure=repeated.isoformat(), checked_at=later.isoformat())
        _ = self.alert([], later)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"] * 2)
        self.assertEqual(self.noted[1].values, self.noted[0].values)

    def test_success_before_the_claim_lock_stops_a_login_retry(self) -> None:
        made = self.now - timedelta(days=1)
        failure = self.now
        self.save_token(made, failure=failure.isoformat())
        self.push_errors.append(quota_alert.PushUnconfirmed("fixture timeout"))
        _ = quota_alert.mac_token_alerts(failure)

        retry_at = failure + timedelta(days=1)
        self.save_token(made, failure=failure.isoformat(), checked_at=retry_at.isoformat())
        state_file = quota_alert.state_file

        def save_success_before_lock() -> AbstractContextManager[quota_alert.State]:
            self.save_token(made, failure=failure.isoformat(), good=retry_at.isoformat(),
                            checked_at=retry_at.isoformat())
            return state_file()

        with patch.object(quota_alert, "state_file", side_effect=save_success_before_lock):
            _ = quota_alert.mac_token_alerts(retry_at)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"])
        self.assert_login_claim(made, failure, delivered=False)

    def test_interrupted_login_send_leaves_an_undelivered_claim(self) -> None:
        made = self.now - timedelta(days=1)
        self.save_token(made, failure=self.now.isoformat())
        with patch.object(quota_alert, "tell_user", side_effect=InterruptedError("fixture interruption")):
            with self.assertRaises(InterruptedError):
                _ = self.alert([], self.now)
        self.assert_login_claim(made, self.now, delivered=False)
        retry_at = self.now + timedelta(days=1)
        self.save_token(made, failure=self.now.isoformat(), checked_at=retry_at.isoformat())
        _ = self.alert([], retry_at)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"])
        self.assert_login_claim(made, self.now, attempted_at=retry_at)

    def test_previous_login_claim_retries_as_unconfirmed(self) -> None:
        made = self.now - timedelta(days=1)
        _ = quota_alert.STATE.write_text(json.dumps({
            "mac_token_login_claim": {"made": made.isoformat(), "failure": self.now.isoformat()},
        }), encoding="utf-8")
        self.save_token(made, failure=self.now.isoformat())
        _ = self.alert([], self.now)
        self.assertEqual(self.noted, [])
        self.assert_login_claim(made, self.now, delivered=False)
        retry_at = self.now + timedelta(days=1)
        self.save_token(made, failure=self.now.isoformat(), checked_at=retry_at.isoformat())
        _ = self.alert([], retry_at)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"])
        self.assert_login_claim(made, self.now, attempted_at=retry_at)

    def test_interrupted_attempt_retries_at_24_hours_and_confirms_once(self) -> None:
        made = self.now - timedelta(days=345)
        self.save_token(made)
        with patch.object(quota_alert, "tell_user", side_effect=InterruptedError("fixture interruption")):
            with self.assertRaises(InterruptedError):
                _ = self.alert([], self.now)
        _ = self.alert([], self.now + timedelta(hours=23, minutes=59))
        self.assertEqual(self.noted, [])
        for hours in (24, 48):
            at = self.now + timedelta(hours=hours)
            self.save_token(made, checked_at=at.isoformat())
            _ = self.alert([], at)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_expiry"])

    def test_clear_failure_allows_retry_in_the_next_pass(self) -> None:
        self.save_token(self.now - timedelta(days=345))
        self.push_errors.append(quota_alert.PushFailed("fixture refusal"))
        _ = self.alert([], self.now)
        _ = self.alert([], self.now + timedelta(minutes=2))
        _ = self.alert([], self.now + timedelta(minutes=4))
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_expiry"] * 2)

    def test_phone_sender_marks_mac_alert_timeout_unconfirmed(self) -> None:
        push = push_texts.render("quota.mac_token_expiry", date="2027-10-01")
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("send", 1)):
            delivery = support.REAL_TELL_USER(push)
        self.assertIsInstance(delivery, quota_alert.PushUnconfirmed)

    def test_good_use_after_a_login_failure_suppresses_the_warning(self) -> None:
        self.save_token(self.now - timedelta(days=1), failure=(self.now - timedelta(minutes=1)).isoformat(),
                        good=self.now.isoformat())
        _ = self.alert([], self.now)
        self.assertEqual(self.noted, [])

    def test_invalid_expiry_days_do_not_suppress_the_login_alert(self) -> None:
        self.save_token(self.now - timedelta(days=345), failure=self.now.isoformat())
        default = quota_alert.CONFIG.read_text(encoding="utf-8")
        for invalid in ("0 7", "366 7", "7 7", "x 7"):
            with self.subTest(value=invalid):
                _ = quota_alert.CONFIG.write_text(default.replace("30 7", invalid), encoding="utf-8")
                quota_alert.STATE.unlink(missing_ok=True)
                self.noted.clear()
                log = self.alert([], self.now)
                self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login"])
                self.assertIn("quota-alert: Mac token expiry days unavailable", log)

    def test_both_alerts_require_a_current_observation(self) -> None:
        made = self.now - timedelta(days=345)
        failure = self.now.isoformat()
        for observed in ("unknown", (self.now - timedelta(days=1, seconds=1)).isoformat(),
                         (self.now + timedelta(seconds=1)).isoformat()):
            with self.subTest(observed=observed):
                self.save_token(made, failure=failure, checked_at=observed)
                _ = self.alert([], self.now)
                self.assertEqual(self.noted, [])
                with quota_alert.state_file() as state:
                    self.assertEqual(state.get("mac_token_alerts", {}), {})
        self.save_token(made, failure=failure)
        _ = self.alert([], self.now)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_login", "quota.mac_token_expiry"])

    def test_silence_saves_no_attempt_and_next_pass_uses_current_window(self) -> None:
        made = self.now - timedelta(days=335)
        self.save_token(made)
        with redirect_stdout(StringIO()):
            self.assertEqual(silence.on(None, lambda _argv: 0), 0)
            _ = self.alert([], self.now)
            self.assertEqual(self.noted, [])
            self.assertEqual(silence.read_kept(), [])
            with quota_alert.state_file() as state:
                self.assertEqual(state.get("mac_token_alerts", {}), {})
            self.assertEqual(silence.off(lambda _argv: 0), 0)
        at = self.now + timedelta(days=23)
        self.save_token(made, checked_at=at.isoformat())
        _ = self.alert([], at)
        self.assertEqual([push.id for push in self.noted], ["quota.mac_token_expiry"])
        with quota_alert.state_file() as state:
            self.assertEqual(set(state.get("mac_token_alerts", {})), {"quota.mac_token_expiry/7"})
