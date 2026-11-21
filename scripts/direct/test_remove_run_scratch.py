#!/usr/bin/env python3
"""Tests for remove_run_scratch.py on a fabricated run folder."""

from __future__ import annotations

import json
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from collections.abc import Callable
from contextlib import redirect_stderr
from typing import override, cast
from unittest import mock

from scripts.direct import remove_run_scratch, workers
from scripts.lib import scratch
from scripts.lib.git_test_support import fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, fixture_member, fixture_worker

SCRIPT = Path(__file__).parent / "remove_run_scratch.py"


class RemoveRunScratchTests(unittest.TestCase):
    owner_id: str = ""

    @override
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="run-scratch-fixture-")).resolve()
        self.addCleanup(shutil.rmtree, root)
        self.enterContext(isolated_staff_file(root / "staff.json", {}))
        environment = fixture_git_environment(root)
        _ = environment.pop("STAFF_CONFIG", None)
        environment["HOME"] = str(root / "home")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment, clear=True)))
        owner = fixture_member("showrunner", name="demo")
        self.owner_id = fixture_member("unit", name="builders", parent=owner.id).id

    def make_run(self) -> tuple[Path, Path]:
        """A run folder with copies at several depths, and a folder outside it."""
        root = Path(tempfile.mkdtemp(prefix="run-scratch-fixture-")).resolve()
        self.addCleanup(shutil.rmtree, root)
        run = root / "run"
        (run / "verify_cache").mkdir(parents=True)
        _ = (run / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": self.owner_id}))
        _ = (run / "verify_cache" / "pass").write_text("ok\n", encoding="utf-8")
        _ = (run / "workers").write_text("id\tname\n", encoding="utf-8")
        _ = (run / "heartbeat.log").write_text("beat\n", encoding="utf-8")
        _ = (run / "board.json").write_text("{}", encoding="utf-8")
        # A gate copy: a clone with its own .git folder and a target inside it.
        gate = run / "phases" / "p4" / "gate"
        _ = subprocess.run(["git", "init", "-q", str(gate)], check=True, capture_output=True)
        _ = (gate / ".git/info/exclude").write_text("/target\n")
        (gate / "target").mkdir()
        _ = (gate / "target" / "CACHEDIR.TAG").write_text("tag\n", encoding="utf-8")
        _ = (run / "phases" / "p4" / "review.diff").write_text("diff\n", encoding="utf-8")
        # A worktree checkout: .git is a file.
        source = root / "source"
        _ = subprocess.run(["git", "init", "-q", str(source)], check=True, capture_output=True)
        _ = subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t",
                            "commit", "-qm", "Create the fixture", "--allow-empty"], check=True, capture_output=True)
        _ = subprocess.run(["git", "-C", str(source), "worktree", "add", "-q", "--detach",
                            str(run / "scratch/export"), "HEAD"], check=True, capture_output=True)
        # A finished cargo target with no tag.
        (run / "p26_before" / "debug").mkdir(parents=True)
        _ = (run / "p26_before" / ".rustc_info.json").write_text("{}", encoding="utf-8")
        # A link to a checkout outside the run folder.
        outside = root / "outside"
        (outside / ".git").mkdir(parents=True)
        (run / "scratch" / "linked").symlink_to(outside)
        return run, outside

    def test_tracked_cleanup_preserves_the_working_directory(self) -> None:
        run, outside = self.make_run()
        kept = [scratch.KeptFolder(outside, "selected by the keep rule")]
        with (mock.patch.object(scratch, "end", return_value=kept) as end,
              mock.patch.object(workers, "protected_working_directories", return_value=[outside])):
            self.assertEqual(remove_run_scratch.end_tracked_scratch(run), kept)
        end.assert_called_once_with(f"run:{run.name}", keep=mock.ANY)
        keep = cast(Callable[[Path], bool], end.call_args.kwargs["keep"])
        self.assertTrue(keep(outside))
        self.assertFalse(keep(run))

    def test_run_reports_folders_and_keeps_an_unreadable_cargo_record(self) -> None:
        run, _ = self.make_run()
        checkout = run / "phases" / "p4" / "gate"
        mark = scratch.cargo_removal_mark(checkout)
        _ = mark.write_text("{broken")
        with (mock.patch.object(workers, "protected_working_directories", return_value=()),
              mock.patch.object(sys, "argv", [str(SCRIPT), str(run)]),
              mock.patch.object(scratch, "remove_folder") as remove,
              redirect_stderr(io.StringIO()) as report):
            self.assertEqual(remove_run_scratch.main(), 0)
        remove.assert_not_called()
        self.assertEqual(mark.read_text(), "{broken")
        for folder in (checkout, run / "scratch/export", run / "p26_before", run / "verify_cache"):
            self.assertTrue(folder.is_dir())
            self.assertIn(f"kept for a manual removal list: {folder} (", report.getvalue())

    def test_run_keeps_removal_records_after_the_source_disappears(self) -> None:
        run, _ = self.make_run()
        checkout = run / "phases/p4/gate"
        task_mark = scratch.removal_mark(checkout)
        cargo_mark = scratch.cargo_removal_mark(checkout)
        scratch.atomic_record(cargo_mark, [])
        scratch.atomic_record(task_mark, {"task": "gate:fixture"})
        shutil.rmtree(checkout)
        self.assertFalse(checkout.exists())
        self.assertTrue(task_mark.is_file())
        self.assertTrue(cargo_mark.is_file())
        self.assertNotIn(checkout, remove_run_scratch.scratch_folders(run))
        with (mock.patch.object(workers, "protected_working_directories", return_value=()),
              mock.patch.object(sys, "argv", [str(SCRIPT), str(run)])):
            self.assertEqual(remove_run_scratch.main(), 0)
        self.assertTrue(task_mark.is_file())
        self.assertTrue(cargo_mark.is_file())

    def test_cleanup_keeps_the_checkout_holding_a_paused_workers_live_command(self) -> None:
        run, _ = self.make_run()
        checkout = run / "phases" / "p4" / "gate"
        command_cwd = checkout / "target"
        records = run / "detached_runs"
        records.mkdir()
        record = records / "verify.json"
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                                  "detached_runs.py", "supervise", str(record)], stdin=subprocess.PIPE)
        self.addCleanup(lambda: child.communicate(timeout=TEST_HANG_GUARD_SECS))
        environment = {"CODEX_HOME": str(run.parent / "codex")}
        _ = self.enterContext(isolated_staff_file(run.parent / "staff.json", environment))
        owner = fixture_member("showrunner", name="demo")
        parent = fixture_member("unit", name="builders", parent=owner.id,
                                worktree=staff.PresentWorktree("natedev", command_cwd))
        _ = (run / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": parent.id}))
        member = fixture_worker("w7", parent.id, backend=staff.CodexBackend(Path(environment["CODEX_HOME"])))
        _ = record.write_text(json.dumps({"status": "running", "pid": child.pid, "worker": "w7", "staff_id": member.id}))
        with mock.patch.dict(os.environ, environment):
            result = subprocess.run([sys.executable, str(SCRIPT), str(run)], capture_output=True,
                                    text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(command_cwd.is_dir(), result.stdout)
            self.assertTrue((run / "scratch" / "export").is_dir())
            self.assertNotIn(f"kept for a manual removal list: {checkout} (", result.stderr)
            self.assertTrue(record.exists())
            _ = record.write_text(json.dumps({"status": "finished", "pid": os.getpid(), "worker": "w7", "staff_id": member.id}))
            result = subprocess.run([sys.executable, str(SCRIPT), str(run)], capture_output=True,
                                    text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(checkout.is_dir())
            self.assertIn(f"kept for a manual removal list: {checkout} (", result.stderr)

    def test_run_reports_old_folders_beside_a_protected_branch(self) -> None:
        run, _ = self.make_run()
        protected = run / "phases" / "p5" / "gate"
        (protected / ".git").mkdir(parents=True)
        cwd = protected / "build"
        cwd.mkdir()
        old_target = run / "phases" / "p3" / "p3_before"
        old_target.mkdir(parents=True)
        _ = (old_target / ".rustc_info.json").write_text("{}")
        with (mock.patch.object(workers, "protected_working_directories", return_value=(cwd,)),
              mock.patch.object(sys, "argv", [str(SCRIPT), str(run)]),
              redirect_stderr(io.StringIO()) as report):
            self.assertEqual(remove_run_scratch.main(), 0)
        self.assertTrue(cwd.is_dir())
        self.assertTrue((run / "phases" / "p4" / "gate").is_dir())
        self.assertTrue(old_target.is_dir())
        self.assertNotIn(f"kept for a manual removal list: {protected} (", report.getvalue())
        self.assertIn(f"kept for a manual removal list: {old_target} (", report.getvalue())
        self.assertTrue((run / "phases" / "p4" / "review.diff").is_file())

    def test_run_reports_checkouts_targets_and_verify_cache(self) -> None:
        run, outside = self.make_run()
        found = remove_run_scratch.scratch_folders(run)
        self.assertEqual(
            sorted(found),
            sorted([run / "verify_cache", run / "phases" / "p4" / "gate", run / "scratch" / "export",
                    run / "p26_before"]),
        )

        result = subprocess.run([sys.executable, str(SCRIPT), str(run)], check=False, capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(result.stderr.splitlines(),
                              [f"kept for a manual removal list: {folder} (remove_run_scratch, the run ended)"
                               for folder in found])
        for folder in found:
            self.assertTrue(folder.is_dir())
        for kept in ("workers", "heartbeat.log", "board.json", "phases/p4/review.diff"):
            self.assertTrue((run / kept).is_file(), kept)
        self.assertTrue((run / "scratch" / "linked").is_symlink())
        self.assertTrue((outside / ".git").is_dir())

    def test_the_run_folder_itself_stays_when_it_is_a_checkout(self) -> None:
        run, _ = self.make_run()
        (run / ".git").mkdir()
        self.assertNotIn(run, remove_run_scratch.scratch_folders(run))
        self.assertIn(run / "p26_before", remove_run_scratch.scratch_folders(run))

    def test_a_linked_run_folder_and_a_missing_one_are_left_alone(self) -> None:
        run, _ = self.make_run()
        linked = run.parent / "linked-run"
        linked.symlink_to(run)
        self.assertEqual(remove_run_scratch.scratch_folders(linked), [])
        self.assertEqual(remove_run_scratch.scratch_folders(run.parent / "missing"), [])

        result = subprocess.run([sys.executable, str(SCRIPT), str(linked)], check=False, capture_output=True, text=True)

        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertTrue((run / "verify_cache").is_dir())

    def worktree_run(self) -> tuple[Path, Path, Path]:
        """A source repo, and a run folder holding a linked worktree of its branch copy."""
        root = Path(tempfile.mkdtemp(prefix="run-scratch-fixture-")).resolve()
        self.addCleanup(shutil.rmtree, root)
        source, run = root / "source", root / "run"
        def git(*arguments: str) -> None:
            _ = subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                               check=True, capture_output=True)
        source.mkdir()
        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "start")
        git("branch", "copy")
        run.mkdir()
        _ = (run / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": self.owner_id}))
        git("worktree", "add", "-q", str(run / "phases" / "p4" / "gate"), "copy")
        return source, run, run / "phases" / "p4" / "gate"

    def worktrees(self, source: Path) -> list[str]:
        listing = subprocess.run(["git", "-C", str(source), "worktree", "list", "--porcelain"],
                                 check=True, capture_output=True, text=True).stdout
        return [line.removeprefix("worktree ") for line in listing.splitlines() if line.startswith("worktree ")]

    def test_run_keeps_the_worktree_and_its_source_entry(self) -> None:
        source, run, gate = self.worktree_run()

        result = subprocess.run([sys.executable, str(SCRIPT), str(run)], check=False, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(gate.is_dir())
        self.assertCountEqual(self.worktrees(source), [str(source), str(gate)])

    def test_a_worktree_the_source_cannot_remove_is_deleted_and_the_source_pruned(self) -> None:
        source, _, gate = self.worktree_run()
        real = remove_run_scratch.git
        def refuse_remove(common: Path, *arguments: str) -> bool:
            return False if arguments[0] == "remove" else real(common, *arguments)

        with mock.patch.object(remove_run_scratch, "git", side_effect=refuse_remove) as git:
            scratch.remove_marked(gate, source_deletion=scratch.SourceDeletion.RUN)

        self.assertEqual([call.args[1] for call in git.call_args_list], ["remove", "prune"])
        self.assertFalse(gate.exists())
        self.assertEqual(self.worktrees(source), [str(source)])

    def test_run_keeps_a_worktree_with_staged_changes(self) -> None:
        source, run, gate = self.worktree_run()

        def git(*arguments: str) -> str:
            return subprocess.run(["git", "-C", str(gate), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                                  check=True, capture_output=True, text=True).stdout

        _ = (gate / "tracked").write_text("The last tracked file\n")
        _ = git("add", "tracked")
        _ = git("commit", "-qm", "Add the tracked file")
        _ = git("rm", "-q", "tracked")
        common = source / ".git" / "worktrees" / gate.name / "commondir"
        target = common.rename(common.with_name("shared-folder"))
        common.symlink_to(target.name)
        self.assertEqual(git("status", "--porcelain"), "D  tracked\n")

        result = subprocess.run([sys.executable, str(SCRIPT), str(run)], check=False, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"kept for a manual removal list: {gate} (", result.stderr)
        self.assertTrue(gate.is_dir())
        self.assertEqual(git("status", "--porcelain"), "D  tracked\n")

    def test_run_keeps_a_worktree_after_its_source_disappears(self) -> None:
        source, run, gate = self.worktree_run()
        shutil.rmtree(source)
        self.assertIsNone(remove_run_scratch.worktree_source(gate))

        result = subprocess.run([sys.executable, str(SCRIPT), str(run)], check=False, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(gate.is_dir())
        self.assertIn(f"kept for a manual removal list: {gate} (", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
