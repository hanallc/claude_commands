"""Test removal checks with private folders and a private mount table."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from typing import override
from unittest import mock

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

    def mirror_guard(self, *mounts: Path) -> removal_guard.RemovalGuard:
        mirror = self.home / ".local/state/mac-test/mirror"
        mirror.mkdir(parents=True, exist_ok=True)
        return removal_guard.read_mirror(mirror, mount_reader=lambda: (Path("/"), *mounts))

    def test_mirror_guard_accepts_only_cargo_output_forms(self) -> None:
        guard = self.mirror_guard()
        target = guard.root / "repo/target"
        artifact = target / "debug/deps/libsample-0123456789abcdef.rlib"
        source = target / "debug/deps/source.rs"
        artifact.parent.mkdir(parents=True)
        _ = artifact.write_bytes(b"output")
        _ = source.write_bytes(b"source")
        with guard.check(artifact) as checked:
            self.assertEqual(checked.form, removal_guard.MirrorOutputForm.UNIT)
        for path in (artifact.parent, source):
            with self.subTest(path=path), self.assertRaises(removal_guard.Refused):
                with guard.check(path):
                    self.fail("The guard approved a path outside the Cargo output forms.")

    def test_mirror_guard_refuses_targets_outside_the_configured_root(self) -> None:
        guard = self.mirror_guard()
        for parent in (self.home, self.home / "rust", self.home / ".cargo",
                       self.home / ".claude", self.fixture / "tmp"):
            target = parent / "repo/target"
            target.mkdir(parents=True)
            with self.subTest(target=target), self.assertRaises(removal_guard.Refused):
                with guard.check(target):
                    self.fail("The guard approved output outside the configured mirror root.")
        with self.assertRaises(removal_guard.Refused):
            _ = removal_guard.read_mirror(self.home / "rust", mount_reader=lambda: (Path("/"),))

    def test_mirror_inventory_refuses_source_before_any_removal(self) -> None:
        guard = self.mirror_guard()
        target = guard.root / "repo/target"
        artifact = target / "debug/deps/libsample-0123456789abcdef.rlib"
        artifact.parent.mkdir(parents=True)
        _ = artifact.write_bytes(b"output")
        with guard.check(target) as checked:
            for source in ("Cargo.toml", ".git"):
                marker = artifact.parent / source
                _ = marker.write_bytes(b"source")
                with self.subTest(source=source), self.assertRaises(removal_guard.Refused):
                    _ = removal_guard.inspect_mirror_output(checked, frozenset(), lambda: None)
                self.assertEqual(artifact.read_bytes(), b"output")
                marker.unlink()
            with self.assertRaises(removal_guard.Refused):
                _ = removal_guard.inspect_mirror_output(
                    checked, frozenset({artifact}), lambda: None)
        self.assertEqual(artifact.read_bytes(), b"output")

    def test_mirror_guard_refuses_a_case_alias_of_an_excluded_tree(self) -> None:
        excluded = self.home / "Rust"
        mirror = excluded / "mirror"
        mirror.mkdir(parents=True)
        (self.home / "rust").symlink_to(excluded, target_is_directory=True)
        for root in (excluded, mirror):
            with self.subTest(root=root), self.assertRaises(removal_guard.Refused):
                _ = removal_guard.read_mirror(root, mount_reader=lambda: (Path("/"),))

    def test_mirror_guard_refuses_links_and_mounts(self) -> None:
        guard = self.mirror_guard()
        target = guard.root / "repo/target"
        inner = target / "mounted"
        inner.mkdir(parents=True)
        external = self.fixture / "outside"
        external.mkdir()
        link = target / "link"
        link.symlink_to(external, target_is_directory=True)
        with guard.check(target) as checked, self.assertRaises(removal_guard.Refused):
            _ = removal_guard.inspect_mirror_output(checked, frozenset(), lambda: None)
        with self.assertRaises(removal_guard.Refused):
            with self.mirror_guard(inner).check(target):
                self.fail("The guard approved a mount below the candidate.")
        self.assertTrue(external.is_dir())

    def test_mirror_guard_refuses_a_root_replaced_after_the_check(self) -> None:
        guard = self.mirror_guard()
        target = guard.root / "repo/target"
        target.mkdir(parents=True)
        _ = (target / "output").write_bytes(b"build output")
        renamed = guard.root.with_name("previous-mirror")
        with guard.check(target) as checked:
            _ = guard.root.rename(renamed)
            target.mkdir(parents=True)
            _ = (target / "source").write_bytes(b"source")
            with self.assertRaises(removal_guard.ChangedEntry):
                with removal_guard.stage_mirror_output(checked):
                    self.fail("The guard staged output under a replaced root.")
        self.assertEqual((renamed / "repo/target/output").read_bytes(), b"build output")
        self.assertEqual((target / "source").read_bytes(), b"source")

    def test_mirror_unlink_refuses_a_replaced_leaf_after_the_name_check(self) -> None:
        guard = self.mirror_guard()
        target = guard.root / "repo/target"
        target.mkdir(parents=True)
        _ = (target / "output").write_bytes(b"build output")
        outside = self.fixture / "tracked"
        _ = outside.write_bytes(b"tracked source")
        verify = removal_guard.require_identity

        def replace_leaf(parent: int, name: str, expected: removal_guard.Identity) -> None:
            verify(parent, name, expected)
            if name == "output":
                os.rename(name, "previous", src_dir_fd=parent, dst_dir_fd=parent)
                os.rename(outside, name, dst_dir_fd=parent)

        with guard.check(target) as checked:
            inventory = removal_guard.inspect_mirror_output(checked, frozenset(), lambda: None)
            with removal_guard.stage_mirror_output(checked) as staged:
                with (mock.patch.object(sys, "platform", "darwin"),
                      mock.patch.object(removal_guard, "mount_id", return_value=checked.mount),
                      mock.patch.object(removal_guard, "require_identity", side_effect=replace_leaf),
                      self.assertRaises(removal_guard.ChangedEntry)):
                    removal_guard.remove(staged, mirror=inventory)
        self.assertEqual((target / "output").read_bytes(), b"tracked source")
        self.assertEqual((target / "previous").read_bytes(), b"build output")

    def test_mirror_unlink_refuses_a_parent_renamed_after_the_leaf_opens(self) -> None:
        guard = self.mirror_guard()
        target = guard.root / "repo/target"
        target.mkdir(parents=True)
        _ = (target / "output").write_bytes(b"build output")
        open_entry = os.open

        with guard.check(target) as checked:
            inventory = removal_guard.inspect_mirror_output(checked, frozenset(), lambda: None)
            with removal_guard.stage_mirror_output(checked) as staged:
                def open_and_move(path: str, flags: int, mode: int = 0o777, *,
                                  dir_fd: int | None = None) -> int:
                    fd = open_entry(path, flags, mode, dir_fd=dir_fd)
                    if path == "output":
                        os.rename(staged.name, "moved", src_dir_fd=staged.parent, dst_dir_fd=staged.parent)
                        os.mkdir(staged.name, dir_fd=staged.parent)
                    return fd

                try:
                    with (mock.patch.object(os, "open", side_effect=open_and_move),
                          self.assertRaises(removal_guard.ChangedEntry)):
                        removal_guard.remove(staged, mirror=inventory)
                finally:
                    os.rmdir(staged.name, dir_fd=staged.parent)
                    os.rename("moved", staged.name, src_dir_fd=staged.parent, dst_dir_fd=staged.parent)
        self.assertEqual((target / "output").read_bytes(), b"build output")

    def test_mirror_unlink_refuses_a_staging_ancestor_moved_outside_the_mirror(self) -> None:
        guard = self.mirror_guard()
        target = guard.root / "repo/target"
        target.mkdir(parents=True)
        _ = (target / "output").write_bytes(b"build output")
        outside = self.fixture / "outside"
        verify = removal_guard.require_identity
        checked_names: list[str] = []

        def move_ancestor(parent: int, name: str, expected: removal_guard.Identity) -> None:
            verify(parent, name, expected)
            checked_names.append(name)
            if checked_names.count("output") == 2:
                _ = (guard.root / "repo").rename(outside)

        with guard.check(target) as checked:
            inventory = removal_guard.inspect_mirror_output(checked, frozenset(), lambda: None)
            with self.assertRaises(OSError), removal_guard.stage_mirror_output(checked) as staged:
                with mock.patch.object(removal_guard, "require_identity", side_effect=move_ancestor):
                    removal_guard.remove(staged, mirror=inventory)
        self.assertEqual([path.read_bytes() for path in outside.glob(".mac-floor-*/target/output")],
                         [b"build output"])

    def test_mirror_unlink_refuses_a_leaf_replaced_during_the_ancestor_check(self) -> None:
        guard = self.mirror_guard()
        target = guard.root / "repo/target"
        target.mkdir(parents=True)
        _ = (target / "output").write_bytes(b"build output")
        verify = removal_guard.require_identity
        checked_names: list[str] = []

        def record(parent: int, name: str, expected: removal_guard.Identity) -> None:
            verify(parent, name, expected)
            checked_names.append(name)

        def replace_leaf() -> None:
            # The second check of the leaf is the one right before its unlink.
            if checked_names.count("output") == 2 and "replaced" not in checked_names:
                checked_names.append("replaced")
                [staged_output] = (guard.root / "repo").glob(".mac-floor-*/target/output")
                _ = staged_output.rename(staged_output.with_name("previous"))
                _ = staged_output.write_bytes(b"replacement")

        with guard.check(target) as checked:
            inventory = removal_guard.inspect_mirror_output(checked, frozenset(), lambda: None)
            with self.assertRaises(removal_guard.ChangedEntry), \
                    removal_guard.stage_mirror_output(checked) as staged:
                with mock.patch.object(removal_guard, "require_identity", side_effect=record):
                    removal_guard.remove(staged, mirror=inventory, check_time=replace_leaf)
        self.assertIn("replaced", checked_names)
        self.assertEqual((target / "output").read_bytes(), b"replacement")
        self.assertEqual((target / "previous").read_bytes(), b"build output")

    def test_mirror_stage_refuses_a_staging_folder_replaced_before_its_first_stat(self) -> None:
        guard = self.mirror_guard()
        target = guard.root / "repo/target"
        target.mkdir(parents=True)
        _ = (target / "output").write_bytes(b"build output")
        make_folder = os.mkdir
        replaced: list[str] = []

        def replace_stage(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            make_folder(path, mode, dir_fd=dir_fd)
            os.rmdir(path, dir_fd=dir_fd)
            make_folder(path, 0o755, dir_fd=dir_fd)
            os.chmod(path, 0o755, dir_fd=dir_fd)
            replaced.append(path)

        with guard.check(target) as checked:
            with (mock.patch.object(os, "mkdir", side_effect=replace_stage),
                  self.assertRaises(removal_guard.ChangedEntry),
                  removal_guard.stage_mirror_output(checked)):
                self.fail("The take staged output in a replaced staging folder.")
        self.assertEqual([(guard.root / "repo" / name).is_dir() for name in replaced], [True])
        self.assertEqual((target / "output").read_bytes(), b"build output")

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
