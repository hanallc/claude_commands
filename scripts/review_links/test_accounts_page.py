"""The console Accounts page draws the accounts board table."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import unittest
from typing import cast, override
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.review_links import accounts_page, console_theme, page_frame
from scripts.whoami import agent_accounts

AT = datetime(2026, 11, 15, 15, tzinfo=timezone.utc)


def status_row(label: str) -> accounts.StatusRow:
    return accounts.StatusRow(
        label=label, tool="claude", folder=f"~/.{label}", login="fixture@example.invalid",
        roles=("units",), role_counts=(accounts.RoleSessionCounts(1, 0, 0, 0),), weekly_left_percent=60,
        resets_at=AT + timedelta(days=1), credits=0, on_credits="no", use_credits=False, read_at=AT,
        read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=AT)


class AccountsPageTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(Path(__file__).resolve().parents[2] / "config")})))

    def test_board_draws_the_board_table_with_moves_and_problems(self) -> None:
        reading = accounts.StatusReading((status_row("claude1"),), ("* <stale>",), (), timezone.utc, AT,
                                         upcoming_moves=("Upcoming moves:", "units → claude1"))
        markup = accounts_page.board(reading)
        table = accounts.status_board(reading)
        heads = "".join(f'<th class="{column["align"]}">{column["heading"]}</th>' for column in table["columns"])
        self.assertIn(f"<thead><tr>{heads}</tr></thead>", markup)
        self.assertEqual(markup.count('<tr class="account">'), 2)
        self.assertEqual(markup.count("<tr>"), 1)
        self.assertIn('<td class="left"><span class="default bold">claude1</span></td>', markup)
        self.assertIn('<td class="left"><span class="default bold">total</span></td>', markup)
        self.assertIn('<td class="right"><span class="default">60%</span></td>', markup)
        self.assertIn('<li>units → claude1</li>', markup)
        self.assertIn('<h2>Problems</h2><ul class="problems"><li>&lt;stale&gt;</li></ul>', markup)

    def test_page_reads_stored_status_and_holds_the_refresh_root(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, AT)
        with patch.object(accounts, "read_status", return_value=reading) as status:
            page = accounts_page.page().decode()
        source: object = status.call_args.kwargs["source"]  # pyright: ignore[reportAny]
        self.assertIsInstance(source, accounts.StoredUsageOnly)
        self.assertIn("<div data-accounts>", page)
        self.assertIn(console_theme.CSS, page)
        self.assertIn(page_frame.STYLE, page)
        self.assertIn('<h1 class="page-title">Accounts</h1>', page)
        self.assertIn("'console-shown'", page)

    def test_render_runs_the_page_in_a_child_and_names_its_failure(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 0, b"<p>page</p>", b"")) as run:
            self.assertEqual(accounts_page.render(), b"<p>page</p>")
        self.assertEqual(run.call_args.args[0], accounts_page.CHILD)
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess((), 1, b"", b"Traceback\nOSError: no staff\n")),
              self.assertRaisesRegex(OSError, "exited 1: OSError: no staff$")):
            _ = accounts_page.render()
        with (patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired((), 30)),
              self.assertRaisesRegex(OSError, "took longer than 30 s")):
            _ = accounts_page.render()


class ConsoleThemeTests(unittest.TestCase):
    def test_a_page_takes_the_theme_or_only_its_tokens_at_one_placeholder(self) -> None:
        self.assertEqual(console_theme.with_theme(f"<style>{console_theme.PLACEHOLDER}p{{}}</style>"),
                         f"<style>{console_theme.CSS}p{{}}</style>")
        self.assertEqual(console_theme.with_theme(f"<style>{console_theme.TOKENS_PLACEHOLDER}p{{}}</style>"),
                         f"<style>{console_theme.TOKENS}p{{}}</style>")
        self.assertTrue(console_theme.CSS.startswith(console_theme.TOKENS))
        # page_frame owns the main column and the page title.
        self.assertNotRegex(console_theme.CSS, r"(^|[}\n])(main|h1)\{")
        for source in ("<style></style>", console_theme.PLACEHOLDER + console_theme.TOKENS_PLACEHOLDER):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "needs one"):
                _ = console_theme.with_theme(source)


if __name__ == "__main__":
    _ = unittest.main()
