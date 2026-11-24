"""Numbered copies retain work, reuse files, and enforce their locks."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from typing import cast, override
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.lib import build_folders, build_slots, copy_record, file_lock, git_rescue
from scripts.lib.build_slots_test_support import private_slot_config
from scripts.lib.descendants import adopt_descendants
from scripts.lib.git_test_support import commit_repository
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class BuildSlotsTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()
    base: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        configuration = private_slot_config(self.root, repository="widget")
        self.enterContext(mock.patch.dict(os.environ, configuration.environment))
        self.source = self.root / "source-folder"
        self.source.mkdir()
        _ = self.git(self.source, "init", "-q", "-b", "trunk")
        _ = self.git(self.source, "remote", "add", "origin", "git@example.invalid:group/widget.git")
        _ = (self.source / "same.txt").write_text("unchanged\n")
        _ = (self.source / "change.txt").write_text("first commit\n")
        _ = (self.source / ".gitignore").write_text("target/\n")
        self.base = commit_repository(self.source, "first")

    def git(self, folder: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(folder), *arguments], check=True, capture_output=True,
                              text=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        checkout = Path(__file__).resolve().parents[2]
        return subprocess.run([sys.executable, "-m", "scripts.lib.build_slots", *arguments],
                              cwd=checkout, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(checkout), "PYTHONSAFEPATH": "1"},
                              timeout=TEST_HANG_GUARD_SECS)

    def run_arguments(self, unit: str = "queue") -> tuple[str, ...]:
        return ("run", "--source", str(self.source), "--commit", self.base,
                "--showrunner", "builds", "--unit", unit, "--work", "proof", "--purpose", "test", "--")

    def test_assigned_copy_uses_only_its_number(self) -> None:
        def assigned():
            return build_slots.take_copy(self.source, self.base, showrunner='builds', unit='queue',
                work='assigned', purpose='test', selection=build_slots.ExactSlotRequest(2))

        first = build_folders.copy_path('widget', 'queue', 1)
        second = first.with_name('2')
        with assigned() as copy:
            self.assertEqual((copy.number, copy.path, copy.cargo_dir), (2, second, second / 'target'))
            self.assertFalse(first.exists())
            with self.assertRaisesRegex(BlockingIOError, 'assigned copy 2'), assigned():
                self.fail('The assigned copy admitted another holder')
        with assigned() as copy:
            self.assertEqual(copy.path, second)
        self.assertFalse(first.exists())

    def take_queue_copy(self, source: Path | None = None,
                        commit: str | None = None) -> AbstractContextManager[build_slots.LeasedCopy]:
        return build_slots.take_copy(self.source if source is None else source, self.base if commit is None else commit,
                                     showrunner="builds", unit="queue", work="reuse", purpose="test")

    def rescue_refs(self) -> str:
        return self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/")

    def lease(self, path: Path) -> dict[str, object]:
        return cast(dict[str, object], json.loads(path.with_name(path.name + ".lease.json").read_text()))

    def clone_source(self) -> tuple[Path, str]:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}):
            source = copies.clone(self.source, task="gate:clone-slot")
        _ = (source / "unpublished.txt").write_text("only in the clone\n")
        commit = commit_repository(source, "unpublished clone commit")
        return source, commit

    def commit_allow_list_ignore(self) -> None:
        _ = (self.source / ".gitignore").write_text(
            "*\n!.gitignore\n!*.txt\n!scripts/\n!scripts/**\n!src/\n!src/**\n**/__pycache__/\n")
        for module in ("scripts/tool.py", "src/lib.rs"):
            (self.source / module).parent.mkdir()
            _ = (self.source / module).write_text("source\n")
        self.base = commit_repository(self.source, "ignore all but the tracked paths")

    def test_free_copy_with_only_tool_output_folders_is_taken_again(self) -> None:
        self.commit_allow_list_ignore()
        notices = io.StringIO()
        _ = self.enterContext(redirect_stderr(notices))
        output = ("__pycache__/top.pyc", "scripts/__pycache__/tool.pyc", "target/keep")
        with self.take_queue_copy() as copy:
            path = copy.path
            for name in output:
                (path / name).parent.mkdir(exist_ok=True)
                _ = (path / name).write_text(f"{name} output\n")
        refs = self.rescue_refs()
        with self.take_queue_copy() as reused:
            self.assertEqual(reused.path, path)
            for name in output:
                self.assertEqual((path / name).read_text(), f"{name} output\n")
        self.assertEqual(self.rescue_refs(), refs)
        self.assertNotIn("untracked folders", notices.getvalue())

    def test_free_copy_with_an_untracked_folder_is_kept(self) -> None:
        self.commit_allow_list_ignore()
        exclude = self.source / ".git/info/exclude"
        exclude.parent.mkdir(exist_ok=True)
        with exclude.open("a") as output:
            _ = output.write("saved/\n")
        notices = io.StringIO()
        _ = self.enterContext(redirect_stderr(notices))
        for folder in ("notes", "saved", "src/target"):
            with self.subTest(folder=folder):
                with self.take_queue_copy() as copy:
                    path = copy.path
                    (path / folder).mkdir()
                    _ = (path / folder / "draft.txt").write_text("unsaved work\n")
                with self.take_queue_copy() as other:
                    self.assertNotEqual(other.path, path)
                self.assertIn(f"kept for a manual removal list: {path} (build_slots, untracked folders)",
                              notices.getvalue())
                self.assertEqual((path / folder / "draft.txt").read_text(), "unsaved work\n")

    def test_copy_from_a_clone_removes_its_private_ref_on_release_and_failure(self) -> None:
        source, commit = self.clone_source()
        with self.take_queue_copy() as first:
            path = first.path
        reference = "refs/build-slots/"

        def imported_commit() -> str:
            refs = self.git(self.source, "for-each-ref", "--format=%(refname)", reference).splitlines()
            self.assertEqual(len(refs), 1)
            self.assertTrue(refs[0].startswith(reference + "take-"))
            return self.git(self.source, "rev-parse", refs[0])

        for ending in ("release", "caller failure", "checkout failure", "fetch failure"):
            with self.subTest(ending=ending):
                # Restore the copy's HEAD so every row starts from the source history.
                with self.take_queue_copy():
                    pass
                original = cast(Callable[..., str], getattr(build_slots, "_git"))

                def fail_git(folder: Path, *arguments: str, pass_fds: Sequence[int] = (),
                             accepted: tuple[int, ...] = (0,)) -> str:
                    result = original(folder, *arguments, pass_fds=pass_fds, accepted=accepted)
                    if ((ending == "checkout failure" and "checkout" in arguments)
                            or (ending == "fetch failure" and "fetch" in arguments)):
                        raise build_slots.SlotGitFailure("injected Git failure")
                    return result

                with mock.patch.object(build_slots, "_git", side_effect=fail_git):
                    if ending in ("checkout failure", "fetch failure"):
                        with self.assertRaisesRegex(build_slots.SlotGitFailure, "injected Git failure"):
                            with self.take_queue_copy(source, commit):
                                self.fail("Git failure reached the caller")
                    elif ending == "caller failure":
                        with self.assertRaisesRegex(RuntimeError, "caller failed"):
                            with self.take_queue_copy(source, commit):
                                self.assertEqual(imported_commit(), commit)
                                raise RuntimeError("caller failed")
                    else:
                        with self.take_queue_copy(source, commit):
                            self.assertEqual(imported_commit(), commit)
                self.assertEqual(self.git(self.source, "for-each-ref", reference), "")
                self.assertEqual(build_folders.build_folder_state(path), "free")

    def test_copy_from_a_clone_without_shared_history_is_refused_after_fetch(self) -> None:
        source, _ = self.clone_source()
        _ = self.git(source, "checkout", "--orphan", "unrelated")
        _ = self.git(source, "rm", "-rf", ".")
        _ = (source / "foreign.txt").write_text("foreign history\n")
        commit = commit_repository(source, "unrelated root")
        with self.take_queue_copy() as first:
            path = first.path
        before = self.git(path, "status", "--porcelain=v2")
        original = cast(Callable[..., str], getattr(build_slots, "_git"))
        fetched: list[str] = []
        references: list[str] = []
        comparisons: list[tuple[str, ...]] = []

        def observe_git(folder: Path, *arguments: str, pass_fds: Sequence[int] = (),
                        accepted: tuple[int, ...] = (0,)) -> str:
            result = original(folder, *arguments, pass_fds=pass_fds, accepted=accepted)
            if "fetch" in arguments:
                reference = arguments[-1].partition(":")[2]
                references.append(reference)
                fetched.append(self.git(folder, "rev-parse", reference))
            if arguments[:1] == ("merge-base",):
                comparisons.append(arguments)
            return result

        with mock.patch.object(build_slots, "_git", side_effect=observe_git):
            with self.assertRaises(build_slots.SlotRepositoryMismatch):
                with self.take_queue_copy(source, commit):
                    self.fail("the unrelated repository received a copy")
        self.assertEqual(fetched, [commit])
        self.assertEqual(comparisons, [("merge-base", references[0], self.base)])
        self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(path, "status", "--porcelain=v2"), before)
        self.assertEqual(self.git(self.source, "for-each-ref", "refs/build-slots/"), "")
        self.assertFalse((path / "foreign.txt").exists())

    def test_clone_creates_a_copy_in_its_persistent_source_repository(self) -> None:
        source, commit = self.clone_source()
        session_copy = self.root / "private-session-copy"
        _ = source.rename(session_copy)
        source = session_copy
        with self.take_queue_copy(source, commit) as copy:
            path = copy.path
            self.assertEqual(self.git(path, "rev-parse", "--git-common-dir"), str(self.source / ".git"))
            self.assertEqual(self.git(path, "rev-parse", "HEAD"), commit)
            self.assertEqual(self.lease(path)["commit"], commit)
        self.assertEqual(self.lease(path)["commit"], commit)
        shutil.rmtree(source)
        with self.take_queue_copy() as reused:
            self.assertEqual(reused.path, path)
            self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.base)

    def test_copy_refuses_stale_or_cyclic_copy_records(self) -> None:
        for outcome in ("stale", "cyclic"):
            with self.subTest(outcome=outcome):
                source, commit = self.clone_source()
                record = source / copy_record.RECORD
                fields = cast(dict[str, object], json.loads(record.read_text()))
                fields["source_path"] = str(self.root / "missing" if outcome == "stale" else source)
                _ = record.write_text(json.dumps(fields))
                reason = "no available persistent source" if outcome == "stale" else "copy sources form a cycle"
                with self.assertRaisesRegex(build_slots.SlotGitFailure, reason):
                    with self.take_queue_copy(source, commit):
                        self.fail("The invalid copy record created a copy.")

    def test_two_copies_share_one_git_config_migration(self) -> None:
        common = self.source / ".git"
        lock_path = common / "build-copy-config.lock"
        first_read, resume_first = threading.Event(), threading.Event()
        git = cast(Callable[..., str], getattr(build_slots, "_git"))
        flock = fcntl.flock
        first_copy = build_folders.copy_path("widget", "queue", 1)

        def pause_after_read(folder: Path, *arguments: str, pass_fds: Sequence[int] = (),
                             accepted: tuple[int, ...] = (0,)) -> str:
            result = git(folder, *arguments, pass_fds=pass_fds, accepted=accepted)
            if folder == first_copy and arguments == ("config", "--local", "--get", "core.bare"):
                first_read.set()
                self.assertTrue(resume_first.wait(TEST_HANG_GUARD_SECS), "The second caller did not reach the migration.")
            return result

        def observe_contention(descriptor: int, mode: int) -> None:
            # Resume the first caller when the real lock makes the second caller wait.
            if mode == fcntl.LOCK_EX and lock_path.exists() and os.path.samestat(os.fstat(descriptor), lock_path.stat()):
                try:
                    flock(descriptor, mode | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    resume_first.set()
            flock(descriptor, mode)

        def take(number: int) -> Path:
            with build_slots.take_copy(self.source, self.base, showrunner="builds", unit="queue",
                    work="proof", purpose="test", selection=build_slots.ExactSlotRequest(number)) as copy:
                return copy.path

        with (mock.patch.object(build_slots, "_git", side_effect=pause_after_read),
              mock.patch.object(fcntl, "flock", new=observe_contention),
              ThreadPoolExecutor(max_workers=1) as worker):
            first = worker.submit(take, 1)
            try:
                self.assertTrue(first_read.wait(TEST_HANG_GUARD_SECS), "The first caller did not read core.bare.")
                second_path = take(2)
            finally:
                resume_first.set()
            first_path = first.result(timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual((first_path, second_path), (first_copy, first_copy.with_name("2")))
        self.assertEqual(self.git(self.source, "config", "--local", "--get-all", "extensions.worktreeConfig"), "true")
        self.assertNotIn("core.bare=", self.git(self.source, "config", "--local", "--list"))
        self.assertEqual(self.git(self.source, "config", "--file", str(common / "config.worktree"),
                                  "--get-all", "core.bare"), "false")

    def test_git_inherits_the_shared_lock_during_copy_config_migration(self) -> None:
        lock_path = self.source / ".git/build-copy-config.lock"
        git = cast(Callable[..., str], getattr(build_slots, "_git"))
        locked_commands: list[tuple[str, ...]] = []

        def observe_git(folder: Path, *arguments: str, pass_fds: Sequence[int] = (),
                        accepted: tuple[int, ...] = (0,)) -> str:
            if lock_path.exists():
                contender = file_lock.take_exclusive(lock_path)
                if isinstance(contender, file_lock.ExclusiveLock):
                    contender.close()
                else:
                    lock_stat = lock_path.stat()
                    self.assertTrue(any(os.path.samestat(os.fstat(fd), lock_stat) for fd in pass_fds),
                                    f"Git did not inherit the shared lock: {arguments}")
                    locked_commands.append(arguments)
            return git(folder, *arguments, pass_fds=pass_fds, accepted=accepted)

        with mock.patch.object(build_slots, "_git", side_effect=observe_git), self.take_queue_copy():
            pass

        self.assertIn(("config", "--local", "--unset", "core.bare"), locked_commands)
        self.assertIn(("config", "--local", "extensions.worktreeConfig", "true"), locked_commands)

    def test_build_output_keeps_new_and_reused_copies_clean_without_a_tracked_ignore_file(self) -> None:
        (self.source / ".gitignore").unlink()
        self.base = commit_repository(self.source, "no tracked ignore file")
        for use in ("new", "reused"):
            with self.subTest(use=use), self.take_queue_copy() as copy:
                _ = (copy.cargo_dir / "build-output").write_text("compiled output\n")
                self.assertEqual(self.git(copy.path, "status", "--porcelain"), "")
        (self.source / "target").mkdir()
        _ = (self.source / "target/build-output").write_text("source build output\n")
        self.assertEqual(self.git(self.source, "status", "--porcelain"), "?? target/")

    def test_copy_rescue_failure_preserves_source_and_output_and_frees_the_lock(self) -> None:
        with self.take_queue_copy() as copy:
            path = copy.path
            _ = (path / "change.txt").write_text("do not lose this edit\n")
            _ = (copy.cargo_dir / "keep").write_text("cached output\n")
        error = git_rescue.RescueError(("git", "update-ref"), "fixture write failure")
        with mock.patch.object(git_rescue, "rescue", side_effect=error) as rescue:
            with self.assertRaises(git_rescue.RescueError), self.take_queue_copy():
                self.fail("reset continued after a failed rescue")
            rescue.assert_called_once()
        self.assertEqual((path / "change.txt").read_text(), "do not lose this edit\n")
        self.assertEqual((path / "target/keep").read_text(), "cached output\n")
        self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.base)
        self.assertEqual(build_folders.build_folder_state(path), "free")

    def test_a_damaged_copy_index_and_source_edits_restore_from_the_rescue_before_reset(self) -> None:
        for number, damaged in enumerate((b"damaged index\x00\xff", b"DIRC\x00\x00\x00\xff")):
            with self.subTest(index=damaged):
                before = set(self.rescue_refs().splitlines())
                with self.take_queue_copy() as copy:
                    index = Path(self.git(copy.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
                    _ = (copy.path / "change.txt").write_text("source edit before reset\n")
                    _ = (copy.path / "untracked.txt").write_text("untracked source\n")
                    _ = (copy.cargo_dir / "keep").write_text("cached build output\n")
                    _ = index.write_bytes(damaged)
                with self.take_queue_copy() as recovered:
                    self.assertEqual(self.git(recovered.path, "status", "--porcelain"), "")
                    self.assertEqual((recovered.path / "change.txt").read_text(), "first commit\n")
                    self.assertEqual((recovered.cargo_dir / "keep").read_text(), "cached build output\n")
                after = set(self.rescue_refs().splitlines())
                self.assertEqual(len(after - before), 1)
                saved, = after - before
                restored = self.root / f"restored-{number}"
                _ = self.git(self.source, "worktree", "add", "--detach", str(restored), saved)
                self.assertEqual((restored / "change.txt").read_text(), "source edit before reset\n")
                self.assertEqual((restored / "untracked.txt").read_text(), "untracked source\n")
                backups = tuple(restored.glob(".rescue-index-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), damaged)

    def test_copy_size_limit_deletes_output_after_rescue_and_retains_source_times(self) -> None:
        with self.take_queue_copy() as copy:
            path = copy.path
            _ = (copy.cargo_dir / "final").write_bytes(b"x" * 256)
            _ = (path / "change.txt").write_text("save this source\n")
            source_stat = (path / "same.txt").stat()
        output = io.StringIO()
        with redirect_stdout(output), self.take_queue_copy() as reused:
            build_slots.limit_copy_size(reused, 64)
            self.assertEqual(reused.path, path)
            self.assertFalse((reused.cargo_dir / "final").exists())
            self.assertTrue(reused.cargo_dir.is_dir())
            current = (path / "same.txt").stat()
            self.assertEqual((current.st_ino, current.st_mtime_ns), (source_stat.st_ino, source_stat.st_mtime_ns))
            self.assertEqual((path / "change.txt").read_text(), "first commit\n")
        refs = self.rescue_refs().splitlines()
        self.assertEqual(len(refs), 1)
        self.assertEqual(self.git(self.source, "show", refs[0] + ":change.txt"), "save this source")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn(str(path), output.getvalue())

    def test_assigned_copy_recovers_cargo_reset_after_sigkill(self) -> None:
        self.assert_cargo_reset_recovers(self.interrupt_cargo_reset())

    def test_assigned_copy_recovers_cargo_reset_under_a_linked_build_root(self) -> None:
        real_root = self.root / "real-build"
        real_root.mkdir()
        build_root = build_folders.build_root()
        build_root.symlink_to(real_root, target_is_directory=True)
        path = self.interrupt_cargo_reset()
        self.assertTrue(path.is_relative_to(build_root))
        self.assertTrue(path.resolve().is_relative_to(real_root))

        lock_path = path / "target.lock"
        with lock_path.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (self.assertRaisesRegex(BlockingIOError, "assigned copy 2"),
                  build_slots.take_copy(self.source, self.base, showrunner="builds", unit="queue",
                                        work="next", purpose="test", selection=build_slots.ExactSlotRequest(2))):
                self.fail("The assigned copy admitted a job while the target lock was held")
            self.assertTrue(os.path.samestat(lock_path.stat(), os.fstat(lock.fileno())))
            self.assertIn("cargo_removal", self.lease(path))

        self.assert_cargo_reset_recovers(path)

    def interrupt_cargo_reset(self) -> Path:
        reset = """
