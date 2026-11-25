"""Test the shared action reader with fixture stores and comparison caches."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.for_you.action import ActionStore
from scripts.production import staff
from scripts.rebuild import need
from scripts.review_links import change_browser, change_page, for_you, review_items, review_page, staff_names
from scripts.statusline import for_you_count, rebuild_row


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
            f"rebuild.repo={self.root / 'nixos'}", "rebuild.ref=main", "rebuild.natedev=natedev", "rebuild.mac=MacBook-Pro",
            f"rebuild.cwd.natedev={self.root / 'nixos'}", "rebuild.cwd.mac=/fixture/mac/nixos",
            "rebuild.ssh=mac", "rebuild.interval=60", "rebuild.eval_timeout=120", "rebuild.eval_retry_seconds=600",
            f"rebuild.generations={self.root / 'generations.log'}",
            f"rebuild.cache={self.cache}")), encoding="utf-8")
        self.write_comparison()
        self.store = ActionStore(self.root / "actions")
        self.enterContext(patch.dict(os.environ, {"FOR_YOU_ACTIONS_DIR": str(self.store.folder),
                                               "REBUILD_NEEDS_DIR": str(self.root / "needs"),
                                               "CONF_DIR": str(self.root), "USER_ZONE": "America/Los_Angeles"}))
        members = staff.StaffSnapshot((staff.StaffRow(staff.StaffId("staff-a"), "producer", "Alice", staff.StaffRoot(),
                                                    current_session=staff.StaffId("session-a")),))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=members))
        _ = self.enterContext(patch("scripts.accounts.accounts.role_colors", return_value={}))
        _ = self.enterContext(patch("subprocess.run", side_effect=AssertionError("The reader executed a command.")))

    def write_comparison(self, machines: str = "", due: str = "9999999999", *,
                         running_paths: dict[str, str] | None = None) -> None:
        details = {"commit": "a" * 40, "source": [str(self.root / "nixos"), "main", "natedev", "MacBook-Pro"],
                   "natedev": "/nix/store/expected-linux", "mac": "/nix/store/expected-mac",
                   "running_system_paths": running_paths or {},
                   "running_system_probes": {"natedev": True, "mac": True}}
        _ = self.cache.write_text(f"{machines}\n{due}\n\n{json.dumps(details)}\n", encoding="utf-8")

    def read(self) -> for_you.ForYouSnapshot:
        return for_you.read(self.config)

    def add_action(self, dedupe: str = "access") -> str:
        return self.store.add(title="Restore access", command="github-warmup", machine="mac", cwd="/fixture/repo",
                              requested_by="staff-a", reason="The login requires the user.", waits="Release",
                              concerns="console", dedupe_key=dedupe).key

    def complete_need(self, machine: need.Machine = "natedev", *, commit: str = "c" * 40,
                      reason: str = "Apply completed settings.", at: str = "2026-11-17T12:00:00Z") -> Path:
        need.add_need(machine, commit, reason, "console", "staff-a")
        folder = need.needs_folder()
        source = folder / f"{machine}-{commit}.json"
        value = cast(dict[str, object], json.loads(source.read_text()))
        value["created_at"] = "2026-11-10T12:00:00+00:00"
        value["cleared_by"] = f"{at} switch 1 abc /nix/store/system"
        _ = source.write_text(json.dumps(value))
        (folder / "done").mkdir(exist_ok=True)
        return source.rename(folder / "done" / source.name)

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
        self.assertEqual(local.requests[0].requested_by, "Alice")
        self.assertEqual({request.reason for request in local.requests}, {"Apply setting A.", "Apply setting B."})
        self.assertIn("console", local.waits)
        self.assertEqual(local.command, "rebuild")
        self.assertEqual(local.cwd, str(self.root / "nixos"))
        self.assertEqual(rows["rebuild/mac"].waits, "Not recorded")
        self.assertEqual(snapshot.open_count, for_you.KnownOpenCount(2))

    def test_current_comparison_alone_creates_open_machine_row(self) -> None:
        running = "/nix/store/running-mac"
        self.write_comparison("rebuild mac", running_paths={"mac": running})
        changes = rebuild_row.changes_path(self.cache)
        self.assertEqual(self.read().rows[0].reason, rebuild_row.CHANGES_NOT_READ)
        at = "2026-11-21T12:00:00Z"
        commits = (rebuild_row.Change("a" * 40, at, "Update the editor", ""),
                   rebuild_row.Change("c" * 40, at, "Update the shell", ""))
        comparison = rebuild_row.ComparedChanges(running, "b" * 40, "a" * 40, commits)
        _ = changes.write_text(json.dumps({"mac": asdict(comparison)}), encoding="utf-8")
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].key, "rebuild/mac")
        self.assertEqual(snapshot.rows[0].status, "open")
        self.assertEqual(snapshot.rows[0].requests, ())
        self.assertEqual(snapshot.rows[0].reason, "Update the editor · Update the shell")
        self.assertEqual(snapshot.rows[0].created_at, "")
        self.assertEqual(snapshot.open_count, for_you.KnownOpenCount(1))

    def test_completed_needs_leave_for_you_and_do_not_set_the_age(self) -> None:
        _ = self.complete_need()
        snapshot = self.read()
        self.assertEqual(snapshot.rows, ())
        self.assertEqual(snapshot.open_count, for_you.KnownOpenCount(0))
        need.add_need("natedev", "b" * 40, "Apply new settings.", "console", "staff-a")
        need.add_need("natedev", "d" * 40, "Apply the oldest open request.", "console", "staff-a")
        oldest = "2026-11-18T12:00:00+00:00"
        for commit, at in (("d" * 40, oldest), ("b" * 40, "2026-11-19T12:00:00+00:00")):
            source = need.needs_folder() / f"natedev-{commit}.json"
            value = cast(need.NeedRecord, json.loads(source.read_text()))
            value["created_at"] = at
            _ = source.write_text(json.dumps(value))
        row, = self.read().rows
        self.assertEqual(row.status, "open")
        self.assertEqual([request.reason for request in row.requests],
                         ["Apply the oldest open request.", "Apply new settings."])
        self.assertEqual(row.requested_by, "Alice")
        self.assertEqual(row.created_at, oldest)
        self.assertEqual(review_items.for_you_item(row, staff.snapshot()).times.filed,
                         datetime.fromisoformat(oldest))

    def test_rebuild_card_shows_why_and_a_legacy_request_without_a_reason(self) -> None:
        need.add_need("natedev", "a" * 40, "Let you see when the <toolchain> test fails.", "console", "staff-a")
        need.add_need("natedev", "b" * 40, "Old text.", "console", "staff-a")
        source = need.needs_folder() / f"natedev-{'b' * 40}.json"
        value = cast(dict[str, object], json.loads(source.read_text()))
        del value["reason"]
        _ = source.write_text(json.dumps(value))
        row, = self.read().rows
        members = staff.snapshot()
        inputs = review_page.ReviewBodyInputs(change_page.ReviewBrowser("fixture"), members, 4, "",
                                              {}, ZoneInfo("America/Los_Angeles"), staff_names.StaffNames(members))
        markup = review_page.details(review_items.for_you_item(row, members), inputs)
        self.assertIn("<h3>Why</h3>", markup)
        self.assertIn('<span class="rebuild-secondary">No reason given</span>', markup)
        self.assertIn("Let you see when the &lt;toolchain&gt; test fails.", markup)
        self.assertIn("<dt>Asked by</dt><dd>Alice</dd>", markup)
        self.assertIn("The running revision is unknown.", markup)
        self.assertEqual(markup.count(" · <time datetime="), 2)
        self.assertNotIn("Evidence", markup)

    def test_rebuild_details_shows_completed_history_once_newest_first(self) -> None:
        old = self.complete_need(reason="Older request", at="2026-11-19T12:00:00+00:00")
        _ = self.complete_need("mac", reason="Newest request", at="2026-11-19T09:00:00-04:00")
        _ = old.with_name("duplicate.json").write_text(old.read_text())
        body = change_browser.render_rebuilds().decode()
        self.assertIn("Completed requests", body)
        self.assertLess(body.index("Newest request"), body.index("Older request"))
        self.assertEqual(body.count("Older request"), 1)
        self.assertEqual(body.count("Newest request"), 1)
        self.assertIn('data-count="0" data-label=""', body)

    def test_rebuild_details_names_bad_archive_and_keeps_valid_rows(self) -> None:
        completed = self.complete_need()
        _ = completed.with_name("bad<&>.json").write_text("{")
        need.add_need("natedev", "d" * 40, "Apply open settings.", "staff-a")
        body = change_browser.render_rebuilds().decode()
        self.assertIn("Apply open settings.", body)
        self.assertIn("Apply completed settings.", body)
        self.assertEqual(body.count("Cannot read completed request bad&lt;&amp;&gt;.json."), 1)

    def test_rebuild_details_skips_archive_that_exceeds_recursion_limit(self) -> None:
        completed = self.complete_need()
        depth = 10_000
        _ = completed.with_name("deep.json").write_text("[" * depth + "]" * depth)
        body = change_browser.render_rebuilds().decode()
        self.assertIn("Apply completed settings.", body)
        self.assertEqual(body.count("Cannot read completed request deep.json."), 1)

    def test_rebuild_requests_keep_a_legacy_time_and_a_time_with_offset(self) -> None:
        for commit, time in (("a" * 40, "2026-11-19T01:00:00"), ("b" * 40, "2026-11-19T00:00:00-02:00")):
            need.add_need("natedev", commit, "Apply settings.", "staff-a")
            path = need.needs_folder() / f"natedev-{commit}.json"
            record = cast(need.NeedRecord, json.loads(path.read_text()))
            record["created_at"] = time
            _ = path.write_text(json.dumps(record))
        snapshot = self.read()
        row, = snapshot.rows
        self.assertEqual(snapshot.unavailable, ())
        self.assertEqual(row.created_at, "2026-11-19T01:00:00+00:00")
        self.assertEqual([request.created_at for request in row.requests],
                         ["2026-11-19T01:00:00+00:00", "2026-11-19T00:00:00-02:00"])

    def test_damaged_open_record_keeps_valid_requests_and_marks_the_count(self) -> None:
        _ = self.add_action()
        need.add_need("mac", "a" * 40, "Apply settings.", "staff-a")
        _ = (need.needs_folder() / "bad.json").write_text("{")
        snapshot = self.read()
        issue, = snapshot.unavailable
        self.assertIn("Rebuild request bad.json", issue.reason)
        self.assertEqual(snapshot.open_count, for_you.UnavailableOpenCount(2))
        self.assertEqual(snapshot.status_line_actions(), for_you.StatusLineActions(("mac",), 1, partial=True))
        self.assertEqual(next(row for row in snapshot.rows if row.kind == "rebuild").requests[0].reason, "Apply settings.")

    def test_mac_status_line_reads_the_partial_count(self) -> None:
        fields = {"available": False, "partial": True, "other_count": 1, "rebuild_machines": ["mac"]}
        with patch("scripts.review_links.for_you._console_count_payload", return_value=fields):
            self.assertEqual(for_you.console_status_line_actions(),
                             for_you.StatusLineActions(("mac",), 1, partial=True))

    def refresh_with_revision_history(self, listing: str, *, dirty: bool = False) -> None:
        self.cache.unlink()
        running = "/nix/store/running-linux"
        configured = "87bbeb2" + "0" * 33
        switched = "03c7c81" + "0" * 33

        def run(command: list[str], *, timeout: int = 30) -> str:
            _ = timeout
            if command[0] == "git":
                if command[3] == "show":
                    return configured
                self.assertEqual(command[-1], f"{switched}..{configured}")
                result = subprocess.CompletedProcess(command, 0, listing + "\n", "")
                with patch("subprocess.run", return_value=result):
                    return rebuild_row.command_output(command)
            if command[0] == "nix":
                return json.dumps({"natedev": "/nix/store/expected-linux", "mac": "/nix/store/expected-mac"})
            if command[0] == "sh":
                revision = switched + "-dirty" if dirty else switched
                return self.switch_probe(running, 1, revision)
            return running if command[0] == "realpath" else "/nix/store/expected-mac"

        rebuild_row.write(self.config, run, clock=lambda: 9_999_999_900)
        rebuild_row.write_changes(self.config, run)

    def test_comparison_uses_commit_subjects_after_a_need_completes(self) -> None:
        _ = self.complete_need()
        at = "2026-11-21T12:00:00Z"
        self.refresh_with_revision_history(
            f"{'87bbeb2' + '0' * 33}\x1f{at}\x1fUpdate the editor\x1f\n"
            + f"{'b722af7' + '0' * 33}\x1f{at}\x1fUpdate the shell\x1f")
        row, = self.read().rows
        self.assertEqual(row.status, "open")
        self.assertEqual(row.requested_by, "")
        self.assertEqual(row.reason, "Update the editor · Update the shell")
        self.assertEqual(row.requests, ())
        self.assertEqual(row.evidence, "")

    def test_comparison_names_known_revisions_when_commit_history_is_unavailable(self) -> None:
        self.refresh_with_revision_history("")
        row, = self.read().rows
        self.assertEqual(row.running_revision, "03c7c81" + "0" * 33)

    @staticmethod
    def switch_probe(running: str, generation: int, revision: str) -> str:
        return (f"{running}\nactivated 2026-11-19T13:00:00Z 100\n"
                f"generation system-{generation}-link 2026-11-19T12:59:59Z 101\n--- generations ---\n"
                f"2026-11-19T13:00:01Z switch system-{generation}-link {revision} {running}")

    def test_refresh_uses_active_switch_or_embedded_revision(self) -> None:
        covered, other, configured, embedded, unrelated = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
        need.add_need("natedev", covered, "Show the completed request.", "console", "staff-a")
        need.add_need("natedev", other, "Keep the newer request open.", "console", "staff-a")
        need.add_need("mac", covered, "Show the result on the Mac.", "console", "staff-a")
        local, mac = "/nix/store/live-natedev", "/nix/store/live-mac"
        log = f"2026-11-19T13:00:00Z switch system-281-link {unrelated} /nix/store/older"
        mac_switch = self.switch_probe(mac, 10, unrelated)
        probes = {"natedev": self.switch_probe(local, 282, configured),
                  "mac": mac_switch.replace(mac + "\n", f"{mac}\nrevision {embedded}-dirty\n", 1)}
        checked: list[tuple[str, str]] = []

        def run(command: list[str], *, timeout: int = 30) -> str:
            _ = timeout
            match command[0], command[3] if len(command) > 3 else "":
                case "git", "show":
                    return "" if "--name-only" in command else configured
                case "git", "merge-base":
                    commit, revision = command[-2:]
                    checked.append((commit, revision))
                    return covered if commit == covered and revision in (configured, embedded) else unrelated
                case "git", "log":
                    return ""
                case "git", _:
                    self.fail(f"Unexpected Git command: {command}")
                case "nix", _:
                    return json.dumps({"natedev": local, "mac": "/nix/store/expected-mac"})
                case "realpath", _:
                    return local
                case "sh", _:
                    return probes["natedev"]
                case "ssh", _:
                    return probes["mac"] if len(command) == 5 and "configuration-revision" in command[-1] else mac
                case _:
                    self.fail(f"Unexpected command: {command}")

        self.cache.unlink()
        rebuild_row.write(self.config, run, clock=lambda: 9_999_999_900)
        rebuild_row.write_changes(self.config, run)
        rows = {row.machine: row for row in self.read().rows}
        self.assertEqual(rows["natedev"].running_revision, configured)
        self.assertEqual([request.reason for request in rows["natedev"].requests], ["Keep the newer request open."])
        self.assertEqual(rows["mac"].running_revision, embedded + "-dirty")
        self.assertEqual(len(rows["mac"].requests), 1)
        self.assertEqual(checked, [(covered, configured), (other, configured)])
        self.assertEqual([record["machine"] for record in need.read_completed_needs()], ["natedev"])

        probes["mac"] = f"{mac}\nrevision {embedded}\n--- generations ---\n{log}"
        rebuild_row.write_changes(self.config, run)
        rows = {row.machine: row for row in self.read().rows}
        self.assertEqual(rows["mac"].running_revision, embedded)
        self.assertEqual(rows["mac"].requests, ())
        self.assertEqual(checked[-1], (covered, embedded))
        self.assertEqual({record["machine"] for record in need.read_completed_needs()}, {"natedev", "mac"})

    def test_unlogged_generation_keeps_open_request_and_unknown_revision(self) -> None:
        commit = "a" * 40
        need.add_need("natedev", commit, "Show the build result.", "staff-a")
        running, mac = "/nix/store/linux", "/nix/store/mac"

        def run(command: list[str], *, timeout: int = 30) -> str:
            _ = timeout
            if command[0] == "git":
                self.assertEqual(command[3], "show")
                return commit
            if command[0] == "nix":
                return json.dumps({"natedev": running, "mac": mac})
            if command[0] == "realpath":
                return running
            if command[0] == "sh":
                return (f"{running}\nactivated 2026-11-19T13:00:00Z 200\n"
                        "generation system-43-link 2026-11-19T12:59:59Z 201\n--- generations ---\n"
                        f"2026-11-19T12:59:01Z switch system-42-link {commit} {running}")
            return mac

        self.cache.unlink()
        rebuild_row.write(self.config, run, clock=lambda: 9_999_999_900)
        rebuild_row.write_changes(self.config, run)
        row, = self.read().rows
        self.assertEqual(row.running_revision, "")
        self.assertEqual([request.reason for request in row.requests], ["Show the build result."])
        self.assertEqual(need.read_completed_needs(), ())
        names = staff.snapshot()
        inputs = review_page.ReviewBodyInputs(change_page.ReviewBrowser("fixture"), names, 4, "",
                                              {}, ZoneInfo("America/Los_Angeles"), staff_names.StaffNames(names))
        markup = review_page.details(review_items.for_you_item(row, names), inputs)
        self.assertIn("The running revision is unknown.", markup)

    def test_stale_comparison_does_not_claim_a_running_revision(self) -> None:
        self.refresh_with_revision_history(
            f"{'87bbeb2' + '0' * 33}\x1f2026-11-21T12:00:00Z\x1fUpdate the editor\x1f")
        answer = rebuild_row.read(self.config, clock=lambda: 10_000_000_000)
        snapshot = for_you.read(self.config, rebuild=answer)
        row, = snapshot.rows
        self.assertEqual(snapshot.open_count, for_you.UnavailableOpenCount(1))
        self.assertEqual(row.running_revision, "")

    def test_dirty_running_revision_keeps_its_dirty_suffix(self) -> None:
        self.refresh_with_revision_history(
            f"{'87bbeb2' + '0' * 33}\x1f2026-11-21T12:00:00Z\x1fUpdate the editor\x1f", dirty=True)
        row, = self.read().rows
        self.assertEqual(row.running_revision, "03c7c81" + "0" * 33 + "-dirty")

    def test_rebuilds_lists_changes_after_a_dirty_switch(self) -> None:
        self.refresh_with_revision_history(
            f"{'87bbeb2' + '0' * 33}\x1f2026-11-21T12:00:00Z\x1fUpdate the editor\x1f", dirty=True)
        body = change_browser.rebuild_list(rebuild_row.read(self.config), rebuild_row.read_recorded_changes(self.config),
                                           staff.snapshot())
        self.assertIn('<article data-machine="natedev"><h2>Rebuild natedev</h2>', body)
        self.assertIn("<h3>Commits</h3>", body)
        self.assertIn("<dd>Update the editor</dd>", body)

    def test_old_change_cache_cannot_prove_the_running_revision_is_clean(self) -> None:
        self.refresh_with_revision_history("")
        changes = rebuild_row.changes_path(self.cache)
        commit = rebuild_row.Change("87bbeb2" + "0" * 33, "2026-11-21T12:00:00Z", "Update the editor", "")
        _ = changes.write_text(json.dumps({"natedev": {"running_path": "/nix/store/running-linux",
            "running_revision": "03c7c81" + "0" * 33, "configured_revision": "87bbeb2" + "0" * 33,
            "commits": [vars(commit)]}}))
        row, = self.read().rows
        self.assertEqual(row.running_revision, "")
        self.assertEqual(rebuild_row.read_changes(self.config)["natedev"], (commit,))

    def test_global_count_does_not_read_compared_changes(self) -> None:
        self.write_comparison("rebuild natedev")
        need.add_need("mac", "b" * 40, "Apply Mac settings.", "console", "staff-a")
        _ = self.add_action()
        with (patch.object(rebuild_row, "DEFAULT_CONFIG", self.config),
              patch.object(rebuild_row, "read_compared_changes", return_value={}) as changes):
            self.assertEqual(for_you.global_action_count(), "3")
            self.assertEqual(for_you_count.global_action_count(self.config), "3")
            actions = for_you.read_for_status_line(self.config).status_line_actions()
            self.assertEqual(actions, for_you.StatusLineActions(("natedev", "mac"), 1))
        changes.assert_not_called()

    def test_revision_cache_must_match_the_system_and_configuration(self) -> None:
        self.refresh_with_revision_history(f"{'87bbeb2' + '0' * 33}\x1f2026-11-21T12:00:00Z\x1fUpdate the editor\x1f")
        answer = rebuild_row.read(self.config)
        expected = answer.expected
        assert isinstance(expected, rebuild_row.ExpectedSystems)
        for comparison in (
            replace(answer, running_paths={"natedev": "/nix/store/another-system"}),
            replace(answer, expected=replace(expected, commit="b" * 40)),
        ):
            with self.subTest(comparison=comparison):
                row, = for_you.read(self.config, rebuild=comparison).rows
                self.assertEqual(row.running_revision, "")
                self.assertEqual(row.reason, rebuild_row.CHANGES_NOT_READ)

    def test_stale_comparison_never_proves_completion(self) -> None:
        need.add_need("natedev", "c" * 40, "Show the build result.", "console", "staff-a")
        self.write_comparison(due="0")
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].status, "open")
        self.assertEqual(snapshot.open_count, for_you.UnavailableOpenCount(1))
        self.assertEqual(snapshot.unavailable[0].source, "rebuild comparison")

    def test_stale_positive_comparison_preserves_open_signal(self) -> None:
        self.write_comparison("rebuild mac", "0")
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].status, "open")
        self.assertEqual(snapshot.open_count, for_you.UnavailableOpenCount(1))

    def test_missing_or_damaged_comparison_is_unavailable(self) -> None:
        need.add_need("natedev", "c" * 40, "Show the build result.", "console", "staff-a")
        self.cache.unlink()
        snapshot = self.read()
        self.assertEqual(snapshot.rows[0].status, "open")
        self.assertTrue(snapshot.unavailable)
        _ = self.cache.write_text("damaged")
        self.assertIsInstance(self.read().open_count, for_you.UnavailableOpenCount)

    def test_failed_needs_read_preserves_actions_and_prevents_false_completion(self) -> None:
        key = self.add_action()
        _ = self.complete_need()
        with patch.object(need, "read_needs", side_effect=OSError("Fixture source failure.")):
            snapshot = self.read()
        rows = {row.key: row for row in snapshot.rows}
        self.assertEqual(rows[key].status, "open")
        self.assertNotIn("rebuild/natedev", rows)
        self.assertEqual(snapshot.open_count, for_you.UnavailableOpenCount(1))
        self.assertIn("rebuild needs", [source.source for source in snapshot.unavailable])

    def test_damaged_history_does_not_hide_open_requests(self) -> None:
        self.store.folder.mkdir()
        _ = (self.store.folder / "bad.json").write_text("{")
        done = need.needs_folder() / "done"
        done.mkdir(parents=True)
        _ = (done / "bad.json").write_text("{}")
        need.add_need("mac", "a" * 40, "Show the build result.", "console", "staff-a")
        snapshot = self.read()
        self.assertEqual({source.source for source in snapshot.unavailable}, {"actions"})
        self.assertEqual([row.key for row in snapshot.rows], ["rebuild/mac"])
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
        need.add_need("natedev", "c" * 40, "Show the build result.", "console", "staff-a")
        answer = rebuild_row.read(self.config)
        failed = replace(answer, failure=rebuild_row.EvaluationFailure("a" * 40, (), 1.0))
        with patch.object(rebuild_row, "read", return_value=failed):
            snapshot = self.read()
        self.assertEqual(snapshot.rows[0].status, "open")

    def test_failed_ssh_comparison_cannot_prove_mac_completion(self) -> None:
        need.add_need("mac", "c" * 40, "Show the build result on the Mac.", "console", "staff-a")
        _ = self.complete_need("natedev")
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
        self.assertEqual(rows["rebuild/mac"].status, "open")
        self.assertNotIn("rebuild/natedev", rows)
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
        _ = self.complete_need("mac")
        for machines in ("", "rebuild mac"):
            with self.subTest(machines=machines):
                self.write_comparison(machines)
                lines = self.cache.read_text().splitlines()
                details = cast(dict[str, object], json.loads(lines[3]))
                _ = details.pop("running_system_probes", None)
                lines[3] = json.dumps(details)
                _ = self.cache.write_text("\n".join(lines) + "\n")
                snapshot = self.read()
                self.assertEqual([row.status for row in snapshot.rows], ["open"] if machines else [])
                self.assertIsInstance(snapshot.open_count, for_you.UnavailableOpenCount)


if __name__ == "__main__":
    _ = unittest.main()
