"""Test legacy gate deletion with private folders, locks, and Git worktrees."""
from __future__ import annotations

import fcntl
import io
import os
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import override
import unittest

from scripts.lint import gate_folders_delete
from scripts.lint.sweep import gib
from scripts.lib import scratch


class GateFoldersDeleteTests(unittest.TestCase):
    root: Path = Path()
    gates: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.gates = self.root / "gates"
        self.gates.mkdir()

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
        _ = (folder / ".gate.json").write_text('{"fixture": true}\n')
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

    def test_a_free_gate_and_empty_repository_folders_are_deleted(self) -> None:
        gate = self.gate()
        (self.gates / "checkout").mkdir()
        (self.gates / "reload").mkdir()
        size = len(b"generated output") + len(b'{"fixture": true}\n')
        lines = self.delete()
        self.assertIn(f"deleted {gate}: {gib(size)}", lines)
        self.assertIn(f"deleted {gate.parent}: {gib(0)}", lines)
        self.assertIn(f"deleted {self.gates / 'checkout'}: {gib(0)}", lines)
        self.assertIn(f"deleted {self.gates / 'reload'}: {gib(0)}", lines)
        self.assertEqual(list(self.gates.iterdir()), [])

    @unittest.skipIf(os.geteuid() == 0, "Root can remove folders despite the fixture's read-only permissions.")
    def test_an_empty_repository_removal_error_does_not_stop_later_gate_deletion(self) -> None:
        blocked = self.gates / "aaa-empty"
        blocked.mkdir()
        gate = self.gate()
        self.addCleanup(self.gates.chmod, 0o755)
        self.gates.chmod(0o555)
        try:
            lines = self.delete()
        finally:
            self.gates.chmod(0o755)
        self.assertTrue(any(line.startswith(f"kept {blocked}: ") for line in lines), lines)
        self.assertEqual(list(blocked.iterdir()), [])
        self.assertTrue(any(line.startswith(f"deleted {gate}: ") for line in lines), lines)
        self.assertEqual(list(gate.parent.iterdir()), [])

    def test_a_held_busy_mark_keeps_its_gate_until_released(self) -> None:
        gate = self.gate()
        with (gate / scratch.BUSY).open("w") as busy:
            fcntl.flock(busy, fcntl.LOCK_SH | fcntl.LOCK_NB)
            lines = self.delete()
            self.assertIn(f"kept {gate}: busy", lines)
            self.assertEqual((gate / "target" / "output").read_bytes(), b"generated output")
        _ = self.delete()
        self.assertEqual(list(self.gates.iterdir()), [])

    def test_a_linked_worktree_edit_is_rescued_in_the_source_before_deletion(self) -> None:
        gate = self.gate()
        source, checkout = self.linked_checkout(gate)
        edits = {checkout: "edited source\n"}
        for relative in ("nested/second", "nested/deeper/third"):
            linked = gate / relative
            linked.parent.mkdir(parents=True, exist_ok=True)
            _ = self.git(source, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
            edits[linked] = f"edited {linked.name}\n"
            _ = (linked / "source.txt").write_text(edits[linked])
            (linked / "target").mkdir()
            _ = (linked / "target" / "output").write_bytes(b"generated nested output")
        lines = self.delete()
        self.assertEqual(list(self.gates.iterdir()), [])
        refs = self.git(source, "for-each-ref", "--format=%(refname)", "refs/rescue/").splitlines()
        self.assertEqual(len(refs), len(edits))
        for linked, text in edits.items():
            with self.subTest(checkout=linked):
                matching = [ref for ref in refs if ref.startswith(f"refs/rescue/{linked.name}-")]
                self.assertEqual(len(matching), 1)
                ref, = matching
                self.assertRegex(ref, rf"^refs/rescue/{linked.name}-\d{{8}}T\d{{12}}Z$")
                self.assertEqual(self.git(source, "show", f"{ref}:source.txt"), text)
                self.assertNotIn("target/output", self.git(source, "ls-tree", "-r", "--name-only", ref).splitlines())
        self.assertTrue(any(line.startswith(f"deleted {gate}: ") for line in lines))
        worktrees = self.git(source, "worktree", "list", "--porcelain").splitlines()
        self.assertEqual([line for line in worktrees if line.startswith("worktree ")], [f"worktree {source}"])

    def test_a_real_rescue_failure_keeps_the_gate_and_its_edits(self) -> None:
        gate = self.gate()
        source, checkout = self.linked_checkout(gate)
        shutil.rmtree(source)
        lines = self.delete()
        self.assertTrue(any(line.startswith(f"kept {gate}: rescue failed: ") for line in lines), lines)
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
        self.assertTrue(any(line.startswith(f"would delete {gate}: ") for line in lines))
        self.assertIn(f"would delete {empty}: {gib(0)}", lines)
        self.assertEqual((checkout / "source.txt").read_text(), "edited source\n")

    def test_main_exit_status_reports_gate_errors(self) -> None:
        for case, expected_status in (("free", 0), ("busy", 0), ("rescue-failure", 1)):
            with self.subTest(case=case), ExitStack() as contexts:
                gate = self.gate(case)
                if case == "busy":
                    busy = contexts.enter_context((gate / scratch.BUSY).open("w"))
                    fcntl.flock(busy, fcntl.LOCK_SH | fcntl.LOCK_NB)
                elif case == "rescue-failure":
                    source = self.linked_checkout(gate)[0]
                    shutil.rmtree(source)
                result = subprocess.run(
                    [sys.executable, gate_folders_delete.__file__, "--root", str(self.gates)],
                    capture_output=True, text=True, check=False, timeout=20)
                self.assertEqual(result.returncode, expected_status, result.stdout + result.stderr)
                if case == "free":
                    size = len(b"generated output") + len(b'{"fixture": true}\n')
                    self.assertIn(f"deleted {gate}: {gib(size)}", result.stdout.splitlines())
                elif case == "busy":
                    self.assertIn(f"kept {gate}: busy", result.stdout.splitlines())
                    self.assertEqual((gate / "target" / "output").read_bytes(), b"generated output")
                else:
                    self.assertIn(f"kept {gate}: rescue failed: ", result.stdout)
                    self.assertEqual((gate / "checkout" / "source.txt").read_text(), "edited source\n")

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