import os
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from scripts.lib import build_folders, build_slots

rename = build_folders._rename_without_replacing

def stop_after_staging(directory, source, destination):
    rename(directory, source, destination)
    if source == "target" and destination.startswith(".removing-"):
        os.kill(os.getpid(), signal.SIGKILL)

with build_slots.take_copy(Path(sys.argv[1]), sys.argv[2], showrunner="builds", unit="queue",
                           work="reset", purpose="test", selection=build_slots.ExactSlotRequest(2)) as copy:
    (copy.cargo_dir / "old").write_text("old Cargo output")
    with patch.object(build_folders, "_rename_without_replacing", stop_after_staging):
        build_slots.limit_copy_size(copy, 0)
"""
        checkout = Path(__file__).resolve().parents[2]
        stopped = subprocess.run([sys.executable, "-c", reset, str(self.source), self.base],
                                 cwd=checkout, env={**os.environ, "PYTHONPATH": str(checkout)},
                                 capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(stopped.returncode, -signal.SIGKILL, stopped.stderr)
        path = build_folders.copy_path("widget", "queue", 2)
        stage, = (entry for entry in path.glob(".removing-*") if entry.is_dir())
        identity = stage.stat()
        self.assertEqual(self.lease(path).get("cargo_removal"),
                         {"removal_stage": stage.name, "copy_device": identity.st_dev, "copy_inode": identity.st_ino})
        self.assertEqual((stage / "old").read_text(), "old Cargo output")
        self.assertFalse((path / "target").exists())
        return path

    def assert_cargo_reset_recovers(self, path: Path) -> None:
        with build_slots.take_copy(self.source, self.base, showrunner="builds", unit="queue",
                                  work="next", purpose="test", selection=build_slots.ExactSlotRequest(2)) as copy:
            self.assertEqual(copy.path, path)
            self.assertEqual(list(copy.cargo_dir.iterdir()), [])
            self.assertEqual(list(path.glob(".removing-*")), [])
            self.assertFalse((path / "target.lock").exists())
            self.assertNotIn("cargo_removal", self.lease(path))
        self.assertEqual(self.rescue_refs(), "")

    def test_run_gives_the_child_the_cargo_environment_and_lock_and_keeps_its_exit_code(self) -> None:
        checkout = Path(__file__).resolve().parents[2]
        probe = f"import sys\nsys.path.insert(0, {str(checkout)!r})\n" + """import fcntl, json, os, pathlib
