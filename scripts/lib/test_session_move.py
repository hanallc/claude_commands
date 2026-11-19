"""Check the cutover with private roots and one live process."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import ctypes
import io
import json
import os
from pathlib import Path
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import cast, override

from scripts.lib import copy_record, scratch, session_folder, session_move
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.transcript_test_support import compressed_copy
from scripts.lint.memory_test_support import config_environment
from scripts.message import session_records
from scripts.production import staff


class SessionMoveTests(unittest.TestCase):
    base: Path = Path()
    root: Path = Path()
    former: Path = Path()
    scratch_root: Path = Path()
    holds: Path = Path()
    claude: str = ""
    project: str = "-workspace"

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "session"
        self.former = self.base / "former-temp" / "claude" / "session"
        self.scratch_root = self.base / "scratch"
        self.holds = self.base / "holds.json"
        self.claude = f"claude-{os.getuid()}"
        for path in (self.root, self.former, self.scratch_root):
            path.mkdir(parents=True)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(self.base / "config"),
            session_folder.ROOT_ENV: str(self.root),
            session_folder.FORMER_ROOT_ENV: str(self.former),
            copy_record.ROOT_ENV: str(self.scratch_root),
            scratch.HOLDS_ENV: str(self.holds),
        })))
        _ = self.enterContext(mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())))
        _ = self.enterContext(mock.patch.object(session_records, "read_registry",
                                                return_value=session_records.RegistryRead((), ())))
        _ = self.enterContext(mock.patch.object(session_records, "config_dirs", return_value=[self.base / "claude-home"]))

    def index(self, session: str, folder: str) -> Path:
        destination = self.root / folder
        destination.mkdir(exist_ok=True)
        session_folder.link(self.root, session, destination)
        return destination

    def leaf(self, session: str, *, shared: bool = False) -> Path:
        parent = self.former.parent.parent if shared else self.former / "former-owner"
        leaf = parent / self.claude / self.project / session
        leaf.mkdir(parents=True)
        _ = (leaf / "draft.md").write_text("scratch content\n", encoding="utf-8")
        return leaf

    def invoke(self, *args: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            status = session_move.main(list(args))
        return status, output.getvalue(), errors.getvalue()

    def test_dry_run_prints_the_copy_and_changes_nothing(self) -> None:
        source = self.leaf("recorded")
        target = self.index("recorded", "demo")

        with mock.patch.object(tempfile, "TemporaryDirectory", side_effect=AssertionError("dry run wrote a preview")):
            status, output, errors = self.invoke("--dry-run")

        self.assertEqual(status, 0, errors)
        self.assertIn(f"copy 16 bytes {source} -> {target / self.claude / self.project / source.name} (.id index)", output)
        self.assertEqual(output.splitlines()[-1],
                         "session_move: would move 1 folders (0.00 GiB), kept 0 live, refused 0 other")
        self.assertTrue(source.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.root / session_folder.LOCK).exists())
        self.assertFalse(self.holds.with_name(self.holds.name + ".lock").exists())

    def test_open_holds_report_the_owner_and_keep_the_tree_until_release(self) -> None:
        source = self.leaf("held")
        target = self.index(source.name, "demo") / self.claude / self.project / source.name
        nested = source / "nested"
        former_scratch = self.scratch_root / "task-folder"
        for folder in (nested, former_scratch):
            folder.mkdir()
            _ = (folder / "note").write_bytes(b"hold contents")
        record = scratch.Hold(maker="holder", task="phase:demo/43g", reason="cutover", made=1.0, ended=0.0)
        scratch.write_holds({str(nested): record, str(former_scratch): record})
        before = self.holds.read_bytes()

        for args in (("--dry-run",), ()):
            with self.subTest(args=args):
                status, output, errors = self.invoke(*args)
                self.assertEqual(status, 1, errors)
                self.assertIn(f"{nested}: open scratch hold, owner holder", errors)
                self.assertIn(f"{former_scratch}: open scratch hold, owner holder", errors)
                self.assertEqual(errors.count("an unreleased hold keeps its folder behind"), 1)
                action = "would move" if args else "moved"
                self.assertEqual(output,
                                 f"session_move: {action} 0 folders (0.00 GiB), kept 0 live, refused 2 other\n")
                self.assertEqual(self.holds.read_bytes(), before)
                self.assertEqual((former_scratch / "note").read_bytes(), b"hold contents")
                self.assertEqual((nested / "note").read_bytes(), b"hold contents")
                self.assertFalse(target.exists())

        self.assertTrue(scratch.release(nested))
        self.assertTrue(scratch.release(former_scratch))
        status, _, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        self.assertFalse(source.exists())
        self.assertEqual((target / "nested" / "note").read_bytes(), b"hold contents")
        self.assertEqual((former_scratch / "note").read_bytes(), b"hold contents")

    def test_compressed_transcript_checks_the_decoder_before_using_the_first_absolute_cwd(self) -> None:
        source = self.leaf("compressed")
        transcript = self.base / "claude-home" / "projects" / self.project / "compressed.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(
            "".join(json.dumps({"cwd": str(cwd)}) + "\n"
                    for cwd in ("relative", self.base / "archive", self.base / "later")),
            encoding="utf-8",
        )
        archive = compressed_copy(transcript)

        with (mock.patch.object(tempfile, "tempdir", str(self.base)),
              mock.patch.object(tempfile, "TemporaryFile", wraps=tempfile.TemporaryFile) as temporary):
            status, output, errors = self.invoke("--dry-run")

        self.assertEqual(status, 0, errors)
        self.assertIn(f"{source} -> {self.root / 'archive' / self.claude / self.project / source.name}", output)
        temporary.assert_not_called()
        self.assertTrue(source.is_dir())
        self.assertEqual(list(self.root.iterdir()), [])

        with archive.open("ab") as output:
            _ = output.write(b"invalid trailing frame")
        for args in (("--dry-run",), ()):
            with self.subTest(args=args):
                status, output, errors = self.invoke(*args)
                self.assertEqual(status, 1)
                action = "would move" if args else "moved"
                self.assertEqual(output,
                                 f"session_move: {action} 0 folders (0.00 GiB), kept 0 live, refused 1 other\n")
                self.assertIn(f"{archive}: zstd decompression failed", errors)
                self.assertTrue(source.is_dir())
                self.assertFalse((self.root / "archive").exists())

    def test_copy_checks_and_deletes_the_source(self) -> None:
        source = self.leaf("old")
        (source / "empty").mkdir()
        (source / "link").symlink_to("draft.md")
        os.mkfifo(source / "queue", mode=0o600)
        if sys.platform == "linux":
            os.setxattr(source / "draft.md", "user.session-move", b"keep")
        before = session_move.tree_manifest(source)
        transcript = self.base / "claude-home" / "projects" / self.project / "old.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(json.dumps({"cwd": str(self.base / "my checkout")}) + "\n", encoding="utf-8")
        sibling = source.parent.parent / "unrelated"
        _ = sibling.write_text("keep", encoding="utf-8")

        status, output, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        target = self.root / "my-checkout" / self.claude / self.project / "old"
        self.assertIn(f"{source} -> {target} (transcript working folder)", output)
        self.assertFalse(source.exists())
        self.assertEqual(session_move.tree_manifest(target), before)
        self.assertEqual((target / "link").readlink(), Path("draft.md"))
        if sys.platform == "linux":
            self.assertEqual(os.getxattr(target / "draft.md", "user.session-move"), b"keep")
        self.assertTrue(stat.S_ISFIFO((target / "queue").lstat().st_mode))
        self.assertEqual(sibling.read_text(encoding="utf-8"), "keep")
        status, output, errors = self.invoke()
        self.assertEqual(status, 0, errors)
        self.assertEqual(output, "session_move: moved 0 folders (0.00 GiB), kept 0 live, refused 0 other\n")
        self.assertIn(str(sibling), errors)

    def test_failed_sha256_check_keeps_source_and_removes_stage(self) -> None:
        source = self.leaf("old")
        target = self.index("old", "demo")
        real_copy = shutil.copyfileobj

        def damaged_copy(original: io.BufferedReader, target: io.BufferedWriter) -> None:
            real_copy(original, target)
            _ = target.seek(0)
            _ = target.write(b"x" * 16)

        with mock.patch.object(shutil, "copyfileobj", side_effect=damaged_copy):
            status, _output, errors = self.invoke()

        self.assertEqual(status, 1)
        self.assertIn("copy check failed", errors)
        self.assertEqual((source / "draft.md").read_bytes(), b"scratch content\n")
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(list((self.root / ".session-move").iterdir()), [])

    def test_merge_preserves_the_existing_leaf_and_refuses_a_conflict(self) -> None:
        source = self.leaf("old", shared=True)
        target = self.index("old", "demo")
        project = target / self.claude / self.project
        untouched = project / "new"
        untouched.mkdir(parents=True)
        _ = (untouched / "draft.md").write_bytes(b"new")
        _ = (source.parent.parent / "unrelated").write_bytes(b"keep")

        status, _output, errors = self.invoke()
        self.assertEqual(status, 0, errors)
        self.assertEqual((project / "old" / "draft.md").read_bytes(), b"scratch content\n")
        self.assertEqual((untouched / "draft.md").read_bytes(), b"new")
        self.assertEqual((source.parent.parent / "unrelated").read_bytes(), b"keep")

        _ = self.leaf("old", shared=True)
        _ = (source / "draft.md").write_bytes(b"different")
        status, _output, errors = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn(f"destination conflict at {project / 'old'}", errors)
        self.assertIn(str(source), errors)
        self.assertEqual((source / "draft.md").read_bytes(), b"different")
        self.assertEqual((project / "old" / "draft.md").read_bytes(), b"scratch content\n")

    def test_live_cwd_or_open_file_keeps_source_until_process_ends(self) -> None:
        for condition in ("cwd", "open file"):
            with self.subTest(condition=condition):
                session = condition.replace(" ", "-")
                source = self.leaf(session)
                target = self.index(session, "demo") / self.claude / self.project / session
                command = ("import sys; "
                           + ("held = open(sys.argv[1], 'rb'); " if condition == "open file" else "")
                           + "print('ready', flush=True); sys.stdin.read()")
                with subprocess.Popen([sys.executable, "-c", command, str(source / "draft.md")],
                                      cwd=source if condition == "cwd" else self.base,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
                    assert child.stdout is not None and child.stdin is not None
                    try:
                        self.assertTrue(select.select([child.stdout], [], [], TEST_HANG_GUARD_SECS)[0])
                        self.assertEqual(child.stdout.readline(), b"ready\n")
                        status, output, errors = self.invoke()
                        self.assertEqual(status, 0, errors)
                        self.assertEqual(output,
                                         "session_move: moved 0 folders (0.00 GiB), kept 1 live, refused 0 other\n")
                        self.assertIn(f"process {child.pid}", errors)
                        self.assertTrue(source.exists())
                        self.assertFalse(target.exists())
                    finally:
                        child.stdin.close()
                status, _output, errors = self.invoke()
                self.assertEqual(status, 0, errors)
                self.assertFalse(source.exists())
                self.assertTrue(target.exists())

    def bind_fixture(self, source: Path, mount_point: Path) -> subprocess.CompletedProcess[str]:
        unshare, mount, shell = shutil.which("unshare"), shutil.which("mount"), shutil.which("sh")
        if sys.platform != "linux" or not unshare or not mount or not shell:
            self.skipTest("Linux bind mounts require unshare and mount")
        code = """
