"""Chart calculations over isolated history."""

from __future__ import annotations

import os
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo


from scripts.lib import conf
from scripts.buildlog import charts, cost_actions, costs, index
from scripts.buildlog import test_moments as fixtures
from scripts.buildlog.test_index import call
from scripts.lint.memory_test_support import write_config_fixture


class ChartFixture(unittest.TestCase):
    fixture: fixtures.MomentTests  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.fixture = fixtures.moment_fixture(self)
        self.fixture.fixture.root.mkdir(parents=True, exist_ok=True)


class PageDataTests(ChartFixture):
    ZONE: ZoneInfo = ZoneInfo("America/New_York")

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"USER_ZONE": str(self.ZONE)})))
        clock = cast(mock.Mock, self.enterContext(mock.patch.object(charts, "datetime", wraps=datetime)))
        cast(mock.Mock, clock.now).return_value = fixtures.END.astimezone(self.ZONE)
        self.configure("TOP_COST_RECENT_DAYS", "2")

    @property
    def config_file(self) -> Path:
        return Path(os.environ["CONF_DIR"]) / "buildlog.conf"

    def configure(self, key: str, value: str) -> None:
        _ = write_config_fixture(self.config_file, key + "=" + value)

    def rows(self, n: int = 3) -> tuple[charts.TopCost, ...]:
        result = charts.page_data(7, n)
        if not isinstance(result, charts.PageData) or isinstance(result.top.rows, charts.NoDataYet):
            self.fail(f"Expected ranked costs, received {result!r}")
        return result.top.rows

    def test_top_costs_ranks_and_limits_the_recent_window(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("old", -86400, target="old", mem_wait_s=900000),
            fixtures.build("wait", 10, mem_wait_s=7200),
            fixtures.build("failure", 20, target="cloud", status=1, duration_s=3600),
            fixtures.build("cold", 30, target="mac", crates_compiled=50, duration_s=1800),
        )):
            rows = self.rows(2)
        self.assertEqual([(row.machine, row.cause, row.recent) for row in rows], [
            ("natedev", "Memory wait", charts.MeasuredCost(7200)),
            ("cloud", "Failed runs", charts.MeasuredCost(3600)),
        ])

    def test_top_costs_preserves_missing_waits_and_the_mac_upper_bound(self) -> None:
        with closing(self.fixture.connection(
            call("eligible", mac="declined", mac_reason="busy", mac_s=0.0),
            fixtures.build("wait", 7200, mem_wait_s=3600, call_id="eligible"),
            fixtures.build("missing", 8000, target="cloud", mem_wait_s=None),
        )):
            rows = self.rows(10)
        values = {(row.machine, row.cause): row for row in rows}
        self.assertEqual(values["cloud", "Memory wait"].recent, charts.CostUnavailable("1 applicable waits missing"))
        self.assertEqual(values["mac", "Mac idle while natedev waits"].recent, charts.MacIdleUpperBound(0, 3600))
        self.assertEqual(values["cloud", "Memory wait"].window, charts.WindowMatchesRecent())
        self.assertEqual(values["mac", "Mac idle while natedev waits"].window, charts.WindowMatchesRecent())

    def test_window_comparison_uses_daily_rates_and_a_strict_half_threshold(self) -> None:
        with closing(self.fixture.connection(*[
            fixtures.build(machine + "-recent", 10, target=machine, mem_wait_s=7200)
            for machine in ("below", "equal", "above", "lower")
        ], *[
            fixtures.build(machine + "-old", -86400, target=machine, mem_wait_s=seconds)
            for machine, seconds in (("below", 30599), ("equal", 30600), ("above", 30601))
        ])):
            values = {row.machine: row.window for row in self.rows(10)}
        self.assertEqual(values["below"], charts.WindowMatchesRecent())
        self.assertEqual(values["equal"], charts.WindowMatchesRecent())
        self.assertEqual(values["above"], charts.WindowDiffers(charts.MeasuredCost(37801 / 7)))
        self.assertEqual(values["lower"], charts.WindowDiffers(charts.MeasuredCost(7200 / 7)))

    def test_page_window_preserves_older_missing_waits(self) -> None:
        with closing(self.fixture.connection(
            fixtures.build("missing", -86400, mem_wait_s=None),
            fixtures.build("recent", 10, mem_wait_s=3600),
        )):
            row = self.rows(1)[0]
        self.assertEqual(row.recent, charts.MeasuredCost(3600))
        self.assertEqual(row.window, charts.WindowDiffers(charts.CostUnavailable("1 applicable waits missing")))

    def test_window_comparison_preserves_unavailable_states(self) -> None:
        unknown = (charts.NoDataYet.VALUE, charts.CostUnavailable("missing coverage"))
        for recent in (charts.MeasuredCost(3600), charts.MacIdleUpperBound(3600, 7200)):
            for window in unknown:
                with self.subTest(recent=recent, window=window):
                    self.assertEqual(charts.window_value(recent, window, 2, 7), charts.WindowDiffers(window))
        for recent in unknown:
            with self.subTest(recent=recent):
                self.assertEqual(charts.window_value(recent, charts.MeasuredCost(3600), 2, 7), charts.WindowMatchesRecent())

    def test_empty_window_keeps_no_data_and_the_user_zone(self) -> None:
        with closing(self.fixture.connection(fixtures.build("old", -8 * 86400, mem_wait_s=9000))):
            data = charts.page_data(7)
        if not isinstance(data, charts.PageData):
            self.fail(f"Expected an empty window, received {data!r}")
        result = data.top
        self.assertEqual(result.rows, charts.NoDataYet.VALUE)
        self.assertEqual(result.start, fixtures.START.astimezone(self.ZONE))
        self.assertEqual(result.end, fixtures.END.astimezone(self.ZONE))
        self.assertEqual((result.start.tzinfo, result.end.tzinfo), (self.ZONE, self.ZONE))
        self.assertEqual(result.recent_days, 2)

    def test_memory_wait_names_the_largest_memory_holder_and_each_day(self) -> None:
        gib = 1 << 30
        with closing(self.fixture.connection(
            fixtures.build("hana-1", 10, repo="hana", mem_wait_s=5400, peak_mem_bytes=8 * gib, duration_s=3600),
            # A verify gate copy has its own folder name and the origin of the repository it copies.
            fixtures.build("hana-2", 90000, repo="checkout", repo_identity="https://github.com/natepiano/hana",
                           mem_wait_s=1800, peak_mem_bytes=10 * gib, duration_s=3600),
            fixtures.build("port", 20, repo="port", step="clippy", mem_wait_s=1800, peak_mem_bytes=30 * gib, duration_s=600),
        )):
            row = self.rows(1)[0]
        self.assertEqual((row.machine, row.cause, row.recent), ("natedev", "Memory wait", charts.MeasuredCost(9000)))
        self.assertEqual(row.days, (charts.MeasuredCost(7200), charts.MeasuredCost(1800)))
        self.assertEqual(row.share, cost_actions.LargestShare(costs.StepKind("hana", "nextest"), 0.8))
        self.assertEqual(row.action, cost_actions.LowerMemoryPeak(costs.StepKind("hana", "nextest"), 9.0, 10 * gib))

    def test_failed_runs_name_the_test_that_failed_most_else_the_step(self) -> None:
        failed = {"status": "failed", "duration_s": 1.0, "attempts": 1}
        with closing(self.fixture.connection(
            fixtures.build("one", 10, target="cloud", repo="hana", status=100, duration_s=3600,
                           tests=[{"binary": "hana", "test": "tests::flaky", **failed},
                                  {"binary": "hana", "test": "tests::other", **failed}]),
            fixtures.build("two", 20, target="cloud", repo="hana", status=100, duration_s=3600,
                           tests=[{"binary": "hana", "test": "tests::flaky", **failed}]),
            fixtures.build("lint", 30, target="mac", repo="port", step="clippy", status=1, duration_s=1800),
        )):
            values = {(row.machine, row.cause): row.action for row in self.rows(10)}
        self.assertEqual(values["cloud", "Failed runs"], cost_actions.FixTest("hana", "tests::flaky", 2))
        self.assertEqual(values["mac", "Failed runs"], cost_actions.FixStep(costs.StepKind("port", "clippy"), 1))

    def test_an_excluded_nextest_failure_names_no_test(self) -> None:
        failed = {"status": "failed", "duration_s": 1.0, "attempts": 1}
        with closing(self.fixture.connection(
            # A verify-mac nextest that exits 4 keeps its raw status without counting as a failure.
            fixtures.build("excluded", 10, target="mac", repo="hana", caller="verify-mac", status=4, duration_s=600,
                           tests=[{"binary": "hana", "test": "tests::excluded", **failed}]),
            fixtures.build("lint", 30, target="mac", repo="port", step="clippy", status=1, duration_s=1800),
        )):
            values = {(row.machine, row.cause): row.action for row in self.rows(10)}
        self.assertEqual(values["mac", "Failed runs"], cost_actions.FixStep(costs.StepKind("port", "clippy"), 1))

    def test_a_range_needs_every_day_measured(self) -> None:
        measured = charts.MeasuredCost(3600)
        self.assertEqual(charts.daily_range((measured, charts.MeasuredCost(7200))), charts.DailyRange(1, 2))
        for unmeasured in (charts.CostUnavailable("1 applicable waits missing"), charts.MacIdleUpperBound(0, 3600),
                           charts.NoDataYet.VALUE):
            with self.subTest(unmeasured=unmeasured):
                self.assertEqual(charts.daily_range((measured, unmeasured)), charts.RangeUnavailable.VALUE)
        self.assertEqual(charts.daily_range(()), charts.RangeUnavailable.VALUE)

    def test_a_missing_wait_day_has_no_range_or_share(self) -> None:
        with closing(self.fixture.connection(fixtures.build("unrecorded", 10, mem_wait_s=None, duration_s=600))):
            row = self.rows(1)[0]
        self.assertEqual((row.cause, row.recent), ("Memory wait", charts.CostUnavailable("1 applicable waits missing")))
        self.assertEqual(charts.daily_range(row.days), charts.RangeUnavailable.VALUE)
        self.assertEqual(row.share, cost_actions.NoShare.VALUE)
        self.assertEqual(cost_actions.share_text(row.share), "")

    def test_a_cost_without_a_named_action_says_so(self) -> None:
        with closing(self.fixture.connection(fixtures.build("cold", 30, target="mac", crates_compiled=50, duration_s=1800))):
            row = self.rows(1)[0]
        self.assertEqual((row.cause, row.action), ("Cold rebuilds", cost_actions.NoActionKnown.VALUE))
        self.assertEqual(cost_actions.action_text(row.action), "No action known.")

    def test_page_data_reads_the_index_once_for_top_costs(self) -> None:
        with closing(self.fixture.connection(fixtures.build("wait", 10, mem_wait_s=3600))), \
             mock.patch.object(costs, "read_costs", wraps=costs.read_costs) as read, \
             mock.patch.object(index, "read_only", wraps=index.read_only) as opened:
            row = self.rows(1)[0]
        self.assertEqual(row.recent, charts.MeasuredCost(3600))
        self.assertEqual(row.window, charts.WindowDiffers(charts.MeasuredCost(3600 / 7)))
        read.assert_called_once_with(mock.ANY, fixtures.END - timedelta(days=7), fixtures.END, boundary_context=True)
        opened.assert_called_once_with()

    def test_window_lengths_come_from_config(self) -> None:
        self.configure("TOP_COST_RECENT_DAYS", "1")
        with closing(self.fixture.connection(
            fixtures.build("recent", 90000, mem_wait_s=3600),
            fixtures.build("older", 10, mem_wait_s=7200),
            fixtures.build("outside", -3 * 86400, mem_wait_s=90000),
        )):
            result = charts.page_data(4, 1)
        if not isinstance(result, charts.PageData) or isinstance(result.top.rows, charts.NoDataYet):
            self.fail(f"Expected ranked costs, received {result!r}")
        rows = result.top.rows
        self.assertEqual(rows[0].recent, charts.MeasuredCost(3600))
        self.assertEqual(rows[0].window, charts.WindowMatchesRecent())

    def test_recent_window_cannot_exceed_the_page_days(self) -> None:
        self.configure("TOP_COST_RECENT_DAYS", "4")
        with closing(self.fixture.connection(fixtures.build("wait", 10, mem_wait_s=3600))):
            with self.assertRaisesRegex(conf.Unusable, r"TOP_COST_RECENT_DAYS.*3"):
                _ = charts.page_data(3)

    def test_page_data_returns_the_index_error(self) -> None:
        database = index.index_path()
        database.parent.mkdir(parents=True, exist_ok=True)
        for damage, content, reason in (
            ("absent", None, "unable to open database"),
            ("corrupt", b"This file is not a SQLite index.", "not a database"),
        ):
            if content is not None:
                _ = database.write_bytes(content)
            with self.subTest(damage=damage):
                result = charts.page_data(7)
                if not isinstance(result, charts.IndexUnavailable):
                    self.fail(f"Expected the index error, received {result!r}")
                self.assertIn(reason, result.reason)
