"""Test the session folder: its name, a live clash, the index link and its restore."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.lib import session_folder
from scripts.lib.session_folder import Member
from scripts.message import session_records
from scripts.production import staff


class SessionFolderTests(unittest.TestCase):
    root: Path = Path()
    former: Path = Path()
    member: Member | None = None
    live: frozenset[str] = frozenset()

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
        self.live = frozenset()
        _ = cast(object, self.enterContext(patch.object(session_folder, "member_of", side_effect=self.member_of)))
        _ = cast(object, self.enterContext(patch.object(session_records, "session_liveness", side_effect=self.liveness)))

    def liveness(self, session_id: str) -> session_records.LiveSessionProcess | session_records.SessionNotLive:
        return session_records.LiveSessionProcess(1) if session_id in self.live else session_records.SessionNotLive()

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

    def test_a_name_gets_a_number_only_while_a_live_session_holds_it(self) -> None:
        self.root.mkdir()
        self.assertEqual(session_folder.launch_folder(Member("m1", "verify"), "a"), self.root / "verify")
        self.live = frozenset({"a"})
        self.assertEqual(session_folder.launch_folder(Member("m2", "verify"), "b"), self.root / "verify-2")
        self.live = frozenset({"a", "b"})
        self.assertEqual(session_folder.launch_folder(Member("m3", "verify")), self.root / "verify-3")
        self.live = frozenset()
        self.assertEqual(session_folder.launch_folder(Member("m4", "verify"), "d"), self.root / "verify")
        self.assertEqual(session_folder.read_record(self.root / "verify"), {"owner": "m4", "session_id": "d"})

    def test_unknown_liveness_counts_as_live(self) -> None:
        self.root.mkdir()
        _ = session_folder.launch_folder(Member("m1", "verify"), "a")
        with patch.object(session_records, "session_liveness",
                          return_value=session_records.SessionLivenessUnknown("registry unreadable")):
            self.assertEqual(session_folder.launch_folder(Member("m2", "verify"), "b"), self.root / "verify-2")

    def test_a_session_without_staff_takes_its_working_folder_name(self) -> None:
        self.root.mkdir()
        self.assertEqual(session_folder.session_folder("abcdef123", "/home/x/worktrees/hana-fix"),
                         self.root / "hana-fix")
        self.assertEqual(session_folder.session_folder("fedcba987"), self.root / "session-fedcba")

    def test_a_worker_name_starts_with_its_unit_name(self) -> None:
        unit = staff.StaffRow(staff.StaffId("u1"), "unit", "demo", staff.StaffRoot(), current_session=staff.StaffId("s1"))
        rows = (unit,
                staff.StaffRow(staff.StaffId("w1"), "worker", "impl", staff.ReportsTo(unit.id),
                               current_session=staff.StaffId("s2")),
                staff.StaffRow(staff.StaffId("w2"), "worker", "demo-test", staff.ReportsTo(unit.id),
                               current_session=staff.StaffId("s3")))
        record = staff.StaffSnapshot(rows)
        self.assertEqual([session_folder.member_for(row, record).name for row in rows], ["demo", "demo-impl", "demo-test"])

    def test_an_unsafe_member_name_is_cleaned(self) -> None:
        self.assertEqual(session_folder.folder_name(Member("m1", "../a b"), "x"), "a-b")
        self.assertEqual(session_folder.folder_name(Member("m1", "///"), "abcdefgh"), "session-abcdef")

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