from scripts.lib import build_folders
path = pathlib.Path.cwd()
descriptor = int(os.environ['BUILD_FOLDER_LOCK_FD'])
assert descriptor == int(os.environ[build_folders.NUMBERED_COPY_LOCK_ENV])
lock = path.with_name(path.name + '.lock')
assert os.path.samestat(os.fstat(descriptor), lock.stat())
with build_folders.numbered_copy_use_lease(path) as child:
    assert isinstance(child, build_folders.NumberedCopyUseLease)
    assert child.source.path == path
    assert os.path.samestat(os.fstat(child.pass_fds[0]), lock.stat())
with lock.open('r+') as competing:
    try:
        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        held = True
    else:
        held = False
print(json.dumps({'path': str(path), 'cargo': os.environ['CARGO_BUILD_BUILD_DIR'], 'held': held}))
sys.exit(7)
"""
        result = self.command(*self.run_arguments(), sys.executable, "-c", probe)

        self.assertEqual(result.returncode, 7, result.stderr)
        report = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(report, {"path": str(build_folders.copy_path("widget", "queue", 1)),
                                  "cargo": str(build_folders.copy_path("widget", "queue", 1) / "target"), "held": True})
        path = build_folders.copy_path("widget", "queue", 1)
        self.assertTrue(path.is_dir())
        self.assertEqual(build_folders.build_folder_state(path), "free")

    @unittest.skipUnless(sys.platform == "linux", "descendant adoption requires Linux")
    def test_run_stops_a_detached_descendant_before_it_releases_the_slot(self) -> None:
        probe = """import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'],
                         close_fds=True, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid, flush=True)
