"""Test the session folder: its name, a live clash, the index link and its restore."""

import io
import os
import pwd
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.lib import conf, scratch, session_folder, session_move, unit_runs
from scripts.lib.session_folder import Member
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import session_records
from scripts.production import staff


class SessionFixtureIsolationTests(unittest.TestCase):
    def test_inherited_session_tmpdir_cannot_receive_test_writes(self) -> None:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        protected = (Path("/tmp/claude"), Path("/session"), home / "session", Path("/build"))
        inherited = "/session/queue"
        make_directory = os.mkdir

        def refuse_live_write(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            full_path = Path(path).resolve()
            self.assertFalse(any(full_path.is_relative_to(root) for root in protected),
                             f"test tried to create {full_path}")
            make_directory(path, mode, dir_fd=dir_fd)

        fixture = SessionFolderTests("test_natedev_creates_the_configured_root_on_first_use")
        self.addCleanup(fixture.doCleanups)
        with (patch.dict(os.environ, {"TMPDIR": inherited}),
              patch.object(tempfile, "tempdir", inherited),
              patch.object(os, "mkdir", side_effect=refuse_live_write)):
            fixture.setUp()
            fixture.test_natedev_creates_the_configured_root_on_first_use()


class SessionFolderTests(unittest.TestCase):
    root: Path = Path()
    former: Path = Path()
    previous: Path = Path()
    member: Member | None = None
    live: frozenset[str] = frozenset()

    @override
    def setUp(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp"))).resolve()
        self.root = base / "session"
        self.former = base / "former"
        self.previous = base / "previous"
        config = base / "config"
        _ = write_config_fixture(config / "build_folders.conf",
                                 f"roots.session={self.root}\nroots.session_former.natedev={self.previous}\n")
        environment = {session_folder.ROOT_ENV: str(self.root), session_folder.FORMER_ROOT_ENV: str(self.former),
                       session_folder.PREVIOUS_ROOT_ENV: str(self.previous), "CONF_DIR": str(config),
                       "TMPDIR": str(base)}
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment)))
        _ = os.environ.pop(session_folder.FOLDER_ENV, None)
        self.member = None
        self.live = frozenset()
        _ = cast(object, self.enterContext(patch.object(session_folder, "member_of", side_effect=self.member_of)))
        _ = cast(object, self.enterContext(patch.object(session_records, "session_liveness", side_effect=self.liveness)))

    def liveness(self, session_id: str) -> session_records.LiveSessionProcess | session_records.SessionNotLive:
        return session_records.LiveSessionProcess(1) if session_id in self.live else session_records.SessionNotLive()

    def member_of(self, session_id: str) -> Member | None:
        del session_id
        return self.member

    def previous_folder(self, session_id: str = "old-id") -> Path:
        folder = self.previous / "verify"
        folder.mkdir(parents=True)
        session_folder.write_record(folder, {"owner": "m1", "session_id": session_id})
        session_folder.link(self.previous, session_id, folder)
        return folder

    def test_natedev_creates_the_configured_root_on_first_use(self) -> None:
        with patch.dict(os.environ, {session_folder.ROOT_ENV: ""}):
            _ = os.environ.pop(session_folder.PREVIOUS_ROOT_ENV, None)
            with patch.object(conf, "_local_host_name", return_value="natedev"):
                self.assertEqual(session_folder.configured_root(), self.root)
                self.assertEqual(session_folder.launch_folder(Member("m1", "verify"), "new-id"), self.root / "verify")
                self.assertEqual(session_folder.configured_roots(), (self.root, self.previous))
            with patch.object(conf, "_local_host_name", return_value="mac"):
                self.assertEqual(session_folder.configured_roots(), (self.root,))
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_lookup_accepts_indexes_in_both_roots(self) -> None:
        self.root.mkdir()
        old = self.previous_folder()
        self.live = frozenset({"old-id"})
        new = session_folder.launch_folder(Member("m2", "builds"), "new-id")
        self.assertEqual(session_folder.session_folder("new-id"), new)
        self.assertEqual(session_folder.session_folder("old-id"), old)

    def test_session_mover_refuses_an_unreadable_hold_file(self) -> None:
        self.root.mkdir()
        base = self.root.parent
        hold_file = base / "holds.json"
        _ = hold_file.write_text('{"partial":', encoding="utf-8")
        roots = session_move.MoveRoots(self.root, self.former, base / "tmp", base / "scratch")
        with (patch.object(session_move.MoveRoots, "configured", return_value=roots),
              patch.dict(os.environ, {scratch.HOLDS_ENV: str(hold_file)}),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(session_move.main([]), 1)
        self.assertEqual(errors.getvalue(), f"session_move: cannot read scratch hold file {hold_file}\n")
        self.assertIn("moved 0 folders (0.00 GiB), kept 0 live, refused 1 other", output.getvalue())

    def test_session_mover_refuses_a_stale_index_and_keeps_the_source(self) -> None:
        target = self.root / "claimed"
        target.mkdir(parents=True)
        session_folder.write_record(target, {"owner": "m2", "session_id": "new-session"})
        session_folder.link(self.root, "old-session", target)
        relative = Path(f"claude-{os.getuid()}") / "-workspace" / "old-session"
        source = self.former / "owner" / relative
        source.mkdir(parents=True)
        draft = source / "draft.md"
        _ = draft.write_text("original scratchpad", encoding="utf-8")
        base = self.root.parent
        roots = session_move.MoveRoots(self.root, self.former, base / "tmp", base / "scratch")
        with (patch.object(session_move.MoveRoots, "configured", return_value=roots),
              patch.dict(os.environ, {scratch.HOLDS_ENV: str(base / "holds.json")}),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(session_records, "read_registry", return_value=session_records.RegistryRead((), ())),
              patch.object(session_move, "process_uses", return_value=()),
              patch.object(session_move, "start_copy", return_value=0) as start,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(session_move.main([]), 1)
        start.assert_not_called()
        self.assertEqual(errors.getvalue(),
                         f"session_move: kept {source}: {target}: session record does not name old-session\n")
        self.assertEqual(draft.read_text(encoding="utf-8"), "original scratchpad")
        self.assertFalse((target / relative).exists())

    def test_start_keeps_the_members_former_folder_for_a_staff_open_run(self) -> None:
        self.root.mkdir()
        old = self.previous_folder()
        run = old / "run" / "demo"
        run.mkdir(parents=True)
        owner = staff.StaffRow(staff.StaffId("m1"), "unit", "verify", staff.StaffRoot(),
                               current_session=staff.StaffId("old-id"), run=staff.OpenRun(run))
        with patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((owner,))):
            self.assertEqual(session_folder.launch_folder(Member("m1", "verify"), "new-id"), old)
        self.assertEqual(session_folder.folder_for(self.previous, "new-id"), old)
        self.assertFalse((self.root / "verify").exists())

    def test_start_keeps_the_members_former_folder_for_a_live_run(self) -> None:
        self.root.mkdir()
        old = self.previous_folder()
        heartbeat = old / "run" / "demo" / unit_runs.HEARTBEAT
        heartbeat.parent.mkdir(parents=True)
        heartbeat.touch()
        with patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())):
            self.assertEqual(session_folder.launch_folder(Member("m1", "verify"), "next"), old)
            past = time.time() - unit_runs.LIVE_HEARTBEAT_SECS - 10
            os.utime(heartbeat, (past, past))
            self.assertEqual(session_folder.launch_folder(Member("m1", "verify"), "later"), self.root / "verify")
        self.assertTrue(heartbeat.is_file())

    def test_no_open_run_at_start_leaves_the_former_folder_and_makes_a_new_one(self) -> None:
        self.root.mkdir()
        old = self.previous_folder()
        saved = old / "notes"
        _ = saved.write_text("keep")
        with (patch.dict(os.environ, {session_folder.ROOT_ENV: ""}),
              patch.object(conf, "_local_host_name", return_value="natedev"),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(()))):
            self.assertEqual(session_folder.launch_folder(Member("m1", "verify"), "old-id"), self.root / "verify")
        self.assertEqual(saved.read_text(), "keep")
        self.assertEqual(session_folder.folder_for(self.root, "old-id"), self.root / "verify")
        self.assertEqual(session_folder.folder_for(self.previous, "old-id"), old)

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
        self.assertFalse(os.path.lexists(self.root / session_folder.INDEX / "first"))
        self.assertEqual(second.stat().st_mode & 0o777, 0o700)

    def test_stale_index_cannot_give_a_live_folder_to_another_session(self) -> None:
        self.root.mkdir()
        self.previous.mkdir()
        current = self.root / "verify"
        previous = self.previous / "verify"
        for folder in (current, previous):
            folder.mkdir()
            session_folder.write_record(folder, {"owner": "m2", "session_id": "live"})
            session_folder.link(folder.parent, "resume", folder)
        self.live = frozenset({"live"})
        self.member = Member("m1", "verify")
        with patch.dict(os.environ, {session_folder.FOLDER_ENV: current.name, "TMPDIR": str(current)}):
            self.assertEqual(session_folder.session_folder("resume", starting=True), self.root / "verify-2")
        self.assertEqual(session_folder.read_record(current), {"owner": "m2", "session_id": "live"})
        self.assertEqual(session_folder.read_record(previous), {"owner": "m2", "session_id": "live"})
        session_folder.link(self.root, "resume", current)
        self.assertEqual(session_folder.launch_folder(self.member, "resume"), self.root / "verify-2")

    def test_start_preserves_a_numbered_current_folder_before_the_former_folder(self) -> None:
        self.root.mkdir()
        member = Member("m1", "verify")
        current = self.root / "verify-2"
        current.mkdir()
        session_folder.write_record(current, {"owner": member.id, "session_id": "ended"})
        former = self.previous_folder()
        run = former / "run" / "demo"
        run.mkdir(parents=True)
        (run / unit_runs.HEARTBEAT).touch()
        with patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())):
            self.assertEqual(session_folder.launch_folder(member, "next"), current)
        self.assertFalse((self.root / "verify").exists())

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

    def test_selector_and_launcher_choose_the_same_available_folder(self) -> None:
        self.root.mkdir()
        first = self.root / "verify"
        first.mkdir()
        record = session_folder.FolderRecord(owner="m1", session_id="live")
        session_folder.write_record(first, record)
        (self.root / "verify-2").symlink_to(first, target_is_directory=True)
        self.live = frozenset({"live"})
        before = sorted(self.root.iterdir())

        selected = session_folder.select_folder(self.root, "verify", "next")

        self.assertEqual(selected, self.root / "verify-3")
        self.assertEqual(sorted(self.root.iterdir()), before)
        self.assertEqual(session_folder.read_record(first), record)
        with patch.object(session_folder, "select_folder", wraps=session_folder.select_folder) as select:
            self.assertEqual(session_folder.launch_folder(Member("m2", "verify"), "next"), selected)
        select.assert_called_once_with(self.root, "verify", "next")

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

    def test_the_caller_folder_follows_the_codex_thread_then_the_claude_session(self) -> None:
        self.root.mkdir()
        self.member = Member("m1", "verify")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}):
            self.assertIsNone(session_folder.caller_folder())
            os.environ["CLAUDE_CODE_SESSION_ID"] = "claude-id"
            self.assertEqual(session_folder.caller_folder(), self.root / "verify")
            os.environ["CODEX_THREAD_ID"] = "codex-id"
            self.member = Member("m2", "demo-impl")
            self.assertEqual(session_folder.caller_folder(), self.root / "demo-impl")
        self.assertEqual(session_folder.folder_for(self.root, "codex-id"), self.root / "demo-impl")

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
