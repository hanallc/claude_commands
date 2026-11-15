"""Rescue keeps every kind of Git work in the source repository before a folder goes."""
from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from typing import cast, override
from unittest import mock

from scripts.lib import build_folders as folders, build_rescue
from scripts.lib.git_test_support import commit_repository, write_fixture_git_config
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True,
                          text=True, timeout=TEST_HANG_GUARD_SECS).stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text, encoding="utf-8")


def new_repository(path: Path, files: dict[str, str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = git(path.parent, "init", "-q", "-b", "main", path.name)
    for name, text in files.items():
        write(path / name, text)
    return commit_repository(path, "base")


class RescueTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        config = self.root / "config"
        config.mkdir()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(config), "TMPDIR": str(self.root),
            "GIT_CONFIG_GLOBAL": str(write_fixture_git_config(self.root)), "GIT_CONFIG_NOSYSTEM": "1",
        })))
        self.source = self.root / "source"
        _ = new_repository(self.source, {"a.txt": "base\n", ".gitignore": "secret.env\ntarget/\n"})

    def show(self, revision: str) -> str:
        return git(self.source, "show", revision)

    def rescue_refs(self, repository: Path) -> list[str]:
        return git(repository, "for-each-ref", "--format=%(refname)", "refs/rescue/").split()

    def test_a_worktree_rescue_keeps_commits_index_changes_files_ignored_source_conflicts_and_nested_work(self) -> None:
        _ = git(self.source, "switch", "-q", "-c", "feature")
        write(self.source / "a.txt", "theirs\n")
        _ = commit_repository(self.source, "theirs")
        _ = git(self.source, "switch", "-q", "main")
        write(self.source / "a.txt", "ours\n")
        _ = commit_repository(self.source, "ours")
        folder = self.root / "slot"
        _ = git(self.source, "worktree", "add", "-q", "--detach", str(folder), "main")
        write(folder / "c.txt", "detached commit\n")
        detached = commit_repository(folder, "detached", "c.txt")
        merge = subprocess.run(["git", "-C", str(folder), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "merge", "-q", "feature"], capture_output=True, text=True,
                               timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertIn("CONFLICT", merge.stdout + merge.stderr)
        write(folder / "b.txt", "staged\n")
        _ = git(folder, "add", "b.txt")
        write(folder / "b.txt", "unstaged\n")
        write(folder / "untracked.txt", "untracked\n")
        write(folder / "secret.env", "ignored source\n")
        write(folder / "target" / "CACHEDIR.TAG", "Signature: 8a477f597d28d172789f06886806bc55\n")
        write(folder / "target" / "debug" / "big.bin", "generated\n")
        nested = folder / "nested"
        nested_head = new_repository(nested, {"n.txt": "nested commit\n"})
        write(nested / "nu.txt", "nested untracked\n")

        rescued = build_rescue.rescue(folder, self.source, "slot-1")

        self.assertIsInstance(rescued, build_rescue.Rescued)
        assert isinstance(rescued, build_rescue.Rescued)
        self.assertRegex(rescued.ref, r"^refs/rescue/slot-1-\d{8}T\d{6}Z-[0-9a-f]{6}$")
        self.assertEqual(self.rescue_refs(self.source), [rescued.ref])
        self.assertEqual(self.rescue_refs(nested), [])
        self.assertEqual(list((self.source / ".git").rglob("rescue-index-*")), [])
        self.assertEqual(list((nested / ".git").rglob("rescue-index-*")), [])
        _ = git(self.source, "worktree", "remove", "--force", str(folder))
        ref = rescued.ref
        self.assertEqual(git(self.source, "rev-parse", f"{ref}^1").strip(), detached)
        self.assertEqual(self.show(f"{ref}:worktree/b.txt"), "unstaged\n")
        self.assertEqual(self.show(f"{ref}:index/0/b.txt"), "staged\n")
        self.assertEqual([self.show(f"{ref}:index/{stage}/a.txt") for stage in (1, 2, 3)],
                         ["base\n", "ours\n", "theirs\n"])
        self.assertIn("<<<<<<<", self.show(f"{ref}:worktree/a.txt"))
        self.assertEqual(self.show(f"{ref}:worktree/untracked.txt"), "untracked\n")
        self.assertEqual(self.show(f"{ref}:worktree/secret.env"), "ignored source\n")
        self.assertNotIn("target", git(self.source, "ls-tree", "--name-only", f"{ref}:worktree").split())
        self.assertIn(f"{detached} HEAD", self.show(f"{ref}:refs"))
        link = git(self.source, "ls-tree", f"{ref}:worktree", "nested").split()
        self.assertEqual(link[:2], ["160000", "commit"])
        self.assertEqual(git(self.source, "rev-parse", f"{link[2]}^1").strip(), nested_head)
        self.assertEqual(self.show(f"{link[2]}:worktree/nu.txt"), "nested untracked\n")
        self.assertEqual(self.show(f"{link[2]}:worktree/n.txt"), "nested commit\n")

    def test_a_clone_rescue_copies_its_objects_and_its_unpushed_branch(self) -> None:
        folder = self.root / "clone"
        _ = git(self.root, "clone", "-q", "--no-local", str(self.source), str(folder))
        _ = git(folder, "switch", "-q", "-c", "local-only")
        write(folder / "l.txt", "only in the clone\n")
        branch = commit_repository(folder, "local only")
        _ = git(folder, "switch", "-q", "main")
        write(folder / "w.txt", "work file\n")

        rescued = build_rescue.rescue(folder, self.source, "clone")

        assert isinstance(rescued, build_rescue.Rescued)
        shutil.rmtree(folder)
        self.assertIn(f"{branch} refs/heads/local-only", self.show(f"{rescued.ref}:refs"))
        self.assertEqual(self.show(f"{branch}:l.txt"), "only in the clone\n")
        self.assertEqual(self.show(f"{rescued.ref}:worktree/w.txt"), "work file\n")
        self.assertEqual(self.rescue_refs(self.source), [rescued.ref])

    def test_a_folder_that_is_not_a_repository_rescues_only_its_repositories(self) -> None:
        plain = self.root / "plain"
        write(plain / "notes.txt", "temporary\n")
        self.assertEqual(build_rescue.rescue(plain, self.source, "plain"), build_rescue.NothingToRescue())
        self.assertEqual(self.rescue_refs(self.source), [])
        inner = plain / "deep" / "inner"
        _ = new_repository(inner, {"i.txt": "inner\n"})
        write(inner / "iu.txt", "inner untracked\n")

        rescued = build_rescue.rescue(plain, self.source, "plain")

        assert isinstance(rescued, build_rescue.Rescued)
        shutil.rmtree(plain)
        link = git(self.source, "ls-tree", f"{rescued.ref}:worktree/deep", "inner").split()
        self.assertEqual(link[:2], ["160000", "commit"])
        self.assertEqual(self.show(f"{link[2]}:worktree/iu.txt"), "inner untracked\n")
        self.assertEqual(self.rescue_refs(self.source), [rescued.ref])

    def test_removal_saves_first_keeps_the_folder_on_a_failed_rescue_and_keeps_a_held_folder(self) -> None:
        (self.root / "folders").mkdir()
        parent = folders.ExplicitBuildParent(self.root / "folders")
        path = folders.create_build_folder(folders.UniqueBuildFolder("rescue-remove-", parent))
        _ = git(self.source, "worktree", "add", "-q", "--detach", str(path / "checkout"), "main")
        write(path / "checkout" / "work.txt", "unsaved work\n")

        self.assertEqual(folders.rescue_and_remove_build_folder(path, self.source, "remove"), folders.FolderHeld())
        release = cast(Callable[[Path], None], getattr(folders, "_release_build_folder"))
        release(path)
        with mock.patch.object(build_rescue, "_verify", side_effect=build_rescue.RescueFailed("injected")):
            with self.assertRaises(build_rescue.RescueFailed):
                _ = folders.rescue_and_remove_build_folder(path, self.source, "remove")
        self.assertEqual((path / "checkout" / "work.txt").read_text(), "unsaved work\n")
        self.assertTrue(path.with_name(path.name + ".lock").is_file())

        rescued = folders.rescue_and_remove_build_folder(path, self.source, "remove")

        assert isinstance(rescued, build_rescue.Rescued)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name + ".lock").exists())
        link = git(self.source, "ls-tree", f"{rescued.ref}:worktree", "checkout").split()
        self.assertEqual(self.show(f"{link[2]}:worktree/work.txt"), "unsaved work\n")


if __name__ == "__main__":
    _ = unittest.main()
