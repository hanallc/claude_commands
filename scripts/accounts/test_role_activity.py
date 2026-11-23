"""The role activity history keeps 24 hours of busy counts per role."""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.accounts import accounts, role_activity
from scripts.whoami import agent_accounts

NOW = datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)


def _row(label: str, role: str, count: accounts.RoleSessionCounts | accounts.UnknownRoleCounts) -> accounts.StatusRow:
    unavailable = accounts.StatusUnavailable("unavailable")
    return accounts.StatusRow(
        label=label, tool="codex" if label.startswith("codex") else "claude", folder=f"~/.{label}",
        login="fixture@example.invalid", roles=(role,), weekly_left_percent=unavailable, resets_at=unavailable,
        credits=unavailable, on_credits="no", use_credits=False, read_at=unavailable,
        read_outcome=agent_accounts.UsageReadOutcome.OK, attempted_at=NOW, role_counts=(count,))


class RoleActivityTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(home)}))

    def test_a_sample_reads_back_and_samples_older_than_a_day_drop_on_the_next_write(self) -> None:
        role_activity.record({"units": 3}, NOW - timedelta(hours=25))
        role_activity.record({"units": 5, "workers": 1}, NOW - timedelta(hours=2))
        role_activity.record({"units": 7}, NOW)
        samples = role_activity.read(NOW)
        self.assertEqual([(sample.at, dict(sample.busy)) for sample in samples],
                         [(NOW - timedelta(hours=2), {"units": 5, "workers": 1}), (NOW, {"units": 7})])
        self.assertEqual(len(role_activity.history_path().read_text().splitlines()), 2)

    def test_read_leaves_out_samples_that_became_older_than_a_day(self) -> None:
        role_activity.record({"units": 2}, NOW - timedelta(hours=23))
        self.assertEqual(len(role_activity.read(NOW)), 1)
        self.assertEqual(role_activity.read(NOW + timedelta(hours=2)), [])

    def test_damaged_lines_are_skipped_and_a_missing_file_reads_as_none(self) -> None:
        self.assertEqual(role_activity.read(NOW), [])
        path = role_activity.history_path()
        path.parent.mkdir(parents=True)
        _ = path.write_text('not json\n{"at": "2026-11-22T23:00:00", "busy": {"units": 1}}\n'
                            + '{"at": "2026-11-22T23:10:00+00:00", "busy": {"units": -1}}\n'
                            + '{"at": "2026-11-22T23:20:00+00:00", "busy": {"units": 4}}\n')
        self.assertEqual([dict(sample.busy) for sample in role_activity.read(NOW)], [{"units": 4}])

    def test_busy_counts_add_over_accounts_and_a_role_with_an_unknown_count_is_left_out(self) -> None:
        rows = [(("units", "showrunners"), (accounts.RoleSessionCounts(3, 1, 0, 0), accounts.RoleSessionCounts(2, 0, 0, 0))),
                (("units", "producers"), (accounts.RoleSessionCounts(4, 2, 1, 0), accounts.UnknownRoleCounts())),
                (("workers",), (accounts.NoRoleCount(),))]
        self.assertEqual(role_activity.busy_by_role(rows), {"units": 7, "showrunners": 2})

    def test_sample_now_records_the_known_roles_while_the_session_registry_is_unreadable(self) -> None:
        reading = accounts.StatusReading((), (), (), timezone.utc, NOW, claude_registry_unreadable=True)
        rows = (_row("claude1", "units", accounts.UnknownRoleCounts()),
                _row("codex1", "workers", accounts.RoleSessionCounts(2, 1, 0, 0)))
        with patch.object(accounts, "read_status", return_value=replace(reading, rows=rows)):
            role_activity.sample_now(NOW)
        self.assertEqual([dict(sample.busy) for sample in role_activity.read(NOW)], [{"workers": 2}])

    def test_a_sample_after_now_stays_when_the_clock_steps_back(self) -> None:
        role_activity.record({"units": 1}, NOW)
        role_activity.record({"units": 2}, NOW - timedelta(minutes=5))
        self.assertEqual([dict(sample.busy) for sample in role_activity.read(NOW)], [{"units": 2}, {"units": 1}])

    def test_a_damaged_byte_loses_only_its_line(self) -> None:
        role_activity.record({"units": 3}, NOW)
        path = role_activity.history_path()
        _ = path.write_bytes(b"\xff\xfe\n" + path.read_bytes())
        self.assertEqual([dict(sample.busy) for sample in role_activity.read(NOW)], [{"units": 3}])
        role_activity.record({"units": 4}, NOW)
        self.assertEqual([dict(sample.busy) for sample in role_activity.read(NOW)], [{"units": 3}, {"units": 4}])


if __name__ == "__main__":
    _ = unittest.main()
