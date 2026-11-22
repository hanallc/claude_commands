"""Check the batch Claude transcript scan against the single-session lookup."""
from __future__ import annotations

import os
from pathlib import Path
from typing import cast
from unittest.mock import call, patch

from scripts.message import session_records
from scripts.production import staff, staff_test_support
from scripts.whoami import agent_accounts


class StaffBackendScanTests(staff_test_support.StaffTestCase):
    def test_scan_each_claude_projects_root_once_with_the_same_backend_evidence(self) -> None:
        home = Path.home()
        primary = home / ".claude" / "projects"
        numbered = home / ".claude2" / "projects"
        for projects in (primary, numbered):
            (projects / "project").mkdir(parents=True)
        alternate = home / ".claude3"
        alternate.symlink_to(primary.parent, target_is_directory=True)
        codex = home / ".codex2"
        (codex / "sessions").mkdir(parents=True)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(primary.parent), "CODEX_HOME": str(codex),
        })))
        parent = staff.ReportsTo(staff.StaffId(staff_test_support.fixture_runtime_id("parent")))
        rows: list[staff.StaffRow] = []
        for index in range(128):
            identity = staff.StaffId(staff_test_support.fixture_runtime_id(f"scan-{index}"))
            rows.append(staff.StaffRow(identity, "worker", f"worker-{index}", parent,
                                       current_session=identity, home=staff.SessionHome(codex)))
        plain, compressed, live, both, wrong_depth, wrong_suffix, codex_only = rows[:7]
        (primary / "project" / f"{plain.current_session}.jsonl").touch()
        (numbered / "project" / f"{compressed.current_session}.jsonl.zst").touch()
        (primary / "project" / f"{both.current_session}.jsonl").touch()
        (codex / "sessions" / f"{both.current_session}.jsonl").touch()
        (numbered / f"{wrong_depth.current_session}.jsonl").touch()
        (primary / "project" / f"{wrong_suffix.current_session}.jsonl.backup").touch()
        (codex / "sessions" / f"{codex_only.current_session}.jsonl").touch()
        running = frozenset({live.current_session})
        old_claude = {row.id for row in rows if row.current_session in running or
                      session_records.transcript_locations(row.current_session)}
        self.assertEqual(old_claude, {plain.id, compressed.id, live.id, both.id})

        with (patch.object(agent_accounts, "observe_workers_home", return_value=codex),
              patch.object(agent_accounts, "codex_homes", return_value=[codex]),
              patch.object(session_records, "transcript_locations", side_effect=AssertionError("per-row lookup")),
              patch.object(Path, "glob", autospec=True, side_effect=Path.glob) as glob):
            backends = staff.StaffSnapshot(tuple(rows), running_sessions=running).backends

        self.assertEqual(backends, {
            plain.id: staff.ClaudeBackend(), compressed.id: staff.ClaudeBackend(),
            live.id: staff.ClaudeBackend(), both.id: staff.CodexBackend(codex),
            codex_only.id: staff.CodexBackend(codex),
        })
        scans = [entry for entry in glob.call_args_list if entry.args[0] != home]
        self.assertCountEqual(scans, [call(primary, "*/*.jsonl*"), call(numbered, "*/*.jsonl*")])