"""
        result = self.command(*self.run_arguments(), sys.executable, "-c", probe)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        descendant = int(result.stdout.strip())
        try:
            with self.assertRaises(ProcessLookupError):
                os.kill(descendant, 0)
            with build_slots.take_copy(self.source, "HEAD", showrunner="builds", unit="queue", work="proof", purpose="test") as reused:
                self.assertEqual(reused.path, build_folders.copy_path("widget", "queue", 1))
        finally:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @unittest.skipUnless(sys.platform == "linux", "the Mac path runs with Linux orphan reaping")
    def test_mac_run_stops_its_background_group_before_it_releases_the_copy(self) -> None:
        wrapper = """import os, pathlib, sys
from unittest import mock
from scripts.lib import build_folders, build_slots
from scripts.lib.descendants import adopt_descendants
adopt_descendants()
with build_slots.take_copy(pathlib.Path(sys.argv[1]), sys.argv[2], showrunner='builds',
                           unit='queue', work='proof', purpose='test') as copy:
    middle = os.fork()
    if middle == 0:
        with mock.patch.object(sys, 'platform', 'darwin'):
            status = build_folders.run_build_command(sys.argv[3:], path=copy.path,
                environment={**os.environ, **copy.env}, pass_fds=copy.pass_fds)
        os._exit(status)
    while True:
        reaped, status = os.wait()
        if reaped == middle:
            status = os.waitstatus_to_exitcode(status)
            break
