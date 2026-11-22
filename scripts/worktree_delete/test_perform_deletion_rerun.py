"""Check worktree removal reports and the checks before reporting."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from typing import override
from unittest.mock import patch

from scripts.lib.git_test_support import fixture_git_environment
from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.worktree import worktrees


SCRIPTS = Path(__file__).resolve().parent
BRANCH = "unit-topic"


class DeletionRerunTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    worktree: Path = Path()
    output: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "repo"
        self.worktree = self.root / "workspaces/repo/unit/topic"
        self.output = self.root / "build/repo/unit/topic"
        settings = self.root / "settings"
        settings.mkdir()
        _ = (settings / "build_folders.conf").write_text(
            f"roots.build={self.root / 'build'}\nroots.workspace={self.root / 'workspaces'}\n")
        self.environment = {
            **fixture_git_environment(self.root),
            **interpreter_cache_environment(self.root / "cache"),
            "CONF_DIR": str(settings), "WORKTREES_STATE_DIR": str(self.root / "state"),
            "GIT_ALLOW_PROTOCOL": "file", "WT_CONFIRM_DIR": str(self.root / "confirmations"),
        }
        remote = self.root / "repo.git"
        _ = self.git("init", "-q", "--bare", str(remote))
        _ = self.git("init", "-q", "--initial-branch=main", str(self.repo))
        _ = self.git("commit", "--allow-empty", "-qm", "fixture", directory=self.repo)
        _ = self.git("remote", "add", "origin", str(remote), directory=self.repo)
        _ = self.git("worktree", "add", "-qb", BRANCH, str(self.worktree), directory=self.repo)
        _ = self.git("push", "-q", "origin", "main", BRANCH, directory=self.repo)
        _ = self.git("remote", "set-head", "origin", "main", directory=self.repo)

        base = self.git("rev-parse", "HEAD", directory=self.repo)
        record = replace(worktrees.new_record(self.worktree, BRANCH, self.repo, base, "unit", "main"),
                         layout=worktrees.RecordedWorktreeOutput("unit", "topic", self.output))
        with patch.dict(os.environ, {"WORKTREES_STATE_DIR": self.environment["WORKTREES_STATE_DIR"]}):
            worktrees.write_record(record)
        self.output.mkdir(parents=True)
        _ = (self.output / "artifact").write_text("keep\n")
        with (self.repo / ".git/info/exclude").open("a") as exclude:
            _ = exclude.write("/target\n")
        (self.worktree / "target").symlink_to(self.output)

    def git(self, *args: str, directory: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=directory or self.root, env=self.environment,
                              capture_output=True, text=True, check=True,
                              timeout=TEST_HANG_GUARD_SECS).stdout.strip()

    def script(self, name: str, cwd: Path, path: Path, *, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPTS / name), str(path), BRANCH],
                              cwd=cwd, env=self.environment, capture_output=True,
                              text=True, check=check, timeout=TEST_HANG_GUARD_SECS)

    def commit_topic(self) -> None:
        _ = (self.worktree / "code.txt").write_text("topic work\n")
        _ = self.git("add", "code.txt", directory=self.worktree)
        _ = self.git("commit", "-qm", "Change code.txt", directory=self.worktree)
        _ = self.git("push", "-q", "origin", BRANCH, directory=self.worktree)

    def assert_retained(self, result: subprocess.CompletedProcess[str], tip: str, *, origin: bool = True) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines(), [
            f"kept for a manual removal list: {path} (worktrees, the checkout is selected for removal)"
            for path in (self.output, self.worktree)
        ])
        self.assertEqual((self.worktree / "target").readlink(), self.output)
        self.assertEqual((self.output / "artifact").read_text(), "keep\n")
        self.assertEqual(self.git("rev-parse", f"refs/heads/{BRANCH}", directory=self.repo), tip)
        if origin:
            self.assertEqual(self.git("ls-remote", "--heads", "origin", BRANCH, directory=self.repo),
                             f"{tip}\trefs/heads/{BRANCH}")

    def test_confirmed_removal_from_a_sibling_lists_both_folders(self) -> None:
        self.commit_topic()
        tip = self.git("rev-parse", "HEAD", directory=self.worktree)
        caller = self.root / "caller"
        _ = self.git("worktree", "add", "-qb", "caller", str(caller), directory=self.repo)
        relative = Path(os.path.relpath(self.worktree, caller))
        lock, lease = self.worktree.with_suffix(".lock"), self.worktree.with_suffix(".lease.json")
        lock.touch()
        _ = lease.write_text("{}\n")
        _ = self.script("record_confirmation.sh", caller, relative, check=True)
        nonce, = (self.root / "confirmations").iterdir()

        result = self.script("perform_deletion.sh", caller, relative)

        self.assertFalse(nonce.exists())
        self.assert_retained(result, tip)
        self.assertEqual((self.worktree / "code.txt").read_text(), "topic work\n")
        self.assertTrue(lock.is_file())
        self.assertEqual(lease.read_text(), "{}\n")
        rerun = self.script("perform_deletion.sh", caller, relative)
        self.assertNotEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("no confirmation nonce", rerun.stdout)
        self.assertIn("work has not landed", rerun.stdout)
        self.assertNotIn("kept for a manual removal list", rerun.stderr)

    def test_landed_cleanup_lists_the_same_folders_on_a_rerun(self) -> None:
        self.commit_topic()
        tip = self.git("rev-parse", "HEAD", directory=self.worktree)
        _ = self.git("merge", "--ff-only", BRANCH, directory=self.repo)
        _ = self.git("push", "-q", "origin", "main", directory=self.repo)
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                self.assert_retained(self.script("perform_deletion.sh", self.repo, self.worktree), tip)

    def test_confirmed_removal_lists_the_folders_without_origin(self) -> None:
        tip = self.git("rev-parse", "HEAD", directory=self.worktree)
        _ = self.git("remote", "remove", "origin", directory=self.repo)
        _ = self.script("record_confirmation.sh", self.repo, self.worktree, check=True)
        self.assert_retained(self.script("perform_deletion.sh", self.repo, self.worktree), tip, origin=False)

    def test_branch_lookup_error_stops_before_reporting_folders(self) -> None:
        tip = self.git("rev-parse", "HEAD", directory=self.worktree)
        _ = self.script("record_confirmation.sh", self.repo, self.worktree, check=True)
        real_git = shutil.which("git")
        assert real_git is not None
        tools = self.root / "bin"
        tools.mkdir()
        shim = tools / "git"
        _ = shim.write_text(
            '#!/bin/sh\nif [ "$1" = show-ref ]; then\n'
            + "    echo 'fatal: injected branch lookup failure' >&2\n    exit 128\nfi\n"
            + f'exec {shlex.quote(real_git)} "$@"\n')
        shim.chmod(0o700)
        with patch.dict(self.environment, {"PATH": str(tools) + os.pathsep + self.environment["PATH"]}):
            result = self.script("perform_deletion.sh", self.repo, self.worktree)
        self.assertEqual(result.returncode, 128, result.stdout + result.stderr)
        self.assertIn("fatal: injected branch lookup failure", result.stderr)
        self.assertNotIn("kept for a manual removal list", result.stderr)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.git("ls-remote", "--heads", "origin", BRANCH, directory=self.repo),
                         f"{tip}\trefs/heads/{BRANCH}")


if __name__ == "__main__":
    _ = unittest.main()
