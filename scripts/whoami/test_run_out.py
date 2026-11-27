"""The readings log and the recent rate for account usage."""

import json
import math
import os
import tempfile
import unittest
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


from scripts.lib import conf
from scripts.whoami.run_out import Reading, read_readings
from scripts.whoami import run_out
from scripts.whoami.note_file import NoResetKnown, NumberedAccountLabel

START = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


STEP = 600


def climb(used: Sequence[float], *, start: datetime = START) -> list[Reading]:
    """One reading every ten minutes."""
    return [Reading(start + timedelta(seconds=STEP * index), percent) for index, percent in enumerate(used)]


class RunOutTests(unittest.TestCase):
    def credit_history(self, values: Sequence[tuple[int, float]]) -> Path:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "readings.jsonl"
        _ = path.write_text("".join(json.dumps({"account": "codex1", "at":
            (START + timedelta(minutes=minute)).isoformat(), "remaining": 0, "credits": balance})
            + "\n" for minute, balance in values))
        return path

    def test_credit_use_counts_decreases_and_skips_a_purchase(self) -> None:
        path = self.credit_history([(-2, 100), (10, 90), (30, 200), (60, 170), (70, 0)])
        used = run_out.credits_used(run_out.read_credit_readings(path), "codex1",
            since=START, until=START + timedelta(hours=1))
        self.assertIsInstance(used, run_out.CreditsUsed)
        assert isinstance(used, run_out.CreditsUsed)
        self.assertEqual(used.used, 40)
        self.assertEqual(used.observed_from, START - timedelta(minutes=2))
        self.assertEqual(used.observed_to, START + timedelta(hours=1))

    def test_credit_use_reports_zero_only_with_observations_covering_the_hour(self) -> None:
        path = self.credit_history([(0, 6370), (30, 6370), (60, 6370)])
        used = run_out.credits_used(run_out.read_credit_readings(path), "codex1",
            since=START, until=START + timedelta(hours=1))
        self.assertIsInstance(used, run_out.CreditsUsed)
        assert isinstance(used, run_out.CreditsUsed)
        self.assertEqual((used.used, used.observed_from, used.observed_to),
                         (0, START, START + timedelta(hours=1)))

    def test_credit_use_without_a_credit_line_names_the_missing_reading(self) -> None:
        path = self.credit_history([])
        _ = path.write_text(json.dumps({"account": "codex1", "at": START.isoformat(), "remaining": 0}) + "\n")
        self.assertIsInstance(run_out.credits_used(run_out.read_credit_readings(path), "codex1",
            since=START, until=START + timedelta(hours=1)), run_out.NoCreditReading)

    def test_credit_use_with_stale_or_short_observation_coverage_is_unknown(self) -> None:
        for values in ([(-120, 100), (-90, 90)], [(40, 100), (60, 90)], [(0, 100), (49, 90)]):
            with self.subTest(values=values):
                path = self.credit_history(values)
                result = run_out.credits_used(run_out.read_credit_readings(path), "codex1",
                    since=START, until=START + timedelta(hours=1))
                self.assertIsInstance(result, run_out.UsageUnknown)
                assert isinstance(result, run_out.UsageUnknown)
                self.assertTrue(result.reason)

    def test_credit_reader_keeps_accounts_separate_orders_them_and_skips_invalid_balances(self) -> None:
        path = self.credit_history([(60, 2114), (0, 6370)])
        with path.open("a") as output:
            _ = output.write("bad json\n[]\n")
            for balance in (True, -1, math.nan, "unlimited", None):
                _ = output.write(json.dumps({"account": "codex1", "at": START.isoformat(),
                    "remaining": 0, "credits": balance}) + "\n")
            _ = output.write(json.dumps({"account": "codex2", "at": START.isoformat(),
                "remaining": 1, "credits": 20}) + "\n")
        readings = run_out.read_credit_readings(path)
        self.assertEqual([reading.at for reading in readings["codex1"]], [START, START + timedelta(hours=1)])
        self.assertEqual(len(readings["codex2"]), 1)
        self.assertEqual(run_out.read_credit_readings(path.with_name("absent")), {})

    def test_readings_are_per_account_in_time_order_and_bad_lines_are_skipped(self) -> None:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "readings.jsonl"
        records: list[dict[str, object]] = [
            {"account": "claude1", "at": (START + timedelta(hours=1)).isoformat(), "remaining": 70},
            {"account": "claude1", "at": START.isoformat(), "remaining": 80},
            {"account": "codex2", "at": START.isoformat(), "remaining": 55.5},
            {"account": "codex2", "at": "2026-10-05T13:00:00", "remaining": 10},
            {"account": "codex2", "at": START.isoformat(), "remaining": True},
            {"account": "codex2", "at": START.isoformat(), "remaining": math.nan},
        ]
        _ = path.write_text("{bad json}\n[]\n" + "".join(json.dumps(record) + "\n" for record in records))
        self.assertEqual(read_readings(path), {
            "claude1": [Reading(START, 20), Reading(START + timedelta(hours=1), 30)],
            "codex2": [Reading(START, 44.5)],
        })
        self.assertEqual(read_readings(path.with_name("missing.jsonl")), {})

    def test_legacy_and_canonical_lines_form_one_credit_and_usage_history(self) -> None:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "readings.jsonl"
        original = "".join(json.dumps({"account": label, "at": at.isoformat(),
            "remaining": remaining, "credits": credits}) + "\n" for label, at, remaining, credits in (
                ("codex 2", START, 80, 100),
                ("codex2", START + timedelta(minutes=30), 60, 90),
                ("codex 2", START + timedelta(hours=1), 40, 70),
        )).encode()
        _ = path.write_bytes(original)
        self.assertEqual(read_readings(path), {"codex2": [Reading(START, 20),
            Reading(START + timedelta(minutes=30), 40), Reading(START + timedelta(hours=1), 60)]})
        credits = run_out.read_credit_readings(path)
        self.assertEqual(set(credits), {"codex2"})
        result = run_out.credits_used(credits, "codex2", since=START, until=START + timedelta(hours=1))
        assert isinstance(result, run_out.CreditsUsed)
        self.assertEqual(result.used, 30)
        self.assertEqual(path.read_bytes(), original)

    def test_hourly_usage_rate_uses_the_configured_window(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = folder / "accounts.conf"
        cases = (
            (30, [2, 20, 22, 24, 26, 28, 30, 32, 90], run_out.UsageRate(12)),
            (30, [2, 20, 22, 24, 26, 27, 28, 29, 90], run_out.UsageRate(6)),
            (50, [2, 20, 22, 24, 26, 27, 28, 29, 90], run_out.UsageRate(8.4)),
            (55, [2, 20, 24, 26, 28, 30, 32, 34, 90], run_out.UsageRate(144 / 11)),
        )
        with patch.dict(os.environ, {"CONF_DIR": str(folder)}):
            for minutes, usage, expected in cases:
                with self.subTest(minutes=minutes, usage=usage):
                    _ = config.write_text(f"# whole: 1..\nrate.lookback_minutes={minutes}\n")
                    now = START + timedelta(minutes=70)
                    self.assertEqual(run_out.hourly_usage_rate(climb(usage), now), expected)

    def test_hourly_usage_rate_requires_a_rise_inside_the_window(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = folder / "accounts.conf"
        _ = config.write_text("# whole: 1..\nrate.lookback_minutes=55\n")
        settings = conf.read(config)
        now = START + timedelta(minutes=70)
        for readings in ([], climb([40], start=now), climb([20, 40]), climb([40] * 8),
                         climb([40, 20], start=now - timedelta(minutes=10))):
            with self.subTest(readings=readings):
                self.assertEqual(run_out.hourly_usage_rate(readings, now, account_config=settings), run_out.NoUsageRate())

    def test_an_account_waiting_its_turn_is_not_quiet_use(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = folder / "accounts.conf"
        _ = config.write_text("# whole: 1..\nrate.lookback_minutes=65\n")
        readings = climb([60, 0, 0, 0, 2, 4, 4, 6])
        self.assertEqual(run_out.hourly_usage_rate(readings, readings[-1].at, account_config=conf.read(config)),
                         run_out.UsageRate(9))

    def test_refill_starts_a_new_stretch_and_the_one_before_still_counts(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = folder / "accounts.conf"
        _ = config.write_text("# whole: 1..\nrate.lookback_minutes=70\n")
        readings = climb([50, 52, 54, 56, 0, 1, 2, 3])
        self.assertEqual(run_out.hourly_usage_rate(readings, readings[-1].at, account_config=conf.read(config)),
                         run_out.UsageRate(9))

    def test_time_at_full_is_skipped(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = folder / "accounts.conf"
        _ = config.write_text("# whole: 1..\nrate.lookback_minutes=70\n")
        readings = climb([96, 98, 100, 100, 100, 0, 2, 4])
        self.assertEqual(run_out.hourly_usage_rate(readings, readings[-1].at, account_config=conf.read(config)),
                         run_out.UsageRate(12))

    def test_time_logged_out_is_not_quiet_time(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = folder / "accounts.conf"
        _ = config.write_text("# whole: 1..\nrate.lookback_minutes=65\n")
        settings = conf.read(config)
        end = START + timedelta(minutes=65)
        before_gap = climb([5, 14, 23], start=START - timedelta(hours=1))
        after_gap = [Reading(end - timedelta(minutes=age), 80 - age) for age in (49, 35, 20, 5, 0)]
        cases = (
            ("exact gap limit", [Reading(end - timedelta(minutes=age), 80 - age)
                                 for age in (65, 50, 35, 20, 5, 0)], run_out.UsageRate(60)),
            ("gap across the window start", before_gap + after_gap, run_out.UsageRate(60)),
            ("only a long gap", [Reading(START, 37), Reading(end, 40)], run_out.NoUsageRate()),
        )
        for case, readings, expected in cases:
            with self.subTest(case=case):
                self.assertEqual(run_out.hourly_usage_rate(readings, end, account_config=settings), expected)

    def test_hourly_usage_rate_uses_only_the_observed_part_of_the_window(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = folder / "accounts.conf"
        _ = config.write_text("# whole: 1..\nrate.lookback_minutes=55\n")
        settings = conf.read(config)
        now = START + timedelta(minutes=70)
        readings = climb([30, 40, 40, 40, 40, 40, 40])
        self.assertEqual(run_out.hourly_usage_rate(readings, now, account_config=settings), run_out.NoUsageRate())
        readings = climb([28, 30, 32, 34, 36, 38, 40])
        for age in (timedelta(microseconds=1), run_out.MAX_GAP):
            with self.subTest(age=age):
                rate = run_out.hourly_usage_rate(readings, readings[-1].at + age, account_config=settings)
                assert isinstance(rate, run_out.UsageRate)
                self.assertAlmostEqual(rate.points_per_hour, 12)
        short = [Reading(now - timedelta(minutes=5), 39), Reading(now, 40)]
        self.assertEqual(run_out.hourly_usage_rate(short, now, account_config=settings), run_out.NoUsageRate())
        stale_at = readings[-1].at + run_out.MAX_GAP + timedelta(microseconds=1)
        self.assertEqual(run_out.hourly_usage_rate(readings, stale_at, account_config=settings), run_out.NoUsageRate())


    def test_page_rate_uses_the_whole_window_at_the_lookback_boundary(self) -> None:
        lookback = timedelta(minutes=40)
        readings = climb([10, 14, 18, 22, 26])
        now = START + lookback
        move = run_out.RoleArrival("units", NumberedAccountLabel("claude", 1),
                                   NumberedAccountLabel("claude", 2), START)
        rate = run_out.page_usage_rate("claude2", readings[-1], now, lookback=lookback,
                                      histories={"claude2": readings}, current_reset=NoResetKnown(),
                                      source_resets={}, arrivals=(move,))
        assert isinstance(rate, run_out.WholeWindowRate)
        self.assertEqual(rate.points_per_hour, 24)
        self.assertEqual((rate.usage.first_reading_at, rate.usage.last_reading_at), (START, now))

    def test_page_rate_sums_only_roles_with_a_same_tool_rate(self) -> None:
        lookback = timedelta(minutes=40)
        now = START + timedelta(minutes=4)
        destination = NumberedAccountLabel("claude", 2)
        moves = (run_out.RoleArrival("units", NumberedAccountLabel("claude", 1), destination, START),
                 run_out.RoleArrival("showrunners", NumberedAccountLabel("claude", 3), destination, START),
                 run_out.RoleArrival("producers", NumberedAccountLabel("claude", 4), destination, START),
                 run_out.RoleArrival("workers", NumberedAccountLabel("codex", 1), destination, START))
        histories = {"claude1": climb([10, 12, 14, 16, 18], start=START - lookback),
                     "claude3": climb([10, 11, 12, 13, 14], start=START - lookback),
                     "claude4": climb([10, 12], start=START - lookback),
                     "codex1": climb([10, 20, 30, 40, 50], start=START - lookback)}
        rate = run_out.page_usage_rate("claude2", Reading(now, 0), now, lookback=lookback,
                                      histories=histories, current_reset=NoResetKnown(), source_resets={}, arrivals=moves)
        assert isinstance(rate, run_out.LastKnownRate)
        self.assertEqual(rate.points_per_hour, 18)
        self.assertEqual([source.role for source in rate.sources], ["units", "showrunners"])

    def test_page_rate_mixes_the_first_pair_in_proportion_to_its_coverage(self) -> None:
        lookback = timedelta(minutes=40)
        source = NumberedAccountLabel("claude", 1)
        move = run_out.RoleArrival("units", source, NumberedAccountLabel("claude", 2), START)
        source_history = climb([10, 12, 14, 16, 18], start=START - lookback)
        before = run_out.page_usage_rate("claude2", Reading(START, 0), START, lookback=lookback,
                                        histories={"claude1": source_history}, current_reset=NoResetKnown(),
                                        source_resets={}, arrivals=(move,))
        assert isinstance(before, run_out.LastKnownRate)
        self.assertEqual(before.points_per_hour, 12)

        for elapsed in (timedelta(seconds=1), timedelta(minutes=10), lookback - timedelta(seconds=1)):
            with self.subTest(elapsed=elapsed):
                now = START + elapsed
                destination_history = [reading for reading in climb([0, 4, 8, 12], start=START)
                                       if reading.at < now]
                current = Reading(now, 24 * elapsed.total_seconds() / 3600)
                rate = run_out.page_usage_rate("claude2", current, now, lookback=lookback,
                                              histories={"claude1": source_history, "claude2": destination_history},
                                              current_reset=NoResetKnown(), source_resets={}, arrivals=(move,))
                assert isinstance(rate, run_out.PartialWindowRate)
                self.assertEqual(rate.usage.duration, elapsed)
                self.assertAlmostEqual(rate.points_per_hour, 12 + 12 * (elapsed / lookback))

    def test_page_partial_rate_skips_invalid_spans_and_uses_the_latest_boundary(self) -> None:
        lookback = timedelta(minutes=53)
        now = START + timedelta(minutes=44)
        reset = START + timedelta(days=7)
        move = run_out.RoleArrival("units", run_out.UnknownMoveSource(), NumberedAccountLabel("claude", 2),
                                   START - timedelta(minutes=3))
        target_history = [Reading(START + timedelta(minutes=minute), used) for minute, used in
                          ((-2, 96), (0, 98), (5, 100), (10, 100), (15, 10), (39, 12))]
        rate = run_out.page_usage_rate("claude2", Reading(now, 14), now, lookback=lookback,
                                      histories={"claude2": target_history}, current_reset=reset, source_resets={},
                                      arrivals=(move,))
        assert isinstance(rate, run_out.PartialWindowRate)
        self.assertIsInstance(rate.prior, run_out.NoUsageRate)
        self.assertEqual((rate.since, rate.usage.duration), (START, timedelta(minutes=10)))
        self.assertEqual(rate.points_per_hour, 24)


if __name__ == "__main__":
    _ = unittest.main()