raise SystemExit(status)
"""
        probe = """import fcntl, json, os, pathlib, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'],
                         close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
path = pathlib.Path.cwd()
with path.with_name(path.name + '.lock').open('r+') as contender:
    try:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        held = True
    else:
        held = False
print(json.dumps({'descendant': child.pid, 'group': os.getpgrp(), 'held': held}), flush=True)
"""
        checkout = Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, "-c", wrapper, str(self.source), self.base, sys.executable, "-c", probe],
                                cwd=checkout, env={**os.environ, "PYTHONPATH": str(checkout)},
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = cast(dict[str, int | bool], json.loads(result.stdout))
        self.assertTrue(report["held"])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(report["descendant"]), 0)
        with self.assertRaises(ProcessLookupError):
            send_process_group(int(report["group"]), 0)
        with self.take_queue_copy() as reused:
            self.assertEqual(reused.path, build_folders.copy_path("widget", "queue", 1))

    @unittest.skipUnless(sys.platform == "linux", "the test adopts the orphaned Git process")
    def test_git_keeps_the_slot_locked_after_its_caller_dies(self) -> None:
        _ = adopt_descendants()
        real_git = shutil.which("git")
        assert real_git is not None
        binaries = self.root / "bin"
        binaries.mkdir()
        fake = binaries / "git"
        _ = fake.write_text(f"#!{sys.executable}\n" + """import json, os, socket, sys
