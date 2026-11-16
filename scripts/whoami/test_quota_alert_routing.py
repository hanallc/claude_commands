"""Quota alerts: routing."""

from __future__ import annotations

from scripts.lib import live_settings, models_registry
from scripts.lib.test_models_registry import write_registry

import json
from pathlib import Path
import tempfile
import unittest
from datetime import timedelta
from typing import cast
from unittest import mock
from scripts.whoami import quota_alert_test_support as support
from scripts.whoami import quota_alert
from scripts.whoami import agent_accounts
from scripts.whoami.credit_accounts_test_support import set_credits
from scripts.accounts import accounts, refusals
from scripts.whoami.quota_alert_test_support import Notes, REGISTRY as REGISTRY

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
    def test_a_quota_refusal_moves_every_codex_function_to_claude_once(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.assertEqual(self.refuse(), "switched to claude for codex1: direct, design_roles")
        self.assertEqual(self.assignments(),
                         {"direct": "claude", "cli": "claude", "design_roles": "claude"})
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            switch = state["fallback"]
            self.assertIsInstance(switch, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(switch, quota_alert.AccountCodexQuotaFallback)
            self.assertIsInstance(switch.hold, quota_alert.NoFallbackHold)
        saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
        assert "switch" in saved
        self.assertNotIn("credits_off_until", saved["switch"])
        self.assertEqual(self.routing_entries()[-1]["kind"], "routed to claude")
        # The user changes design_roles during the episode. The next run keeps that choice.
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "design_roles", "codex"))
        _ = self.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["design_roles"], "codex")

    def test_registry_publication_reports_only_a_publish_or_a_wait(self) -> None:
        journal = Path(self.enterContext(tempfile.TemporaryDirectory())) / "waiting.json"
        with mock.patch.object(live_settings, "journal_path", return_value=journal):
            with mock.patch.object(models_registry, "publish_pending", return_value=False):
                self.assertEqual(live_settings.publish_waiting(), [])
            with mock.patch.object(models_registry, "publish_pending", return_value=True):
                self.assertEqual(live_settings.publish_waiting(), ["registry rows published"])
            with mock.patch.object(models_registry, "publish_pending", side_effect=OSError("git fetch:\n offline")):
                self.assertEqual(live_settings.publish_waiting(), ["registry rows not on main yet: git fetch: offline"])

    def test_successful_claude_fallback_is_logged_without_a_session_or_phone_push(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.sent.clear()
        self.pushed.clear()
        later = self.now + timedelta(minutes=5)
        log = self.block(notes, later)
        self.assertIn("switched to claude for codex1: direct, design_roles", log)
        self.assertTrue(any(line.startswith("quota status codex1:") for line in log))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        self.sent.clear()
        _ = self.block(notes, later + timedelta(minutes=1))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])

    def test_refusal_push_says_when_the_switch_had_already_happened(self) -> None:
        note = self.note("codex1.md", "active", "0")
        _ = self.alert([note], self.now)
        self.pushed.clear()
        stamp = self.now.isoformat(timespec="seconds")
        owner = self.alert_readings([note], self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        with quota_alert.state_file() as state:
            self.assertEqual(quota_alert.switch_to_claude(state, note, stamp, owner),
                             quota_alert.CodexWorkMovedToClaude("codex1", ("direct", "design_roles")))

        log = self.block([note], self.now + timedelta(minutes=1))
        self.assertTrue(any("New work had already moved to Claude" in line for line in log))
        self.assertEqual(self.pushed, [])

    def test_refusal_push_says_when_nothing_was_on_codex(self) -> None:
        registry = quota_alert.REGISTRY
        _ = write_registry(registry, {"assignments": {"direct": "claude", "design_roles": "claude"}}, base_text=REGISTRY)
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        log = self.block(notes, self.now + timedelta(minutes=1))
        self.assertTrue(any("No delegated work needed to move" in line for line in log))
        self.assertEqual(self.pushed, [])

    def test_workers_refusals_keep_codex_when_current_allowance_or_credits_can_pay(self) -> None:
        for left, balance, setting in (("96", "2544", "on"), ("0", "2544", "on"), ("96", "0", "off")):
            with self.subTest(left=left, balance=balance, setting=setting):
                set_credits(self.credits, "codex1", setting)
                notes = [self.note("codex1.md", "active", left, credit_balance=balance)]
                before = self.assignments()
                self.pushed.clear()
                self.assertIsInstance(accounts.pick("codex", self.now, exclude=frozenset({"codex1"})),
                                      accounts.NoAccountWithRoom)
                log: list[str] = []
                with mock.patch.object(quota_alert, "switch_to_claude") as switch:
                    for seconds in (0, 8):
                        checked = self.now + timedelta(seconds=seconds)
                        readings = self.alert_readings(notes, checked)
                        identity = readings.selected_account
                        assert isinstance(identity, agent_accounts.KnownCodexAccount)
                        log = quota_alert.blocked(notes, identity, checked, codex_readings=readings,
                                                  window=refusals.KnownWindow("weekly"))
                        self.assertEqual(self.pushed, [])
                        _ = self.alert(notes, self.now + timedelta(seconds=seconds + 1))
                        self.pushed.clear()
                switch.assert_not_called()
                self.assertTrue(any("can still serve workers" in line for line in log), log)
                self.assertEqual(self.assignments(), before)
                self.assertEqual((self.broadcasts, self.broadcasts.stops, self.broadcasts.fyi), ([], [], []))
                self.assertEqual(self.sent, [])
                self.assertEqual(self.routing_entries(), [])
                with quota_alert.state_file() as state:
                    self.assertFalse(state.get("no_worker_mode", False))
                    self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)


if __name__ == "__main__":
    _ = unittest.main()
