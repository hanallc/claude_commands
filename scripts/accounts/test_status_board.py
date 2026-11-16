"""Test the accounts board columns, lines, tones, and facts for the Hana console Accounts page."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Literal, cast, override
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.accounts import accounts
from scripts.whoami import agent_accounts

NOW = datetime(2026, 10, 16, 18, tzinfo=timezone.utc)
ZONE = ZoneInfo("America/New_York")


def row(label: str, roles: tuple[str, ...], counts: tuple[accounts.RoleSessionCounts | accounts.NoRoleCount, ...],
        weekly: float | None, resets: str | None, credits: float | Literal["unlimited"] | None, *,
        selected: tuple[str, ...] = (), use_credits: bool = False, read: bool = True) -> accounts.StatusRow:
    unavailable = accounts.StatusUnavailable("unavailable")
    return accounts.StatusRow(
        label=label, tool="claude", folder=f"~/.{label}", login="fixture@example.invalid", roles=roles,
        weekly_left_percent=unavailable if weekly is None else weekly,
        resets_at=unavailable if resets is None else datetime.fromisoformat(resets),
        credits=unavailable if credits is None else credits, on_credits="no", use_credits=use_credits,
        read_at=NOW - timedelta(minutes=1) if read else unavailable, read_outcome=agent_accounts.UsageReadOutcome.OK,
        attempted_at=NOW, role_counts=counts, selected_roles=selected)


def counts(busy: int, idle: int, paused: int, exited: int) -> accounts.RoleSessionCounts:
    return accounts.RoleSessionCounts(busy, idle, paused, exited)


def fixture() -> accounts.StatusReading:
    """A readable status fixture for the board: deliberately unsorted and mixed case."""
    rows = (
        row("Gamma", ("workers",), (counts(1, 2, 0, 1),), 74, "2026-10-24T18:00:00+00:00", None),
        row("beta", ("workers",), (counts(7, 5, 0, 0),), 20.2, "2026-10-17T18:00:00+00:00", 13446.2, read=False),
        row("Echo", ("showrunners", "unit directors"), (counts(0, 0, 0, 0), counts(0, 0, 0, 0)), None, None, None, read=False),
        row("aLpha", ("showrunners", "unit directors"), (counts(1, 0, 0, 1), counts(2, 1, 0, 0)), 60.4,
            "2026-10-16T18:00:00+00:00", "unlimited", selected=("showrunners",), use_credits=True),
        row("Foxtrot", ("spare",), (accounts.NoRoleCount(),), 90, "2026-10-22T18:00:00+00:00", None, use_credits=True),
        row("Charlie", ("workers",), (counts(0, 1, 0, 0),), 8.4, "2026-10-18T18:00:00+00:00", None),
        row("Delta", ("spare",), (accounts.NoRoleCount(),), 25, "2026-10-23T18:00:00+00:00", None),
    )
    return accounts.StatusReading(rows, (), (), ZONE, NOW)


def text(spans: list[accounts.BoardSpan]) -> str:
    return "".join(span["text"] for span in spans)


class StatusBoardTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(Path(__file__).resolve().parents[2] / "config")})))

    def test_the_table_matches_the_plugin_columns_lines_and_totals(self) -> None:
        board = accounts.status_board(fixture())
        keys = [column["key"] for column in board["columns"]]
        self.assertEqual([column["heading"] for column in board["columns"]],
                         ["", "account", "left", "role", "busy", "idle", "paused", "exited", "total", "resets", "credits", "enabled"])
        lines = [[text(line["cells"][key]) for key in keys] for line in board["rows"]]
        self.assertEqual(lines, [
            ["●", "aLpha", "60%", "showrunners ✓", "1", "0", "0", "1", "2", "14:00", "unlimited", "yes"],
            ["", "", "", "unit directors", "2", "1", "0", "0", "3", "", "", ""],
            ["●", "beta", "20%", "workers", "7", "5", "0", "0", "12", "Sat 14:00", "13,446", ""],
            ["●", "Charlie", "8%", "workers", "0", "1", "0", "0", "1", "Sun 14:00", "", ""],
            ["●", "Delta", "25%", "spare", "", "", "", "", "", "23 Oct", "", ""],
            ["●", "Echo", "—", "showrunners", "0", "0", "0", "0", "0", "", "", ""],
            ["", "", "", "unit directors", "0", "0", "0", "0", "0", "", "", ""],
            ["●", "Foxtrot", "90%", "spare", "", "", "", "", "", "Thu 14:00", "", "yes"],
            ["●", "Gamma", "74%", "workers", "1", "2", "0", "1", "4", "24 Oct", "", ""],
            ["", "total", "", "", "11", "9", "0", "2", "22", "", "", ""],
        ])
        self.assertEqual([column["align"] for column in board["columns"]],
                         ["left", "left", "right", "left", *["right"] * 5, "right", "right", "left"])

    def test_tones_mark_access_and_low_quota(self) -> None:
        rows = {text(line["cells"]["label"]): line for line in accounts.status_board(fixture())["rows"]}
        self.assertEqual({label: line["cells"]["dot"][0]["tone"] for label, line in rows.items() if line["cells"]["dot"]},
                         {"aLpha": "success", "beta": "error", "Charlie": "success", "Delta": "success", "Echo": "error",
                          "Foxtrot": "success", "Gamma": "success"})
        self.assertEqual({label: line["cells"]["left"][0]["tone"] for label, line in rows.items() if line["cells"]["left"]},
                         {"aLpha": "default", "beta": "warning", "Charlie": "error", "Delta": "default", "Echo": "default",
                          "Foxtrot": "default", "Gamma": "default"})
        self.assertEqual(rows["total"]["cells"]["label"], [accounts.BoardSpan(text="total", tone="default", kind="bold")])

    def test_facts_carry_each_value_for_a_narrow_pane(self) -> None:
        board = accounts.status_board(fixture())
        facts = [" · ".join(text(fact) for fact in line["facts"]) for line in board["rows"][:2]]
        self.assertEqual(facts, [
            "60% left · role showrunners ✓ · busy 1 · idle 0 · paused 0 · exited 1 · total 2 · resets 14:00 · credits unlimited · enabled",
            "role unit directors · busy 2 · idle 1 · paused 0 · exited 0 · total 3"])

    def test_quota_boundaries_color_the_left_cell_and_keep_the_dot(self) -> None:
        rows = tuple(row(f"Level {value:02}", ("workers",), (counts(0, 0, 0, 0),), value, None, None) for value in (9, 10, 24, 25))
        board = accounts.status_board(accounts.StatusReading(rows, (), (), ZONE, NOW))
        cells = [line["cells"] for line in board["rows"][:-1]]
        self.assertEqual([(text(cell["left"]), cell["left"][0]["tone"]) for cell in cells],
                         [("9%", "error"), ("10%", "warning"), ("24%", "warning"), ("25%", "default")])
        self.assertEqual({cell["dot"][0]["tone"] for cell in cells}, {"success"})

    def test_reset_text_is_the_clock_the_weekday_or_the_date_in_the_zone(self) -> None:
        for at, expected in (("2026-10-17T02:00:00+00:00", "22:00"),  # The same New York day, the next UTC day.
                             ("2026-10-10T18:00:00+00:00", "Sat 14:00"), ("2026-10-22T18:00:00+00:00", "Thu 14:00"),
                             ("2026-10-09T18:00:00+00:00", "9 Oct"), ("2026-10-24T18:00:00+00:00", "24 Oct")):
            with self.subTest(at=at):
                board = accounts.status_board(accounts.StatusReading(
                    (row("Low", ("workers",), (counts(0, 0, 0, 0),), 50, at, None),), (), (), ZONE, NOW))
                self.assertEqual(text(board["rows"][0]["cells"]["resets"]), expected)

    def test_zero_is_a_value_and_spare_has_no_counts(self) -> None:
        reading = accounts.StatusReading((row("Zero", ("spare",), (accounts.NoRoleCount(),), 0, None, 0),), (), (), ZONE, NOW)
        cells = accounts.status_board(reading)["rows"][0]["cells"]
        self.assertEqual([text(cells[key]) for key in ("left", "roles", "busy", "total", "credits")], ["0%", "spare", "", "", "0"])

    def test_each_worker_role_keeps_its_own_counts_and_the_total_adds_them(self) -> None:
        reading = accounts.StatusReading((row("Watch", ("workers", "reviewers"), (counts(2, 3, 0, 0), counts(1, 4, 0, 0)),
                                              50, None, None),), (), (), ZONE, NOW)
        board = accounts.status_board(reading)
        keys = ("roles", "busy", "idle", "paused", "exited", "total")
        self.assertEqual([[text(line["cells"][key]) for key in keys] for line in board["rows"]],
                         [["workers", "2", "3", "0", "0", "5"], ["reviewers", "1", "4", "0", "0", "5"], ["", "3", "7", "0", "0", "10"]])

    def test_the_board_is_stale_at_the_next_local_midnight(self) -> None:
        self.assertEqual(accounts.status_board(fixture())["stale_at"], "2026-10-17T04:00:00+00:00")
        late = accounts.StatusReading((), (), (), ZONE, datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc))
        # 23:30 EDT on 31 Oct; the clocks go back at 02:00 on 1 Nov, so midnight is still EDT.
        self.assertEqual(accounts.status_board(late)["stale_at"], "2026-11-01T04:00:00+00:00")

    def test_no_accounts_is_an_empty_table_and_a_stale_read_is_red(self) -> None:
        self.assertEqual(accounts.status_board(accounts.StatusReading((), (), (), ZONE, NOW))["rows"], [])
        stale = replace(fixture().rows[0], read_at=NOW - timedelta(hours=1))
        board = accounts.status_board(accounts.StatusReading((stale,), (), (), ZONE, NOW))
        self.assertEqual(board["rows"][0]["cells"]["dot"][0]["tone"], "error")


if __name__ == "__main__":
    _ = unittest.main()