arguments = sys.argv[1:]
command = ' '.join(arguments[2:4]) if arguments[2] == 'worktree' else arguments[2]
if command == os.environ['SLOT_GIT_OPERATION']:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.connect('\\0' + os.environ['SLOT_GIT_SOCKET'])
        peer.sendall((json.dumps({'pid': os.getpid(), 'command': command}) + '\\n').encode())
        peer.recv(1)
    raise SystemExit(0)
os.execv(os.environ['SLOT_REAL_GIT'], [os.environ['SLOT_REAL_GIT'], *arguments])
""")
        fake.chmod(0o755)
        checkout = Path(__file__).resolve().parents[2]
        for operation in ("worktree add", "checkout", "clean"):
            with self.subTest(operation=operation):
                unit = operation.replace(" ", "-")
                path = build_folders.copy_path("widget", unit, 1)
                if operation != "worktree add":
                    with build_slots.take_copy(
                            self.source, self.base, showrunner="builds", unit=unit, work="proof", purpose="test") as copy:
                        self.assertEqual(copy.path, path)
                address = f"slot-git-{os.getpid()}"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.settimeout(TEST_HANG_GUARD_SECS)
                    listener.bind("\0" + address)
                    listener.listen(1)
                    arguments = (*self.run_arguments(unit), sys.executable, "-c", "pass")
                    process = subprocess.Popen([sys.executable, "-m", "scripts.lib.build_slots", *arguments],
                                               cwd=checkout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                               env={**os.environ, "PYTHONPATH": str(checkout),
                                                    "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                                                    "SLOT_REAL_GIT": real_git, "SLOT_GIT_OPERATION": operation,
                                                    "SLOT_GIT_SOCKET": address})
                    try:
                        peer = listener.accept()[0]
                        with peer, peer.makefile("r") as report:
                            record = cast(dict[str, str | int], json.loads(report.readline()))
                            git_pid = int(record["pid"])
                            self.assertEqual(record["command"], operation)
                            process.kill()
                            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
                            try:
                                self.assertEqual(build_folders.build_folder_state(path), "held")
                                with build_slots.take_copy(
                                        self.source, self.base, showrunner="builds", unit=unit,
                                        work="competing", purpose="test") as competing:
                                    self.assertEqual(competing.number, 2)
                            finally:
                                peer.sendall(b"x")
                                _ = os.waitpid(git_pid, 0)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(build_folders.build_folder_state(path), "free")


if __name__ == "__main__":
    _ = unittest.main()
