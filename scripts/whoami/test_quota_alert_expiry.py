"""Forecast reminders from saved weekly readings."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import cast, final, override
from unittest import mock

from scripts.accounts import accounts, next_moves, role_map
from scripts.lib import conf
from scripts.notify import push_texts, pushover, review, silence
from scripts.notify.user_action import ActionRequired
from scripts.review_links import accounts_page
from scripts.whoami import agent_accounts, note_file, quota_alert, quota_alert_test_support as support, run_out

setUpModule = support.setUpModule


@final
class ClaudeExpiryTests(support.SeededQuotaReminderFixture):
    clock: datetime = datetime.min.replace(tzinfo=timezone.utc)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime(2026, 11, 21, 22, tzinfo=timezone.utc)
        self.resets = (self.now + timedelta(days=2)).isoformat()
        self.clock = self.now
        _ = self.enterContext(mock.patch.object(quota_alert, "_expiry_attempt_time", side_effect=lambda: self.clock))
        _ = self.enterContext(mock.patch.object(run_out, "READINGS_LOG", self.root / "readings.jsonl"))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "USER_ZONE": "America/New_York", "NOTIFY_STATE_DIR": str(self.root / "notify"),
        })))
        with quota_alert.CONFIG.open("a", encoding="utf-8") as config:
            _ = config.write("# whole: 1..168\nclaude_expiry.alert_hours=12\n"
                             + "# whole: 1..168\nclaude_expiry.repeat_hours=2\n")

    def check(self, at: datetime, *, left: float = 20, reset: str = "", delay_seconds: int = 0) -> None:
        self.clock = at + timedelta(seconds=delay_seconds)
        self.save_rates(at, {"claude2": 2}, left=left)
        _ = self.alert([self.note("claude2.md", "active", str(left), reset)], at)

    def save_rates(self, at: datetime, rates: dict[str, float], *, left: float = 20) -> None:
        rows = [json.dumps({"account": label, "at": (at - timedelta(minutes=step * 10)).isoformat(),
                            "remaining": left + step * rate / 6})
                for label, rate in rates.items() for step in range(12, -1, -1)]
        _ = run_out.READINGS_LOG.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def recorded_note(self, at: datetime, left: float, reset: str | None = None) -> note_file.Note:
        path = self.note("claude2.md", "active", f"{left:g}", reset).path
        text = path.read_text(encoding="utf-8")
        updated = text.replace("weekly_remaining_usage:",
                               f"weekly_usage_checked_at: {at.isoformat()}\nweekly_remaining_usage:")
        _ = path.write_text(updated, encoding="utf-8")
        note = note_file.read_note(path)
        assert isinstance(note, note_file.Note)
        return note

    def expiry_accounts(self) -> list[str]:
        return [push.values["account"] for push in self.noted if push.id == "quota.claude_expiry"]

    def test_imminent_expiry_push_names_the_account_time_and_action(self) -> None:
        self.check(self.now)

        self.assertEqual(self.expiry_accounts(), ["claude2"])
        self.assertEqual((self.noted[0].title, self.noted[0].body), ("claude2 runs out Sun 11-22 03:00 EST", ""))
        self.assertIsInstance(self.noted[0].action, ActionRequired)
        assert isinstance(self.noted[0].action, ActionRequired)
        self.assertEqual(self.noted[0].action.text, "In the natedev session, type /agents move <role> <account>")
        self.assertEqual((self.sent, self.pushed), ([], []))

    def test_expiry_push_does_not_repeat_before_two_hours(self) -> None:
        self.check(self.now)
        self.check(self.now + timedelta(hours=2, seconds=-1), left=16)

        self.assertEqual(self.expiry_accounts(), ["claude2"])

    def test_expiry_push_repeats_at_two_hours(self) -> None:
        self.check(self.now)
        self.check(self.now + timedelta(hours=2), left=16)

        self.assertEqual(self.expiry_accounts(), ["claude2", "claude2"])

    def test_scheduled_move_suppresses_the_push_without_resetting_the_repeat(self) -> None:
        self.check(self.now)
        _ = next_moves.add_next_move("units", 1, "claude3")
        self.check(self.now + timedelta(hours=1), left=18)
        self.assertEqual(self.expiry_accounts(), ["claude2"])

        next_moves.remove_next_move("units", next_moves.GroupPosition(1))
        self.check(self.now + timedelta(hours=1, minutes=2), left=18)
        self.assertEqual(self.expiry_accounts(), ["claude2"])
        self.check(self.now + timedelta(hours=2), left=16)
        self.assertEqual(self.expiry_accounts(), ["claude2", "claude2"])

    def test_only_the_account_with_scheduled_coverage_stays_silent(self) -> None:
        _ = next_moves.add_next_move("units", 1, "claude3")
        self.save_rates(self.now, {"claude2": 2, "claude3": 2})
        _ = self.alert([self.note("claude2.md", "active", "20"), self.note("claude3.md", "active", "20")])

        self.assertEqual(self.expiry_accounts(), ["claude3"])

    def test_codex_never_sends_an_expiry_push(self) -> None:
        self.save_rates(self.now, {"claude2": 2, "codex1": 2})
        _ = self.alert([self.note("claude2.md", "active", "20"), self.note("codex1.md", "active", "20")])

        self.assertEqual(self.expiry_accounts(), ["claude2"])

    def test_no_usable_rate_sends_no_expiry_push(self) -> None:
        self.save_rates(self.now, {"claude2": 2, "claude3": 0})
        _ = self.alert([self.note("claude1.md", "active", "20"), self.note("claude2.md", "active", "20"),
                        self.note("claude3.md", "active", "20")])

        self.assertEqual(self.expiry_accounts(), ["claude2"])

    def test_a_short_rise_after_refill_can_send_an_expiry_push(self) -> None:
        lookback = conf.read("accounts").whole("rate.lookback_minutes")
        readings = [run_out.Reading(self.now - timedelta(minutes=lookback), 100),
                    run_out.Reading(self.now - timedelta(minutes=10), 98),
                    run_out.Reading(self.now - timedelta(minutes=5), 19),
                    run_out.Reading(self.now, 20)]
        with mock.patch.object(run_out, "read_readings", return_value={"claude2": readings}):
            _ = self.alert([self.recorded_note(self.now, 80)])
        self.assertEqual(self.expiry_accounts(), ["claude2"])

    def test_forecast_after_reset_or_outside_the_window_stays_silent(self) -> None:
        self.check(self.now)
        checks = ((30, 30, self.resets), (60, 20, (self.now + timedelta(hours=5)).isoformat()))
        for elapsed, left, reset in checks:
            with self.subTest(minutes=elapsed):
                self.check(self.now + timedelta(minutes=elapsed), left=left, reset=reset)
                self.assertEqual(len(self.expiry_accounts()), 1)
                self.check(self.now + timedelta(minutes=elapsed + 2))
                self.assertEqual(len(self.expiry_accounts()), 1)

    def test_reset_excludes_the_same_history_for_the_page_and_the_expiry_push(self) -> None:
        reset = self.now + timedelta(weeks=1, minutes=-1)
        self.save_rates(self.now - timedelta(minutes=2), {"claude2": 6}, left=1)
        note = self.recorded_note(self.now, 98, reset.isoformat())
        row = accounts.StatusRow(
            label="claude2", tool="claude", folder="~/.claude2", login="", roles=(),
            weekly_left_percent=98, resets_at=reset, credits=0, on_credits="no", use_credits=False,
            read_at=self.now, read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=self.now)
        status = accounts.StatusReading((row,), (), (), timezone.utc, self.now)

        page = accounts_page.board(status)
        self.assertEqual(page.count('title="No usage rate is available."'), 2)
        self.assertNotIn("<time", page)
        _ = self.alert([note])
        self.assertEqual(self.expiry_accounts(), [])

    def test_sender_result_that_proves_no_post_retries_at_the_next_check(self) -> None:
        outcomes = (
            (2, "REFUSED: pushover: refused before sending: silence is active"),
            (4, "HELD: not sent, failed review: needs changes"),
            (0, "SKIPPED: existing key"),
        )
        for code, output in outcomes:
            with self.subTest(output=output):
                quota_alert.STATE.unlink(missing_ok=True)
                self.noted.clear()
                with (mock.patch.object(quota_alert, "tell_user", new=support.REAL_TELL_USER),
                      mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, ""))):
                    self.check(self.now)

                self.check(self.now + timedelta(minutes=2), left=19)
                self.check(self.now + timedelta(minutes=4), left=19)
                self.assertEqual(self.expiry_accounts(), ["claude2"])

    def test_lead_and_repeat_hours_come_from_config(self) -> None:
        config = quota_alert.CONFIG
        text = config.read_text(encoding="utf-8")
        _ = config.write_text(text.replace("alert_hours=12", "alert_hours=8").replace("repeat_hours=2", "repeat_hours=1"),
                              encoding="utf-8")
        self.check(self.now)
        self.assertEqual(self.expiry_accounts(), [])
        self.check(self.now + timedelta(hours=1), left=12)
        self.check(self.now + timedelta(hours=2), left=10)

        self.assertEqual(self.expiry_accounts(), ["claude2", "claude2"])

    def test_only_natedev_sends_claude_expiry_pushes(self) -> None:
        with mock.patch.object(socket, "gethostname", return_value="mac"):
            self.check(self.now)
        self.assertEqual(self.expiry_accounts(), [])
        self.check(self.now)
        self.assertEqual(self.expiry_accounts(), ["claude2"])

    def test_consumed_move_does_not_cover_either_claude_quota_reminder(self) -> None:
        move = next_moves.add_next_move("units", 1, "claude3")
        journal = role_map.groups_file().with_name("roles.jsonl")
        self.assertTrue(journal.is_relative_to(self.root))
        record = {"group": "units", "cause": "auto", "why": f"auto: {move.id}"}
        _ = journal.write_text(json.dumps(record) + "\n", encoding="utf-8")

        self.check(self.now, left=1)

        self.assertCountEqual([push.id for push in self.noted], ["quota.claude_expiry", "quota.claude_next_move"])

    def test_silence_records_nothing_and_the_next_pass_uses_a_fresh_forecast(self) -> None:
        with redirect_stdout(StringIO()):
            self.assertEqual(silence.on(None, lambda _argv: 0), 0)
            self.check(self.now)
            self.assertEqual(self.expiry_accounts(), [])
            self.assertEqual(silence.read_kept(), [])
            with quota_alert.state_file() as state:
                self.assertEqual(state.get("claude_expiry", {}), {})
            self.assertEqual(silence.off(lambda _argv: 0), 0)
        self.assertEqual(self.expiry_accounts(), [])

        self.check(self.now + timedelta(minutes=2), left=18)

        self.assertEqual(self.expiry_accounts(), ["claude2"])
        self.assertEqual((self.noted[0].title, self.noted[0].body), ("claude2 runs out Sun 11-22 02:02 EST", ""))

    def test_phone_sender_never_keeps_an_expiry_push_if_silence_starts(self) -> None:
        rendered = push_texts.render("quota.claude_expiry", account="claude2", empty_at="Sun 11-22 07:00 EST")
        draft = review.Draft(rendered.sender, "0", rendered.title, rendered.action, rendered.body, False)
        with (redirect_stdout(StringIO()), redirect_stderr(StringIO()), mock.patch.object(pushover, "log"),
              mock.patch.object(pushover, "post_message", side_effect=AssertionError("unexpected phone send"))):
            self.assertEqual(silence.on(None, lambda _argv: 0), 0)
            self.assertEqual(pushover.deliver(draft, text_id=rendered.id), 2)
            self.assertEqual(silence.read_kept(), [])

    def test_repeat_uses_attempt_time_and_preserves_it_during_forecast_changes(self) -> None:
        started = self.now + timedelta(seconds=20)

        def send(push: push_texts.RenderedPush) -> quota_alert.PushDelivery:
            self.clock += timedelta(seconds=30)
            return self.tell_user(push)

        with mock.patch.object(quota_alert, "tell_user", side_effect=send):
            self.check(self.now, delay_seconds=20)
        self.check(self.now + timedelta(hours=1), left=30)
        self.check(self.now + timedelta(hours=1, minutes=2), left=18)
        self.check(started + timedelta(hours=2, seconds=-1), left=16)
        self.assertEqual(self.expiry_accounts(), ["claude2"])
        self.check(started + timedelta(hours=2), left=16)
        self.assertEqual(self.expiry_accounts(), ["claude2", "claude2"])

    def test_attempt_survives_an_interruption_before_the_sender_reports_a_result(self) -> None:
        with mock.patch.object(quota_alert, "tell_user", side_effect=InterruptedError("fixture interrupted")):
            with self.assertRaises(InterruptedError):
                self.check(self.now)

        self.check(self.now + timedelta(minutes=2))
        self.assertEqual(self.expiry_accounts(), [])
        self.check(self.now + timedelta(hours=2), left=16)
        self.assertEqual(self.expiry_accounts(), ["claude2"])

    def test_missing_sender_result_preserves_the_attempt(self) -> None:
        outcomes = (
            subprocess.CompletedProcess([], -9, "", ""),
            subprocess.CompletedProcess([], 3, "FAILED: the user was not reached: timed out", ""),
            subprocess.CompletedProcess([], 3, "FAILED: the user was not reached: exit -9", ""),
            subprocess.CompletedProcess([], 3, "FAILED: the user was not reached: pushover: could not reach Pushover:", ""),
            subprocess.TimeoutExpired([], 1),
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                quota_alert.STATE.unlink(missing_ok=True)
                self.noted.clear()
                with (mock.patch.object(quota_alert, "tell_user", new=support.REAL_TELL_USER),
                      mock.patch.object(subprocess, "run", side_effect=[outcome])):
                    self.check(self.now)

                self.check(self.now + timedelta(minutes=2))
                self.assertEqual(self.expiry_accounts(), [])
                self.check(self.now + timedelta(hours=2), left=16)
                self.assertEqual(self.expiry_accounts(), ["claude2"])

    def test_truncated_phone_response_keeps_the_attempt(self) -> None:
        failed = subprocess.CompletedProcess([], 3,
            "FAILED: the user was not reached: Traceback (most recent call last): "
            + "http.client.IncompleteRead: IncompleteRead(14 bytes read, 20 more expected)", "")
        with (mock.patch.object(quota_alert, "tell_user", new=support.REAL_TELL_USER),
              mock.patch.object(subprocess, "run", return_value=failed)):
            self.check(self.now)

        self.check(self.now + timedelta(minutes=2), left=19)
        self.assertEqual(self.expiry_accounts(), [])
        self.check(self.now + timedelta(hours=2), left=16)
        self.assertEqual(self.expiry_accounts(), ["claude2"])

    def test_expiry_requires_current_approval_without_starting_live_review(self) -> None:
        argv = ["--source", "quota_alert", "--text-id", "quota.claude_expiry", "--value", "account=claude2",
                "--value", "empty_at=Sun 11-22 07:00 EST"]
        with (mock.patch.object(push_texts, "approval_date", return_value="") as approval,
              mock.patch.object(review, "review", side_effect=AssertionError("live review started")),
              mock.patch.object(review, "switched_off", return_value=False),
              mock.patch.object(review, "log_review"),
              mock.patch.object(pushover, "log"), mock.patch.object(pushover, "deliver", return_value=0) as deliver,
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            self.assertEqual(pushover.send(argv), 2)
            deliver.assert_not_called()
            approval.return_value = "2026-11-21"
            self.assertEqual(pushover.send(argv), 0)
            deliver.assert_called_once()

    def test_forecast_uses_the_latest_balance_and_starts_at_now(self) -> None:
        observed = self.now - timedelta(minutes=10)
        left = 24 + 1 / 6
        self.save_rates(self.now, {"claude2": 2}, left=left)
        note = self.recorded_note(self.now, left)
        _ = self.alert([note])
        self.assertEqual(self.expiry_accounts(), [])

        config = quota_alert.CONFIG
        settings = config.read_text(encoding="utf-8")
        _ = config.write_text(settings.replace("claude_expiry.alert_hours=12", "claude_expiry.alert_hours=13"),
                              encoding="utf-8")
        self.save_rates(observed, {"claude2": 2}, left=left)
        _ = self.alert([self.recorded_note(observed, left)])

        self.assertEqual(self.expiry_accounts(), ["claude2"])
        self.assertEqual((self.noted[0].title, self.noted[0].body), ("claude2 runs out Sun 11-22 05:05 EST", ""))

    def test_each_host_override_needs_a_whole_number_limit(self) -> None:
        config = quota_alert.CONFIG
        valid = config.read_text(encoding="utf-8")
        for key in ("claude_expiry.alert_hours", "claude_expiry.repeat_hours"):
            with self.subTest(key=key), redirect_stderr(StringIO()):
                _ = config.write_text(valid + f"{key}.natedev=169\n", encoding="utf-8")
                self.check(self.now)
                self.assertEqual(self.expiry_accounts(), [])
