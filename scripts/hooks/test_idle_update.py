"""Idle update mode tells a staff session which short form its text to nate takes."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from contextlib import AbstractContextManager
from typing import cast, final, override
from unittest import mock

from scripts.hooks import idle_update
from scripts.lib import conf
from scripts.production.staff_test_support import StaffTestCase, fixture_member
from scripts.production import staff

LIMITS = idle_update.Limits(after_seconds=300, renew_seconds=600)


@final
class IdleUpdateContextTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        _ = self.enterContext(cast(AbstractContextManager[object],
                                   mock.patch.dict(os.environ, {"CONVERSATION_PAUSE_STATE_DIR": folder.name})))

    def test_the_first_prompt_starts_the_count_and_adds_nothing(self) -> None:
        self.assertEqual(idle_update.context("fixture", 1000, LIMITS), "")
        self.assertEqual(idle_update.context("fixture", 1299, LIMITS), "")

    def test_the_first_turn_after_the_idle_time_asks_for_one_short_update(self) -> None:
        idle_update.typed("fixture", 1000)
        self.assertEqual(idle_update.context("fixture", 1299, LIMITS), "")
        text = idle_update.context("fixture", 1300, LIMITS)
        self.assertIn("for 5 min", text)
        self.assertIn("what you last had on deck with him", text)
        self.assertIn("only after he says yes", text)

    def test_turns_before_the_renewal_end_with_a_dash_alone(self) -> None:
        idle_update.typed("fixture", 1000)
        _ = idle_update.context("fixture", 1300, LIMITS)
        text = idle_update.context("fixture", 1500, LIMITS)
        self.assertIn('End this turn with "—" alone', text)
        self.assertIn("due in 7 min", text)

    def test_the_first_turn_after_the_renewal_time_asks_only_for_news(self) -> None:
        idle_update.typed("fixture", 1000)
        _ = idle_update.context("fixture", 1300, LIMITS)
        text = idle_update.context("fixture", 1950, LIMITS)
        self.assertIn("only the news since your last update", text)
        self.assertIn('End this turn with "—" alone', idle_update.context("fixture", 1960, LIMITS))

    def test_a_typed_prompt_ends_the_mode(self) -> None:
        idle_update.typed("fixture", 1000)
        _ = idle_update.context("fixture", 1300, LIMITS)
        idle_update.typed("fixture", 1400)
        self.assertEqual(idle_update.context("fixture", 1500, LIMITS), "")
        self.assertIn("what you last had on deck", idle_update.context("fixture", 1700, LIMITS))

    def test_a_damaged_record_restarts_the_count(self) -> None:
        idle_update.state_dir().mkdir(parents=True)
        _ = (idle_update.state_dir() / "fixture.json").write_text("{", encoding="utf-8")
        self.assertEqual(idle_update.context("fixture", 5000, LIMITS), "")
        self.assertEqual(idle_update.context("fixture", 5299, LIMITS), "")


@final
class IdleUpdateLimitsTests(unittest.TestCase):
    def test_the_limits_come_from_the_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            _ = (Path(folder) / "idle_update.conf").write_text(
                "# whole: 1..120\nafter_minutes=3\n# whole: 1..240\nrenew_minutes=20\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CONF_DIR": folder}):
                self.assertEqual(idle_update.limits(), idle_update.Limits(180, 1200))

    def test_the_repository_file_holds_both_keys_in_range(self) -> None:
        source = Path(__file__).resolve().parents[2] / "config"
        with mock.patch.dict(os.environ, {"CONF_DIR": str(source)}):
            limits = idle_update.limits()
        self.assertGreater(limits.after_seconds, 0)
        self.assertGreater(limits.renew_seconds, 0)

    def test_an_out_of_range_value_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            _ = (Path(folder) / "idle_update.conf").write_text(
                "# whole: 1..120\nafter_minutes=0\n# whole: 1..240\nrenew_minutes=20\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CONF_DIR": folder}), self.assertRaises(conf.Unusable):
                _ = idle_update.limits()


@final
class IdleUpdateRoleTests(StaffTestCase):
    def test_producer_showrunner_and_unit_rows_have_the_mode(self) -> None:
        owner = fixture_member("showrunner", name="TEST-production")
        unit = fixture_member("unit", name="TEST-director", parent=owner.id)
        self.assertTrue(idle_update.has_role(staff.StaffSnapshot((owner,))))
        self.assertTrue(idle_update.has_role(staff.StaffSnapshot((unit,))))
        self.assertFalse(idle_update.has_role(staff.StaffSnapshot(())))


if __name__ == "__main__":
    _ = unittest.main()
