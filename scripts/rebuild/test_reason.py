"""Check how the cached system comparison supplies rebuild reasons."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import staff
from scripts.rebuild import need
from scripts.review_links import change_browser, change_page, for_you, review_items, review_page, staff_names
from scripts.statusline import assignment, rebuild_row


class RebuildReasonTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    config: Path = Path()
    cache: Path = Path()
    running: str = "/nix/store/running-natedev"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "nixos"
        self.repo.mkdir()
        self.config = self.root / "statusline.conf"
        self.cache = self.root / "comparison"
        _ = write_config_fixture(self.root / "user_zone.conf")
        self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(self.root),
            "FOR_YOU_ACTIONS_DIR": str(self.root / "actions"),
            "REBUILD_NEEDS_DIR": str(self.root / "needs"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())))
        _ = self.config.write_text("\n".join((
            f"rebuild.repo={self.repo}", "rebuild.ref=main", "rebuild.natedev=natedev",
            "rebuild.mac=MacBook-Pro", "rebuild.ssh=mac", "rebuild.interval=60",
            "rebuild.eval_timeout=120", "rebuild.eval_retry_seconds=600",
            f"rebuild.cache={self.cache}", f"rebuild.generations={self.root / 'generations.log'}",
            f"rebuild.cwd.natedev={self.repo}", "rebuild.cwd.mac=~/nixos",
        )), encoding="utf-8")
        _ = self.git("init", "--initial-branch=main")

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", *args],
            text=True, stderr=subprocess.PIPE).strip()

    def commit(self, subject: str, files: tuple[str, ...]) -> str:
        for name in files:
            _ = (self.repo / name).write_text(subject, encoding="utf-8")
        _ = self.git("add", "--", *files)
        _ = self.git("commit", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def refresh_changes(self, active: str, target: str, *, probe: bool = True) -> rebuild_row.RebuildAnswer:
        command_result = need.command_result

        def run(command: list[str], *, timeout: int = 30) -> str | None:
            if command[0] == "nix":
                return json.dumps({"natedev": "/nix/store/configured-natedev", "mac": "/nix/store/configured-mac"})
            if command[0] == "realpath":
                return self.running
            if command[0] == "ssh":
                return "/nix/store/configured-mac"
            if command[0] == "sh":
                return f"{self.running}\nrevision {active}\n--- generations ---" if probe else None
            self.assertEqual(command[:3], ["git", "-C", str(self.repo)])
            return command_result(command, timeout=timeout)

        self.cache.unlink(missing_ok=True)
        with patch.object(need, "command_result", side_effect=run):
            rebuild_row.write(self.config, clock=lambda: 9_999_999_900)
            rebuild_row.write_changes(self.config)
        answer = rebuild_row.read(self.config)
        self.assertEqual(answer.configured_revision(), target)
        return answer

    def review_card(self, row: for_you.ForYouRow) -> str:
        members = staff.snapshot()
        inputs = review_page.ReviewBodyInputs(change_page.ReviewBrowser("fixture"), members, 4,
                                              {}, ZoneInfo("America/New_York"), staff_names.StaffNames(members))
        return review_page.details(review_items.for_you_item(row, members), inputs)

    def test_comparison_shows_the_same_commit_subjects_on_review_and_rebuilds(self) -> None:
        active = self.commit("Running settings", ("settings.nix",))
        subjects = [f"Apply setting <{index}>" for index in range(7)]
        for subject in subjects[:-1]:
            _ = self.commit(subject, ("settings.nix",))
        target = self.commit(subjects[-1], ("settings.nix",))
        answer = self.refresh_changes(active, target)
        self.assertEqual(answer.machines, ("natedev",))
        self.assertEqual(tuple(answer.needs), ())
        with patch("subprocess.run", side_effect=AssertionError("A cache reader ran a command.")):
            row, = for_you.read(self.config).rows
            page = change_browser.render_rebuilds().decode()
            card = self.review_card(row)
        self.assertEqual(row.reason, " · ".join(reversed(subjects[-5:])) + " · +2 more")
        for markup in (card, page):
            for index in range(2, 7):
                self.assertIn(f"Apply setting &lt;{index}&gt;", markup)
            self.assertNotIn("Apply setting &lt;1&gt;", markup)
            self.assertIn("+2 more", markup)
        self.assertIn("<dt>Asked by</dt><dd>Found by the system check</dd>", card)
        self.assertNotIn("No reason given", card)

    def test_comparison_with_unknown_revision_uses_changed_paths(self) -> None:
        _ = self.commit("Running settings", ("old.nix",))
        target = self.commit("Configured settings", tuple(f"setting-{index}.nix" for index in range(6)))
        answer = self.refresh_changes("", target)
        changes = rebuild_row.read_compared_changes(self.config, answer)["natedev"]
        self.assertEqual(changes.active_revision, "")
        self.assertEqual(changes.commits, ())
        self.assertEqual(len(changes.paths), 6)
        row, = for_you.read(self.config).rows
        self.assertIn("Files changed in the configured commit: setting-0.nix", row.reason)
        self.assertIn("+1 more", row.reason)
        page = change_browser.render_rebuilds().decode()
        self.assertIn("setting-0.nix", page)
        self.assertNotIn("setting-5.nix", page)
        self.assertIn("+1 more", page)

    def test_failed_probe_keeps_identities_and_hides_subjects_for_the_old_target(self) -> None:
        active = self.commit("Running settings", ("settings.nix",))
        old = self.commit("Old target settings", ("settings.nix",))
        _ = self.refresh_changes(active, old)
        retained = rebuild_row.read_recorded_changes(self.config)["natedev"]
        self.assertIsInstance(retained, rebuild_row.ComparedChanges)
        same = self.refresh_changes(active, old, probe=False)
        self.assertEqual(rebuild_row.read_compared_changes(self.config, same)["natedev"], retained)

        target = self.commit("New target settings", ("settings.nix",))
        answer = self.refresh_changes(active, target, probe=False)
        self.assertEqual(rebuild_row.read_recorded_changes(self.config)["natedev"], retained)
        self.assertNotIn("natedev", rebuild_row.read_compared_changes(self.config, answer))
        row, = for_you.read(self.config).rows
        status = for_you.read_for_status_line(self.config, include_reasons=True).status_line_actions()
        for words in (self.review_card(row), change_browser.render_rebuilds().decode(), status.rebuild_text()):
            self.assertIn("Changes not read yet · New target settings", words)
            self.assertNotIn("Old target settings", words)

    def test_blank_need_keeps_its_history_and_shows_the_comparison_on_both_pages(self) -> None:
        active = self.commit("Running settings", ("settings.nix",))
        target = self.commit("Update the editor", ("settings.nix",))
        _ = self.refresh_changes(active, target)
        need.add_need("natedev", target, "Legacy reason", "console")
        request = need.needs_folder() / f"natedev-{target}.json"
        _ = request.write_text(request.read_text().replace('"Legacy reason"', '""'), encoding="utf-8")

        row, = for_you.read(self.config).rows
        self.assertEqual(row.requests[0].reason, "")
        for markup in (self.review_card(row), change_browser.render_rebuilds().decode()):
            self.assertIn("No reason given", markup)
            self.assertIn("Update the editor", markup)

    def test_both_pages_exclude_commits_with_filed_reasons(self) -> None:
        active = self.commit("Running settings", ("settings.nix",))
        _ = self.commit("Oldest cached subject", ("settings.nix",))
        legacy = self.commit("Legacy cached subject", ("settings.nix",))
        subjects = [f"Cached subject {index}" for index in range(4)]
        for subject in subjects:
            _ = self.commit(subject, ("settings.nix",))
        target = self.commit("Subject already explained", ("settings.nix",))
        _ = self.refresh_changes(active, target)
        need.add_need("natedev", target, "Filed explanation", "console")
        need.add_need("natedev", legacy, "Legacy reason", "console")
        request = need.needs_folder() / f"natedev-{legacy}.json"
        _ = request.write_text(request.read_text().replace('"Legacy reason"', '""'), encoding="utf-8")

        row, = for_you.read(self.config).rows
        for markup in (self.review_card(row), change_browser.render_rebuilds().decode()):
            for subject in (*subjects, "Legacy cached subject", "Filed explanation", "No reason given"):
                self.assertIn(subject, markup)
            self.assertNotIn("Oldest cached subject", markup)
            self.assertNotIn("Subject already explained", markup)
            self.assertIn("+1 more", markup)
            self.assertNotIn("+2 more", markup)

    def test_legacy_need_filters_its_commit_subject_after_cache_refresh(self) -> None:
        active = self.commit("Running settings", ("settings.nix",))
        subjects = [f"Pending subject {index}" for index in range(5)]
        for subject in subjects:
            _ = self.commit(subject, ("settings.nix",))
        target = self.commit("Subject with a filed reason", ("settings.nix",))
        need.add_need("natedev", target, "Filed explanation", "console")
        prefix = target[:12]
        record = need.read_needs()[0].newest
        folder = need.needs_folder()
        legacy = {"id": f"natedev-{prefix}", "nixos_commit": prefix}
        request = (folder / f"natedev-{target}.json").rename(folder / f"natedev-{prefix}.json")
        _ = request.write_text(json.dumps({**record, **legacy}))

        answer = self.refresh_changes(active, target)
        changes = rebuild_row.read_compared_changes(self.config, answer)["natedev"]
        self.assertEqual(changes.legacy_commits, {prefix: target})
        self.assertEqual(next(iter(answer.needs)).newest["nixos_commit"], prefix)
        with patch("subprocess.run", side_effect=AssertionError("A cache reader ran a command.")):
            row, = for_you.read(self.config).rows
            for markup in (self.review_card(row), change_browser.render_rebuilds().decode()):
                for subject in subjects:
                    self.assertIn(subject, markup)
                self.assertIn("Filed explanation", markup)
                self.assertNotIn("Subject with a filed reason", markup)
                self.assertNotIn("+1 more", markup)

    def test_rollback_reports_paths_between_revisions_after_an_empty_range(self) -> None:
        configured = self.commit("Set the original limits", ("old-limits.nix",))
        active = self.commit("Add new limits", ("new-limits.nix",))
        _ = self.git("branch", "configured", configured)
        _ = self.config.write_text(self.config.read_text().replace("rebuild.ref=main", "rebuild.ref=configured"))

        answer = self.refresh_changes(active, configured)
        changes = rebuild_row.read_compared_changes(self.config, answer)["natedev"]
        self.assertEqual((changes.commits, changes.paths, changes.paths_between_revisions),
                         ((), ("new-limits.nix",), True))
        with patch("subprocess.run", side_effect=AssertionError("A cache reader ran a command.")):
            row, = for_you.read(self.config).rows
            status = for_you.read_for_status_line(self.config, include_reasons=True).status_line_actions()
            for words in (self.review_card(row), change_browser.render_rebuilds().decode(), status.rebuild_text()):
                self.assertIn("Files changed between revisions: new-limits.nix", words)
                self.assertNotIn("old-limits.nix", words)

        _ = self.git("revert", "--no-edit", active)
        same_files = self.git("rev-parse", "HEAD")
        _ = self.refresh_changes(same_files, configured)
        row, = for_you.read(self.config).rows
        self.assertEqual(row.reason, "The two revisions have the same files.")

    def test_dirty_running_revision_reports_local_changes_when_commit_files_match(self) -> None:
        configured = self.commit("Set the original limits", ("limits.nix",))
        _ = self.git("branch", "configured", configured)
        _ = self.git("commit", "--allow-empty", "-m", "Keep the original limits")
        same_files = self.git("rev-parse", "HEAD")
        _ = self.config.write_text(self.config.read_text().replace("rebuild.ref=main", "rebuild.ref=configured"))

        for commit in (configured, same_files):
            with self.subTest(commit=commit):
                _ = self.refresh_changes(f"{commit}-dirty", configured)
                with patch("subprocess.run", side_effect=AssertionError("A cache reader ran a command.")):
                    row, = for_you.read(self.config).rows
                    status = for_you.read_for_status_line(self.config, include_reasons=True).status_line_actions()
                    for words in (self.review_card(row), change_browser.render_rebuilds().decode(),
                                  status.rebuild_text()):
                        self.assertIn("The running build has local changes.", words)
                        self.assertNotIn("The two revisions have the same files.", words)

    def test_git_range_with_an_unavailable_running_commit_uses_changed_paths(self) -> None:
        _ = self.commit("Running settings", ("old.nix",))
        target = self.commit("Configured settings", ("settings.nix",))
        absent = "f" * 40
        answer = self.refresh_changes(absent, target)
        changes = rebuild_row.read_compared_changes(self.config, answer)["natedev"]
        self.assertEqual((changes.active_revision, changes.commits, changes.paths), (absent, (), ("settings.nix",)))
        row, = for_you.read(self.config).rows
        self.assertEqual(row.reason, "Files changed in the configured commit: settings.nix")

    def test_status_text_uses_cached_subjects_and_keeps_the_count_when_truncated(self) -> None:
        active = self.commit("Running settings", ("settings.nix",))
        _ = self.commit("Set the memory limit for builds", ("settings.nix",))
        subject = "Raise the builds swap limit from 4 GiB to 8 GiB for concurrent builds"
        target = self.commit(subject, ("settings.nix",))
        _ = self.refresh_changes(active, target)
        need.add_need("mac", "b" * 40, "Apply the Mac settings.", "console")
        with (patch.object(rebuild_row, "DEFAULT_CONFIG", self.config),
              patch("subprocess.run", side_effect=AssertionError("A status reader ran a command."))):
            actions = assignment.read_actions(remote=False)
            folder = self.root / "actions"
            folder.mkdir()
            _ = (folder / "corrupt.json").write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
            damaged = assignment.read_actions(remote=False)
        self.assertEqual(actions.value.waiting_rebuilds,
                         "rebuild natedev: Raise the builds swap limit from 4 GiB to 8 GiB for conc… +1, "
                         + "rebuild mac: Apply the Mac settings.")
        changes = rebuild_row.changes_path(self.cache)
        self.assertEqual(actions.sources.stamps[changes], assignment.file_stamp(changes))
        self.assertEqual(damaged.value.other_count, "unavailable")
        self.assertEqual(damaged.value.waiting_rebuilds, actions.value.waiting_rebuilds)
