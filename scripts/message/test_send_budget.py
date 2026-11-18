"""Caller budgets follow live delivery and cleanup settings."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.message import send_budget
from scripts.lint.memory_test_support import write_config_fixture


def write_configs(directory: Path) -> None:
    _ = write_config_fixture(directory / "send.conf",
        "delivery_timeout=40\nkill_grace=10\nremote_transport_seconds=30\ncaller_margin_seconds=15\n"
        + "waits_review_seconds=30\nuser_cancel_timeout=30\ncheck_timeout=20\n")
    _ = write_config_fixture(directory / "postmaster.conf",
                             "wait_seconds=3.0\nclaim_grace_seconds=15.0\n")
    _ = write_config_fixture(directory / "codex_daemon.conf", "timeout_seconds=5.0\n")


class SendBudgetTests(unittest.TestCase):
    def test_default_budget_tracks_delivery_and_cleanup_changes_without_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CONF_DIR": directory}):
            write_configs(Path(directory))
            config = Path(directory) / "send.conf"
            before = send_budget.seconds()
            _ = config.write_text(config.read_text().replace("delivery_timeout=40", "delivery_timeout=120")
                                  .replace("kill_grace=10", "kill_grace=80"))
            after = send_budget.seconds()
            self.assertEqual(after - before, 220)
            self.assertGreater(after, 120 + 2 * 80 + 30)

    def test_user_budget_preserves_the_larger_delivery_or_review_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CONF_DIR": directory}):
            write_configs(Path(directory))
            config = Path(directory) / "send.conf"
            _ = config.write_text(config.read_text().replace("delivery_timeout=40", "delivery_timeout=120")
                                  .replace("kill_grace=10", "kill_grace=80"))
            self.assertEqual(send_budget.user_seconds(60), 413)
            self.assertEqual(send_budget.user_seconds(180), 473)

    def test_remote_transport_setting_changes_both_sender_budgets_without_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CONF_DIR": directory}):
            write_configs(Path(directory))
            config = Path(directory) / "send.conf"
            caller, remote = send_budget.seconds(), send_budget.remote_seconds(40)
            _ = config.write_text(config.read_text().replace("remote_transport_seconds=30", "remote_transport_seconds=90"))
            self.assertEqual(send_budget.seconds() - caller, 60)
            self.assertEqual(send_budget.remote_seconds(40) - remote, 60)
            self.assertGreater(send_budget.seconds(), send_budget.remote_seconds(40))

    def test_review_and_discovery_settings_extend_the_caller_budget_without_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CONF_DIR": directory}):
            write_configs(Path(directory))
            send = Path(directory) / "send.conf"
            daemon = Path(directory) / "codex_daemon.conf"
            before = send_budget.seconds()
            _ = send.write_text(send.read_text().replace("waits_review_seconds=30", "waits_review_seconds=90"))
            _ = daemon.write_text(daemon.read_text().replace("timeout_seconds=5.0", "timeout_seconds=25.0"))
            self.assertEqual(send_budget.seconds() - before, 160)
