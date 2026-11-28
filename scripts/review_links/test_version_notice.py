"""Test the revision scan and the saved notice."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.production import staff, the_list
from scripts.review_links import version_notice


class VersionNoticeTests(unittest.TestCase):
    def test_scan_keeps_one_line_per_list_item_across_merged_commits(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        checkout = folder / "checkout"
        checkout.mkdir()
        git = version_notice.git
        _ = git(checkout, "init", "-b", "main")

        def commit(subject: str) -> None:
            _ = git(checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                    "commit", "--allow-empty", "-m", subject)

        commit("Initial commit")
        version_notice.start(folder, checkout)
        _ = git(checkout, "switch", "-c", "feature")
        commit("Open the shared overlay (List 493)")
        commit("Keep the same item for List 493")
        _ = git(checkout, "switch", "main")
        commit("Update Review (List 499)")
        _ = git(checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "merge", "--no-ff", "feature", "-m", "Merge a console change")
        keeper = staff.StaffId("fixture")
        rows = [the_list.ScannedListRow(keeper, Mock(spec=the_list.ListItem,
            id=f"L{number}", number=number, title=title)) for number, title in (
                (493, "Open the shared overlay"), (499, "Show the List item number"))]
        with patch.object(the_list, "display_list_records", return_value=rows) as records:
            version_notice.start(folder, checkout)
            version_notice.start(folder, checkout)
        records.assert_called_once_with(include_closed=True)
        notice = version_notice.read(folder)
        self.assertEqual(len(notice.changes), 2)
        self.assertEqual({(change.number, change.key, change.title) for change in notice.changes},
                         {(493, "fixture/L493", "Open the shared overlay"),
                          (499, "fixture/L499", "Show the List item number")})
        _ = version_notice.clear(folder, notice.revision, None)
        commit("Update the console tests")
        with patch.object(the_list, "display_list_records") as records:
            version_notice.start(folder, checkout)
        records.assert_not_called()
        self.assertEqual(version_notice.read(folder).changes, ())

    def test_first_start_records_head_without_a_message(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (patch.object(version_notice, "git", return_value="a" * 40 + "\n") as git,
              patch.object(the_list, "display_list_records") as records):
            version_notice.start(folder, folder)
        git.assert_called_once_with(folder, "rev-parse", "--verify", "HEAD")
        records.assert_not_called()
        self.assertEqual(version_notice.read(folder), version_notice.Notice("a" * 40))
