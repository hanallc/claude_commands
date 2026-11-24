"""Read Mac idle settings through the shared configuration reader."""

from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import cast
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.lib import conf
from scripts.buildlog import mac_idle, mac_idle_alert
from scripts.buildlog.test_index import use_test_log
from scripts.buildlog.mac_idle_test_support import LiveClaim, write_idle_alert, write_idle_config, write_live_mac_state
from scripts.lint.memory_test_support import commit_config_fixture


use_test_log()


class SavedAlertTests(unittest.TestCase):
    def test_free_alert_names_kept_claims_and_uses_the_waiting_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_idle_config(Path(directory))
            path = write_idle_alert(Path(directory), 1600)
            live = write_live_mac_state(Path(directory) / "mac-state")
            row = cast(dict[str, object], json.loads(path.read_text()))
            row["cause"] = "free"
            for waiting, words in ((0, "0 steps wait"), (1, "1 step waits"), (2, "2 steps wait")):
                for count in (0, 1, 2):
                    with self.subTest(waiting=waiting, kept=count), \
                         mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent), "MAC_TEST_STATE_DIR": str(live)}):
                        row["waiting"], row["kept_claims"] = waiting, count
                        _ = path.write_text(json.dumps(row))
                        line = mac_idle_alert.alert_line(path, 1600, ZoneInfo("UTC")) or ""
                        if waiting == 0:
                            self.assertEqual(line, "")
                            continue
                        self.assertIn(f"{words} for the Mac", line)
                        if count:
                            noun = "claim" if count == 1 else "claims"
                            self.assertIn(f"{count} {noun} kept for cleanup", line)
                            self.assertIn("Kept claims hold no work", line)
                            self.assertNotIn("no claim", line)
                        else:
                            self.assertIn("No block and no claim", line)
                            self.assertNotIn("kept for cleanup", line)

    def test_invalid_kept_claim_counts_are_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_idle_alert(Path(directory), 1600)
            row = cast(dict[str, object], json.loads(path.read_text()))
            for count in (-1, True, "1", 1.5):
                with self.subTest(count=count):
                    row["kept_claims"] = count
                    _ = path.write_text(json.dumps(row))
                    self.assertEqual(mac_idle_alert.read_alert(path), mac_idle_alert.UnreadableAlert("invalid kept_claims"))

    def test_alert_without_cause_change_time_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_idle_alert(Path(directory), 1600)
            row = cast(dict[str, object], json.loads(path.read_text()))
            del row["changed"]
            _ = path.write_text(json.dumps(row))
            self.assertEqual(mac_idle_alert.read_alert(path), mac_idle_alert.UnreadableAlert("invalid changed"))


    def test_a_shown_block_carries_its_diagnostics_and_an_omitted_block_keeps_only_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_idle_config(Path(directory))
            path = write_idle_alert(Path(directory), 1600)
            live = write_live_mac_state(Path(directory) / "mac-state", block=True)
            zone = ZoneInfo("UTC")
            with mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent), "MAC_TEST_STATE_DIR": str(live)}):
                self.assertEqual(mac_idle_alert.alert_line(path, 1600, zone, block=mac_idle_alert.BlockText.OMITTED), None)
                row = cast(dict[str, object], json.loads(path.read_text()))
                row["admission_error"] = "socket closed"
                _ = path.write_text(json.dumps(row))
                self.assertEqual(mac_idle_alert.alert_line(path, 1600, zone, block=mac_idle_alert.BlockText.OMITTED),
                                 "MAC IDLE WATCH: admission query failed: socket closed.")
                self.assertEqual(mac_idle_alert.alert_line(path, 1600, zone),
                                 "MAC BLOCKED by research since 00:16 (P15 F11 L0 run): 7 steps wait for the Mac, idle 10 min; "
                                 + "/build:mac unblock clears it; admission query failed: socket closed.")

    def test_a_saved_condition_shows_only_while_the_live_mac_state_agrees(self) -> None:
        live_states: dict[str, tuple[bool, LiveClaim, float]] = {
            "free": (False, "none", 0), "blocked": (True, "none", 0), "claim_idle": (False, "idle", 0),
            "working": (True, "working", 0), "unreachable": (False, "none", 1900), "unreachable_ended": (False, "none", 1500),
        }
        saved_states = {
            "blocked": ("idle", "MAC BLOCKED by research"), "claim_idle": ("idle", "MAC IDLE 10 min"),
            "free": ("idle", "MAC IDLE 10 min"), "disk": ("idle", "MAC IDLE 10 min"),
            "unreachable": ("unreachable", "MAC UNREACHABLE until"),
        }
        # An ended backoff leaves a free Mac.
        agreeing = {("blocked", "blocked"), ("claim_idle", "claim_idle"), ("free", "free"), ("disk", "free"),
                    ("free", "unreachable_ended"), ("disk", "unreachable_ended"), ("unreachable", "unreachable")}
        with tempfile.TemporaryDirectory() as directory:
            config = write_idle_config(Path(directory))
            path = write_idle_alert(Path(directory), 1600)
            row = cast(dict[str, object], json.loads(path.read_text()))
            for saved_cause, (state, shown) in saved_states.items():
                for live_name, (block, claim, until) in live_states.items():
                    live = write_live_mac_state(Path(directory) / "mac-state", block=block, claim=claim,
                                                unreachable_until=until)
                    row.update(state=state, cause="" if state == "unreachable" else saved_cause, until=1900)
                    for admission_error in ("", "socket closed"):
                        with self.subTest(saved=saved_cause, live=live_name, admission_error=admission_error), \
                             mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent), "MAC_TEST_STATE_DIR": str(live),
                                                          "MAC_IDLE_STATE_DIR": str(path.parent)}):
                            row["admission_error"] = admission_error
                            _ = path.write_text(json.dumps(row))
                            line = mac_idle.view_alert_line(1600, ZoneInfo("UTC"))
                            if (saved_cause, live_name) in agreeing:
                                self.assertIn(shown, line or "")
                            else:
                                self.assertEqual(line, "MAC IDLE WATCH: admission query failed: socket closed."
                                                 if admission_error else None)

