"""Numbered copies retain work, reuse files, and enforce their locks."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, redirect_stderr, redirect_stdout
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
from scripts.lib import build_copies, build_folder_lock, build_folders, copy_record, file_lock, git_files, git_rescue, scratch, session_folder
from scripts.lib.build_copies_test_support import private_copy_config
from scripts.lib.descendants import adopt_descendants
from scripts.lib.git_test_support import commit_repository
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff


class BuildCopiesTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()
    base: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        configuration = private_copy_config(self.root)
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
        return subprocess.run([sys.executable, "-m", "scripts.lib.build_copies", *arguments],
                              cwd=checkout, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(checkout), "PYTHONSAFEPATH": "1"},
                              timeout=TEST_HANG_GUARD_SECS)

    def run_arguments(self, unit: str = "queue") -> tuple[str, ...]:
        return ("run", "--source", str(self.source), "--commit", self.base,
                "--showrunner", "builds", "--unit", unit, "--work", "proof", "--purpose", "test", "--")

    def test_assigned_copy_uses_only_its_number(self) -> None:
        def assigned():
            return build_copies.take_copy(self.source, self.base, showrunner='builds', unit='queue',
                work='assigned', purpose='test', selection=build_copies.ExactCopyRequest(2))

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
                        commit: str | None = None) -> AbstractContextManager[build_copies.LeasedCopy]:
        return build_copies.take_copy(self.source if source is None else source, self.base if commit is None else commit,
                                     showrunner="builds", unit="queue", work="reuse", purpose="test")

    def rescue_refs(self) -> str:
        return self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/")

    def lease(self, path: Path) -> dict[str, object]:
        return cast(dict[str, object], json.loads(path.with_name(path.name + ".lease.json").read_text()))

    def test_reusing_a_copy_ignores_the_old_lease_number(self) -> None:
        with self.take_queue_copy() as copy:
            path = copy.path
            self.assertNotIn("number", self.lease(path))

        lease = self.lease(path)
        lease["slot"] = 99
        sidecar = build_folders.lease_path(path)
        _ = sidecar.write_text(json.dumps(lease))
        record = build_folders.numbered_copy_record(path)
        self.assertIsInstance(record, build_folders.NumberedCopySource)

        with self.take_queue_copy() as reused:
            self.assertEqual(reused.path, path)
            self.assertEqual(reused.number, 1)
            self.assertNotIn("slot", self.lease(path))
            self.assertNotIn("number", self.lease(path))

        lease = self.lease(path)
        self.assertNotIn("slot", lease)
        self.assertNotIn("number", lease)
        self.assertIs(lease["released"], True)

    def test_sweep_respects_the_numbered_copy_lease(self) -> None:
        from scripts.lint import sweep

        with self.take_queue_copy() as copy:
            output = copy.cargo_dir
            lease, = sweep.output_copy_leases(str(output), outputs=sweep.CargoOutputIndex(()))
            self.assertEqual(lease.checkout, copy.path)
            with build_folders.collector_lease(lease.checkout, wait=False) as acquired:
                self.assertFalse(acquired)

        with build_folders.collector_lease(lease.checkout, wait=False) as acquired:
            self.assertTrue(acquired)

    def clone_source(self) -> tuple[Path, str]:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}):
            source = copies.clone(self.source, task="gate:clone-copy")
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

    def test_free_copy_with_only_ignored_folders_is_taken_again(self) -> None:
        _ = (self.source / ".gitignore").write_text("target/\noutput/\n")
        self.base = commit_repository(self.source, "ignore output")
        with self.take_queue_copy() as copy:
            path = copy.path
            draft = path / "output/draft.txt"
            draft.parent.mkdir()
            _ = draft.write_text("ignored file\n")
            self.assertEqual(self.git(path, "check-ignore", str(draft)), str(draft))

        with self.take_queue_copy() as copy:
            self.assertEqual(copy.path, path)

    def test_rescue_saves_an_ignored_file_that_the_next_commit_tracks(self) -> None:
        _ = (self.source / ".gitignore").write_text("target/\noutput/\n")
        self.base = commit_repository(self.source, "ignore output")
        with self.take_queue_copy() as copy:
            draft = copy.path / "output/draft.txt"
            draft.parent.mkdir()
            _ = draft.write_text("local draft\n")

        upstream = self.source / "output/draft.txt"
        upstream.parent.mkdir()
        _ = upstream.write_text("tracked draft\n")
        _ = self.git(self.source, "add", "--force", "--", str(upstream))
        next_commit = commit_repository(self.source, "track the draft")
        before = set(self.rescue_refs().splitlines())
        with self.take_queue_copy(commit=next_commit) as reused:
            self.assertEqual(reused.path, copy.path)
            self.assertEqual(draft.read_text(), "tracked draft\n")
        saved, = set(self.rescue_refs().splitlines()) - before
        self.assertEqual(self.git(self.source, "show", f"{saved}:output/draft.txt"), "local draft")

    def test_free_copy_with_an_untracked_folder_is_kept(self) -> None:
        with self.take_queue_copy() as copy:
            draft = copy.path / "notes/draft.txt"
            draft.parent.mkdir()
            _ = draft.write_text("unsaved work\n")

        notices = io.StringIO()
        with redirect_stderr(notices), self.take_queue_copy() as other:
            self.assertEqual(other.number, 2)
        self.assertIn(f"kept for a manual removal list: {copy.path} (build_copies, untracked folders)",
                      notices.getvalue())
        self.assertEqual(draft.read_text(), "unsaved work\n")

    def make_temporary(self, parent: Path | None = None) -> Path:
        path = (build_folders.build_root() if parent is None else parent) / "p-fixture1"
        path.mkdir(parents=True)
        _ = build_folders.lease_path(path).write_text(json.dumps({"pid": os.getpid()}))
        with build_folders.folder_removal_lease(path, create=True):
            pass
        return path

    def unused_build_folders(self) -> list[build_copies.UnusedBuildFolder]:
        return build_copies.unused_build_folders(build_folders.build_root())

    def test_inventory_lists_only_free_copies_and_measures_them_once(self) -> None:
        with self.take_queue_copy() as held:
            with self.take_queue_copy() as free:
                artifact = free.cargo_dir / "artifact"
                _ = artifact.write_text("build output\n")
            lease = build_folders.lease_path(free.path)
            before = lease.read_bytes()
            with (mock.patch.object(scratch, "borrower_index", wraps=scratch.borrower_index) as index,
                  mock.patch.object(build_copies, "_disk_bytes", return_value=4096) as size):
                candidates = self.unused_build_folders()
            index.assert_called_once()
            size.assert_called_once_with(free.path)
            self.assertEqual(candidates, [build_copies.UnusedBuildFolder(free.path, 4096, "free numbered copy")])
            self.assertTrue(held.path.is_dir())
            self.assertEqual(lease.read_bytes(), before)
            self.assertFalse((git_files.at(free.path).git_dir / build_folders.BUSY).exists())
            self.assertEqual(artifact.read_text(), "build output\n")

    def test_claim_refuses_a_numbered_copy_held_by_take(self) -> None:
        with self.take_queue_copy() as copy:
            with self.assertRaisesRegex(build_copies.BuildFolderInUse, "claim lock"), (
                    build_copies.claim_unused_build_folder(copy.path)):
                self.fail("The claim admitted a held copy")

    def test_keep_file_excludes_a_numbered_copy_from_inventory_and_claims(self) -> None:
        with self.take_queue_copy() as copy:
            path = copy.path
        marker = build_folders.copy_keep_file(path)
        self.assertIn(path, [row.path for row in self.unused_build_folders()])

        for reason, state in (("", "free"), ("checkout cleanup remains unconfirmed\n", "kept")):
            with self.subTest(state=state):
                _ = marker.write_text(reason)
                self.assertEqual(build_folders.build_folder_state(path), state)
                self.assertNotIn(path, [row.path for row in self.unused_build_folders()])
                with self.assertRaisesRegex(build_copies.BuildFolderInUse, "has a keep file"), (
                        build_copies.claim_unused_build_folder(path)):
                    self.fail("The claim admitted a numbered copy with a keep file")

    def test_claim_keeps_a_copy_and_blocks_another_take(self) -> None:
        with self.take_queue_copy() as copy:
            artifact = copy.cargo_dir / "artifact"
            _ = artifact.write_text("build output\n")

        with build_copies.claim_unused_build_folder(copy.path):
            self.assertEqual(artifact.read_text(), "build output\n")
            with self.take_queue_copy() as other:
                self.assertEqual(other.number, 2)
        self.assertEqual(artifact.read_text(), "build output\n")
        with self.take_queue_copy() as reused:
            self.assertEqual(reused.path, copy.path)

    def test_take_refuses_an_ancestor_claim_before_trying_another_number(self) -> None:
        parent = self.make_temporary()
        _ = self.git(self.source, "remote", "set-url", "origin", f"git@example.invalid:group/{parent.name}.git")

        def first_number() -> Iterator[int]:
            yield 1
            self.fail("The take tried a second number")

        with (mock.patch.object(os, "kill", side_effect=ProcessLookupError),
              mock.patch.object(build_copies, "_copy_order", return_value=first_number()),
              build_copies.claim_unused_build_folder(parent)):
            with self.assertRaises(build_folders.BuildFolderClaimHeld) as held, self.take_queue_copy():
                self.fail("The take entered a claimed ancestor")
            self.assertEqual(str(held.exception), f"{parent}: the build folder claim lock is held")

    def test_take_ignores_a_missing_alternate_object_store(self) -> None:
        temporary = self.make_temporary()
        alternates = self.source / ".git/objects/info/alternates"
        store = temporary / "objects"
        _ = alternates.write_text(f"{store}\n")
        root = build_folders.build_root()
        with build_folders.build_folder_claim_lock(temporary, root, exclusive=True):
            with self.take_queue_copy() as copy:
                self.assertEqual(self.git(copy.path, "rev-parse", "HEAD"), self.base)
        _ = shutil.copytree(self.source / ".git/objects", store, ignore=shutil.ignore_patterns("alternates"))
        with build_folders.build_folder_claim_lock(temporary, root, exclusive=True):
            with self.assertRaisesRegex(build_folders.CopySourceBusy, "claim lock is held"), self.take_queue_copy():
                self.fail("The take used an object store in a claimed folder")

    def test_take_ignores_a_regular_file_named_as_an_alternate_object_store(self) -> None:
        temporary = self.make_temporary()
        file = temporary / "objects"
        _ = file.write_text("regular file\n")
        alternates = self.source / ".git/objects/info/alternates"
        _ = alternates.write_text(f"{file}\n")

        with build_folders.build_folder_claim_lock(temporary, build_folders.build_root(), exclusive=True):
            with self.take_queue_copy() as copy:
                self.assertEqual(self.git(copy.path, "rev-parse", "HEAD"), self.base)

            store = self.root / "alternate-objects"
            (store / "info").mkdir(parents=True)
            (store / "info/alternates").symlink_to(file)
            _ = file.write_text(f"{self.source / '.git/objects'}\n")
            _ = alternates.write_text(f"{store}\n")
            with self.assertRaisesRegex(build_folders.CopySourceBusy, "claim lock is held"), self.take_queue_copy():
                self.fail("The take read an alternates file in a claimed folder")

    def test_claim_blocks_a_new_shared_scratch_clone(self) -> None:
        with self.take_queue_copy() as copy:
            path = copy.path

        session = self.root / "session-clones"
        session.mkdir()
        for caller in (None, session):
            with self.subTest(caller=caller), mock.patch.object(session_folder, "caller_folder", return_value=caller):
                with build_copies.claim_unused_build_folder(path):
                    with self.assertRaisesRegex(copies.CopyError, "claim lock"):
                        _ = copies.clone(path, task="gate:blocked")
                clone = copies.clone(path, task="gate:free")
                self.assertTrue((clone / ".git/objects/info/alternates").is_file())

    def test_ssh_source_holds_the_numbered_copy_through_fetch(self) -> None:
        with self.take_queue_copy() as copy:
            path = copy.path
        tree = self.git(path, "rev-parse", "HEAD^{tree}")
        real_git = copies.git
        fetches: list[Path] = []

        def check_fetch(folder: Path, *arguments: str, data: bytes | None = None) -> bytes:
            if arguments[:4] == ("fetch", "-q", "--", str(path)):
                with self.assertRaisesRegex(build_copies.BuildFolderInUse, "claim lock"), (
                        build_copies.claim_unused_build_folder(path)):
                    self.fail("The claim admitted the SSH source during fetch")
                fetches.append(folder)
            return real_git(folder, *arguments, data=data)

        trust = copies.SshSourceTrust(lambda _: False, lambda _: False, lambda _: False)
        with mock.patch.object(copies, "git", side_effect=check_fetch):
            with copies.remote_step_source(path, tree, self.source, trust) as checkout:
                self.assertEqual((checkout / "same.txt").read_text(), "unchanged\n")
        self.assertEqual(fetches, [checkout])

    def test_standing_holds_numbered_sources_before_reading_them(self) -> None:
        with self.take_queue_copy() as source, self.take_queue_copy() as edited:
            _ = (edited.path / "same.txt").write_text("working changes\n")
        policy = copies.WorktreeSource(self.base, edited.path)
        read_commit = copies.source_commit
        apply_changes = copies.apply_changes

        def check_read(folder: Path, requested: copies.SourcePolicy) -> str:
            with self.assertRaisesRegex(build_copies.BuildFolderInUse, "claim lock"), (
                    build_copies.claim_unused_build_folder(folder)):
                self.fail("The claim admitted the source during the commit read")
            return read_commit(folder, requested)

        def check_worktree(tree: Path, worktree: Path, base: str) -> copies.EnvrcReference:
            with self.assertRaisesRegex(build_copies.BuildFolderInUse, "claim lock"), (
                    build_copies.claim_unused_build_folder(worktree)):
                self.fail("The claim admitted the worktree while its changes were read")
            return apply_changes(tree, worktree, base)

        with (mock.patch.object(copies, "source_commit", side_effect=check_read) as read,
              mock.patch.object(copies, "apply_changes", side_effect=check_worktree) as apply,
              copies.standing("another-unit", source.path, policy) as prepared):
            self.assertEqual((prepared / "same.txt").read_text(), "working changes\n")
        read.assert_called_once_with(source.path, policy)
        apply.assert_called_once_with(prepared, edited.path, self.base)

    def test_inventory_and_claim_accept_ownerless_temporary_folders(self) -> None:
        root = build_folders.build_root()
        top = self.make_temporary()
        nested = self.make_temporary(root / "widget")
        artifact = top / "artifact"
        _ = artifact.write_text("build output\n")
        nested_artifact = nested / "output"
        _ = nested_artifact.write_text("build output\n")
        (nested / "artifact").symlink_to(nested_artifact.name)
        (nested / "objects").mkdir()
        stray = root / "widget/stray/1"
        stray.mkdir(parents=True)
        unrecorded = nested.with_name("p-freetmp1")
        unrecorded.mkdir()
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            rows = self.unused_build_folders()
            self.assertEqual([entry.path for entry in rows], [top, nested])
            self.assertTrue(all(entry.reason == "ownerless temporary build folder" for entry in rows))
            self.assertFalse(any(entry.review_only for entry in rows))
            self.assertFalse((root / ".build-claim-locks").exists())
            for path in (top, nested):
                with build_copies.claim_unused_build_folder(path):
                    self.assertTrue(path.is_dir())

    def test_inventory_marks_ownerless_temporary_git_storage_for_review(self) -> None:
        root = build_folders.build_root()
        checkout = self.make_temporary()
        objects = self.make_temporary(root / "objects-fixture")
        repository = checkout / "nested" / "source"
        repository.mkdir(parents=True)
        _ = self.git(repository, "init", "-q")
        _ = shutil.copytree(self.source / ".git/objects", objects / "object-store")
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            rows = self.unused_build_folders()
            self.assertEqual({row.path for row in rows}, {checkout, objects})
            for row in rows:
                with self.subTest(path=row.path):
                    self.assertTrue(row.review_only)
                    self.assertEqual(row.reason, git_files.GIT_STORAGE_REASON)
                    self.assertGreater(row.bytes, 0)
                    with self.assertRaisesRegex(build_copies.BuildFolderReviewOnly, row.reason), (
                            build_copies.claim_unused_build_folder(row.path)):
                        self.fail("The claim admitted a temporary folder with Git storage")
                    cleanup = mock.Mock()
                    self.assertFalse(build_folders.remove_build_folder(row.path, delete=cleanup))
                    cleanup.assert_not_called()

    def test_inventory_and_claim_refuse_an_external_symbolic_link(self) -> None:
        temporary = self.make_temporary()
        nested = temporary / "nested"
        nested.mkdir()
        (nested / "outside").symlink_to(os.path.relpath(self.source.parent, nested), target_is_directory=True)
        self.assertTrue((nested / "outside" / self.source.name / ".git").is_dir())
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            row, = self.unused_build_folders()
            self.assertEqual(row.path, temporary)
            self.assertTrue(row.review_only)
            self.assertEqual(row.reason, build_folders.EXTERNAL_LINK_REASON)
            with self.assertRaisesRegex(build_copies.BuildFolderReviewOnly, row.reason), (
                    build_copies.claim_unused_build_folder(temporary)):
                self.fail("The claim admitted a link outside the temporary folder")
            cleanup = mock.Mock()
            self.assertFalse(build_folders.remove_build_folder(temporary, delete=cleanup))
            cleanup.assert_not_called()

    def test_temporary_review_scans_each_directory_once_without_path_status_calls(self) -> None:
        temporary = self.make_temporary()
        nested = temporary / "nested"
        nested.mkdir()
        artifact = nested / "artifact"
        _ = artifact.write_text("build output\n")
        (temporary / "link").symlink_to("nested/artifact")
        (nested / "link").symlink_to("../nested/artifact")
        with (
            ExitStack() as leases,
            mock.patch.object(os, "scandir", wraps=os.scandir) as scan,
            mock.patch.object(os, "stat", wraps=os.stat) as status,
            mock.patch.object(os, "lstat", wraps=os.lstat) as link_status,
        ):
            self.assertIsNone(build_folders.hold_child_leases(temporary, leases, review_temporary=True))
            self.assertEqual(scan.call_count, 2)
            self.assertTrue(all(call.kwargs.get("dir_fd") is not None for call in status.call_args_list))
            link_status.assert_not_called()

    def test_inventory_and_claim_keep_a_temporary_folder_with_a_live_owner(self) -> None:
        path = self.make_temporary()
        _ = build_folders.lease_path(path).write_text(json.dumps({"pid": os.getpid(), "released": True}))
        self.assertEqual(self.unused_build_folders(), [])
        with self.assertRaisesRegex(build_copies.BuildFolderInUse, "the lease owner is active$"), (
                build_copies.claim_unused_build_folder(path)):
            self.fail("The claim admitted a temporary folder with a live owner")

    def test_inventory_and_claim_keep_a_temporary_folder_with_a_held_lease(self) -> None:
        path = self.make_temporary()
        with (mock.patch.object(os, "kill", side_effect=ProcessLookupError),
              build_folders.folder_removal_lease(path)):
            self.assertEqual(self.unused_build_folders(), [])
            with self.assertRaisesRegex(build_copies.BuildFolderInUse, "a lease lock is active"), (
                    build_copies.claim_unused_build_folder(path)):
                self.fail("The claim admitted a held temporary folder")

    def test_temporary_owner_cleanup_keeps_a_source_during_clone(self) -> None:
        root = build_folders.build_root()
        root.mkdir()
        owner_code = """import sys
