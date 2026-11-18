"""The Git file reader gives the answers Git gives."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override

from scripts.lib import git_files


def git(folder: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(folder), *arguments], capture_output=True, text=True,
                          check=True).stdout.strip()


class GitFilesTests(unittest.TestCase):
    root: Path = Path()
    main: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.main = self.root / "main"
        self.main.mkdir()
        _ = git(self.main, "init", "-q", "-b", "trunk")
        _ = git(self.main, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                "commit", "-q", "--allow-empty", "-m", "start")

    def assert_same_as_git(self, folder: Path) -> None:
        checkout = git_files.locate(folder)
        self.assertEqual(checkout.toplevel, Path(git(folder, "rev-parse", "--show-toplevel")))
        self.assertEqual(checkout.git_dir, Path(git(folder, "rev-parse", "--absolute-git-dir")))
        self.assertEqual(checkout.common_dir,
                         Path(git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")))
        self.assertEqual(git_files.current_branch(folder), git(folder, "branch", "--show-current"))

    def test_main_checkout_and_its_subfolder(self) -> None:
        (self.main / "sub").mkdir()
        for folder in (self.main, self.main / "sub"):
            with self.subTest(folder=folder):
                self.assert_same_as_git(folder)
        self.assertEqual(git_files.current_branch(self.main), "trunk")

    def test_worktree_file_names_git_dir_and_common_dir(self) -> None:
        worktree = self.root / "linked"
        _ = git(self.main, "worktree", "add", "-q", "-b", "side", str(worktree))
        self.assertTrue((worktree / ".git").is_file())
        self.assert_same_as_git(worktree)
        checkout = git_files.locate(worktree)
        self.assertEqual(checkout.common_dir, self.main / ".git")
        self.assertEqual(checkout.git_dir.parent, self.main / ".git" / "worktrees")
        self.assertEqual(git_files.current_branch(worktree), "side")

    def test_moved_worktree_reads_the_new_folder(self) -> None:
        worktree = self.root / "before"
        _ = git(self.main, "worktree", "add", "-q", "-b", "side", str(worktree))
        moved = self.root / "after"
        _ = git(self.main, "worktree", "move", str(worktree), str(moved))
        self.assert_same_as_git(moved)

    def test_detached_head_has_no_branch(self) -> None:
        worktree = self.root / "detached"
        _ = git(self.main, "worktree", "add", "-q", "--detach", str(worktree))
        _ = git(self.main, "switch", "-q", "--detach")
        for folder in (self.main, worktree):
            with self.subTest(folder=folder):
                self.assert_same_as_git(folder)
                self.assertEqual(git_files.current_branch(folder), "")

    def test_folder_outside_a_checkout_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        for folder in (outside, self.root / "missing"):
            with self.subTest(folder=folder), self.assertRaises(git_files.NotACheckout):
                _ = git_files.locate(folder)
        with self.assertRaises(git_files.NotACheckout):
            _ = git_files.current_branch(outside)

    def test_worktree_file_with_a_lost_git_dir_is_refused(self) -> None:
        worktree = self.root / "lost"
        _ = git(self.main, "worktree", "add", "-q", "-b", "side", str(worktree))
        _ = (worktree / ".git").write_text("gitdir: /nonexistent/worktrees/lost\n", encoding="utf-8")
        with self.assertRaises(git_files.UnreadableGitFiles) as raised:
            _ = git_files.locate(worktree)
        self.assertEqual(raised.exception.path, worktree / ".git")

    def test_only_a_missing_gitdir_target_has_the_missing_result(self) -> None:
        worktree = self.root / "linked"
        worktree.mkdir()
        target = self.root / "target"
        pointer = worktree / ".git"
        _ = pointer.write_text(f"gitdir: {target}\n", encoding="utf-8")
        with self.assertRaises(git_files.MissingReferencedGitDirectory) as raised:
            _ = git_files.at(worktree)
        self.assertEqual(raised.exception.path, pointer)

        target.touch()
        with self.assertRaises(git_files.UnreadableGitFiles) as raised:
            _ = git_files.at(worktree)
        self.assertIs(type(raised.exception), git_files.UnreadableGitFiles)

    def test_at_reads_only_the_given_folder(self) -> None:
        (self.main / "sub").mkdir()
        self.assertEqual(git_files.at(self.main), git_files.locate(self.main / "sub"))
        with self.assertRaises(git_files.NotACheckout):
            _ = git_files.at(self.main / "sub")

    def test_head_names_the_branch_or_the_detached_commit(self) -> None:
        checkout = git_files.at(self.main)
        self.assertEqual(git_files.head(checkout), git_files.OnBranch("trunk"))
        _ = git(self.main, "switch", "-q", "--detach")
        self.assertEqual(git_files.head(checkout), git_files.DetachedHead(git(self.main, "rev-parse", "HEAD")))

    def assert_unreadable_metadata(self, folder: Path, metadata: Path) -> None:
        program = """import errno, os, sys
from pathlib import Path
from unittest.mock import patch
from scripts.lib import git_files

folder, metadata = map(Path, sys.argv[1:])
open_path = os.open
wrap = os.fdopen
descriptors = []

def track(path, flags, *args, **kwargs):
    if path == metadata:
        assert (flags & os.O_ACCMODE) == os.O_RDONLY
        assert flags & os.O_NONBLOCK
    descriptor = open_path(path, flags, *args, **kwargs)
    if path == metadata:
        descriptors.append(descriptor)
    return descriptor

def check_wrap(descriptor, *args, **kwargs):
    assert descriptor not in descriptors, "The reader wrapped a non-regular file."
    return wrap(descriptor, *args, **kwargs)

with patch.object(os, "open", track), patch.object(os, "fdopen", check_wrap):
    try:
        git_files.locate(folder)
    except git_files.UnreadableGitFiles as error:
        assert error.path == metadata, error
        assert error.reason == "expected a regular file", error
    else:
        raise AssertionError("The reader accepted a non-regular file.")

assert len(descriptors) == 1, descriptors
try:
    os.fstat(descriptors[0])
except OSError as error:
    assert error.errno == errno.EBADF
else:
    raise AssertionError("The metadata descriptor is still open.")
"""
        run = subprocess.run([sys.executable, "-c", program, str(folder), str(metadata)],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_fifo_git_entry_is_unreadable_and_closes_its_descriptor(self) -> None:
        folder = self.root / "fifo-checkout"
        folder.mkdir()
        metadata = folder / ".git"
        os.mkfifo(metadata)
        self.assert_unreadable_metadata(folder, metadata)

    def test_fifo_commondir_is_unreadable_and_closes_its_descriptor(self) -> None:
        metadata = self.main / ".git" / "commondir"
        os.mkfifo(metadata)
        self.assert_unreadable_metadata(self.main, metadata)

    def test_commondir_directory_is_unreadable_and_closes_its_descriptor(self) -> None:
        metadata = self.main / ".git" / "commondir"
        metadata.mkdir()
        self.assert_unreadable_metadata(self.main, metadata)


    def test_commondir_link_to_fifo_is_unreadable_and_closes_its_descriptor(self) -> None:
        metadata = self.main / ".git" / "commondir"
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        metadata.symlink_to(fifo)
        self.assert_unreadable_metadata(self.main, metadata)
