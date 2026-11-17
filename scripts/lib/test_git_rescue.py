"""Rescue refs preserve work from disposable detached checkouts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.lib import git_rescue
from scripts.lib.git_test_support import commit_repository, write_fixture_git_config
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class GitRescueTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()
    slot: Path = Path()
    base: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        config = write_fixture_git_config(self.root)
        self.enterContext(mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }))
        self.source = self.root / "source"
        self.source.mkdir()
        _ = self.git(self.source, "init", "-q", "-b", "trunk")
        _ = (self.source / "tracked.txt").write_text("base\n")
        _ = (self.source / ".gitignore").write_text("ignored/\ngenerated/\n")
        self.base = commit_repository(self.source, "base")
        self.slot = self.root / "slot"
        _ = self.git(self.source, "worktree", "add", "-q", "--detach", str(self.slot), self.base)

    def git(self, folder: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(folder), *arguments], check=True, capture_output=True,
            text=True, timeout=TEST_HANG_GUARD_SECS,
        ).stdout.strip()

    def saved(self, name: str, *, generated: tuple[Path, ...] = ()) -> git_rescue.Saved:
        result = git_rescue.rescue(self.slot, name, generated=generated)
        self.assertIsInstance(result, git_rescue.Saved)
        assert isinstance(result, git_rescue.Saved)
        return result

    def restore(self, commit: str, name: str = "restored") -> Path:
        folder = self.root / name
        _ = self.git(self.source, "worktree", "add", "-q", "--detach", str(folder), commit)
        return folder

    def index_bytes(self) -> bytes:
        return Path(self.git(self.slot, "rev-parse", "--path-format=absolute", "--git-path", "index")).read_bytes()

    def test_clean_reachable_worktree_needs_no_rescue_even_with_generated_data(self) -> None:
        generated = self.slot / "generated"
        generated.mkdir()
        _ = (generated / "object.o").write_bytes(b"build output")

        result = git_rescue.rescue(self.slot, "clean", generated=(generated,))

        self.assertIsInstance(result, git_rescue.NothingToSave)
        self.assertEqual(self.git(self.source, "for-each-ref", "refs/rescue/"), "")
        self.assertEqual((generated / "object.o").read_bytes(), b"build output")

    def test_restore_keeps_local_commit_index_worktree_and_untracked_source(self) -> None:
        _ = (self.slot / "local.txt").write_text("local commit\n")
        local = commit_repository(self.slot, "local")
        _ = (self.slot / "tracked.txt").write_text("staged\n")
        _ = self.git(self.slot, "add", "tracked.txt")
        _ = (self.slot / "tracked.txt").write_text("unstaged\n")
        _ = (self.slot / "untracked.txt").write_text("untracked source\n")
        ignored = self.slot / "ignored"
        ignored.mkdir()
        _ = (ignored / "source.py").write_text("saved = True\n")
        before_index = self.index_bytes()
        before_status = self.git(self.slot, "status", "--porcelain=v2", "--untracked-files=all", "--ignored=matching")

        result = self.saved("work")
        restored = self.restore(result.commit)

        self.assertEqual(result.ref, "refs/rescue/work")
        self.assertEqual(self.git(self.source, "rev-parse", result.ref), result.commit)
        self.assertEqual(self.git(self.source, "rev-parse", result.commit + "^1"), local)
        self.assertEqual(self.git(self.source, "show", result.commit + "^2:tracked.txt"), "staged")
        self.assertEqual((restored / "tracked.txt").read_text(), "unstaged\n")
        self.assertEqual((restored / "local.txt").read_text(), "local commit\n")
        self.assertEqual((restored / "untracked.txt").read_text(), "untracked source\n")
        self.assertEqual((restored / "ignored/source.py").read_text(), "saved = True\n")
        self.assertEqual(self.index_bytes(), before_index)
        self.assertEqual(self.git(self.slot, "rev-parse", "HEAD"), local)
        self.assertEqual(self.git(self.slot, "status", "--porcelain=v2", "--untracked-files=all", "--ignored=matching"), before_status)

    def test_clean_local_commit_survives_without_a_source_branch(self) -> None:
        _ = (self.slot / "local.txt").write_text("detached work\n")
        local = commit_repository(self.slot, "detached")

        result = self.saved("detached")
        restored = self.restore(result.commit)

        self.assertEqual(self.git(self.source, "rev-parse", result.ref + "^1"), local)
        self.assertEqual((restored / "local.txt").read_text(), "detached work\n")

    def test_conflict_rescue_keeps_all_stages_and_merge_parent(self) -> None:
        _ = (self.slot / "tracked.txt").write_text("ours\n")
        ours = commit_repository(self.slot, "ours")
        _ = (self.source / "tracked.txt").write_text("theirs\n")
        theirs = commit_repository(self.source, "theirs")
        merge = subprocess.run(["git", "-C", str(self.slot), "merge", "--no-edit", theirs],
                               capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(merge.returncode, 1, merge.stdout + merge.stderr)
        before_index = self.index_bytes()
        conflicted = (self.slot / "tracked.txt").read_bytes()

        result = self.saved("conflict")
        restored = self.restore(result.commit)

        for stage, text in ((1, "base\n"), (2, "ours\n"), (3, "theirs\n")):
            with self.subTest(stage=stage):
                self.assertEqual((restored / f".rescue-conflicts/{stage}/tracked.txt").read_text(), text)
        self.assertEqual((restored / "tracked.txt").read_bytes(), conflicted)
        parents = self.git(self.source, "show", "-s", "--format=%P", result.commit).split()
        self.assertEqual(len(parents), 3)
        self.assertEqual(parents[0], ours)
        self.assertEqual(parents[2], theirs)
        self.assertEqual(self.git(self.source, "show", "-s", "--format=%P", parents[1]), ours)
        self.assertEqual(self.git(self.source, "ls-tree", "--name-only", parents[1]), ".gitignore")
        self.assertEqual(self.index_bytes(), before_index)
        self.assertEqual(self.git(self.slot, "rev-parse", "MERGE_HEAD"), theirs)

    def test_cherry_pick_rescue_keeps_unheld_history_and_sequencer_commits(self) -> None:
        history_file = self.slot / "history-only.txt"
        _ = history_file.write_text("ancestor source\n")
        ancestor = commit_repository(self.slot, "unheld ancestor")
        history_blob = self.git(self.slot, "rev-parse", ancestor + ":history-only.txt")
        history_file.unlink()
        _ = (self.slot / "tracked.txt").write_text("incoming\n")
        picked = commit_repository(self.slot, "unheld pick")
        _ = (self.slot / "pending.txt").write_text("pending source\n")
        pending = commit_repository(self.slot, "pending pick")
        _ = (self.source / "tracked.txt").write_text("ours\n")
        ours = commit_repository(self.source, "ours")
        _ = self.git(self.slot, "checkout", "-q", "--detach", ours)
        pick = subprocess.run(["git", "-C", str(self.slot), "cherry-pick", picked, pending],
                              capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(pick.returncode, 1, pick.stdout + pick.stderr)
        self.assertEqual(self.git(self.slot, "rev-parse", "CHERRY_PICK_HEAD"), picked)
        self.assertEqual(self.git(self.source, "rev-list", "--all"), f"{ours}\n{self.base}")
        before_index = self.index_bytes()
        conflicted = (self.slot / "tracked.txt").read_bytes()

        result = self.saved("cherry-pick")
        parents = self.git(self.source, "show", "-s", "--format=%P", result.commit).split()
        reachable = self.git(self.source, "rev-list", "--objects", result.ref).splitlines()

        self.assertIn(picked, parents)
        self.assertIn(pending, parents)
        self.assertIn(history_blob, [line.split(" ", 1)[0] for line in reachable])
        self.assertNotIn("history-only.txt", self.git(self.source, "ls-tree", "-r", "--name-only", result.ref).splitlines())
        self.assertEqual(self.git(self.source, "show", picked + "^:history-only.txt"), "ancestor source")
        self.assertEqual(self.index_bytes(), before_index)
        self.assertEqual((self.slot / "tracked.txt").read_bytes(), conflicted)
        self.assertEqual(self.git(self.slot, "rev-parse", "CHERRY_PICK_HEAD"), picked)

        _ = self.git(self.slot, "cherry-pick", "--abort")
        git_dir = Path(self.git(self.slot, "rev-parse", "--path-format=absolute", "--git-dir"))
        for relative, content in (
            ("CHERRY_PICK_HEAD", picked + "\n"), ("REVERT_HEAD", picked + "\n"),
            ("sequencer/head", picked + "\n"),
            ("sequencer/todo", f"# pending commits\npick {picked[:12]} incoming\nrevert {pending[:12]} pending\n"),
            ("CHERRY_PICK_HEAD", ""), ("REVERT_HEAD", ""),
            ("sequencer/head", ""), ("sequencer/todo", ""),
        ):
            with self.subTest(relative=relative, content=content):
                path = git_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text(content)
                before_index = self.index_bytes()

                saved = self.saved("operation-state")

                if content:
                    parents = self.git(self.source, "show", "-s", "--format=%P", saved.commit).split()
                    self.assertIn(picked, parents)
                    if relative == "sequencer/todo":
                        self.assertIn(pending, parents)
                self.assertEqual(path.read_text(), content)
                self.assertEqual(self.index_bytes(), before_index)
                path.unlink()
        (git_dir / "sequencer").rmdir()

    def test_nested_repository_commit_and_edits_are_recoverable_from_source(self) -> None:
        nested = self.slot / "nested"
        nested.mkdir()
        _ = self.git(nested, "init", "-q", "-b", "trunk")
        _ = (nested / "source.txt").write_text("nested commit\n")
        nested_commit = commit_repository(nested, "nested")
        _ = (nested / "source.txt").write_text("nested edit\n")

        result = self.saved("nested-work")
        restored = self.restore(result.commit)
        manifest = cast(object, json.loads((restored / ".rescue-nested").read_text()))
        parents = self.git(self.source, "show", "-s", "--format=%P", result.ref).split()

        self.assertEqual(len(parents), 2)
        self.assertEqual(parents[0], self.base)
        nested_rescue = parents[1]
        self.assertEqual(manifest, {"nested": {"commit": nested_rescue, "refs": {"refs/heads/trunk": nested_commit}}})
        self.assertEqual(self.git(self.source, "show", "-s", "--format=%P", nested_rescue), nested_commit)
        nested_restored = self.restore(nested_rescue, "nested-restored")
        self.assertEqual((nested_restored / "source.txt").read_text(), "nested edit\n")
        self.assertEqual(self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/"), result.ref)
        self.assertEqual(self.git(nested, "rev-parse", "HEAD"), nested_commit)
        self.assertEqual((nested / "source.txt").read_text(), "nested edit\n")

    def test_conflicts_keep_staged_changes_to_other_paths(self) -> None:
        _ = (self.slot / "tracked.txt").write_text("ours\n")
        ours = commit_repository(self.slot, "ours")
        _ = (self.source / "tracked.txt").write_text("theirs\n")
        theirs = commit_repository(self.source, "theirs")
        merge = subprocess.run(["git", "-C", str(self.slot), "merge", "--no-edit", theirs],
                               capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(merge.returncode, 1, merge.stdout + merge.stderr)
        _ = (self.slot / "other.txt").write_text("staged source\n")
        _ = self.git(self.slot, "add", "other.txt")
        _ = (self.slot / "other.txt").write_text("working source\n")
        before_index = self.index_bytes()
        conflicted = (self.slot / "tracked.txt").read_bytes()

        result = self.saved("conflict-staged")
        parents = self.git(self.source, "show", "-s", "--format=%P", result.commit).split()

        self.assertEqual(len(parents), 3)
        self.assertEqual(parents[0], ours)
        self.assertEqual(parents[2], theirs)
        self.assertEqual(self.git(self.source, "show", parents[1] + ":other.txt"), "staged source")
        self.assertNotIn("tracked.txt", self.git(self.source, "ls-tree", "--name-only", parents[1]).splitlines())
        restored = self.restore(result.commit)
        self.assertEqual((restored / "other.txt").read_text(), "working source\n")
        self.assertEqual((restored / "tracked.txt").read_bytes(), conflicted)
        for stage, text in ((1, "base\n"), (2, "ours\n"), (3, "theirs\n")):
            with self.subTest(stage=stage):
                self.assertEqual((restored / f".rescue-conflicts/{stage}/tracked.txt").read_text(), text)
        self.assertEqual(self.index_bytes(), before_index)

    def test_flagged_files_keep_hidden_working_edits(self) -> None:
        for flag, content in (("--assume-unchanged", "base\n"), ("--skip-worktree", "base\n"),
                              ("--assume-unchanged", "hidden assume edit\n"),
                              ("--skip-worktree", "hidden skip edit\n")):
            with self.subTest(flag=flag, content=content):
                _ = self.git(self.slot, "update-index", flag, "tracked.txt")
                _ = (self.slot / "tracked.txt").write_text(content)
                self.assertEqual(self.git(self.slot, "status", "--porcelain=v2"), "")
                before_index = self.index_bytes()

                result = self.saved("flagged")

                self.assertEqual(self.git(self.source, "show", result.commit + ":tracked.txt"), content.strip())
                self.assertEqual(self.index_bytes(), before_index)
                _ = self.git(self.slot, "update-index", "--no-assume-unchanged", "--no-skip-worktree", "tracked.txt")
                _ = (self.slot / "tracked.txt").write_text("base\n")

    def test_nested_repository_keeps_branch_and_peeled_tag_tips(self) -> None:
        nested = self.slot / "nested"
        nested.mkdir()
        _ = self.git(nested, "init", "-q", "-b", "trunk")
        _ = (nested / "source.txt").write_text("nested base\n")
        trunk = commit_repository(nested, "nested base")
        _ = self.git(nested, "checkout", "-q", "-b", "other")
        _ = (nested / "source.txt").write_text("other branch\n")
        other = commit_repository(nested, "other branch")
        _ = self.git(nested, "checkout", "-q", "--detach", trunk)
        _ = (nested / "source.txt").write_text("tagged work\n")
        tagged = commit_repository(nested, "tagged work")
        _ = self.git(nested, "tag", "-a", "archive", "-m", "archive", tagged)
        _ = self.git(nested, "checkout", "-q", "trunk")
        _ = (nested / "source.txt").write_text("working edit\n")

        result = self.saved("nested-refs")
        nested_rescue = self.git(self.source, "rev-parse", result.commit + "^2")
        manifest = cast(object, json.loads(self.git(self.source, "show", result.commit + ":.rescue-nested")))

        self.assertEqual(manifest, {"nested": {"commit": nested_rescue, "refs": {
            "refs/heads/trunk": trunk, "refs/heads/other": other, "refs/tags/archive": tagged,
        }}})
        self.assertEqual(self.git(self.source, "show", "-s", "--format=%P", nested_rescue).split(),
                         [trunk, other, tagged])
        for tip in (trunk, other, tagged):
            with self.subTest(tip=tip):
                _ = self.git(self.source, "merge-base", "--is-ancestor", tip, result.commit)
        self.assertEqual(self.git(self.source, "show", tagged + ":source.txt"), "tagged work")
        self.assertEqual(self.git(self.source, "show", other + ":source.txt"), "other branch")

    def test_metadata_collision_retains_source_and_creates_no_ref(self) -> None:
        nested = self.slot / "nested"
        nested.mkdir()
        _ = self.git(nested, "init", "-q", "-b", "trunk")
        _ = (nested / "source.txt").write_text("nested source\n")
        _ = commit_repository(nested, "nested")
        _ = (nested / "source.txt").write_text("nested edit\n")
        _ = (self.slot / "tracked.txt").write_text("ours\n")
        _ = commit_repository(self.slot, "ours")
        _ = (self.source / "tracked.txt").write_text("theirs\n")
        theirs = commit_repository(self.source, "theirs")
        merge = subprocess.run(["git", "-C", str(self.slot), "merge", "--no-edit", theirs],
                               capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(merge.returncode, 1, merge.stdout + merge.stderr)
        for relative in (".rescue-nested", ".rescue-conflicts/1/tracked.txt"):
            for tracked in (False, True):
                with self.subTest(relative=relative, tracked=tracked):
                    path = self.slot / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _ = path.write_text("user source\n")
                    if tracked:
                        _ = self.git(self.slot, "add", "--", relative)
                    before_index = self.index_bytes()

                    with self.assertRaises(git_rescue.RescueError):
                        _ = git_rescue.rescue(self.slot, "collision")

                    self.assertEqual(path.read_text(), "user source\n")
                    self.assertEqual(self.index_bytes(), before_index)
                    self.assertEqual(self.git(self.source, "for-each-ref", "refs/rescue/"), "")
                    if tracked:
                        _ = self.git(self.slot, "update-index", "--force-remove", "--", relative)
                    path.unlink()
                    if relative.startswith(".rescue-conflicts/"):
                        path.parent.rmdir()
                        path.parent.parent.rmdir()

    def test_nested_repository_gets_no_ref_when_outer_publication_fails(self) -> None:
        nested = self.slot / "nested"
        nested.mkdir()
        _ = self.git(nested, "init", "-q", "-b", "trunk")
        _ = (nested / "source.txt").write_text("nested source\n")
        _ = commit_repository(nested, "nested")
        _ = (nested / "source.txt").write_text("nested edit\n")
        lock = self.source / ".git/refs/rescue/locked.lock"
        lock.parent.mkdir(parents=True)
        _ = lock.write_text("held by test\n")

        with self.assertRaises(git_rescue.RescueError):
            _ = git_rescue.rescue(self.slot, "locked")

        self.assertEqual(self.git(nested, "for-each-ref", "refs/rescue/"), "")
        self.assertEqual(self.git(self.source, "for-each-ref", "refs/rescue/"), "")
        self.assertEqual((nested / "source.txt").read_text(), "nested edit\n")
        lock.unlink()
        result = self.saved("locked")
        self.assertEqual(self.git(nested, "for-each-ref", "refs/rescue/"), "")
        self.assertEqual(self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/"), result.ref)

    def test_generated_folder_is_excluded_but_ignored_source_is_saved(self) -> None:
        generated = self.slot / "generated"
        generated.mkdir()
        _ = (generated / "artifact").write_text("do not save\n")
        ignored = self.slot / "ignored"
        ignored.mkdir()
        _ = (ignored / "source.rs").write_text("fn main() {}\n")

        result = self.saved("source-only", generated=(generated,))
        restored = self.restore(result.commit)

        self.assertFalse((restored / "generated").exists())
        self.assertEqual((restored / "ignored/source.rs").read_text(), "fn main() {}\n")
        self.assertEqual((generated / "artifact").read_text(), "do not save\n")

    def test_second_rescue_uses_a_suffix_and_keeps_the_first_tree(self) -> None:
        _ = (self.slot / "tracked.txt").write_text("first\n")
        first = self.saved("repeat")
        _ = (self.slot / "tracked.txt").write_text("second\n")

        second = self.saved("repeat")

        self.assertEqual(first.ref, "refs/rescue/repeat")
        self.assertEqual(second.ref, "refs/rescue/repeat-2")
        self.assertEqual(self.git(self.source, "rev-parse", first.ref), first.commit)
        self.assertEqual((self.restore(first.commit, "first") / "tracked.txt").read_text(), "first\n")
        self.assertEqual((self.restore(second.commit, "second") / "tracked.txt").read_text(), "second\n")

    def test_failed_ref_write_leaves_worktree_index_and_head_unchanged(self) -> None:
        _ = (self.slot / "tracked.txt").write_text("staged work\n")
        _ = self.git(self.slot, "add", "tracked.txt")
        _ = (self.slot / "tracked.txt").write_text("unstaged work\n")
        _ = (self.slot / "untracked.txt").write_text("source work\n")
        before_index = self.index_bytes()
        before_status = self.git(self.slot, "status", "--porcelain=v2", "--untracked-files=all")
        lock = self.source / ".git/refs/rescue/locked.lock"
        lock.parent.mkdir(parents=True)
        _ = lock.write_text("held by test\n")

        with self.assertRaises(git_rescue.RescueError) as raised:
            _ = git_rescue.rescue(self.slot, "locked")

        self.assertIn("git", str(raised.exception))
        self.assertIn("lock", str(raised.exception))
        self.assertEqual(self.git(self.slot, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.index_bytes(), before_index)
        self.assertEqual(self.git(self.slot, "status", "--porcelain=v2", "--untracked-files=all"), before_status)
        self.assertEqual((self.slot / "tracked.txt").read_text(), "unstaged work\n")
        self.assertEqual((self.slot / "untracked.txt").read_text(), "source work\n")
        self.assertEqual(self.git(self.source, "for-each-ref", "refs/rescue/"), "")


if __name__ == "__main__":
    _ = unittest.main()
