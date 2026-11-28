"""Test the revision scan and the saved notice."""

from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.production import staff, the_list
from scripts.review_links import list_page, version_notice


def commit(checkout: Path, subject: str) -> None:
    _ = version_notice.git(checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                           "commit", "--allow-empty", "-m", subject)


class VersionNoticeTests(unittest.TestCase):
    def test_scan_keeps_one_line_per_list_item_across_merged_commits(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = folder / "checkout"
        checkout.mkdir()
        git = version_notice.git
        _ = git(checkout, "init", "-b", "main")

        commit(checkout, "Initial commit")
        version_notice.start(folder, checkout)
        _ = git(checkout, "switch", "-c", "feature")
        commit(checkout, "Open the shared overlay (List 493)")
        commit(checkout, "Keep the same item for List 493")
        _ = git(checkout, "switch", "main")
        commit(checkout, "Update Review (List 499)")
        _ = git(checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "merge", "--no-ff", "feature", "-m", "Merge a console change")
        keeper = staff.StaffId("fixture")
        rows = [list_page.ListRecord(keeper, Mock(spec=the_list.ListItem,
            id=f"L{number}", number=number, title=title)) for number, title in (
                (493, "Open the shared overlay"), (499, "Show the List item number"))]
        with patch.object(list_page, "records", return_value=rows) as records:
            version_notice.start(folder, checkout)
            version_notice.start(folder, checkout)
        records.assert_called_once_with(include_closed=True)
        notice = version_notice.read(folder)
        self.assertEqual(len(notice.changes), 2)
        self.assertEqual({(change.number, change.key, change.title) for change in notice.changes},
                         {(493, "fixture/L493", "Open the shared overlay"),
                          (499, "fixture/L499", "Show the List item number")})
        commit(checkout, "Update the console tests")
        with patch.object(list_page, "records") as records:
            version_notice.start(folder, checkout)
        records.assert_not_called()
        updated = version_notice.read(folder)
        self.assertNotEqual(updated.revision, notice.revision)
        self.assertEqual(updated.changes, notice.changes)

    def test_missing_revision_sets_a_new_baseline_and_keeps_uncleared_lines(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = folder / "checkout"
        checkout.mkdir()
        git = version_notice.git
        _ = git(checkout, "init", "-b", "main")
        commit(checkout, "Update the Studio filters (List 504)")
        kept = version_notice.Change(493, "fixture/L493", "Open the shared overlay")
        cleared = version_notice.Change(499, "fixture/L499", "Show the List item number", cleared=True)
        previous = version_notice.Notice("a" * 40, (kept, cleared))
        _ = (folder / version_notice.FILE).write_text(json.dumps(asdict(previous)), encoding="utf-8")

        with patch.object(list_page, "records") as records:
            version_notice.start(folder, checkout)

        records.assert_not_called()
        revision = git(checkout, "rev-parse", "--verify", "HEAD").strip()
        self.assertEqual(version_notice.read(folder), version_notice.Notice(revision, (kept,)))

    def test_failed_scan_keeps_a_resolvable_baseline_for_the_next_start(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = folder / "checkout"
        checkout.mkdir()
        git = version_notice.git
        _ = git(checkout, "init", "-b", "main")
        commit(checkout, "Initial commit")
        baseline = git(checkout, "rev-parse", "--verify", "HEAD").strip()
        kept = version_notice.Change(493, "fixture/L493", "Open the shared overlay")
        previous = version_notice.Notice(baseline, (kept,))
        _ = (folder / version_notice.FILE).write_text(json.dumps(asdict(previous)), encoding="utf-8")
        commit(checkout, "Update the Studio filters (List 504)")
        revision = git(checkout, "rev-parse", "--verify", "HEAD").strip()
        fail = subprocess.CalledProcessError(128, ["git", "log"], stderr="Could not read an intervening commit.")

        def fail_scan(checkout: Path, *arguments: str) -> str:
            if arguments[0] == "log":
                raise fail
            return git(checkout, *arguments)

        change = version_notice.Change(504, "fixture/L504", "Update the Studio filters")
        row = list_page.ListRecord(staff.StaffId("fixture"),
            Mock(spec=the_list.ListItem, id="L504", number=change.number, title=change.title))
        with patch.object(list_page, "records", return_value=[row]) as records:
            with patch.object(version_notice, "git", side_effect=fail_scan):
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    version_notice.start(folder, checkout)
            self.assertIs(raised.exception, fail)
            self.assertEqual(version_notice.read(folder), previous)
            records.assert_not_called()
            version_notice.start(folder, checkout)

        self.assertEqual(version_notice.read(folder), version_notice.Notice(revision, (change, kept)))

    def test_first_start_records_head_without_a_message(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (patch.object(version_notice, "git", return_value="a" * 40 + "\n") as git,
              patch.object(list_page, "records") as records):
            version_notice.start(folder, folder)
        git.assert_called_once_with(folder, "rev-parse", "--verify", "HEAD")
        records.assert_not_called()
        self.assertEqual(version_notice.read(folder), version_notice.Notice("a" * 40))
