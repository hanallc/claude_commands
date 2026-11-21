"""Test manual gate review with private folders, locks, and Git worktrees."""
from __future__ import annotations

import fcntl
import hashlib
import io
import os
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import override
from unittest import mock
import unittest

from scripts.lint import gate_folders_delete
from scripts.lint.sweep import gib
from scripts.lib import removal_guard, scratch
from scripts.lib.git_test_support import fixture_git_environment

GATE_RECORD = '{"owner":"fixture","repo":"repository","commit":"' + "1" * 40 + '"}\n'


class GateFoldersDeleteTests(unittest.TestCase):
    root: Path = Path()
    gates: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.gates = self.root / "gates"
        self.gates.mkdir()
        home = self.root / "home"
        config = self.root / "config"
        home.mkdir()
        config.mkdir()
        environment = fixture_git_environment(self.root)
        environment.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "CONF_DIR": str(config)})
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))
        top = self.root / "build"
        top.mkdir()
        _ = (config / "build_folders.conf").write_text(
            f"removal.path_templates={top}/<repo>/<unit>/<short>\nroots.workspace={self.root / 'workspaces'}\n"
            + f"roots.build={top}\n")

    def git(self, folder: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(folder), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "core.hooksPath=/dev/null", *arguments],
            capture_output=True, text=True, check=True, timeout=20)
        return result.stdout

    def repository(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        _ = self.git(folder, "init", "--quiet")
        _ = self.git(folder, "config", "user.name", "Fixture")
        _ = self.git(folder, "config", "user.email", "fixture@example.invalid")
        _ = (folder / "source.txt").write_text("original source\n")
        _ = self.git(folder, "add", "source.txt")
        _ = self.git(folder, "commit", "--quiet", "-m", "Create the fixture.")
        return folder

    def gate(self, name: str = "gate") -> Path:
        folder = self.gates / "repository" / name
        (folder / "target").mkdir(parents=True)
        _ = (folder / "target" / "output").write_bytes(b"generated output")
        _ = (folder / ".gate.json").write_text(GATE_RECORD)
        return folder

    def linked_checkout(self, gate: Path) -> tuple[Path, Path]:
        source = self.repository(self.root / "source")
        checkout = gate / "checkout"
        _ = self.git(source, "worktree", "add", "--quiet", "--detach", str(checkout), "HEAD")
        _ = (checkout / "source.txt").write_text("edited source\n")
        (checkout / "target").mkdir()
        _ = (checkout / "target" / "output").write_bytes(b"generated checkout output")
        return source, checkout

    def delete(self, *, dry_run: bool = False) -> list[str]:
        output = io.StringIO()
        with redirect_stdout(output):
            _ = gate_folders_delete.delete_gate_folders(self.gates, dry_run=dry_run)
        return output.getvalue().splitlines()

    def assert_manual_gate(self, lines: list[str], gate: Path) -> None:
        [plan] = [Path(line.removeprefix("removal list: ")) for line in lines if line.startswith("removal list: ")]
        data = plan.read_bytes()
        rows = [removal_guard.ListedEntry.parse(line) for line in data.decode().splitlines()]
        self.assertIn(gate, [row.path for row in rows])
        output = io.StringIO()
        top = self.root / "build"
        guard = removal_guard.read(build_top=top, mount_reader=lambda: (Path("/"), top),
                                   names=removal_guard.KnownNames(frozenset(), frozenset()))
        with mock.patch.object(removal_guard, "read", return_value=guard), redirect_stdout(output):
            self.assertEqual(removal_guard.remove_list(plan, hashlib.sha256(data).hexdigest()), 0)
        self.assertIn(f"kept {str(gate)!r}", output.getvalue())
        self.assertTrue(gate.is_dir())

    def test_a_free_gate_is_listed_and_empty_repositories_stay(self) -> None:
        gate = self.gate()
        (self.gates / "checkout").mkdir()
        (self.gates / "reload").mkdir()
        size = len(b"generated output") + len(GATE_RECORD.encode())
        lines = self.delete()
        self.assertIn(f"review {gate}: {gib(size)}", lines)
        reason = removal_guard.RefusalReason.UNKNOWN_MAKER.value
        self.assertIn(f"kept {self.gates / 'checkout'}: {reason}", lines)
        self.assertIn(f"kept {self.gates / 'reload'}: {reason}", lines)
        self.assert_manual_gate(lines, gate)

    def test_a_read_only_review_root_keeps_its_gates(self) -> None:
        blocked = self.gates / "aaa-empty"
        blocked.mkdir()
        gate = self.gate()
        self.gates.chmod(0o555)
        try:
            lines = self.delete()
        finally:
            self.gates.chmod(0o755)
        self.assertTrue(any(line.startswith(f"kept {blocked}: ") for line in lines), lines)
        self.assertEqual(list(blocked.iterdir()), [])
        self.assert_manual_gate(lines, gate)

    def test_a_held_busy_mark_keeps_its_gate_until_released(self) -> None:
        gate = self.gate()
        with (gate / scratch.BUSY).open("w") as busy:
            fcntl.flock(busy, fcntl.LOCK_SH | fcntl.LOCK_NB)
            lines = self.delete()
            self.assertIn(f"kept {gate}: busy", lines)
            self.assertEqual((gate / "target" / "output").read_bytes(), b"generated output")
        self.assert_manual_gate(self.delete(), gate)

    def test_a_linked_worktree_edit_and_its_refs_stay(self) -> None:
        gate = self.gate()
        source, checkout = self.linked_checkout(gate)
        worktrees = self.git(source, "worktree", "list", "--porcelain")
        self.assert_manual_gate(self.delete(), gate)
        self.assertEqual(self.git(source, "for-each-ref", "--format=%(refname)", "refs/rescue/"), "")
        self.assertEqual(self.git(source, "worktree", "list", "--porcelain"), worktrees)
        self.assertEqual((checkout / "source.txt").read_text(), "edited source\n")

    def test_a_missing_source_keeps_the_gate_and_its_edits(self) -> None:
        gate = self.gate()
        source, checkout = self.linked_checkout(gate)
        shutil.rmtree(source)
        self.assert_manual_gate(self.delete(), gate)
        self.assertEqual((checkout / "source.txt").read_text(), "edited source\n")
        self.assertEqual((gate / "target" / "output").read_bytes(), b"generated output")

    def test_a_repository_with_its_own_git_folder_keeps_the_entire_gate(self) -> None:
        for relative, separate_git_dir in ((".", False), ("checkout", False), ("target/checkout", False),
                                            ("separate-checkout", True)):
            with self.subTest(relative=relative, separate_git_dir=separate_git_dir):
                gate = self.gate("own-repository-" + relative.replace("/", "-"))
                if separate_git_dir:
                    (gate / relative).mkdir()
                    _ = self.git(gate / relative, "init", "--quiet", "--separate-git-dir", str(gate / "metadata"))
                repository = self.repository(gate / relative)
                _ = (repository / "source.txt").write_text("unsaved repository source\n")
                lines = self.delete()
                self.assertIn(f"kept {gate}: repository", lines)
                self.assertEqual((repository / "source.txt").read_text(), "unsaved repository source\n")
                self.assertEqual(self.git(repository, "show", "HEAD:source.txt"), "original source\n")

    def test_a_case_alias_of_git_metadata_keeps_the_gate(self) -> None:
        gate = self.gate()
        git = gate / ".GIT"
        git.mkdir()
        self.assertIn(f"kept {gate}: repository", self.delete())
        self.assertTrue(git.is_dir())

    def test_a_regular_file_alias_of_git_metadata_keeps_the_gate(self) -> None:
        gate = self.gate()
        marker = gate / ".GIT"
        contents = "gitdir: missing-metadata\n"
        _ = marker.write_text(contents)
        lines = self.delete()
        self.assertIn(f"kept {gate}: repository", lines)
        self.assertFalse(any(line.startswith(f"review {gate}: ") for line in lines), lines)
        self.assertEqual(marker.read_text(), contents)

    def test_a_dry_run_prints_decisions_without_changing_files_or_refs(self) -> None:
        gate = self.gate()
        source, checkout = self.linked_checkout(gate)
        empty = self.gates / "reload"
        empty.mkdir()
        before = {str(path.relative_to(self.gates)): path.read_bytes() if path.is_file() else None
                  for path in self.gates.rglob("*")}
        refs = self.git(source, "for-each-ref", "--format=%(refname) %(objectname)")
        lines = self.delete(dry_run=True)
        after = {str(path.relative_to(self.gates)): path.read_bytes() if path.is_file() else None
                 for path in self.gates.rglob("*")}
        self.assertEqual(after, before)
        self.assertEqual(self.git(source, "for-each-ref", "--format=%(refname) %(objectname)"), refs)
        self.assertTrue(any(line.startswith(f"review {gate}: ") for line in lines))
        self.assertIn(f"kept {empty}: {removal_guard.RefusalReason.UNKNOWN_MAKER.value}", lines)
        self.assertEqual((checkout / "source.txt").read_text(), "edited source\n")

    def test_main_exits_zero_when_a_gate_stays(self) -> None:
        for case in ("free", "busy", "missing-source"):
            with self.subTest(case=case), ExitStack() as contexts:
                gate = self.gate(case)
                if case == "busy":
                    busy = contexts.enter_context((gate / scratch.BUSY).open("w"))
                    fcntl.flock(busy, fcntl.LOCK_SH | fcntl.LOCK_NB)
                elif case == "missing-source":
                    source = self.linked_checkout(gate)[0]
                    shutil.rmtree(source)
                result = subprocess.run(
                    [sys.executable, gate_folders_delete.__file__, "--root", str(self.gates)],
                    capture_output=True, text=True, check=False, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if case == "free":
                    size = len(b"generated output") + len(GATE_RECORD.encode())
                    self.assertIn(f"review {gate}: {gib(size)}", result.stdout.splitlines())
                elif case == "busy":
                    self.assertIn(f"kept {gate}: busy", result.stdout.splitlines())
                    self.assertEqual((gate / "target" / "output").read_bytes(), b"generated output")
                else:
                    self.assertIn(f"review {gate}: ", result.stdout)
                    self.assertEqual((gate / "checkout" / "source.txt").read_text(), "edited source\n")

    def test_a_home_root_keeps_top_level_folders_and_files(self) -> None:
        home = Path.home()
        candidate = home / "repository" / "gate"
        candidate.mkdir(parents=True)
        marker = candidate / ".gate.json"
        _ = marker.write_text(GATE_RECORD)
        top_file = home / "notes"
        _ = top_file.write_text("keep")
        before = sorted(home.rglob("*"))
        output = io.StringIO()
        with redirect_stdout(output):
            decisions = gate_folders_delete.delete_gate_folders(home)
        self.assertEqual(before, sorted(home.rglob("*")))
        [refusal] = [row.refusal for row in decisions if isinstance(row, gate_folders_delete.KeptByGuard)]
        self.assertEqual(refusal.reason, removal_guard.RefusalReason.INVALID_ROOT)
        self.assertIn(f"kept {home}: ", output.getvalue())

    def test_remote_mirrors_dot_folders_and_root_files_are_kept(self) -> None:
        entries = {".remote-mirrors": "mirror record", ".claude": "private state", ".other": "other state"}
        for name, contents in entries.items():
            folder = self.gates / name
            folder.mkdir()
            _ = (folder / "record").write_text(contents)
        root_file = self.gates / "record.json"
        _ = root_file.write_text("root file")
        external = self.root / "external"
        external.mkdir()
        _ = (external / "source.txt").write_text("external source")
        link = self.gates / "linked-root-entry"
        link.symlink_to(external, target_is_directory=True)
        lines = self.delete()
        for name, contents in entries.items():
            self.assertIn(f"kept {self.gates / name}: root entry", lines)
            self.assertEqual((self.gates / name / "record").read_text(), contents)
        self.assertIn(f"kept {root_file}: root entry", lines)
        self.assertEqual(root_file.read_text(), "root file")
        self.assertIn(f"kept {link}: root entry", lines)
        self.assertEqual(link.readlink(), external)
        self.assertEqual((external / "source.txt").read_text(), "external source")


if __name__ == "__main__":
    _ = unittest.main()
