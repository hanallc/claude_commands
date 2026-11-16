"""The accounts board table: the columns, lines, tones and narrow facts that /showrunner:accounts shows."""
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
    """The unit-board plugin's readable fixture: deliberately unsorted and mixed case."""
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

    def test_no_accounts_is_an_empty_table_and_a_stale_read_is_red(self) -> None:
        self.assertEqual(accounts.status_board(accounts.StatusReading((), (), (), ZONE, NOW))["rows"], [])
        stale = replace(fixture().rows[0], read_at=NOW - timedelta(hours=1))
        board = accounts.status_board(accounts.StatusReading((stale,), (), (), ZONE, NOW))
        self.assertEqual(board["rows"][0]["cells"]["dot"][0]["tone"], "error")


if __name__ == "__main__":
    _ = unittest.main()
