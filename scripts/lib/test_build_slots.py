"""Repository slots retain work, reuse files, and enforce their locks."""
from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stderr
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
from typing import cast, override
import unittest
from unittest import mock

from scripts.lib import build_folders, build_slots
from scripts.lib.build_slots_test_support import private_slot_config
from scripts.lib.descendants import adopt_descendants
from scripts.lib.git_test_support import commit_repository
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

    def take_queue_copy(self) -> AbstractContextManager[build_slots.LeasedCopy]:
        return build_slots.take_copy(self.source, self.base, showrunner="builds", unit="queue",
                                     work="reuse", purpose="test")

    def rescue_refs(self) -> str:
        return self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/")

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
