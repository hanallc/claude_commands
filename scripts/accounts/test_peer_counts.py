"""Show the age of cached Mac counts on the Accounts page."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone, tzinfo
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import Self, cast, override
from unittest.mock import patch

from scripts.accounts import accounts, role_map
from scripts.accounts.home_test_support import scratch_account_home
from scripts.agents import remote
from scripts.lint.memory_test_support import write_config_fixture
from scripts.review_links import accounts_page


fetch_session_counts = cast(Callable[[], int], getattr(accounts, "_fetch_session_counts"))


class PeerCountsClock(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls(2026, 11, 21, 5, 4, 2, tzinfo=timezone.utc).astimezone(tz)


class PeerCountsTests(unittest.TestCase):
    cache: Path = Path()

    @override
    def setUp(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(scratch_account_home(home, {
            "AGENT_NOTES_DIR": str(home / "notes"), "USER_ZONE": "America/New_York",
        }))
        _ = self.enterContext(patch.object(accounts, "datetime", PeerCountsClock))
        _ = write_config_fixture(accounts.read_account_settings().source.parent / "statusline.conf")
        self.cache = home / ".local/state/accounts/peer_session_counts.json"
        self.cache.parent.mkdir(parents=True)
        _ = self.enterContext(patch.object(remote, "other_machine", return_value="mac"))
        _ = self.enterContext(patch.object(role_map, "is_role_map_host", return_value=True))
        _ = self.enterContext(patch.object(role_map, "ssh_role_map", side_effect=AssertionError("unexpected SSH")))
        _ = self.enterContext(patch.object(accounts, "_read_local_role_sessions",
            return_value=accounts.LocalRoleSessions({}, {}, (), (), (), False, frozenset())))
        _ = self.enterContext(patch.object(accounts, "_start_session_counts_fetch", return_value=True))
        _ = self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected process")))

    def board(self) -> str:
        return accounts_page.board(accounts.read_status(source=accounts.StoredUsageOnly()))

    def save_counts(self, minutes: int) -> None:
        at = PeerCountsClock.now(timezone.utc) - timedelta(minutes=minutes, seconds=2)
        _ = self.cache.write_text(json.dumps({"machine": "mac", "at": at.isoformat(), "accounts": {}}))

    def test_board_shows_cached_mac_counts_age_when_refresh_is_late(self) -> None:
        self.save_counts(7)

        page = self.board()

        self.assertIn("<li>mac counts from 2026-11-20 23:57 EST, 7 min old</li>", page)
        self.assertNotIn("mac unavailable", page)

    def test_board_shows_the_age_after_a_failed_fetch_and_clears_it_after_success(self) -> None:
        self.save_counts(0)
        self.assertNotIn("mac counts from", self.board())
        saved = self.cache.read_text()

        with patch.object(role_map, "ssh_role_map", side_effect=OSError("fixture offline")):
            self.assertEqual(fetch_session_counts(), 1)

        page = self.board()
        self.assertIn("<li>mac counts from 00:04 EST, 0 min old</li>", page)
        self.assertNotIn("mac unavailable", page)
        self.assertEqual(self.cache.read_text(), saved)

        with patch.object(role_map, "ssh_role_map", return_value=saved):
            self.assertEqual(fetch_session_counts(), 0)
        self.assertNotIn("mac counts from", self.board())

    def test_board_reports_unavailable_when_counts_are_missing_or_expired(self) -> None:
        for has_copy in (False, True):
            with self.subTest(has_copy=has_copy):
                if has_copy:
                    self.save_counts(11)

                page = self.board()

                self.assertIn("<li>mac unavailable: counts are natedev only</li>", page)
                self.assertNotIn("mac counts from", page)

    def test_board_reports_one_problem_for_an_unreadable_peer_claude_registry(self) -> None:
        _ = self.cache.write_text(json.dumps({"machine": "mac",
            "at": PeerCountsClock.now(timezone.utc).isoformat(), "accounts": {}, "claude_registry_unreadable": True}))

        page = self.board()

        self.assertEqual(page.count("<li>mac Claude sessions unreadable: Claude counts are natedev only</li>"), 1)
        self.assertNotIn("mac unavailable", page)

    def test_peer_response_shows_starting_worker_and_pane_in_the_page_and_json(self) -> None:
        worker = accounts.StartingWorkerDocument(session="peer-thread", name="peer-worker", state="starting")
        pane = accounts.StartingPaneDocument(session="agents", pane="w1:p1", state="starting")
        observed = accounts.LocalRoleSessions({}, {}, (), (), (), False, frozenset(),
                                             starting_workers=(worker,), starting_panes=(pane,))
        with (patch.object(accounts, "_local_session_counts", return_value=observed),
              patch.object(accounts, "_session_counts_machine", return_value="mac"),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(accounts.main(["session-counts-read"]), 0)
        published = cast(accounts.MachineSessionCounts, json.loads(output.getvalue()))
        self.assertEqual(published.get("starting_workers"), [worker])
        self.assertEqual(published.get("starting_panes"), [pane])

        with patch.object(role_map, "ssh_role_map", return_value=output.getvalue()) as fetch:
            self.assertEqual(fetch_session_counts(), 0)
        fetch.assert_called_once_with(("session-counts-read",), machine="mac")
        reading = accounts.read_status(source=accounts.StoredUsageOnly(), include_models=True)
        page = accounts_page.board(reading)
        document = cast(accounts.StatusDocument, json.loads(accounts.render_status_json(reading)))
        self.assertIn(">workers</span><br>peer-worker (mac): starting</td>", page)
        self.assertIn("<li>pane agents:w1:p1 (mac): starting</li>", page)
        self.assertEqual(document.get("starting_workers"), [{**worker, "machine": "mac"}])
        self.assertEqual(document.get("starting_panes"), [{**pane, "machine": "mac"}])
        self.assertTrue(all("starting" not in problem for problem in document["problems"]))

        published["at"] = (PeerCountsClock.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        _ = self.cache.write_text(json.dumps(published))
        self.assertNotIn("peer-worker", self.board())