class MacIdleConfigTests(unittest.TestCase):
    def test_conf_dir_values_apply_without_a_second_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_idle_config(Path(directory), "alert_seconds=7\n"
                                     + "stale_seconds=13\nrepeat_minutes=17\nsend_timeout_s=19\n")
            with mock.patch.dict(os.environ, {"CONF_DIR": directory, "MAC_TEST_CONFIG": "/unused"}), \
                 mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read:
                settings = mac_idle_alert.read_idle_config()
            self.assertEqual(settings, mac_idle_alert.MacIdleConfig(7, 13, 17, 19))
            self.assertEqual([call.args[0] for call in read.call_args_list], [path])

    def test_invalid_values_recover_from_history_without_code_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_idle_config(Path(directory), "alert_seconds=23\n")
            commit_config_fixture(path)
            _ = write_idle_config(Path(directory), "alert_seconds=nan\n")
            errors = io.StringIO()
            with mock.patch.dict(os.environ, {"CONF_DIR": directory}), redirect_stderr(errors):
                settings = mac_idle_alert.read_idle_config()
            self.assertEqual(settings.alert_seconds, 23)
            self.assertIn("alert_seconds='nan'", errors.getvalue())
            path.unlink()
            with (mock.patch.dict(os.environ, {"CONF_DIR": directory}), redirect_stderr(io.StringIO()),
                  self.assertRaises(conf.Unusable) as caught):
                _ = mac_idle_alert.read_idle_config()
            self.assertIn(f"{path}: ", str(caught.exception))
            self.assertIn("is missing", str(caught.exception))

    def test_missing_or_invalid_values_without_history_raise_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"CONF_DIR": directory}):
            path = Path(directory) / "mac_test.conf"
            with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
                _ = mac_idle_alert.read_idle_config()
            for key in ("alert_seconds", "stale_seconds", "repeat_minutes", "send_timeout_s"):
                for value in ("0", "-1", "nan", "inf", "bad"):
                    with self.subTest(key=key, value=value):
                        _ = write_idle_config(Path(directory), f"{key}={value}\n")
                        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
                            _ = mac_idle_alert.read_idle_config()
            _ = path.write_text("")
            with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
                _ = mac_idle_alert.read_idle_config()
