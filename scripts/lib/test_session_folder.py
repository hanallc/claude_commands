"""Test the session folder: its name, its owner, the index link and its restore."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.lib import session_folder
from scripts.lib.session_folder import Member


class SessionFolderTests(unittest.TestCase):
    root: Path = Path()
    former: Path = Path()
    member: Member | None = None

    @override
    def setUp(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = base / "session"
        self.former = base / "former"
        environment = {session_folder.ROOT_ENV: str(self.root), session_folder.FORMER_ROOT_ENV: str(self.former)}
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment)))
        for name in (session_folder.FOLDER_ENV, "TMPDIR"):
            _ = os.environ.pop(name, None)
        self.member = None
        _ = cast(object, self.enterContext(patch.object(session_folder, "member_of", side_effect=self.member_of)))

    def member_of(self, session_id: str) -> Member | None:
        del session_id
        return self.member

    def test_no_session_root_makes_nothing(self) -> None:
        self.assertIsNone(session_folder.session_root())
        self.assertIsNone(session_folder.session_folder("abc"))
        self.assertIsNone(session_folder.launch_folder(Member("m1", "verify"), "abc"))
        self.assertFalse(self.root.exists())

    def test_a_member_keeps_its_folder_across_conversations(self) -> None:
        self.root.mkdir()
        self.member = Member("m1", "verify")
        first = session_folder.session_folder("first")
        second = session_folder.session_folder("second")
        self.assertEqual(first, self.root / "verify")
        self.assertEqual(second, first)
        assert second is not None
        self.assertEqual(session_folder.read_record(second), {"owner": "m1", "session_id": "second"})
        self.assertEqual(session_folder.folder_for(self.root, "first"), second)
        self.assertEqual(second.stat().st_mode & 0o777, 0o700)

    def test_a_second_member_with_the_same_name_gets_a_number(self) -> None:
        self.root.mkdir()
        self.assertEqual(session_folder.launch_folder(Member("m1", "verify"), "a"), self.root / "verify")
        self.assertEqual(session_folder.launch_folder(Member("m2", "verify"), "b"), self.root / "verify-2")
        self.assertEqual(session_folder.launch_folder(Member("m3", "verify")), self.root / "verify-3")
        self.assertEqual(session_folder.launch_folder(Member("m2", "verify"), "c"), self.root / "verify-2")

    def test_a_session_without_staff_gets_its_id_name(self) -> None:
        self.root.mkdir()
        self.assertEqual(session_folder.session_folder("abcdef123"), self.root / "session-abcdef")

    def test_an_unsafe_member_name_is_cleaned(self) -> None:
        self.assertEqual(session_folder.folder_name(Member("m1", "../a b"), "x"), "a-b")
        self.assertEqual(session_folder.folder_name(Member("abcdefgh", "///"), "x"), "member-abcdef")

    def test_the_hook_adopts_the_folder_a_launcher_made(self) -> None:
        self.root.mkdir()
        made = session_folder.launch_folder(Member("m1", "builds"))
        assert made is not None
        self.member = Member("other", "other")
        with patch.dict(os.environ, {"TMPDIR": str(made), session_folder.FOLDER_ENV: made.name}):
            self.assertEqual(session_folder.session_folder("new-id"), made)
        self.assertEqual(session_folder.read_record(made), {"owner": "m1", "session_id": "new-id"})
        self.assertFalse((self.root / "other").exists())

    def test_restore_makes_a_removed_folder_again_under_its_linked_name(self) -> None:
        self.root.mkdir()
        self.member = Member("m1", "verify")
        folder = session_folder.session_folder("abc")
        assert folder is not None
        (folder / session_folder.RECORD).unlink()
        folder.rmdir()
        self.member = Member("m1", "renamed")
        session_folder.restore("abc")
        self.assertEqual(session_folder.folder_for(self.root, "abc"), self.root / "verify")
        self.assertEqual(session_folder.read_record(self.root / "verify"), {"owner": "m1", "session_id": "abc"})

    def test_restore_without_a_link_makes_the_former_folder(self) -> None:
        session_folder.restore("abc")
        self.assertTrue((self.former / "abc").is_dir())
        self.assertEqual((self.former / "abc").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    _ = unittest.main()