import sys
from unittest import mock
from scripts.lib import session_move
from scripts.message import session_records
from scripts.production import staff
with (mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
      mock.patch.object(session_records, "read_registry", return_value=session_records.RegistryRead((), ()))):
    sys.exit(session_move.main([]))
"""
        result = subprocess.run(
            [unshare, "--map-root-user", "--mount", "--propagation", "private",
             shell, "-c", '"$1" --bind "$2" "$3" && shift 3 && exec "$@"', "bind-fixture",
             mount, str(source), str(mount_point), sys.executable, "-c", code],
            capture_output=True, text=True,
            check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        if "unshare failed: Operation not permitted" in result.stderr:
            self.skipTest("the kernel does not permit private mount namespaces")
        return result

    def live_registry(self, session_id: str) -> session_records.RegistryRead:
        start = session_records.process_start(os.getpid())
        assert start is not None
        row: session_records.SessionRecord = {
            "pid": os.getpid(), "sessionId": session_id, "name": session_id, "messagingSocketPath": "",
            "updatedAt": 1, "tmux": "", "formerNames": [], "procStart": start,
        }
        return session_records.RegistryRead((session_records.RegistryObservation(row, {}),), ())

    def test_hold_through_a_bind_mount_keeps_the_source_tree(self) -> None:
        source = self.leaf("aliased")
        target = self.index(source.name, "demo") / self.claude / self.project / source.name
        nested = source / "nested"
        nested.mkdir()
        note = nested / "note"
        _ = note.write_bytes(b"held")
        alias = self.base / "held-alias"
        alias.mkdir()
        record = scratch.Hold(maker="holder", task="phase:demo/43g", reason="cutover", made=1.0, ended=0.0)
        scratch.write_holds({str(alias): record})

        result = self.bind_fixture(nested, alias)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"{alias}: open scratch hold, owner holder", result.stderr)
        self.assertEqual(note.read_bytes(), b"held")
        self.assertFalse(target.exists())

    def test_scratch_copy_reuses_the_stage_and_finishes_partial_deletion(self) -> None:
        source = self.leaf("old")
        target = self.index("old", "demo") / self.claude / self.project / "old"
        real_unlink = os.unlink
        _ = (source / "first").write_bytes(b"another file")
        real_library = ctypes.CDLL(None, use_errno=True)

        def stop_before_publication(_source: int, _entry: bytes, _target: int, _name: bytes, _flag: int) -> int:
            raise KeyboardInterrupt

        def interrupt_deletion(path: str | bytes | Path, *, dir_fd: int | None = None) -> None:
            if path == "first" and target.exists():
                raise KeyboardInterrupt
            real_unlink(path, dir_fd=dir_fd)

        with mock.patch.object(ctypes, "CDLL", return_value=mock.Mock(
                renameat2=stop_before_publication, renamex_np=stop_before_publication, renameatx_np=stop_before_publication)):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.invoke()
        self.assertTrue(source.is_dir())
        self.assertFalse(target.exists())

        with (mock.patch.object(ctypes, "CDLL", return_value=real_library),
              mock.patch.object(shutil, "copytree", side_effect=AssertionError("recopied the tree")),
              mock.patch.object(shutil, "copyfileobj", side_effect=AssertionError("recopied a file")),
              mock.patch.object(os, "unlink", side_effect=interrupt_deletion)):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.invoke()
        self.assertTrue((source / "first").is_file())
        self.assertFalse((source / "draft.md").exists())
        self.assertEqual((target / "draft.md").read_bytes(), b"scratch content\n")

        status, output, errors = self.invoke()
        self.assertEqual(status, 0, errors)
        self.assertEqual(output.splitlines()[-1],
                         "session_move: moved 1 folders (0.00 GiB), kept 0 live, refused 0 other")
        self.assertFalse(source.exists())
        self.assertEqual((target / "first").read_bytes(), b"another file")

    def test_retry_completes_after_a_stop_immediately_after_unlink(self) -> None:
        source = self.leaf("interrupted")
        target = self.index(source.name, "demo") / self.claude / self.project / source.name
        _ = (source / "remaining.md").write_bytes(b"keep")
        os.utime(source, (1, 1))
        original_mtime = source.stat().st_mtime_ns
        real_unlink = os.unlink

        def stop_after_unlink(path: str | bytes | Path, *, dir_fd: int | None = None) -> None:
            real_unlink(path, dir_fd=dir_fd)
            if path == "draft.md" and target.is_dir():
                raise KeyboardInterrupt

        with mock.patch.object(os, "unlink", side_effect=stop_after_unlink):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.invoke()

        self.assertFalse((source / "draft.md").exists())
        self.assertTrue((source / "remaining.md").is_file())
        self.assertNotEqual(source.stat().st_mtime_ns, original_mtime)
        self.assertTrue((target / "draft.md").is_file())

        status, _, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        self.assertFalse(source.exists())
        self.assertEqual((target / "draft.md").read_bytes(), b"scratch content\n")
        self.assertEqual((target / "remaining.md").read_bytes(), b"keep")

    def test_retry_reuses_stage_with_extra_attributes(self) -> None:
        if sys.platform != "linux":
            self.skipTest("this platform does not expose Linux extended attributes")
        source = self.leaf("attributes")
        target = self.index(source.name, "demo") / self.claude / self.project / source.name
        stage = self.root / ".session-move" / "staged"
        os.setxattr(source / "draft.md", "user.source", b"keep")
        copy = session_move.copy_tree

        def stop_after_copy(original: int, staged: int, existing: session_move.TreeManifest,
                            expected: session_move.TreeManifest, journal: int) -> None:
            copy(original, staged, existing, expected, journal)
            raise KeyboardInterrupt

        with mock.patch.object(session_move, "copy_tree", side_effect=stop_after_copy):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.invoke()
        os.setxattr(stage, "user.destination", b"folder")
        os.setxattr(stage / "draft.md", "user.destination", b"file")

        with (mock.patch.object(shutil, "copyfileobj", wraps=shutil.copyfileobj) as copied,
              mock.patch.object(session_move, "rename_exclusive", side_effect=KeyboardInterrupt)):
            try:
                status, _, errors = self.invoke()
            except KeyboardInterrupt:
                pass
            else:
                self.fail(f"the verified copy did not reach publication (status {status}): {errors}")
        copied.assert_not_called()
        self.assertFalse(target.exists())
        os.setxattr(stage, "user.after-verification", b"keep")

        status, _, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        self.assertFalse(source.exists())
        self.assertEqual((target / "draft.md").read_bytes(), b"scratch content\n")
        self.assertEqual(os.getxattr(target / "draft.md", "user.source"), b"keep")
        self.assertEqual(os.getxattr(target / "draft.md", "user.destination"), b"file")
        self.assertEqual(os.getxattr(target, "user.destination"), b"folder")
        self.assertEqual(os.getxattr(target, "user.after-verification"), b"keep")

    def test_idle_registered_session_keeps_its_scratchpad(self) -> None:
        source = self.leaf("idle")
        target = self.index("idle", "demo") / self.claude / self.project / "idle"
        movable = self.leaf("movable")
        moved = self.index(movable.name, "demo") / self.claude / self.project / movable.name

        with mock.patch.object(session_records, "read_registry", return_value=self.live_registry("idle")):
            status, output, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        self.assertIn("session idle is live", errors)
        self.assertTrue((source / "draft.md").exists())
        self.assertFalse(target.exists())
        self.assertFalse(movable.exists())
        self.assertEqual((moved / "draft.md").read_bytes(), b"scratch content\n")
        self.assertEqual(output.splitlines()[-1],
                         "session_move: moved 1 folders (0.00 GiB), kept 1 live, refused 0 other")
        status, _, errors = self.invoke()
        self.assertEqual(status, 0, errors)
        self.assertTrue(target.is_dir())

    def test_session_that_becomes_live_before_copy_keeps_the_next_move(self) -> None:
        source = self.leaf("becomes-live")
        target = self.index(source.name, "live")
        movable = self.leaf("movable")
        moved = self.index(movable.name, "demo") / self.claude / self.project / movable.name
        stopped = session_records.RegistryRead((), ())
        resumed = self.live_registry(source.name)
        before_first_copy = iter((stopped,))

        with mock.patch.object(session_records, "read_registry", side_effect=lambda: next(before_first_copy, resumed)):
            status, output, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        self.assertIn(f"session {source.name} is live", errors)
        self.assertEqual((source / "draft.md").read_bytes(), b"scratch content\n")
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(movable.exists())
        self.assertEqual((moved / "draft.md").read_bytes(), b"scratch content\n")
        self.assertEqual(output.splitlines()[-1],
                         "session_move: moved 1 folders (0.00 GiB), kept 1 live, refused 0 other")

    def test_session_that_resumes_during_verification_keeps_its_source(self) -> None:
        source = self.leaf("resuming")
        target = self.index("resuming", "demo") / self.claude / self.project / source.name
        movable = self.leaf("waiting")
        moved = self.index(movable.name, "demo") / self.claude / self.project / movable.name
        journal = self.root / session_move.JOURNAL / session_move.MOVE_RECORD
        source_info = source.stat()
        scan = session_move.read_manifest
        registry = session_records.RegistryRead((), ())
        resumed = self.live_registry(source.name)

        def resume_during_scan(fd: int, identities: session_move.EntryIdentities | None = None) -> session_move.TreeManifest:
            nonlocal registry
            manifest = scan(fd, identities)
            if target.exists() and os.path.samestat(source_info, os.fstat(fd)):
                registry = resumed
            return manifest

        with (mock.patch.object(session_records, "read_registry", side_effect=lambda: registry),
              mock.patch.object(session_move, "read_manifest", side_effect=resume_during_scan)):
            for phase in ("new copy", "journal recovery"):
                with self.subTest(phase=phase):
                    status, output, errors = self.invoke()

                    self.assertEqual(status, 1, errors)
                    self.assertIn(f"session {source.name} is live", errors)
                    self.assertTrue(journal.is_file())
                    self.assertEqual((source / "draft.md").read_bytes(), b"scratch content\n")
                    self.assertEqual((target / "draft.md").read_bytes(), b"scratch content\n")
                    self.assertTrue(movable.is_dir())
                    self.assertFalse(moved.exists())
                    self.assertEqual(output.splitlines()[-1],
                                     "session_move: moved 0 folders (0.00 GiB), kept 1 live, refused 0 other")

        status, _, errors = self.invoke()
        self.assertEqual(status, 0, errors)
        self.assertFalse(journal.exists())
        self.assertFalse(source.exists())
        self.assertFalse(movable.exists())
        self.assertEqual((moved / "draft.md").read_bytes(), b"scratch content\n")

    def test_mount_and_parent_swap_keep_data_in_the_intended_trees(self) -> None:
        with self.subTest("same-device bind mount"):
            source = self.leaf("mounted")
            target = self.index("mounted", "demo") / self.claude / self.project / "mounted"
            outside = self.base / "outside"
            outside.mkdir()
            _ = (outside / "keep").write_bytes(b"outside")
            (source / "nested").mkdir()

            result = self.bind_fixture(outside, source / "nested")

            try:
                self.assertEqual(result.returncode, 1)
                self.assertTrue((outside / "keep").is_file(), "the move deleted data through a bind mount")
                self.assertEqual((outside / "keep").read_bytes(), b"outside")
                self.assertIn("another mount", result.stderr)
                self.assertFalse(target.exists())
            finally:
                if source.exists():
                    shutil.rmtree(source)

        with self.subTest("parent replaced by symlink"):
            source = self.leaf("swap")
            target = self.index("swap", "demo") / self.claude / self.project / "swap"
            outside = self.base / "outside-swap"
            outside.mkdir()
            old_parent = target.parent.with_name("-saved")
            library = ctypes.CDLL(None, use_errno=True)

            def swap_parent(old_fd: int, old: bytes, new_fd: int, new: bytes, flags: int) -> int:
                _ = target.parent.rename(old_parent)
                target.parent.symlink_to(outside, target_is_directory=True)
                native = library.renameatx_np if sys.platform == "darwin" else library.renameat2
                return cast(int, native(old_fd, old, new_fd, new, flags))

            with mock.patch.object(ctypes, "CDLL", return_value=mock.Mock(renameat2=swap_parent, renameatx_np=swap_parent)):
                status, _, errors = self.invoke()

            self.assertEqual(status, 1, errors)
            self.assertEqual((source / "draft.md").read_bytes(), b"scratch content\n")
            self.assertEqual(list(outside.iterdir()), [])
            target.parent.unlink()
            _ = old_parent.rename(target.parent)
            status, _, errors = self.invoke()
            self.assertEqual(status, 0, errors)
            self.assertEqual((target / "draft.md").read_bytes(), b"scratch content\n")

    def test_uses_launcher_collision_rule_and_indexes_the_new_name(self) -> None:
        source = self.leaf("former")
        holder = staff.StaffId("former")
        current = staff.StaffId("current")
        row = staff.StaffRow(holder, "unit", "demo", staff.StaffRoot(), current_session=current)
        snapshot = staff.StaffSnapshot((row,))
        base = self.root / "demo"
        base.mkdir()
        record = session_folder.FolderRecord(owner=str(holder), session_id=str(current))
        session_folder.write_record(base, record)
        launcher_root = self.base / "launcher-root"
        launcher_root.mkdir()
        (launcher_root / base.name).mkdir()
        session_folder.write_record(launcher_root / base.name, record)

        with (mock.patch.object(staff, "local_snapshot", return_value=snapshot),
              mock.patch.object(session_records, "read_registry", return_value=self.live_registry(str(current)))):
            with mock.patch.dict(os.environ, {session_folder.ROOT_ENV: str(launcher_root)}):
                launcher = session_folder.launch_folder(session_folder.member_for(row, snapshot), str(holder))
            assert launcher is not None
            status, output, errors = self.invoke("--dry-run")
            self.assertEqual(status, 0, errors)
            self.assertIn(str(self.root / launcher.name), output)
            self.assertIsNone(session_folder.folder_for(self.root, str(holder)))
            status, _, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        selected = self.root / launcher.name
        self.assertEqual(selected.name, "demo-2")
        self.assertEqual(session_folder.folder_for(self.root, str(holder)), selected)
        self.assertEqual(session_folder.read_record(base), record)
        self.assertFalse(source.exists())
        self.assertTrue((selected / self.claude / self.project / source.name / "draft.md").exists())

    def test_reports_malformed_project_and_session_entries(self) -> None:
        source = self.leaf("candidate")
        _ = self.index("candidate", "demo")
        invalid_project = source.parent.with_name("-not-a-directory")
        _ = invalid_project.write_bytes(b"project")
        invalid_session = source.with_name("not.a.session")
        invalid_session.mkdir()

        status, _, errors = self.invoke("--dry-run")

        self.assertEqual(status, 1)
        self.assertIn(str(invalid_project), errors)
        self.assertIn(str(invalid_session), errors)
        self.assertEqual(invalid_project.read_bytes(), b"project")
        self.assertTrue(invalid_session.is_dir())

    def test_rechecks_the_destination_after_scanning_the_source(self) -> None:
        source = self.leaf("check")
        destination = self.index("check", "demo") / self.claude / self.project / source.name / "draft.md"
        original = source.stat()
        scan = session_move.read_manifest

        def remove_destination_during_source_scan(
            fd: int, identities: session_move.EntryIdentities | None = None,
        ) -> session_move.TreeManifest:
            manifest = scan(fd, identities)
            if os.path.samestat(original, os.fstat(fd)) and destination.exists():
                destination.unlink()
            return manifest

        with mock.patch.object(session_move, "read_manifest", side_effect=remove_destination_during_source_scan):
            status, _, errors = self.invoke()

        self.assertEqual(status, 1, errors)
        self.assertIn("published copy no longer matches", errors)
        self.assertEqual((source / "draft.md").read_bytes(), b"scratch content\n")

    def test_verified_stage_is_removed_if_the_source_changes(self) -> None:
        source = self.leaf("changed")
        target = self.index("changed", "demo") / self.claude / self.project / source.name

        with mock.patch.object(session_move, "rename_exclusive", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.invoke()
        journal = self.root / ".session-move"
        self.assertTrue((journal / "staged").is_dir())
        _ = (source / "draft.md").write_bytes(b"changed")

        status, _, errors = self.invoke()

        self.assertEqual(status, 1, errors)
        self.assertIn("changed before publication", errors)
        self.assertFalse((journal / "staged").exists())
        self.assertFalse((journal / "move.json").exists())
        self.assertEqual((source / "draft.md").read_bytes(), b"changed")
        status, _, errors = self.invoke()
        self.assertEqual(status, 0, errors)
        self.assertEqual((target / "draft.md").read_bytes(), b"changed")

    def test_stage_cleanup_recovers_after_the_stage_is_removed(self) -> None:
        source = self.leaf("cleanup")
        target = self.index(source.name, "demo") / self.claude / self.project / source.name
        journal = self.root / ".session-move"

        with mock.patch.object(session_move, "rename_exclusive", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.invoke()
        _ = (source / "draft.md").write_bytes(b"new data")
        with mock.patch.object(session_move, "clear_journal", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.invoke()
        self.assertFalse((journal / "staged").exists())
        self.assertTrue((journal / "move.json").is_file())
        self.assertFalse(target.exists())

        status, _, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        self.assertFalse(source.exists())
        self.assertEqual((target / "draft.md").read_bytes(), b"new data")

    def test_source_hard_links_allow_a_complete_move(self) -> None:
        source = self.leaf("hard-links")
        nested = source / "nested"
        nested.mkdir()
        os.link(source / "draft.md", nested / "alias.md")
        target = self.index(source.name, "demo") / self.claude / self.project / source.name

        status, _, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        self.assertFalse(source.exists())
        self.assertEqual((target / "draft.md").read_bytes(), b"scratch content\n")
        self.assertEqual((target / "nested" / "alias.md").read_bytes(), b"scratch content\n")

    def test_preserved_metadata_change_after_verification_keeps_the_source(self) -> None:
        cases = ("mode", "mtime", "xattr") if sys.platform == "linux" else ("mode", "mtime")
        remove = session_move.remove_tree
        for field in cases:
            with self.subTest(field=field):
                source = self.leaf(field)
                _ = self.index(field, "demo")
                file = source / "draft.md"
                if field == "xattr":
                    os.setxattr(file, "user.session-move", b"before")

                def change_before_deletion(parent: int, name: str, fd: int, mount: int,
                                           proof: session_move.CopyDeletion | None = None,
                                           relative: Path = session_move.TREE_ROOT) -> None:
                    if proof is not None and relative == session_move.TREE_ROOT:
                        info = file.stat()
                        if field == "mode":
                            file.chmod(stat.S_IMODE(info.st_mode) ^ stat.S_IXUSR)
                        elif field == "mtime":
                            os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
                        else:
                            os.setxattr(file, "user.session-move", b"after")
                    remove(parent, name, fd, mount, proof, relative)

                with mock.patch.object(session_move, "remove_tree", side_effect=change_before_deletion):
                    status, _, errors = self.invoke()

                try:
                    self.assertEqual(status, 1, errors)
                    self.assertIn("entry changed before deletion", errors)
                    self.assertEqual((source / "draft.md").read_bytes(), b"scratch content\n")
                finally:
                    if source.exists():
                        shutil.rmtree(source)
                    journal = self.root / ".session-move"
                    if journal.exists():
                        shutil.rmtree(journal)

    def test_recovery_syncs_the_source_parent_before_clearing_the_journal(self) -> None:
        source = self.leaf("removed")
        target = self.index(source.name, "demo") / self.claude / self.project / source.name
        real_sync = os.fsync
        real_clear = session_move.clear_journal

        with mock.patch.object(session_move, "clear_journal", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.invoke()
        self.assertFalse(source.exists())
        self.assertTrue(target.is_dir())

        source_parent = source.parent.stat()
        synced = False
        synced_at_clear = False

        def record_sync(fd: int) -> None:
            nonlocal synced
            synced |= os.path.samestat(os.fstat(fd), source_parent)
            real_sync(fd)

        def record_clear(fd: int) -> None:
            nonlocal synced_at_clear
            synced_at_clear = synced
            real_clear(fd)

        with (mock.patch.object(os, "fsync", side_effect=record_sync),
              mock.patch.object(session_move, "clear_journal", side_effect=record_clear)):
            status, _, errors = self.invoke()

        self.assertEqual(status, 0, errors)
        self.assertTrue(synced_at_clear)

    def test_refuses_a_regular_file_in_the_session_index(self) -> None:
        session_id = "blocked-index"
        source = self.leaf(session_id)
        member = staff.StaffId(session_id)
        row = staff.StaffRow(member, "unit", "demo", staff.StaffRoot(), current_session=member)
        index = self.root / session_folder.INDEX / session_id
        index.parent.mkdir()
        _ = index.write_bytes(b"keep existing entry")

        with mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))):
            status, _, errors = self.invoke()

        self.assertEqual(status, 1, errors)
        self.assertIn("expected a session index symlink", errors)
        self.assertFalse(index.is_symlink())
        self.assertEqual(index.read_bytes(), b"keep existing entry")
        self.assertTrue(source.is_dir())


if __name__ == "__main__":
    _ = unittest.main()