from scripts.lib import build_folders as folders
request = folders.UniqueBuildFolder('p-', folders.ExplicitBuildParent(folders.build_root()))
with folders.temporary_build_folder(request) as path:
    print(path, flush=True)
    sys.stdin.read()
"""
        with subprocess.Popen([sys.executable, "-c", owner_code], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as owner:
            assert owner.stdout is not None
            name = cast(str, owner.stdout.readline()).strip()
            self.assertTrue(name, "The owner did not report its folder")
            temporary = Path(name)
            source = temporary / "repository"
            _ = self.git(self.source, "clone", "--", str(self.source), str(source))
            initialize_git = copies.initialize_git

            def finish_owner(tree: Path, checkout: Path) -> None:
                _, errors = owner.communicate(input="", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(owner.returncode, 0, errors)
                self.assertTrue(source.is_dir())
                self.assertTrue(build_folders.lease_path(temporary).is_file())
                self.assertTrue(temporary.with_name(temporary.name + ".lock").is_file())
                with self.assertRaisesRegex(build_copies.BuildFolderInUse, "claim lock"), (
                        build_copies.claim_unused_build_folder(temporary)):
                    self.fail("The claim admitted the temporary source during clone")
                initialize_git(tree, checkout)

            with mock.patch.object(copies, "initialize_git", side_effect=finish_owner):
                clone = copies.clone(source, task="gate:temporary-owner")

        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), self.base)
        shutil.rmtree(clone)
        self.assertEqual([entry.path for entry in self.unused_build_folders() if entry.review_only], [temporary])
        with self.assertRaisesRegex(build_copies.BuildFolderReviewOnly, git_files.GIT_STORAGE_REASON), (
                build_copies.claim_unused_build_folder(temporary)):
            self.fail("The claim admitted a source after the clone ended")

    def test_temporary_owner_cleanup_keeps_the_source_of_a_finished_clone(self) -> None:
        root = build_folders.build_root()
        root.mkdir()
        request = build_folders.UniqueBuildFolder("p-", build_folders.ExplicitBuildParent(root))
        with build_folders.temporary_build_folder(request) as temporary:
            source = temporary / "repository"
            _ = self.git(self.source, "clone", "--", str(self.source), str(source))
            clone = copies.clone(source, task="gate:finished-clone")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(clone, "show", "HEAD:same.txt"), "unchanged")
        self.assertTrue(source.is_dir())
        self.assertTrue(build_folders.lease_path(temporary).is_file())

    def test_temporary_claim_blocks_a_clone_and_refuses_an_existing_borrower(self) -> None:
        temporary = self.make_temporary()
        source = temporary / "repository"
        _ = self.git(self.source, "clone", "--", str(self.source), str(source))
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            with build_folders.build_folder_claim_lock(temporary, build_folders.build_root(), exclusive=True):
                with self.assertRaisesRegex(copies.CopyError, "claim lock"):
                    _ = copies.clone(source, task="gate:blocked")
                with self.assertRaisesRegex(scratch.ScratchError, "claim lock"), scratch.copy_lock(source):
                    self.fail("Scratch admitted a claimed temporary source")
            clone = copies.clone(source, task="gate:borrowed")
            self.assertEqual(self.git(clone, "rev-parse", "HEAD"), self.base)
            with self.assertRaisesRegex(build_copies.BuildFolderReviewOnly, git_files.GIT_STORAGE_REASON), (
                    build_copies.claim_unused_build_folder(temporary)):
                self.fail("The claim admitted the source of a scratch clone")
            self.assertEqual([entry.path for entry in self.unused_build_folders() if entry.review_only], [temporary])

    def test_temporary_claim_refuses_the_source_of_a_numbered_copy(self) -> None:
        temporary = self.make_temporary()
        source = temporary / "repository"
        _ = self.git(self.source, "clone", "--", str(self.source), str(source))
        _ = self.git(source, "remote", "set-url", "origin", "git@example.invalid:group/widget.git")
        with self.take_queue_copy(source, self.base) as copy:
            pass
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            with self.assertRaisesRegex(build_copies.BuildFolderReviewOnly, git_files.GIT_STORAGE_REASON), (
                    build_copies.claim_unused_build_folder(temporary)):
                self.fail("The claim admitted the source of a numbered copy")
            rows = self.unused_build_folders()
        self.assertIn(copy.path, [entry.path for entry in rows if not entry.review_only])
        self.assertEqual([entry.path for entry in rows if entry.review_only], [temporary])

    def test_numbered_claim_refuses_direct_and_transitive_numbered_borrowers(self) -> None:
        with self.take_queue_copy() as candidate:
            source = candidate.path / "repository"
            _ = self.git(self.source, "clone", "--", str(self.source), str(source))
        middle = self.root / "middle"
        outside = self.root / "outside"
        _ = self.git(source, "clone", "--shared", "--", str(source), str(middle))
        _ = self.git(middle, "clone", "--shared", "--", str(middle), str(outside))
        middle_alternates = middle / ".git/objects/info/alternates"
        for unit, repository in (("alternate-borrower", outside), ("direct-borrower", source)):
            with self.subTest(unit=unit):
                if repository == source:
                    _ = middle_alternates.write_text(f"{self.source / '.git/objects'}\n")
                _ = self.git(repository, "remote", "set-url", "origin", "git@example.invalid:group/widget.git")
                with build_copies.take_copy(repository, self.base, showrunner="builds", unit=unit,
                                            work="proof", purpose="test") as borrower:
                    pass
                with self.assertRaisesRegex(build_copies.BuildFolderInUse, "still uses its Git source") as refusal:
                    with build_copies.claim_unused_build_folder(candidate.path):
                        self.fail("The claim admitted a source that a numbered copy uses")
                self.assertIn(str(borrower.path), str(refusal.exception))
                self.assertNotIn(candidate.path, [entry.path for entry in self.unused_build_folders()])
                self.assertEqual(self.git(borrower.path, "show", "HEAD:same.txt"), "unchanged")

    def test_claim_protects_a_git_directory_link_used_by_an_outside_checkout(self) -> None:
        with self.take_queue_copy() as copy:
            pass
        link = copy.path / "git-directory"
        link.symlink_to(self.source / ".git", target_is_directory=True)
        hop = self.root / "git-hop"
        hop.symlink_to(link, target_is_directory=True)
        outside = copy_record.copies_root() / "widget-legacy" / "borrower"
        with build_copies.claim_unused_build_folder(copy.path):
            outside.mkdir(parents=True)
            _ = (outside / ".git").write_text(f"gitdir: {hop}\n")
            with self.assertRaisesRegex(copies.CopyError, "claim lock"):
                _ = copies.clone(outside, task="gate:git-directory-link")
        self.assertEqual(self.git(outside, "rev-parse", "HEAD"), self.base)
        with self.assertRaisesRegex(build_copies.BuildFolderInUse, "still uses a Git directory link"), (
                build_copies.claim_unused_build_folder(copy.path)):
            self.fail("The claim admitted a Git directory link that an outside checkout uses")
        self.assertEqual(self.unused_build_folders(), [])

    def test_claim_protects_a_git_directory_with_an_external_common_directory(self) -> None:
        with self.take_queue_copy() as copy:
            pass
        outside = copy_record.copies_root() / "widget-legacy" / "borrower"
        outside.parent.mkdir(parents=True)
        _ = self.git(self.source, "worktree", "add", "--detach", str(outside))
        checkout = git_files.at(outside)
        admin = copy.path / "admin"
        _ = checkout.git_dir.rename(admin)
        _ = (admin / "commondir").write_text(f"{checkout.common_dir}\n")
        _ = (outside / ".git").write_text(f"gitdir: {admin}\n")
        self.assertEqual(self.git(outside, "show", "HEAD:same.txt"), "unchanged")
        with self.assertRaisesRegex(build_copies.BuildFolderInUse, "still uses a Git directory in"), (
                build_copies.claim_unused_build_folder(copy.path)):
            self.fail("The claim admitted the Git directory of an outside checkout")
        self.assertEqual(self.unused_build_folders(), [])

    def test_temporary_claim_checks_transitive_alternates_and_intermediate_links(self) -> None:
        temporary = self.make_temporary()
        repository = temporary / "repository"
        _ = self.git(self.source, "clone", "--", str(self.source), str(repository))
        outside = self.root / "unregistered"
        _ = self.git(self.source, "clone", "--shared", "--", str(self.source), str(outside))
        _ = (outside / ".git/objects/info/alternates").write_text(str(repository / ".git/objects") + "\n")
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            with build_folders.build_folder_claim_lock(temporary, build_folders.build_root(), exclusive=True):
                with self.assertRaisesRegex(copies.CopyError, "claim lock"):
                    _ = copies.clone(outside, task="gate:alternate")
            shutil.rmtree(repository)

            alternate = temporary / "objects-list"
            _ = alternate.write_text(str(self.source / ".git/objects") + "\n")
            intermediate = temporary / "objects-link"
            intermediate.symlink_to(alternate)
            borrower = copy_record.copies_root() / "widget-legacy" / "borrower"
            borrower.mkdir(parents=True)
            _ = self.git(borrower, "init", "-q")
            (borrower / ".git/objects/info/alternates").symlink_to(intermediate)
            with self.assertRaisesRegex(build_copies.BuildFolderInUse, "uses an alternates file"), (
                    build_copies.claim_unused_build_folder(temporary)):
                self.fail("The claim admitted an intermediate alternates link")
            self.assertEqual(self.unused_build_folders(), [])

    def test_worktree_paths_outside_a_claimable_folder_do_not_take_claim_locks(self) -> None:
        worktree = build_folders.build_root() / "widget/queue/worktree"
        with build_folders.build_folder_use(worktree) as descriptors:
            self.assertEqual(descriptors, ())
        self.assertFalse((build_folders.build_root() / ".build-claim-locks").exists())

    def test_inventory_lists_a_gone_unit_after_all_its_copies(self) -> None:
        with mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())):
            with self.take_queue_copy() as held:
                with self.take_queue_copy() as other:
                    path = other.path
                unit = held.path.parent
                self.assertEqual([entry.path for entry in self.unused_build_folders()], [path])
            self.assertEqual([entry.path for entry in self.unused_build_folders()], [held.path, path, unit])
            self.assertTrue(unit.is_dir())
            _ = (unit / "unrelated").write_text("keep this file")
            self.assertEqual([entry.path for entry in self.unused_build_folders()], [held.path, path])
            empty = unit.with_name("former-unit")
            with build_folders.unit_allocation_lock(empty, exclusive=False):
                empty.mkdir()
                self.assertNotIn(empty, [entry.path for entry in self.unused_build_folders()])
            self.assertIn(empty, [entry.path for entry in self.unused_build_folders()])

    def test_claim_refuses_a_source_used_by_a_sessionless_clone(self) -> None:
        with self.take_queue_copy() as copy:
            source = copy.path / "repository"
            _ = self.git(self.source, "clone", "--", str(self.source), str(source))
        with mock.patch.object(session_folder, "caller_folder", return_value=None):
            clone = copies.clone(source, task="gate:borrowed")
        self.assertEqual(clone.parent, copy_record.root())
        self.assertNotIn(str(clone), scratch.holds())
        with self.assertRaisesRegex(build_copies.BuildFolderInUse, "still uses its git objects"), (
                build_copies.claim_unused_build_folder(copy.path)):
            self.fail("The claim admitted a source with a scratch clone")
        self.assertNotIn(copy.path, [entry.path for entry in self.unused_build_folders()])

    def test_claim_blocks_a_new_child_maker(self) -> None:
        with self.take_queue_copy() as copy:
            path = copy.path
        with build_copies.claim_unused_build_folder(path):
            with self.assertRaisesRegex(build_folders.BuildFolderClaimHeld, "claim lock"), (
                    build_folders.tool_folder("tool", parent=path)):
                self.fail("A tool started inside the claimed copy")
        self.assertFalse((path / "tool").exists())
        with build_folders.tool_folder("tool", parent=path) as child:
            self.assertTrue(child.is_dir())

    def test_claim_blocks_tool_folder_after_removal_of_its_temporary_parent(self) -> None:
        path = self.make_temporary()
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            with build_copies.claim_unused_build_folder(path):
                self.assertTrue(build_folders.remove_build_folder(path))
                self.assertFalse(build_folders.lease_path(path).exists())
                with self.assertRaisesRegex(build_folders.BuildFolderClaimHeld, "claim lock"), (
                        build_folders.tool_folder("tool", parent=path)):
                    self.fail("A tool created its parent during the claim")
                self.assertFalse(path.exists())
        with build_folders.tool_folder("tool", parent=path) as child:
            self.assertTrue(child.is_dir())

    def test_claim_blocks_stable_folder_after_removal_of_its_temporary_parent(self) -> None:
        path = self.make_temporary()
        child = path / "stable"
        command = [sys.executable, "-m", "scripts.lib.build_folders", "stable", "--path", str(child)]
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            with build_copies.claim_unused_build_folder(path):
                self.assertTrue(build_folders.remove_build_folder(path))
                self.assertFalse(build_folders.lease_path(path).exists())
                result = subprocess.run(command, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("claim lock is held", result.stderr)
                with self.assertRaisesRegex(build_folders.BuildFolderClaimHeld, "claim lock"), (
                        build_folder_lock.folder_parent(child, build_folders.build_root(), create=True)):
                    self.fail("The parent opener created a folder during the claim")
                self.assertFalse(path.exists())
        result = subprocess.run(command, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(child.is_dir())

    def test_nested_temporary_claim_blocks_a_parent_lease_and_a_tool(self) -> None:
        parent = build_folders.build_root() / "p-testroot"
        path = self.make_temporary(parent)
        command = [sys.executable, "-m", "scripts.lib.build_folders", "stable", "--path", str(parent)]
        self.assertFalse(build_folders.lease_path(parent).exists())
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            with build_copies.claim_unused_build_folder(path):
                result = subprocess.run(command, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("nested build folder claim lock is held", result.stderr)
                self.assertFalse(build_folders.lease_path(parent).exists())
                with self.assertRaisesRegex(build_folders.BuildFolderClaimHeld, "claim lock"), (
                        build_folders.tool_folder("tool", parent=path)):
                    self.fail("A tool started inside the claimed folder")
        result = subprocess.run(command, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(build_folders.lease_path(parent).is_file())
        with build_folders.tool_folder("tool", parent=path) as tool:
            self.assertTrue(tool.is_dir())

    def test_claim_blocks_a_workspace_in_a_plain_descendant(self) -> None:
        with self.take_queue_copy() as copy:
            tree = copy.path / "plain"
            tree.mkdir()
        with build_copies.claim_unused_build_folder(copy.path):
            with self.assertRaisesRegex(copies.CopyError, "claim lock"), copies.workspace(
                    "queue", self.source, self.base, name="plain", branch="work", path=tree):
                self.fail("A workspace started inside the claimed copy")
        self.assertEqual(list(tree.iterdir()), [])

    def test_claim_blocks_an_alternate_from_a_worktree_source(self) -> None:
        with self.take_queue_copy() as copy:
            source = copy.path
        tree = self.root / "private-copy"
        tree.mkdir()
        _ = self.git(tree, "init", "-q")
        alternate = tree / ".git/objects/info/alternates"
        policy = copies.WorktreeSource(self.base, source)
        with build_copies.claim_unused_build_folder(source):
            with self.assertRaisesRegex(build_folders.BuildFolderClaimHeld, "claim lock"), (
                    copies.requested_source(self.source, policy, copies.NO_MERGE)):
                self.fail("The snapshot read a worktree during the claim")
            with self.assertRaisesRegex(build_folders.BuildFolderClaimHeld, "claim lock"), (
                    copies.worktree_objects(tree, source)):
                self.fail("The copy added a borrower during the claim")
            self.assertFalse(alternate.exists())
        with copies.worktree_objects(tree, source):
            self.assertEqual(alternate.read_text().strip(), str(self.source / ".git/objects"))
        with mock.patch.object(build_folders, "inherited_temporary_parent", return_value=self.root):
            with copies.requested_source(self.source, policy, copies.NO_MERGE) as snapshot:
                self.assertTrue((snapshot / "same.txt").is_file())
                with self.assertRaisesRegex(build_copies.BuildFolderInUse, "claim lock"), (
                        build_copies.claim_unused_build_folder(source)):
                    self.fail("The claim admitted a worktree source still in use")

    def test_claim_refuses_an_intermediate_alternates_link(self) -> None:
        with self.take_queue_copy() as copy:
            intermediate = copy.path / "objects-link"
            alternate = self.root / "objects-list"
            _ = alternate.write_text(str(self.source / ".git/objects") + "\n")
            intermediate.symlink_to(alternate)
        borrower = copy_record.copies_root() / "widget-legacy" / "borrower"
        borrower.mkdir(parents=True)
        _ = self.git(borrower, "init", "-q")
        (borrower / ".git/objects/info/alternates").symlink_to(intermediate)
        with self.assertRaisesRegex(build_copies.BuildFolderInUse, "uses an alternates file"), (
                build_copies.claim_unused_build_folder(copy.path)):
            self.fail("The claim admitted an intermediate alternates link")
        self.assertNotIn(copy.path, [entry.path for entry in self.unused_build_folders()])

    def test_claim_blocks_a_clone_that_needs_an_alternate_in_a_copy(self) -> None:
        alternate_source = self.root / "unregistered"
        _ = self.git(self.source, "clone", "--shared", "--", str(self.source), str(alternate_source))
        with self.take_queue_copy() as copy:
            nested = copy.path / "repository"
            _ = self.git(self.source, "clone", "--", str(self.source), str(nested))
        alternate = alternate_source / ".git/objects/info/alternates"
        _ = alternate.write_text(str(nested / ".git/objects") + "\n")
        with build_copies.claim_unused_build_folder(copy.path):
            with self.assertRaisesRegex(copies.CopyError, "claim lock"):
                _ = copies.clone(alternate_source, task="gate:alternate")
        clone = copies.clone(alternate_source, task="gate:alternate")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), self.base)
        with self.assertRaisesRegex(build_copies.BuildFolderInUse, "still uses its git objects"), (
                build_copies.claim_unused_build_folder(copy.path)):
            self.fail("The claim admitted a numbered copy with a transitive borrower")

    def test_claim_refuses_a_child_lease_and_live_owner(self) -> None:
        with self.take_queue_copy() as copy:
            pass
        specification = build_folders.BuildFolderSpec(
            kind="tool", parent=copy.path, purpose="tool", suffix_length=0)
        with build_folders.leased_build_folder(specification) as child:
            with self.assertRaisesRegex(build_copies.BuildFolderInUse, r"tool\.lock"), (
                    build_copies.claim_unused_build_folder(copy.path)):
                self.fail("The claim admitted a held child lease")
        with self.assertRaisesRegex(build_copies.BuildFolderInUse, r"tool\.lease\.json"), (
                build_copies.claim_unused_build_folder(copy.path)):
            self.fail(f"The claim admitted the live owner of {child}")

    def test_claim_refuses_nested_and_external_busy_marks(self) -> None:
        with self.take_queue_copy() as copy:
            path = copy.path
            checkout = path / "worktree"
            _ = self.git(self.source, "worktree", "add", "--detach", str(checkout))
            nested = path / "plain/nested"
            nested.mkdir(parents=True)
        git_directory = git_files.at(checkout).git_dir
        self.assertFalse(git_directory.is_relative_to(path))
        for folder in (nested, git_directory):
            with self.subTest(folder=folder), (folder / build_folders.BUSY).open("w") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                with self.assertRaisesRegex(build_copies.BuildFolderInUse, "busy mark is held"), (
                        build_copies.claim_unused_build_folder(path)):
                    self.fail(f"The claim ignored the busy mark in {folder}")
                self.assertNotIn(path, [entry.path for entry in self.unused_build_folders()])
        with build_copies.claim_unused_build_folder(path):
            self.assertTrue(checkout.is_dir())

    def test_repository_with_p_prefix_still_lists_numbered_copies(self) -> None:
        _ = self.git(self.source, "remote", "set-url", "origin", "git@example.invalid:group/p-abcd1234.git")
        with self.take_queue_copy() as copy:
            path = copy.path
        self.assertEqual(path.parent.parent.name, "p-abcd1234")
        self.assertIn(path, [entry.path for entry in self.unused_build_folders()])

    def test_claim_refuses_a_copy_with_a_borrower(self) -> None:
        with self.take_queue_copy() as copy:
            path = copy.path
        record = build_folders.numbered_copy_record(path)
        assert isinstance(record, build_folders.NumberedCopySource)
        borrower = copy_record.copies_root() / "widget-legacy" / "borrower"
        borrower.mkdir(parents=True)
        with mock.patch.object(scratch, "borrower_index", wraps=scratch.borrower_index) as index:
            first = self.unused_build_folders()
            self.assertEqual([entry.path for entry in first], [path])
            _ = (borrower / copy_record.RECORD).write_text(json.dumps({
                "task": "gate:borrower", "borrows_from": str(record.git_directory / "objects"),
            }))
            with self.assertRaisesRegex(build_copies.BuildFolderInUse, "still uses its git objects"), (
                    build_copies.claim_unused_build_folder(path)):
                self.fail("The claim admitted the source of a borrower")
            self.assertEqual(index.call_count, 2)
        self.assertEqual(self.unused_build_folders(), [])
        self.assertTrue(path.is_dir())

    def test_copy_from_a_clone_removes_its_private_ref_on_release_and_failure(self) -> None:
        source, commit = self.clone_source()
        with self.take_queue_copy() as first:
            path = first.path
        reference = "refs/build-copies/"

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
                original = cast(Callable[..., str], getattr(build_copies, "_git"))

                def fail_git(folder: Path, *arguments: str, pass_fds: Sequence[int] = (),
                             accepted: tuple[int, ...] = (0,)) -> str:
                    result = original(folder, *arguments, pass_fds=pass_fds, accepted=accepted)
                    if ((ending == "checkout failure" and "checkout" in arguments)
                            or (ending == "fetch failure" and "fetch" in arguments)):
                        raise build_copies.CopyGitFailure("injected Git failure")
                    return result

                with mock.patch.object(build_copies, "_git", side_effect=fail_git):
                    if ending in ("checkout failure", "fetch failure"):
                        with self.assertRaisesRegex(build_copies.CopyGitFailure, "injected Git failure"):
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
        original = cast(Callable[..., str], getattr(build_copies, "_git"))
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

        with mock.patch.object(build_copies, "_git", side_effect=observe_git):
            with self.assertRaises(build_copies.CopyRepositoryMismatch):
                with self.take_queue_copy(source, commit):
                    self.fail("the unrelated repository received a copy")
        self.assertEqual(fetched, [commit])
        self.assertEqual(comparisons, [("merge-base", references[0], self.base)])
        self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(path, "status", "--porcelain=v2"), before)
        self.assertEqual(self.git(self.source, "for-each-ref", "refs/build-copies/"), "")
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
                with self.assertRaisesRegex(build_copies.CopyGitFailure, reason):
                    with self.take_queue_copy(source, commit):
                        self.fail("The invalid copy record created a copy.")

    def test_two_copies_share_one_git_config_migration(self) -> None:
        common = self.source / ".git"
        lock_path = common / "build-copy-config.lock"
        first_read, resume_first = threading.Event(), threading.Event()
        git = cast(Callable[..., str], getattr(build_copies, "_git"))
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
            if mode == fcntl.LOCK_EX | fcntl.LOCK_NB and lock_path.exists() and os.path.samestat(os.fstat(descriptor), lock_path.stat()):
                try:
                    flock(descriptor, mode)
                except BlockingIOError:
                    resume_first.set()
                    raise
            else:
                flock(descriptor, mode)

        def take(number: int) -> Path:
            with build_copies.take_copy(self.source, self.base, showrunner="builds", unit="queue",
                    work="proof", purpose="test", selection=build_copies.ExactCopyRequest(number)) as copy:
                return copy.path

        with (mock.patch.object(build_copies, "_git", side_effect=pause_after_read),
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

    def test_config_migration_times_out_when_the_repository_lock_is_held(self) -> None:
        common = self.source / ".git"
        lock_path = common / "build-copy-config.lock"
        enable_worktree_config = cast(Callable[[Path, Path, int], None], getattr(build_copies, "_enable_worktree_config"))
        with lock_path.open("w") as held, mock.patch.object(build_folders, "repository_git_timeout", return_value=0.3):
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with self.assertRaisesRegex(build_copies.CopyGitFailure,
                    r"build-copy-config\.lock: timed out after 0\.3 seconds waiting for the repository config lock"):
                enable_worktree_config(self.source, common, held.fileno())
        self.assertNotIn("extensions.worktreeconfig=", self.git(self.source, "config", "--local", "--list"))

    def test_numbered_take_opens_the_claim_once(self) -> None:
        root = build_folders.build_root()
        with mock.patch.object(build_folder_lock, "hold_named_lock", wraps=build_folder_lock.hold_named_lock) as lock:
            with self.take_queue_copy() as copy:
                relative = copy.path.relative_to(root)
                claim = root / ".build-claim-locks" / relative
                unit = root / ".unit-locks" / relative.parent
                self.assertEqual([call.args for call in lock.call_args_list],
                                 [(unit, root), (claim.parent.parent, root), (claim.parent, root), (claim, root)])
                inode = claim.with_suffix(".lock").stat()
                matches = [fd for fd in copy.pass_fds if os.path.samestat(os.fstat(fd), inode)]
                self.assertEqual(len(matches), 1)

    def test_claim_and_unit_locks_refuse_a_changed_inode(self) -> None:
        with self.take_queue_copy() as copy:
            pass
        with mock.patch.object(build_folder_lock, "lock_current", return_value=False):
            with self.assertRaisesRegex(ValueError, "unit lock changed"), (
                    build_folders.unit_allocation_lock(copy.path.parent, exclusive=True)):
                self.fail("The unit allocation accepted a changed lock")
            with self.assertRaisesRegex(build_copies.BuildFolderInUse, "claim lock changed"), (
                    build_copies.claim_unused_build_folder(copy.path)):
                self.fail("The claim accepted a changed lock")
        with build_copies.claim_unused_build_folder(copy.path):
            self.assertTrue(copy.path.is_dir())

    def test_git_inherits_the_shared_lock_during_copy_config_migration(self) -> None:
        lock_path = self.source / ".git/build-copy-config.lock"
        git = cast(Callable[..., str], getattr(build_copies, "_git"))
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

        with mock.patch.object(build_copies, "_git", side_effect=observe_git), self.take_queue_copy():
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

    def test_rows_28_29_fixed_reuse_refuses_untracked_directories_and_keeps_ignored_files(self) -> None:
        _ = (self.source / ".gitignore").write_text("target/\n*.cache\n")
        revision = commit_repository(self.source, "ignore build cache")
        reserved = ("target", build_folders.COPY_USE_STAMP)
        with build_folders.fixed_repository_build_folder("widget", machine="mac-linux", unit="queue") as path:
            build_copies.prepare_fixed_checkout(self.source, path, revision, reserved=reserved)
            untracked = path / "untracked"
            untracked.mkdir()
            proof = untracked / "proof"
            _ = proof.write_text("untracked changes")
            tracked = path / "change.txt"
            _ = tracked.write_text("keep edits before refusal")
            with self.assertRaisesRegex(build_copies.UntrackedCopyDirectories, "untracked folders"):
                build_copies.prepare_fixed_checkout(self.source, path, revision, reserved=reserved)
            self.assertEqual(tracked.read_text(), "keep edits before refusal")
            self.assertEqual(proof.read_text(), "untracked changes")
            proof.unlink()
            untracked.rmdir()
            ignored = path / "scratch.cache"
            _ = ignored.write_text("keep cache")
            build_copies.prepare_fixed_checkout(self.source, path, revision, reserved=reserved)
            self.assertEqual(ignored.read_text(), "keep cache")
            self.assertEqual(tracked.read_text(), "first commit\n")

    def test_row_54_fixed_registration_rescues_existing_files_before_checkout(self) -> None:
        reserved = ("target", build_folders.COPY_USE_STAMP)
        with build_folders.fixed_repository_build_folder("widget", machine="mac-linux", unit="queue") as path:
            tracked = path / "change.txt"
            _ = tracked.write_text("prior checkout content\n")
            build_copies.prepare_fixed_checkout(self.source, path, self.base, reserved=reserved)
            self.assertEqual(tracked.read_text(), "first commit\n")
            self.assertIn("source", self.lease(path))
        saved, = self.rescue_refs().splitlines()
        self.assertEqual(self.git(self.source, "show", f"{saved}:change.txt"), "prior checkout content")

    def test_row_28_fixed_reuse_refuses_a_changed_source_record(self) -> None:
        reserved = ("target", build_folders.COPY_USE_STAMP)
        with build_folders.fixed_repository_build_folder("widget", machine="mac-linux", unit="queue") as path:
            build_copies.prepare_fixed_checkout(self.source, path, self.base, reserved=reserved)
            _ = (path / "change.txt").write_text("keep after refusal")
        lease = self.lease(path)
        lease["source"] = {}
        _ = build_folders.lease_path(path).write_text(json.dumps(lease))
        with build_folders.fixed_repository_build_folder("widget", machine="mac-linux", unit="queue") as reused:
            with self.assertRaisesRegex(ValueError, "fixed checkout source changed"):
                build_copies.prepare_fixed_checkout(self.source, reused, self.base, reserved=reserved)
        self.assertEqual((path / "change.txt").read_text(), "keep after refusal")

    def test_row_28_fixed_reuse_refuses_a_missing_source_record(self) -> None:
        reserved = ("target", build_folders.COPY_USE_STAMP)
        with build_folders.fixed_repository_build_folder("widget", machine="mac-linux", unit="queue") as path:
            build_copies.prepare_fixed_checkout(self.source, path, self.base, reserved=reserved)
            tracked = path / "change.txt"
            _ = tracked.write_text("keep after refusal")
        lease = self.lease(path)
        del lease["source"]
        _ = build_folders.lease_path(path).write_text(json.dumps(lease))

        with build_folders.fixed_repository_build_folder("widget", machine="mac-linux", unit="queue") as reused:
            with self.assertRaisesRegex(ValueError, "no saved source record"):
                build_copies.prepare_fixed_checkout(self.source, reused, self.base, reserved=reserved)

        self.assertEqual(tracked.read_text(), "keep after refusal")
        self.assertNotIn("source", self.lease(path))

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
            build_copies.limit_copy_size(reused, 64)
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

    def test_row_138_cargo_reset_refuses_a_held_child_lease(self) -> None:
        with self.take_queue_copy() as copy:
            child = copy.cargo_dir / "child"
            child.mkdir()
            artifact = child / "artifact"
            _ = artifact.write_text("Cargo output")
            with child.with_suffix(".lock").open("wb") as lock:
                build_folder_lock.mark_lock(lock.fileno())
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(build_folders.reset_numbered_copy_cargo(copy.path))
                self.assertEqual(artifact.read_text(), "Cargo output")
                self.assertNotIn("cargo_removal", self.lease(copy.path))
            self.assertTrue(build_folders.reset_numbered_copy_cargo(copy.path))
            self.assertFalse(copy.cargo_dir.exists())

    def test_row_197_cargo_recovery_refuses_a_held_child_lease(self) -> None:
        path = self.interrupt_cargo_reset()
        stage, = (entry for entry in path.glob(".removing-*") if entry.is_dir())
        child = stage / "child"
        child.mkdir()
        recorded = self.lease(path)["cargo_removal"]
        with child.with_suffix(".lock").open("wb") as lock:
            build_folder_lock.mark_lock(lock.fileno())
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (self.assertRaisesRegex(BlockingIOError, "assigned copy 2"),
                  build_copies.take_copy(self.source, self.base, showrunner="builds", unit="queue",
                                        work="next", purpose="test", selection=build_copies.ExactCopyRequest(2))):
                self.fail("Cargo recovery removed a child with a held lease")
            self.assertEqual(self.lease(path)["cargo_removal"], recorded)
            self.assertEqual((stage / "old").read_text(), "old Cargo output")
            self.assertTrue(child.is_dir())
        self.assert_cargo_reset_recovers(path)

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
                  build_copies.take_copy(self.source, self.base, showrunner="builds", unit="queue",
                                        work="next", purpose="test", selection=build_copies.ExactCopyRequest(2))):
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

from scripts.lib import build_folders, build_copies

rename = build_folders._rename_without_replacing

def stop_after_staging(directory, source, destination):
    rename(directory, source, destination)
    if source == "target" and destination.startswith(".removing-"):
        os.kill(os.getpid(), signal.SIGKILL)

with build_copies.take_copy(Path(sys.argv[1]), sys.argv[2], showrunner="builds", unit="queue",
                           work="reset", purpose="test", selection=build_copies.ExactCopyRequest(2)) as copy:
    (copy.cargo_dir / "old").write_text("old Cargo output")
    with patch.object(build_folders, "_rename_without_replacing", stop_after_staging):
        build_copies.limit_copy_size(copy, 0)
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
        with build_copies.take_copy(self.source, self.base, showrunner="builds", unit="queue",
                                  work="next", purpose="test", selection=build_copies.ExactCopyRequest(2)) as copy:
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
    def test_run_stops_a_detached_descendant_before_it_releases_the_copy(self) -> None:
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
            with build_copies.take_copy(self.source, "HEAD", showrunner="builds", unit="queue", work="proof", purpose="test") as reused:
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
from scripts.lib import build_folders, build_copies
from scripts.lib.descendants import adopt_descendants
adopt_descendants()
with build_copies.take_copy(pathlib.Path(sys.argv[1]), sys.argv[2], showrunner='builds',
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
    def test_git_keeps_the_copy_locked_after_its_caller_dies(self) -> None:
        _ = adopt_descendants()
        real_git = shutil.which("git")
        assert real_git is not None
        binaries = self.root / "bin"
        binaries.mkdir()
        fake = binaries / "git"
        _ = fake.write_text(f"#!{sys.executable}\n" + """import json, os, socket, sys
