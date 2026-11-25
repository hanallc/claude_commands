"""Test removal checks with private folders and a private mount table."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from typing import override
from unittest import mock

from scripts.disks import cleanup_old_folders
from scripts.lib import cargo_output, removal_guard, scratch
from scripts.production import staff


class RemovalGuardTests(unittest.TestCase):
    fixture: Path = Path()
    home: Path = Path()
    root: Path = Path()
    config: Path = Path()
    target: Path = Path()
    names: removal_guard.KnownNames = removal_guard.KnownNames(
        frozenset(("sample", "empty", "session", "claude")), frozenset(("Unit", "scratch", "CI", "Main")))

    @override
    def setUp(self) -> None:
        self.fixture = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.fixture / "home"
        self.root = self.fixture / "build"
        self.target = self.root / "sample" / "unit" / "short"
        self.config = self.fixture / "build_folders.conf"
        self.home.mkdir()
        self.target.mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home), "CONF_DIR": str(self.fixture)}))
        _ = self.config.write_text("removal.path_templates=" + " ".join(
            str(self.root.joinpath(*form.value)) for form in removal_guard.Form) + "\n")

    def guard(self, *mounts: Path) -> removal_guard.RemovalGuard:
        return removal_guard.read(mount_reader=lambda: (Path("/"), self.root, *mounts),
                                  build_top=self.root, names=self.names)

    def assert_refused(self, path: Path, reason: removal_guard.RefusalReason,
                       *, mounts: tuple[Path, ...] = ()) -> None:
        with self.assertRaises(removal_guard.Refused) as result:
            with self.guard(*mounts).check(path):
                self.fail("the guard approved the folder")
        self.assertEqual(result.exception.reason, reason)

    def test_whole_folder_removal_keeps_symlink_targets(self) -> None:
        keep = self.home / "keep"
        _ = keep.write_text("outside")
        (self.target / "link").symlink_to(keep)
        with self.guard().check(self.target) as checked:
            removal_guard.remove(checked)
        self.assertFalse(self.target.exists())
        self.assertEqual(keep.read_text(), "outside")

    def test_mount_at_between_or_below_candidate_refuses(self) -> None:
        inner = self.target / "mounted"
        inner.mkdir()
        for mount in (self.target.parent, self.target, inner):
            with self.subTest(mount=mount):
                self.assert_refused(self.target, removal_guard.RefusalReason.MOUNT, mounts=(mount,))

    def test_case_alias_of_home_refuses_by_identity(self) -> None:
        alternate = self.fixture / "Home"
        try:
            if not alternate.exists():
                alternate.symlink_to(self.home, target_is_directory=True)
        except OSError as error:
            self.fail(str(error))
        guard = self.guard()
        with (mock.patch.object(Path, "resolve", return_value=alternate),
              self.assertRaises(removal_guard.Refused) as result):
            with guard.check(alternate):
                self.fail("the guard approved home")
        self.assertEqual(result.exception.reason, removal_guard.RefusalReason.PROTECTED_PATH)

    def test_parent_must_be_empty(self) -> None:
        parent = self.root / "sample" / "unit"
        self.assert_refused(parent, removal_guard.RefusalReason.OUTSIDE_ROOT)
        self.target.rmdir()
        with self.guard().check(parent) as checked:
            removal_guard.remove(checked)
        self.assertFalse(parent.exists())

    def test_form_rows_omit_maker_evidence_and_manual_rows_keep_it(self) -> None:
        plan = removal_guard.RemovalList(removal_guard.Tool.BUILD)
        with self.guard().check(self.target) as checked:
            _ = plan.add_checked(checked, 0, "idle build folder", removal_guard.fingerprint(self.target, checked))
        form = plan.rows[self.target]
        self.assertNotIn('"maker"', form.line())
        self.assertEqual(removal_guard.ListedEntry.parse(form.line()), form)
        self.assertEqual(removal_guard.ListedEntry.parse(form.line().replace(
            "{", f'{{"maker": {json.dumps(str(self.target))}, ', 1)), form)

        outside = self.fixture / "outside"
        outside.mkdir()
        marker = outside / removal_guard.CARGO_TAG
        marker.touch()
        _ = plan.add_manual(removal_guard.Tool.BUILD, outside, outside, 0,
                            "manual review", removal_guard.review_evidence(outside))
        row = removal_guard.ListedEntry.parse(plan.rows[outside].line())
        self.assertEqual(row.evidence, marker)
        self.assertFalse(row.fingerprint)
        self.assert_refused(outside, removal_guard.RefusalReason.OUTSIDE_ROOT)

    def test_git_metadata_check_is_case_folded(self) -> None:
        git = self.target / ".GIT"
        _ = git.write_text("keep Git metadata")
        with self.guard().check(self.target) as checked:
            with self.assertRaisesRegex(removal_guard.ChangedEntry, "Git metadata"):
                _ = removal_guard.fingerprint(self.target, checked)
            with self.assertRaisesRegex(removal_guard.ChangedEntry, "Git metadata"):
                removal_guard.remove(checked)
        self.assertEqual(git.read_text(), "keep Git metadata")

    def test_session_and_legacy_scratch_stay(self) -> None:
        for parts in (("session",), ("claude", "scratch")):
            path = self.root.joinpath(*parts)
            path.mkdir(parents=True)
            self.assert_refused(path, removal_guard.RefusalReason.OUTSIDE_ROOT)
            self.assertTrue(path.is_dir())

    def test_unknown_repo_unit_and_extra_depth_refuse(self) -> None:
        for path in (self.root / "unknown" / "unit" / "short",
                     self.root / "sample" / "unknown" / "short", self.target / "debug"):
            path.mkdir(parents=True)
            self.assert_refused(path, removal_guard.RefusalReason.OUTSIDE_ROOT)

    def test_path_with_parent_step_refuses_before_resolution(self) -> None:
        path = self.target / ".." / self.target.name
        self.assert_refused(path, removal_guard.RefusalReason.OUTSIDE_ROOT)

    def test_replacement_after_check_stays(self) -> None:
        original = self.target.with_name("original")
        with self.guard().check(self.target) as checked:
            _ = self.target.rename(original)
            self.target.mkdir()
            hold = self.target / "keep"
            _ = hold.write_text("replacement")
            with self.assertRaises(removal_guard.ChangedEntry):
                removal_guard.remove(checked)
        self.assertTrue(original.is_dir())
        self.assertEqual(hold.read_text(), "replacement")

    def test_cleanup_approval_checks_exact_paths_without_extending_build_forms(self) -> None:
        candidate = self.home / "old"
        candidate.mkdir()
        child = candidate / "payload"
        child.mkdir()
        payload = child / "file"
        _ = payload.write_text("old output")
        (child / "hardlink").hardlink_to(payload)
        neighbour = self.home / "unlisted"
        neighbour.mkdir()
        bounds = cleanup_old_folders.CleanupBounds(self.home, self.root, self.names.repositories)
        guard = cleanup_old_folders.read_cleanup_guard(frozenset((candidate, self.target)), bounds,
                                                 mount_reader=lambda: (Path("/"),))
        self.assert_refused(candidate, removal_guard.RefusalReason.OUTSIDE_ROOT)
        for refused, reason in ((child, removal_guard.RefusalReason.OUTSIDE_ROOT),
                                (neighbour, removal_guard.RefusalReason.OUTSIDE_ROOT),
                                (self.target, removal_guard.RefusalReason.PROTECTED_PATH)):
            with (self.subTest(path=refused), self.assertRaises(removal_guard.Refused) as error,
                  guard.check(refused)):
                self.fail("the cleanup guard accepted a protected path")
            self.assertEqual(error.exception.reason, reason)
        with guard.check(candidate) as checked:
            entries: dict[Path, cleanup_old_folders.CleanupEntry] = {}
            _ = cleanup_old_folders.inspect_cleanup_tree(checked, entries)
            approval = removal_guard.ApprovedCleanup(entries, removal_guard.CleanupRemoval.PLAIN)
            removal_guard.remove_cleanup(checked, approval)
        self.assertFalse(candidate.exists())
        self.assertTrue(neighbour.is_dir())
        self.assertTrue(self.target.is_dir())

    def test_cleanup_removal_refuses_new_or_replaced_entries(self) -> None:
        for change, name in (("new file", "new"), ("replacement", "approved"), ("bare Git", "HEAD")):
            with self.subTest(change=change):
                candidate = self.home / change
                folder = candidate / "nested"
                folder.mkdir(parents=True)
                original = folder / "approved"
                _ = original.write_text("old output")
                changed = folder / name
                bounds = cleanup_old_folders.CleanupBounds(self.home, self.root, self.names.repositories)
                guard = cleanup_old_folders.read_cleanup_guard(frozenset((candidate,)), bounds,
                                                         mount_reader=lambda: (Path("/"),))
                listdir = os.listdir
                folder_info = folder.stat()

                def change_before_listing(directory: int) -> list[str]:
                    if os.path.samestat(os.fstat(directory), folder_info):
                        if change == "replacement":
                            replacement = self.home / "replacement-payload"
                            _ = replacement.write_text("keep the new entry")
                            _ = replacement.replace(original)
                        else:
                            _ = changed.write_text("keep the new entry")
                            if change == "bare Git":
                                (folder / "refs").mkdir()
                                (folder / "objects").mkdir()
                    return listdir(directory)

                with guard.check(candidate) as checked:
                    entries: dict[Path, cleanup_old_folders.CleanupEntry] = {}
                    _ = cleanup_old_folders.inspect_cleanup_tree(checked, entries)
                    approval = removal_guard.ApprovedCleanup(entries, removal_guard.CleanupRemoval.PLAIN)
                    reason = "holds Git work" if change == "bare Git" else "changed before removal"
                    with (mock.patch.object(os, "listdir", side_effect=change_before_listing),
                          self.assertRaisesRegex(removal_guard.ChangedEntry, reason)):
                        removal_guard.remove_cleanup(checked, approval)
                self.assertEqual(changed.read_text(), "keep the new entry")
                if change != "replacement":
                    self.assertEqual(original.read_text(), "old output")

    def test_cleanup_guard_refuses_mounts_aliases_git_and_replaced_directories(self) -> None:
        candidate = self.home / "old"
        nested = candidate / "nested"
        nested.mkdir(parents=True)
        linked = self.home / "linked"
        linked.symlink_to(candidate, target_is_directory=True)
        bounds = cleanup_old_folders.CleanupBounds(self.home, self.root, self.names.repositories)
        for mount in (candidate, nested):
            guard = cleanup_old_folders.read_cleanup_guard(frozenset((candidate,)), bounds,
                                                     mount_reader=lambda: (Path("/"), mount))
            with (self.subTest(mount=mount), self.assertRaises(removal_guard.Refused) as error,
                  guard.check(candidate)):
                self.fail("the cleanup guard approved a mounted tree")
            self.assertEqual(error.exception.reason, removal_guard.RefusalReason.MOUNT)
        alias = linked / nested.name
        guard = cleanup_old_folders.read_cleanup_guard(frozenset((candidate, linked, alias)), bounds,
                                                 mount_reader=lambda: (Path("/"),))
        for path in (linked, alias):
            with self.subTest(path=path), self.assertRaises(OSError), guard.check(path):
                self.fail("the cleanup guard followed a link")
        git = nested / ".git"
        _ = git.write_text("keep Git metadata")
        with guard.check(candidate) as checked:
            entries: dict[Path, cleanup_old_folders.CleanupEntry] = {}
            measured = cleanup_old_folders.inspect_cleanup_tree(checked, entries)
            self.assertIs(measured.git, cleanup_old_folders.CleanupGit.PROTECTED)
            self.assertGreaterEqual(measured.size, git.stat().st_blocks * 512)
            approval = removal_guard.ApprovedCleanup(entries, removal_guard.CleanupRemoval.PLAIN)
            with self.assertRaisesRegex(removal_guard.ChangedEntry, "holds Git work"):
                removal_guard.remove_cleanup(checked, approval)
            saved = candidate.with_name("saved")
            _ = candidate.rename(saved)
            candidate.mkdir()
            with self.assertRaises(removal_guard.ChangedEntry):
                removal_guard.remove_cleanup(checked, approval)
        self.assertEqual((saved / "nested/.git").read_text(), "keep Git metadata")
        self.assertTrue(candidate.is_dir())

    def test_each_form_checks_depth_and_number(self) -> None:
        cases = (
            (self.target, removal_guard.Form.SHORT),
            (self.target.with_name("42"), removal_guard.Form.NUMBER),
            (self.root / "sample" / "CI" / "42", removal_guard.Form.CI),
            (self.root / "sample" / "Main", removal_guard.Form.MAIN),
            (self.root / "sample" / "scratch", removal_guard.Form.EMPTY_UNIT),
        )
        guard = self.guard()
        for path, form in cases:
            with self.subTest(path=path):
                path.mkdir(parents=True, exist_ok=True)
                with guard.check(path) as checked:
                    self.assertEqual(checked.form, form)
        ci = self.root / "sample" / "CI"
        for path in (ci / "short", self.root / "sample" / "Main" / "42"):
            path.mkdir()
            self.assert_refused(path, removal_guard.RefusalReason.OUTSIDE_ROOT)
        (ci / "short").rmdir()
        (ci / "42").rmdir()
        self.assert_refused(ci, removal_guard.RefusalReason.OUTSIDE_ROOT)
        self.assert_refused(self.root / "sample", removal_guard.RefusalReason.OUTSIDE_ROOT)
        empty_repo = self.root / "empty"
        empty_repo.mkdir()
        with guard.check(empty_repo) as checked:
            self.assertEqual(checked.form, removal_guard.Form.EMPTY_REPO)

    def test_bad_mount_reader_refuses(self) -> None:
        def unreadable() -> tuple[Path, ...]:
            raise OSError("unreadable")
        with self.assertRaises(removal_guard.Refused) as result:
            _ = removal_guard.read(mount_reader=unreadable, build_top=self.root, names=self.names)
        self.assertEqual(result.exception.reason, removal_guard.RefusalReason.UNAVAILABLE_MOUNTS)

    def test_missing_forms_refuse(self) -> None:
        self.config.unlink()
        with self.assertRaises(removal_guard.Refused) as result:
            _ = self.guard()
        self.assertEqual(result.exception.reason, removal_guard.RefusalReason.UNAVAILABLE_ROOTS)

    def test_saved_gate_removal_list_keeps_the_folder_for_review(self) -> None:
        path = self.fixture / "gate_folders_delete-saved.jsonl"
        data = (json.dumps({
            "tool": "gate_folders_delete", "path": str(self.target), "anchor": str(self.target),
            "bytes": 0, "reason": "idle gate folder", "fingerprint": "", "assessed_ns": 1,
        }) + "\n").encode()
        _ = path.write_bytes(data)
        console = io.StringIO()
        errors = io.StringIO()
        with (mock.patch.object(removal_guard, "read", return_value=self.guard()),
              redirect_stdout(console), redirect_stderr(errors)):
            self.assertEqual(removal_guard.remove_list(path, hashlib.sha256(data).hexdigest()), 0)
        self.assertEqual(console.getvalue().splitlines(), [f"kept {str(self.target)!r}: the row is for review only"])
        self.assertEqual(errors.getvalue(), "")
        self.assertTrue(self.target.is_dir())

    def test_unreadable_staff_authority_refuses_the_list(self) -> None:
        plan = removal_guard.RemovalList(removal_guard.Tool.BUILD)
        with self.guard().check(self.target) as checked:
            stamp = removal_guard.fingerprint(self.target, checked)
            _ = plan.add_checked(checked, 0, "idle build folder", stamp)
        path = plan.publish()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        read = removal_guard.read
        errors = io.StringIO()

        def read_fixture_guard() -> removal_guard.RemovalGuard:
            return read(build_top=self.root, mount_reader=lambda: (Path("/"), self.root))

        with (mock.patch.object(removal_guard, "read", side_effect=read_fixture_guard),
              mock.patch.object(staff, "authority_snapshot",
                                side_effect=staff.StaffUnreadable("natedev not reached\nconnection closed")) as authority,
              redirect_stderr(errors)):
            self.assertEqual(removal_guard.remove_list(path, digest), 1)
        authority.assert_called_once_with()
        self.assertEqual(errors.getvalue().splitlines(),
                         ["removal list refused: cannot read the staff record: natedev not reached connection closed"])
        self.assertTrue(self.target.is_dir())

    def test_external_cargo_review_refuses_a_replaced_source(self) -> None:
        source = self.target
        saved_source = removal_guard.identity(source.stat())
        old_source = source.with_name("old")
        _ = source.rename(old_source)
        source.mkdir()
        output = self.fixture / "external"
        output.mkdir()
        keep = output / "keep"
        _ = keep.write_text("working output")
        saved = scratch.SavedCargoOutput(cargo_output.CargoOutputRoots(source, output, output), (), (), 0.0)
        with mock.patch.object(subprocess, "run") as run:
            with self.assertRaisesRegex(scratch.CargoOutputKept, "source identity changed"):
                scratch.clean_cargo_outputs(source, (saved,), source=saved_source)
        run.assert_not_called()
        self.assertFalse((self.home / removal_guard.LIST_DIRECTORY).exists())
        self.assertEqual(keep.read_text(), "working output")

    def test_reviewed_external_cargo_output_stays(self) -> None:
        output = self.fixture / "external"
        output.mkdir()
        saved = scratch.SavedCargoOutput(cargo_output.CargoOutputRoots(self.target, output, output), (), (), 0.0)
        console = io.StringIO()
        with redirect_stdout(console):
            scratch.clean_cargo_outputs(self.target, (saved,))
            [plan] = list((self.home / removal_guard.LIST_DIRECTORY).iterdir())
            data = plan.read_bytes()
            self.assertEqual(removal_guard.ListedEntry.parse(data.decode().strip()).path, output)
            with mock.patch.object(removal_guard, "read", return_value=self.guard()):
                self.assertEqual(removal_guard.remove_list(plan, hashlib.sha256(data).hexdigest()), 0)
        self.assertIn(f"kept {str(output)!r}", console.getvalue())
        self.assertTrue(output.is_dir())
