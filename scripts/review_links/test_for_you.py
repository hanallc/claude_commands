"""Test the shared action reader with fixture stores and comparison caches."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.for_you.action import ActionStore
from scripts.production import staff
from scripts.rebuild import need
from scripts.review_links import for_you
from scripts.statusline import rebuild_row


class ForYouTests(unittest.TestCase):
    """Read fixtures without contacting a machine or changing a real store."""

    root: Path = Path()
    config: Path = Path()
    cache: Path = Path()
    store: ActionStore = ActionStore(Path())

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "comparison"
        self.config = self.root / "statusline.conf"
        _ = self.config.write_text("\n".join((
            f"rebuild.repo={self.root / 'nixos'}", "rebuild.ref=main", "rebuild.ref.mac=origin/main",
            "rebuild.natedev=natedev", "rebuild.mac=MacBook-Pro",
            f"rebuild.cwd.natedev={self.root / 'nixos'}", "rebuild.cwd.mac=/fixture/mac/nixos",
            "rebuild.ssh=mac", "rebuild.interval=60", "rebuild.eval_timeout=120", "rebuild.eval_retry_seconds=600",
            f"rebuild.cache={self.cache}")), encoding="utf-8")
        self.write_comparison()
        self.store = ActionStore(self.root / "actions")
        self.enterContext(patch.dict(os.environ, {"FOR_YOU_ACTIONS_DIR": str(self.store.folder),
                                               "REBUILD_NEEDS_DIR": str(self.root / "needs")}))
        members = staff.StaffSnapshot((staff.StaffRow(staff.StaffId("staff-a"), "producer", "Alice", staff.StaffRoot(),
                                                    current_session=staff.StaffId("session-a")),))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=members))
        _ = self.enterContext(patch("subprocess.run", side_effect=AssertionError("The reader executed a command.")))

    def write_comparison(self, machines: str = "", due: str = "9999999999") -> None:
        commit = "a" * 40
        details = {"commits": {"natedev": commit, "mac": commit},
                   "source": [str(self.root / "nixos"), "main", "origin/main", "natedev", "MacBook-Pro"],
                   "paths": {"natedev": "/nix/store/expected-linux", "mac": "/nix/store/expected-mac"},
                   "failures": {},
                   "running_system_probes": {"natedev": True, "mac": True}}
        _ = self.cache.write_text(f"{machines}\n{due}\n\n{json.dumps(details)}\n", encoding="utf-8")

    def read(self) -> for_you.ForYouSnapshot:
        return for_you.read(self.config)

    def add_action(self, dedupe: str = "access") -> str:
        return self.store.add(title="Restore access", command="github-warmup", machine="mac", cwd="/fixture/repo",
                              requested_by="staff-a", reason="The login requires the user.", waits="Release",
                              concerns="console", dedupe_key=dedupe).key

    def complete_need(self, machine: need.Machine = "natedev") -> None:
        need.add_need(machine, "c" * 40, "Apply completed settings.", "console", "staff-a")
        folder = need.needs_folder()
        source = folder / f"{machine}-{'c' * 40}.json"
        value = cast(dict[str, object], json.loads(source.read_text()))
        value["cleared_by"] = "2026-11-17T12:00:00Z switch 1 abc /nix/store/system"
        _ = source.write_text(json.dumps(value))
        (folder / "done").mkdir(exist_ok=True)
        _ = source.rename(folder / "done" / source.name)

    def test_empty_sources_have_known_zero_without_store_creation(self) -> None:
        snapshot = self.read()
        self.assertEqual(snapshot.rows, ())
        self.assertEqual(snapshot.unavailable, ())
        self.assertEqual(snapshot.open_count, for_you.KnownOpenCount(0))
        self.assertFalse(self.store.folder.exists())
        self.assertFalse(need.needs_folder().exists())

    def test_agent_actions_preserve_status_evidence_and_display_name(self) -> None:
        first = self.add_action()
        second = self.add_action("release")
        third = self.add_action("another")
        _ = self.store.mark_done(second, evidence="Confirmed by the user.")
        _ = self.store.withdraw(third, requested_by="staff-a", reason="The release was cancelled.")
        rows = {row.key: row for row in self.read().rows}
        self.assertEqual(rows[first].requested_by, "Alice")
        self.assertEqual(rows[first].command, "github-warmup")
        self.assertEqual(rows[second].status, "done")
        self.assertEqual(rows[second].evidence, "Confirmed by the user.")
        self.assertEqual(rows[third].status, "withdrawn")
        self.assertIn("cancelled", rows[third].evidence)
        self.assertEqual(self.read().open_count, for_you.KnownOpenCount(1))

    def test_rebuild_needs_group_by_machine_and_include_every_request(self) -> None:
        need.add_need("natedev", "a" * 40, "Apply setting A.", "staff-a", "staff-a")
        need.add_need("natedev", "b" * 40, "Apply setting B.", "console", "staff-b")
        need.add_need("mac", "a" * 40, "Apply Mac settings.", "", "staff-a")
        self.write_comparison("rebuild natedev")
        snapshot = self.read()
        self.assertEqual(len(snapshot.rows), 2)
        rows = {row.key: row for row in snapshot.rows}
        local = rows["rebuild/natedev"]
        self.assertEqual(len(local.requests), 2)
        self.assertIn("Alice", local.reason)
        self.assertIn("setting A", local.reason)
        self.assertIn("setting B", local.reason)
        self.assertIn("console", local.waits)
        self.assertEqual(local.command, "rebuild")
        self.assertEqual(local.cwd, str(self.root / "nixos"))
        self.assertEqual(rows["rebuild/mac"].waits, "Not recorded")
        self.assertEqual(snapshot.open_count, for_you.KnownOpenCount(2))

    def test_current_comparison_alone_creates_open_machine_row(self) -> None:
        self.write_comparison("rebuild mac")
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].key, "rebuild/mac")
        self.assertEqual(snapshot.rows[0].status, "open")
        self.assertEqual(snapshot.rows[0].requests, ())
        self.assertEqual(snapshot.open_count, for_you.KnownOpenCount(1))

    def test_completed_needs_provide_one_history_row_per_machine(self) -> None:
        self.complete_need()
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].status, "done")
        self.assertIn("switch 1", snapshot.rows[0].evidence)
        self.assertEqual(snapshot.rows[0].completion, "automatic")
        self.assertEqual(snapshot.open_count, for_you.KnownOpenCount(0))
        need.add_need("natedev", "b" * 40, "Apply new settings.", "console", "staff-a")
        current = self.read()
        self.assertEqual(len(current.rows), 1)
        self.assertEqual(current.rows[0].status, "open")
        self.assertEqual(len(current.rows[0].requests), 2)

    def test_stale_comparison_never_proves_completion(self) -> None:
        self.complete_need()
        self.write_comparison(due="0")
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].status, "unknown")
        self.assertEqual(snapshot.open_count, for_you.UnavailableOpenCount(0))
        self.assertEqual(snapshot.unavailable[0].source, "rebuild comparison")

    def test_stale_positive_comparison_preserves_open_signal(self) -> None:
        self.write_comparison("rebuild mac", "0")
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].status, "open")
        self.assertEqual(snapshot.open_count, for_you.UnavailableOpenCount(1))

    def test_missing_or_damaged_comparison_is_unavailable(self) -> None:
        self.complete_need()
        self.cache.unlink()
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].status, "unknown")
        self.assertTrue(snapshot.unavailable)
        _ = self.cache.write_text("damaged")
        self.assertIsInstance(self.read().open_count, for_you.UnavailableOpenCount)

    def test_failed_needs_read_preserves_actions_and_prevents_false_completion(self) -> None:
        key = self.add_action()
        self.complete_need()
        with patch.object(need, "read_needs", side_effect=OSError("Fixture source failure.")):
            snapshot = self.read()
        rows = {row.key: row for row in snapshot.rows}
        self.assertEqual(rows[key].status, "open")
        self.assertEqual(rows["rebuild/natedev"].status, "unknown")
        self.assertEqual(snapshot.open_count, for_you.UnavailableOpenCount(1))
        self.assertIn("rebuild needs", [source.source for source in snapshot.unavailable])

    def test_damaged_action_and_history_are_unavailable(self) -> None:
        self.store.folder.mkdir()
        _ = (self.store.folder / "bad.json").write_text("{")
        done = need.needs_folder() / "done"
        done.mkdir(parents=True)
        _ = (done / "bad.json").write_text("{}")
        snapshot = self.read()
        self.assertEqual({source.source for source in snapshot.unavailable}, {"actions", "completed rebuild needs"})
        self.assertIsInstance(snapshot.open_count, for_you.UnavailableOpenCount)

    def test_failed_staff_read_preserves_raw_requester_and_marker(self) -> None:
        _ = self.add_action()
        with patch.object(staff, "snapshot", side_effect=staff.StaffUnreadable("Fixture staff failure.")):
            snapshot = self.read()
        self.assertEqual(snapshot.rows[0].requested_by, "staff-a")
        self.assertEqual(snapshot.unavailable[0].source, "staff")

    def test_failed_config_preserves_open_need_without_inventing_cwd(self) -> None:
        need.add_need("mac", "a" * 40, "Apply settings.", "console", "staff-a")
        self.config.unlink()
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].status, "open")
        self.assertEqual(snapshot.rows[0].cwd, "")
        self.assertEqual(snapshot.open_count, for_you.UnavailableOpenCount(1))

    def test_open_count_counts_each_machine_and_each_action_once(self) -> None:
        _ = self.add_action()
        _ = self.add_action("other")
        need.add_need("mac", "a" * 40, "Apply A.", "console", "staff-a")
        need.add_need("mac", "b" * 40, "Apply B.", "console", "staff-a")
        self.write_comparison("rebuild mac, rebuild natedev")
        self.assertEqual(self.read().open_count, for_you.KnownOpenCount(4))

    def test_failed_comparison_evaluation_cannot_prove_completion(self) -> None:
        self.complete_need("natedev")
        self.complete_need("mac")
        local_commit, mac_commit = "b" * 40, "a" * 40
        paths = {"natedev": "/nix/store/expected-linux", "mac": "/nix/store/expected-mac"}
        for failed in ("natedev", "mac"):
            with self.subTest(failed=failed):
                self.cache.unlink()

                def run(command: list[str], *, timeout: int = 30) -> str:
                    _ = timeout
                    if command[0] == "git":
                        return local_commit if command[-1] == "main^{commit}" else mac_commit
                    if command[0] == "nix":
                        machine = "natedev" if f"?rev={local_commit}" in command[-1] else "mac"
                        return "" if machine == failed else json.dumps({machine: paths[machine]})
                    return paths["natedev" if command[0] == "realpath" else "mac"]

                rebuild_row.write(self.config, run, clock=lambda: 9_999_999_900)
                snapshot = self.read()
                rows = {row.key: row for row in snapshot.rows}
                for machine in paths:
                    self.assertEqual(rows[f"rebuild/{machine}"].status, "unknown" if machine == failed else "done")
                self.assertEqual(snapshot.unavailable, (for_you.UnavailableSource(
                    "rebuild comparison", f"{failed}: the expected system is unavailable."),))

    def test_failed_ssh_comparison_cannot_prove_mac_completion(self) -> None:
        self.complete_need("mac")
        self.complete_need("natedev")
        self.cache.unlink()

        def run(command: list[str], *, timeout: int = 30) -> str:
            _ = timeout
            if command[0] == "git":
                return "a" * 40
            if command[0] == "nix":
                return json.dumps({"natedev": "/nix/store/expected-linux", "mac": "/nix/store/expected-mac"})
            return "/nix/store/expected-linux" if command[0] == "realpath" else ""

        rebuild_row.write(self.config, run, clock=lambda: 9_999_999_900)
        snapshot = self.read()
        rows = {row.key: row for row in snapshot.rows}
        self.assertEqual(rows["rebuild/mac"].status, "unknown")
        self.assertEqual(rows["rebuild/natedev"].status, "done")
        self.assertIsInstance(snapshot.open_count, for_you.UnavailableOpenCount)
        self.assertTrue(any("mac" in source.reason for source in snapshot.unavailable))

    def test_rebuild_cwd_uses_the_selected_machine_config(self) -> None:
        self.write_comparison("rebuild mac, rebuild natedev")
        rows = {row.key: row for row in self.read().rows}
        self.assertEqual(rows["rebuild/mac"].cwd, "/fixture/mac/nixos")
        self.assertEqual(rows["rebuild/natedev"].cwd, str(self.root / "nixos"))

    def test_waits_includes_staff_name_and_current_phase(self) -> None:
        run = self.root / "alice-run"
        run.mkdir()
        _ = (run / "progress_history_state.json").write_text(
            json.dumps({"open_phases": [{"id": "restore-access", "status": "active"}]}))
        member = staff.StaffRow(staff.StaffId("staff-a"), "unit", "Alice", staff.StaffRoot(),
                                current_session=staff.StaffId("session-a"), run=staff.OpenRun(run))
        need.add_need("mac", "a" * 40, "Apply settings.", "staff-a", "staff-a")
        with patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((member,))):
            row = self.read().rows[0]
        self.assertEqual(row.waits, "Alice: restore-access")

    def test_waits_marks_missing_staff_and_missing_current_work(self) -> None:
        need.add_need("mac", "a" * 40, "Apply settings A.", "staff-a", "staff-a")
        need.add_need("mac", "b" * 40, "Apply settings B.", "staff-missing", "staff-a")
        waits = self.read().rows[0].waits
        self.assertIn("Alice: Not recorded", waits)
        self.assertIn("staff-missing: Not recorded", waits)

    def test_legacy_comparison_without_probe_fields_is_unavailable(self) -> None:
        self.complete_need("mac")
        for machines in ("", "rebuild mac"):
            with self.subTest(machines=machines):
                self.write_comparison(machines)
                lines = self.cache.read_text().splitlines()
                details = cast(dict[str, object], json.loads(lines[3]))
                _ = details.pop("running_system_probes", None)
                lines[3] = json.dumps(details)
                _ = self.cache.write_text("\n".join(lines) + "\n")
                snapshot = self.read()
                self.assertEqual(snapshot.rows[0].status, "open" if machines else "unknown")
                self.assertIsInstance(snapshot.open_count, for_you.UnavailableOpenCount)


if __name__ == "__main__":
    _ = unittest.main()