arguments = sys.argv[1:]
command = ' '.join(arguments[2:4]) if arguments[2] == 'worktree' else arguments[2]
if command == os.environ['COPY_GIT_OPERATION']:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.connect('\\0' + os.environ['COPY_GIT_SOCKET'])
        peer.sendall((json.dumps({'pid': os.getpid(), 'command': command}) + '\\n').encode())
        peer.recv(1)
    raise SystemExit(0)
os.execv(os.environ['COPY_REAL_GIT'], [os.environ['COPY_REAL_GIT'], *arguments])
""")
        fake.chmod(0o755)
        checkout = Path(__file__).resolve().parents[2]
        for operation in ("worktree add", "checkout", "clean"):
            with self.subTest(operation=operation):
                unit = operation.replace(" ", "-")
                path = build_folders.copy_path("widget", unit, 1)
                if operation != "worktree add":
                    with build_copies.take_copy(
                            self.source, self.base, showrunner="builds", unit=unit, work="proof", purpose="test") as copy:
                        self.assertEqual(copy.path, path)
                address = f"copy-git-{os.getpid()}"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.settimeout(TEST_HANG_GUARD_SECS)
                    listener.bind("\0" + address)
                    listener.listen(1)
                    arguments = (*self.run_arguments(unit), sys.executable, "-c", "pass")
                    process = subprocess.Popen([sys.executable, "-m", "scripts.lib.build_copies", *arguments],
                                               cwd=checkout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                               env={**os.environ, "PYTHONPATH": str(checkout),
                                                    "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                                                    "COPY_REAL_GIT": real_git, "COPY_GIT_OPERATION": operation,
                                                    "COPY_GIT_SOCKET": address})
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
                                with build_copies.take_copy(
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
