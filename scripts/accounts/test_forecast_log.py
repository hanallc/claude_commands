"""The forecast log keeps each account's hourly prediction and the real empty, reset and role times."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.accounts import accounts, forecast_log
from scripts.whoami import agent_accounts, quota_alert, run_out

NOW = datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=2)
LOGIN = "fixture@example.invalid"


def _row(label: str, left: float | None, *, reset: datetime = RESET, read_at: datetime = NOW,
         roles: tuple[str, ...] = ("units",)) -> accounts.StatusRow:
    unavailable = accounts.StatusUnavailable("unavailable")
    return accounts.StatusRow(
        label=label, tool="codex" if label.startswith("codex") else "claude", folder=f"~/.{label}",
        login=LOGIN, roles=roles, weekly_left_percent=unavailable if left is None else left, resets_at=reset,
        credits=unavailable, on_credits="no", use_credits=False, read_at=read_at,
        read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=read_at)


def _reading(*rows: accounts.StatusRow, at: datetime = NOW) -> accounts.StatusReading:
    return accounts.StatusReading(rows, (), (), timezone.utc, at)


class ForecastLogTests(unittest.TestCase):
    readings_log: Path = Path()

    @override
    def setUp(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(home)}))
        self.readings_log = home / "readings.jsonl"
        _ = self.enterContext(patch.object(run_out, "READINGS_LOG", self.readings_log))

    def lines(self) -> list[dict[str, object]]:
        try:
            text = forecast_log.log_path().read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [cast(dict[str, object], json.loads(line)) for line in text.splitlines()]

    def kinds(self) -> list[tuple[object, object, object]]:
        return [(line["kind"], line["account"], line["at"]) for line in self.lines()]

    def test_the_first_pass_writes_one_prediction_per_account_with_a_known_percent(self) -> None:
        forecast_log.sample(_reading(_row("claude1", 40), _row("codex1", None)), NOW)
        [line] = self.lines()
        self.assertEqual((line["kind"], line["account"], line["left"], line["resets_at"]),
                         ("forecast", "claude1", 40, RESET.isoformat()))
        self.assertNotIn(LOGIN, forecast_log.log_path().read_text(encoding="utf-8"))

    def test_the_prediction_is_the_one_the_accounts_page_computes(self) -> None:
        rows = [json.dumps({"account": "claude1", "at": (NOW - timedelta(minutes=10 * step)).isoformat(),
                            "remaining": 40 + step}) for step in range(12, -1, -1)]
        _ = self.readings_log.write_text("\n".join(rows) + "\n", encoding="utf-8")
        forecast_log.sample(_reading(_row("claude1", 40)), NOW)
        expected = quota_alert.weekly_forecast(run_out.read_readings(self.readings_log)["claude1"], NOW, RESET,
                                               current=run_out.Reading(NOW, 60))
        assert expected is not None and expected.empty_at is not None
        [line] = self.lines()
        self.assertEqual((line["used_per_hour"], line["empty_at"], line["lasts_to_reset"]),
                         (expected.used_per_hour, expected.empty_at.isoformat(timespec="seconds"), False))

    def test_a_prediction_is_written_once_an_hour(self) -> None:
        for minutes in (0, 2, 58, 60, 62):
            at = NOW + timedelta(minutes=minutes)
            forecast_log.sample(_reading(_row("claude1", 40, read_at=at), at=at), at)
        self.assertEqual([line["at"] for line in self.lines()],
                         [NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat()])

    def test_the_first_reading_at_zero_writes_one_empty_line(self) -> None:
        for minutes, left in ((0, 3), (2, 0), (4, 0)):
            at = NOW + timedelta(minutes=minutes)
            forecast_log.sample(_reading(_row("codex2", left, read_at=at - timedelta(seconds=30)), at=at), at)
        self.assertEqual([kind for kind in self.kinds() if kind[0] == "empty"],
                         [("empty", "codex2", (NOW + timedelta(minutes=2, seconds=-30)).isoformat())])

    def test_a_weekly_reset_is_written_at_its_time_and_an_early_refill_at_its_read(self) -> None:
        forecast_log.sample(_reading(_row("claude1", 0)), NOW)
        after_reset = RESET + timedelta(minutes=3)
        later = RESET + timedelta(days=7)
        forecast_log.sample(_reading(_row("claude1", 100, reset=later, read_at=after_reset), at=after_reset),
                            after_reset)
        refill = after_reset + timedelta(hours=2)
        forecast_log.sample(_reading(_row("claude1", 80, reset=later, read_at=refill), at=refill), refill)
        forecast_log.sample(_reading(_row("claude1", 100, reset=later, read_at=refill), at=refill), refill)
        rounding = refill + timedelta(minutes=2)
        forecast_log.sample(_reading(_row("claude1", 100.5, reset=later, read_at=rounding), at=rounding), rounding)
        self.assertEqual([kind for kind in self.kinds() if kind[0] == "reset"],
                         [("reset", "claude1", RESET.isoformat()), ("reset", "claude1", refill.isoformat())])

    def test_a_role_change_writes_the_new_roles(self) -> None:
        forecast_log.sample(_reading(_row("claude1", 40, roles=("units",))), NOW)
        at = NOW + timedelta(minutes=2)
        forecast_log.sample(_reading(_row("claude1", 40, roles=("workers", "units"), read_at=at), at=at), at)
        [line] = [line for line in self.lines() if line["kind"] == "roles"]
        self.assertEqual((line["at"], line["roles"]), (at.isoformat(), ["units", "workers"]))

    def test_lines_older_than_the_window_drop_at_the_next_prediction(self) -> None:
        old = NOW - forecast_log.WINDOW - timedelta(hours=1)
        forecast_log.sample(_reading(_row("claude1", 40, read_at=old), at=old), old)
        forecast_log.sample(_reading(_row("claude1", 40)), NOW)
        self.assertEqual([line["at"] for line in self.lines()], [NOW.isoformat()])

    def test_a_damaged_state_file_starts_again_without_events(self) -> None:
        forecast_log.sample(_reading(_row("claude1", 5)), NOW)
        _ = forecast_log.log_path().with_suffix(".state.json").write_text("{not json", encoding="utf-8")
        at = NOW + timedelta(minutes=2)
        forecast_log.sample(_reading(_row("claude1", 0, read_at=at), at=at), at)
        self.assertEqual([kind[0] for kind in self.kinds()], ["forecast", "forecast"])


if __name__ == "__main__":
    _ = unittest.main()
