"""Repository slots retain work, reuse files, and enforce their locks."""
from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
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
import time
from typing import cast, override
import unittest
from unittest import mock

from scripts.lib import build_folders, build_slots, git_rescue
from scripts.build_queue import admission_protocol as wire, slot_wait
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.lib.build_slots_test_support import UseDefaultMaximum, private_slot_config
from scripts.lib.descendants import adopt_descendants
from scripts.lib.git_test_support import commit_repository
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


class BuildSlotsTests(unittest.TestCase):
    root: Path = Path()
    source: Path = Path()
    base: str = ""
    settings: Path = Path()
    slot_root: Path = Path()
    cargo_root: Path = Path()
    machine: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        configuration = private_slot_config(self.root, repository="widget")
        self.settings = configuration.settings
        self.slot_root = configuration.slots
        self.cargo_root = configuration.cargo
        self.machine = configuration.machine
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

    def take(self, commit: str = "HEAD") -> AbstractContextManager[build_slots.Slot]:
        return build_slots.take_slot(self.source, commit, showrunner="builds", unit="queue",
                                     work="proof", purpose="test", wait_policy=build_slots.SlotWaitPolicy.REFUSE)

    def lease(self, path: Path) -> dict[str, object]:
        return cast(dict[str, object], json.loads(path.with_name(path.name + ".lease.json").read_text()))

    def age_lease(self, path: Path) -> None:
        record = self.lease(path)
        record["last_use"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        _ = path.with_name(path.name + ".lease.json").write_text(json.dumps(record) + "\n")

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        checkout = Path(__file__).resolve().parents[2]
        return subprocess.run([sys.executable, "-m", "scripts.lib.build_slots", *arguments],
                              cwd=checkout, capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(checkout), "PYTHONSAFEPATH": "1"},
                              timeout=TEST_HANG_GUARD_SECS)

    def run_arguments(self) -> tuple[str, ...]:
        return ("run", "--source", str(self.source), "--commit", self.base,
                "--showrunner", "builds", "--unit", "queue", "--work", "proof", "--purpose", "test", "--")

    def test_lowest_free_number_grows_to_the_limit_and_reads_a_changed_limit(self) -> None:
        with self.take() as first:
            self.assertEqual(first.number, 1)
            self.assertEqual(first.repository, "widget")
            self.assertEqual(first.path, self.slot_root / "widget-slot1")
            self.assertEqual(first.cargo_dir, self.cargo_root / "widget-slot1")
            self.assertEqual(first.env["CARGO_BUILD_BUILD_DIR"], str(first.cargo_dir))
            with self.take() as second:
                self.assertEqual(second.number, 2)
                self.assertEqual(build_folders.build_folder_state(first.path), "held")
                self.assertEqual(build_folders.build_folder_state(second.path), "held")
                with self.assertRaises(build_slots.SlotsFull) as raised:
                    with self.take():
                        self.fail("a full pool granted a slot")
                self.assertEqual(raised.exception.repository, "widget")
                self.assertEqual(raised.exception.machine, self.machine)
                self.assertEqual(raised.exception.maximum, 2)
                _ = write_config_fixture(self.settings,
                                         f"slots.{self.machine}.widget.max=3\n")
                with self.take() as third:
                    self.assertEqual(third.number, 3)
            with self.take() as reused:
                self.assertEqual(reused.number, 2)
        with self.take() as lowest:
            self.assertEqual(lowest.number, 1)

    def test_missing_repository_limit_uses_the_default_and_prints_the_tune_key(self) -> None:
        _ = private_slot_config(self.root, repository="widget", maximum=UseDefaultMaximum())
        output = io.StringIO()
        with redirect_stdout(output), self.take() as held:
            self.assertEqual(held.number, 1)
        expected = (f"slots: widget on {self.machine} has no maximum of its own; it uses 1. "
                    f"Set slots.{self.machine}.widget.max with /tune.")
        self.assertEqual(output.getvalue().splitlines(), [expected])
        with redirect_stdout(io.StringIO()), self.take():
            with self.assertRaises(build_slots.SlotsFull) as raised:
                with self.take():
                    self.fail("the default maximum did not limit the pool")
        self.assertEqual(raised.exception.maximum, 1)

    def test_missing_slot_root_stops_allocation_and_sweep_until_private_settings_return(self) -> None:
        before = self.git(self.source, "worktree", "list", "--porcelain")
        lines = [line for line in self.settings.read_text().splitlines()
                 if not line.startswith("roots.slots")]
        _ = self.settings.write_text("\n".join(lines) + "\n")

        with self.assertRaises(build_slots.SlotsNotConfigured):
            with self.take():
                self.fail("a slot was allocated without a configured root")
        build_slots.sweep_slots()

        self.assertEqual(self.git(self.source, "worktree", "list", "--porcelain"), before)
        self.assertFalse(self.slot_root.exists())
        self.assertFalse(self.cargo_root.exists())
        _ = private_slot_config(self.root, repository="widget")
        with self.take() as slot:
            self.assertEqual(slot.path, self.slot_root / "widget-slot1")

    def test_linked_source_uses_the_same_pool_and_origin_repository_word(self) -> None:
        with self.take() as first:
            with build_slots.take_slot(first.path, self.base, showrunner="other", unit="unit",
                                       work="another", purpose="check") as second:
                self.assertEqual(second.repository, "widget")
                self.assertEqual(second.path, self.slot_root / "widget-slot2")
                self.assertEqual(self.git(second.path, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                                 str(self.source / ".git"))

    def test_interrupted_creation_recovers_before_a_caller_gets_the_slot(self) -> None:
        probe = """import os, pathlib, subprocess, sys
from unittest import mock
from scripts.lib import build_folders, build_slots
source, path = map(pathlib.Path, sys.argv[1:3])
stage = sys.argv[3]
mkdir, run = os.mkdir, subprocess.run
def interrupt_mkdir(name, mode=0o777, *, dir_fd=None):
    result = mkdir(name, mode, dir_fd=dir_fd)
    if name == path.name and dir_fd is not None:
        os._exit(23)
    return result
def interrupt_checkout(argv, **options):
    result = run(argv, **options)
    if 'worktree' in argv and 'add' in argv:
        os._exit(23)
    return result
patch = (mock.patch.object(build_folders.os, 'mkdir', side_effect=interrupt_mkdir) if stage == 'mkdir'
         else mock.patch.object(subprocess, 'run', side_effect=interrupt_checkout))
with patch, build_slots.take_slot(source, 'HEAD', showrunner='builds', unit='queue', work='proof', purpose='test'):
    raise RuntimeError('the interruption did not occur')
"""
        path = self.slot_root / "widget-slot1"
        checkout = Path(__file__).resolve().parents[2]
        for stage in ("mkdir", "checkout"):
            with self.subTest(stage=stage):
                result = subprocess.run([sys.executable, "-c", probe, str(self.source), str(path), stage],
                                        cwd=checkout, env={**os.environ, "PYTHONPATH": str(checkout)},
                                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
                if stage == "checkout":
                    _ = (path / "recovery.txt").write_text("work before recovery\n")
                with self.take() as recovered:
                    self.assertEqual(recovered.path, path)
                    self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.base)
                    self.assertEqual((path / "change.txt").read_text(), "first commit\n")
                    if stage == "checkout":
                        refs = self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/").splitlines()
                        self.assertEqual(len(refs), 1)
                        self.assertEqual(self.git(self.source, "show", refs[0] + ":recovery.txt"), "work before recovery")
                _ = self.git(self.source, "worktree", "remove", str(path))
                path.with_name(path.name + ".lease.json").unlink()

    def test_source_without_origin_uses_the_main_checkout_name(self) -> None:
        _ = self.git(self.source, "remote", "remove", "origin")
        linked = self.root / "different-name"
        _ = self.git(self.source, "worktree", "add", "--detach", str(linked), self.base)
        with redirect_stdout(io.StringIO()):
            with build_slots.take_slot(linked, self.base, showrunner="builds", unit="queue",
                                       work="proof", purpose="test") as slot:
                self.assertEqual(slot.repository, self.source.name)
                self.assertEqual(slot.path, self.slot_root / f"{self.source.name}-slot1")

    def test_incomplete_slot_keeps_its_private_git_repository(self) -> None:
        path = self.slot_root / "widget-slot1"
        path.mkdir(parents=True)
        _ = self.git(path, "init", "-q", "-b", "saved")
        _ = (path / "local.txt").write_text("local commit\n")
        tip = commit_repository(path, "private commit")
        _ = (path / "edit.txt").write_text("unfinished edit\n")
        with self.assertRaisesRegex(ValueError, "different Git repository; retained"):
            with self.take():
                self.fail("an incomplete private repository became a slot")
        self.assertEqual(self.git(path, "rev-parse", "HEAD"), tip)
        self.assertEqual((path / "edit.txt").read_text(), "unfinished edit\n")

    def test_reuse_removes_a_stale_index_lock_and_an_external_target_link(self) -> None:
        outside = self.root / "external-target"
        outside.mkdir()
        _ = (outside / "keep").write_text("external output\n")
        for damage in ("index-lock", "target-link", "both"):
            with self.subTest(damage=damage):
                with self.take() as slot:
                    path = slot.path
                if damage in ("index-lock", "both"):
                    directory = Path(self.git(path, "rev-parse", "--absolute-git-dir"))
                    _ = (directory / "index.lock").write_text("interrupted Git write\n")
                if damage in ("target-link", "both"):
                    target = path / "target"
                    if target.exists():
                        shutil.rmtree(target)
                    target.symlink_to(outside, target_is_directory=True)
                with self.take() as reused:
                    self.assertEqual(reused.path, path)
                    self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.base)
                    self.assertFalse((path / "target").is_symlink())
                    self.assertEqual((outside / "keep").read_text(), "external output\n")
                    _ = (path / "target").mkdir(exist_ok=True)
                    _ = (path / "target" / "new").write_text("slot output\n")
                    self.assertFalse((outside / "new").exists())

    def test_reset_rescues_edits_and_retains_unchanged_files_target_and_cargo(self) -> None:
        with self.take() as first:
            path, cargo = first.path, first.cargo_dir
            same = (path / "same.txt").stat()
            _ = (path / "change.txt").write_text("slot work\n")
            _ = (path / "untracked.txt").write_text("untracked source\n")
            (path / "target").mkdir()
            _ = (path / "target" / "keep").write_text("target cache\n")
            cargo.mkdir(parents=True, exist_ok=True)
            _ = (cargo / "keep").write_text("cargo cache\n")
        _ = (self.source / "change.txt").write_text("second commit\n")
        second = commit_repository(self.source, "second")

        with self.take(second) as reused:
            self.assertEqual(reused.path, path)
            current = (path / "same.txt").stat()
            self.assertEqual((current.st_ino, current.st_mtime_ns), (same.st_ino, same.st_mtime_ns))
            self.assertEqual((path / "change.txt").read_text(), "second commit\n")
            self.assertFalse((path / "untracked.txt").exists())
            self.assertEqual((path / "target/keep").read_text(), "target cache\n")
            self.assertEqual((cargo / "keep").read_text(), "cargo cache\n")
            self.assertEqual(self.git(path, "rev-parse", "HEAD"), second)
            self.assertEqual(self.git(path, "branch", "--show-current"), "")
        refs = self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/").splitlines()
        self.assertEqual(len(refs), 1)
        self.assertEqual(self.git(self.source, "show", refs[0] + ":change.txt"), "slot work")
        self.assertEqual(self.git(self.source, "show", refs[0] + ":untracked.txt"), "untracked source")

    def test_build_output_keeps_new_and_reused_slots_clean_without_a_tracked_ignore_file(self) -> None:
        (self.source / ".gitignore").unlink()
        self.base = commit_repository(self.source, "no tracked ignore file")
        for use in ("new", "reused"):
            with self.subTest(use=use), self.take() as slot:
                target = slot.path / "target"
                target.mkdir(exist_ok=True)
                _ = (target / "build-output").write_text("compiled output\n")
                self.assertEqual(self.git(slot.path, "status", "--porcelain"), "")
        (self.source / "target").mkdir()
        _ = (self.source / "target/build-output").write_text("source build output\n")
        self.assertEqual(self.git(self.source, "status", "--porcelain"), "?? target/")

    def test_rescue_failure_preserves_source_and_cargo_and_frees_the_lock(self) -> None:
        for action in ("reset", "recovery", "sweep"):
            with self.subTest(action=action):
                with self.take() as slot:
                    path, cargo = slot.path, slot.cargo_dir
                    _ = (path / "change.txt").write_text("do not lose this edit\n")
                    cargo.mkdir(parents=True, exist_ok=True)
                    _ = (cargo / "keep").write_text("cached output\n")
                self.age_lease(path)
                if action == "recovery":
                    lease = self.lease(path)
                    lease["ready"] = False
                    _ = path.with_name(path.name + ".lease.json").write_text(json.dumps(lease))
                output = io.StringIO()
                error = git_rescue.RescueError(("git", "update-ref"), "fixture write failure")
                with mock.patch.object(git_rescue, "rescue", side_effect=error):
                    if action != "sweep":
                        with self.assertRaises(git_rescue.RescueError):
                            with self.take():
                                self.fail("reset continued after a failed rescue")
                    else:
                        with redirect_stdout(output), redirect_stderr(output):
                            build_slots.sweep_slots()
                        self.assertEqual(len(output.getvalue().splitlines()), 1)
                        self.assertIn("fixture write failure", output.getvalue())
                self.assertEqual((path / "change.txt").read_text(), "do not lose this edit\n")
                self.assertEqual((cargo / "keep").read_text(), "cached output\n")
                self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.base)
                self.assertEqual(build_folders.build_folder_state(path), "free")

    def test_stalled_git_times_out_and_releases_each_slot_lock(self) -> None:
        real_git = shutil.which("git")
        assert real_git is not None
        binary = self.root / "bin"
        binary.mkdir()
        fake_git = binary / "git"
        _ = write_config_fixture(self.settings, "git_timeout_seconds=0.1\n")
        for action in ("origin", "reset", "recovery", "sweep"):
            with self.subTest(action=action):
                with self.take() as slot:
                    path = slot.path
                    _ = (path / "change.txt").write_text("keep this source\n")
                    _ = (slot.cargo_dir / "keep").write_text("cached output\n")
                self.age_lease(path)
                if action == "recovery":
                    lease = self.lease(path)
                    lease["ready"] = False
                    _ = build_slots.lease_path(path).write_text(json.dumps(lease))
                command = "remote" if action == "origin" else "status"
                _ = fake_git.write_text(
                    f"#!{sys.executable}\nimport os, sys, time\n"
                    + f"if {command!r} in sys.argv: time.sleep(1)\n"
                    + f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n")
                fake_git.chmod(0o755)
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}):
                    if action == "sweep":
                        with redirect_stderr(output):
                            build_slots.sweep_slots()
                        self.assertIn("timed out after 0.1 s", output.getvalue())
                        self.assertIn(str(path), output.getvalue())
                    else:
                        error_type = build_slots.SlotGitFailure if action == "origin" else git_rescue.RescueError
                        with self.assertRaises(error_type) as raised:
                            with self.take():
                                self.fail("the stalled Git command did not time out")
                        self.assertIn("timed out after 0.1 s", str(raised.exception))
                        self.assertIn(str(self.source if action == "origin" else path), str(raised.exception))
                self.assertEqual((path / "change.txt").read_text(), "keep this source\n")
                self.assertEqual((slot.cargo_dir / "keep").read_text(), "cached output\n")
                self.assertEqual(build_folders.build_folder_state(path), "free")
                with self.take() as reused:
                    self.assertEqual(reused.path, path)

    def test_a_damaged_index_and_source_edits_restore_from_the_rescue_before_reset(self) -> None:
        for number, damaged in enumerate((b"damaged index\x00\xff", b"DIRC\x00\x00\x00\xff")):
            with self.subTest(index=damaged):
                before = set(self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/").splitlines())
                with self.take() as slot:
                    index = Path(self.git(slot.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
                    _ = (slot.path / "change.txt").write_text("source edit before reset\n")
                    _ = (slot.path / "untracked.txt").write_text("untracked source\n")
                    target = slot.path / "target"
                    target.mkdir(exist_ok=True)
                    _ = (target / "keep").write_text("cached build output\n")
                    _ = index.write_bytes(damaged)
                with self.take() as recovered:
                    self.assertEqual(self.git(recovered.path, "status", "--porcelain"), "")
                    self.assertEqual((recovered.path / "change.txt").read_text(), "first commit\n")
                    self.assertEqual((recovered.path / "target/keep").read_text(), "cached build output\n")
                after = set(self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/").splitlines())
                self.assertEqual(len(after - before), 1)
                saved, = after - before
                restored = self.root / f"restored-{number}"
                _ = self.git(self.source, "worktree", "add", "--detach", str(restored), saved)
                self.assertEqual((restored / "change.txt").read_text(), "source edit before reset\n")
                self.assertEqual((restored / "untracked.txt").read_text(), "untracked source\n")
                backups = tuple(restored.glob(".rescue-index-*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), damaged)

    def test_a_different_common_git_folder_is_refused_without_changing_the_slot(self) -> None:
        with self.take() as first:
            path = first.path
        _ = self.git(self.source, "worktree", "remove", str(path))
        other = self.root / "other-repository"
        other.mkdir()
        _ = self.git(other, "init", "-q", "-b", "trunk")
        _ = (other / "foreign.txt").write_text("foreign work\n")
        foreign = commit_repository(other, "foreign")
        _ = self.git(other, "worktree", "add", "--detach", str(path), foreign)

        with self.assertRaises(build_slots.SlotRepositoryMismatch):
            with self.take():
                self.fail("a slot from a different repository was reset")

        self.assertEqual(self.git(path, "rev-parse", "HEAD"), foreign)
        self.assertEqual((path / "foreign.txt").read_text(), "foreign work\n")
        self.assertEqual(build_folders.build_folder_state(path), "free")

    def test_lease_records_the_holder_and_release_time_but_does_not_hold_the_slot(self) -> None:
        with self.take(self.base) as slot:
            path = slot.path
            record = self.lease(path)
            expected: dict[str, object] = {
                "showrunner": "builds", "unit": "queue", "work": "proof", "purpose": "test",
                "pid": os.getpid(), "machine": self.machine, "repository": "widget",
                "slot": 1, "commit": self.base,
            }
            for key, value in expected.items():
                with self.subTest(field=key):
                    self.assertEqual(record[key], value)
            self.assertIsInstance(record["start_time"], str)
            self.assertTrue(record["start_time"])
            taken_at = datetime.fromisoformat(str(record["last_use"]))
            self.assertEqual(taken_at.utcoffset(), timedelta(0))
            self.age_lease(path)

        released_at = datetime.fromisoformat(str(self.lease(path)["last_use"]))
        self.assertGreaterEqual(released_at, taken_at)
        self.assertEqual(released_at.utcoffset(), timedelta(0))
        self.assertEqual(build_folders.build_folder_state(path), "free")
        with self.take() as next_holder:
            self.assertEqual(next_holder.path, path)

    def test_sweep_rescues_old_free_slots_but_keeps_held_slots(self) -> None:
        with ExitStack() as holding:
            with self.take() as first:
                old_path, old_cargo = first.path, first.cargo_dir
                _ = (old_path / "change.txt").write_text("old slot work\n")
                old_cargo.mkdir(parents=True, exist_ok=True)
                _ = (old_cargo / "cache").write_text("old cache\n")
                held = holding.enter_context(self.take())
                held.cargo_dir.mkdir(parents=True, exist_ok=True)
                _ = (held.cargo_dir / "cache").write_text("held cache\n")
            self.age_lease(old_path)
            self.age_lease(held.path)

            build_slots.sweep_slots()

            self.assertFalse(old_path.exists())
            self.assertFalse(old_cargo.exists())
            self.assertTrue(held.path.is_dir())
            self.assertEqual((held.cargo_dir / "cache").read_text(), "held cache\n")
        self.assertNotIn(str(old_path), self.git(self.source, "worktree", "list", "--porcelain"))
        refs = self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/").splitlines()
        self.assertTrue(any(self.git(self.source, "show", ref + ":change.txt") == "old slot work" for ref in refs))

    def test_sweep_deletes_oversized_cargo_once_and_keeps_the_source(self) -> None:
        with self.take() as slot:
            path, cargo = slot.path, slot.cargo_dir
            cargo.mkdir(parents=True, exist_ok=True)
            _ = (cargo / "cache").write_bytes(b"x" * 256)
        with self.take() as reused:
            self.assertEqual(reused.path, path)
            self.assertEqual((cargo / "cache").stat().st_size, 256)
        source_stat = (path / "same.txt").stat()
        _ = write_config_fixture(self.settings,
                                 f"# whole: 1..\nslots.{self.machine}.widget.max_bytes=64\n")
        result = self.command("sweep")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(cargo.exists())
        self.assertEqual((path / "same.txt").read_text(), "unchanged\n")
        self.assertEqual((path / "same.txt").stat().st_ino, source_stat.st_ino)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertRegex(result.stdout, r"slots: widget-slot1 was .+ GiB, over its .+ GiB limit; its build output is deleted\.")
        repeated = io.StringIO()
        with redirect_stdout(repeated):
            build_slots.sweep_slots()
        self.assertEqual(repeated.getvalue(), "")
        self.assertTrue(path.is_dir())

    def test_take_deletes_oversized_output_after_rescue_and_retains_source_times(self) -> None:
        with self.take() as slot:
            path, cargo = slot.path, slot.cargo_dir
            (path / "target").mkdir()
            _ = (path / "target/final").write_bytes(b"x" * 256)
            _ = (cargo / "intermediate").write_bytes(b"y" * 256)
            _ = (path / "change.txt").write_text("save this source\n")
            source_stat = (path / "same.txt").stat()
        _ = write_config_fixture(self.settings,
                                 f"# whole: 1..\nslots.{self.machine}.widget.max_bytes=64\n")
        output = io.StringIO()
        with redirect_stdout(output), self.take() as reused:
            self.assertEqual(reused.path, path)
            self.assertFalse((path / "target/final").exists())
            self.assertFalse((cargo / "intermediate").exists())
            self.assertTrue(cargo.is_dir())
            current = (path / "same.txt").stat()
            self.assertEqual((current.st_ino, current.st_mtime_ns), (source_stat.st_ino, source_stat.st_mtime_ns))
            self.assertEqual((path / "change.txt").read_text(), "first commit\n")
        refs = self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/").splitlines()
        self.assertEqual(len(refs), 1)
        self.assertEqual(self.git(self.source, "show", refs[0] + ":change.txt"), "save this source")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("widget-slot1", output.getvalue())

    def test_sweep_finishes_an_interrupted_delete_from_its_lease(self) -> None:
        with self.take() as slot:
            path, cargo = slot.path, slot.cargo_dir
            _ = (cargo / "cache").write_text("cached output\n")
        self.age_lease(path)
        with mock.patch.object(shutil, "rmtree", side_effect=OSError("cargo removal stopped")):
            with self.assertRaisesRegex(OSError, "cargo removal stopped"):
                build_slots.sweep_slots()
        self.assertFalse(path.exists())
        self.assertEqual((cargo / "cache").read_text(), "cached output\n")
        build_slots.sweep_slots()
        self.assertFalse(cargo.exists())
        self.assertFalse(path.with_name(path.name + ".lease.json").exists())
        with self.take() as replacement:
            self.assertEqual(replacement.path, path)
            self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.base)

    def test_run_gives_the_child_the_cargo_environment_and_lock_and_keeps_its_exit_code(self) -> None:
        probe = """import fcntl, json, os, pathlib, sys
path = pathlib.Path.cwd()
descriptor = int(os.environ['BUILD_FOLDER_LOCK_FD'])
lock = path.with_name(path.name + '.lock')
assert os.path.samestat(os.fstat(descriptor), lock.stat())
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
        self.assertEqual(report, {"path": str(self.slot_root / "widget-slot1"),
                                  "cargo": str(self.cargo_root / "widget-slot1"), "held": True})
        path = self.slot_root / "widget-slot1"
        self.assertTrue(path.is_dir())
        self.assertEqual(build_folders.build_folder_state(path), "free")

    def test_run_exits_75_at_the_maximum_without_starting_the_child(self) -> None:
        _ = write_config_fixture(self.settings,
                                 f"slots.{self.machine}.widget.max=1\n")
        marker = self.root / "child-started"
        original = dict(os.environ)
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = time.time()

            def expire_after_wait(reply: wire.Reply) -> wire.Reply:
                if isinstance(reply, wire.RepositorySlotWaiting):
                    fixture.clock.advance(60)
                return reply

            with mock.patch.dict(os.environ, {**original, "ADMISSION_SOCKET": str(fixture.socket_path)}), \
                    mock.patch.object(fixture.server, "reply_filter", side_effect=expire_after_wait), fixture.serving():
                with self.take():
                    result = self.command(*self.run_arguments(), sys.executable, "-c",
                                          "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker))

        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(len((result.stdout + result.stderr).splitlines()), 2)
        self.assertIn("widget", result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_mac_capacity_wait_expires_without_an_admission_service(self) -> None:
        _ = write_config_fixture(self.settings,
            f"slots.{self.machine}.widget.max=1\n# range: 5e-324..\nslots.wait_seconds=0.01\n")
        with self.take() as held, mock.patch.object(sys, "platform", "darwin"), redirect_stderr(io.StringIO()):
            with self.assertRaises(slot_wait.SlotWaitExpired):
                with build_slots.take_slot(self.source, self.base, showrunner="builds", unit="queue",
                                           work="proof", purpose="test"):
                    self.fail("the held Mac slot was granted")
            self.assertEqual(build_folders.build_folder_state(held.path), "held")
        self.assertEqual(build_folders.build_folder_state(held.path), "free")

    @unittest.skipUnless(sys.platform == "linux", "descendant adoption requires Linux")
    def test_run_stops_a_detached_descendant_before_it_releases_the_slot(self) -> None:
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
            with self.take() as reused:
                self.assertEqual(reused.path, self.slot_root / "widget-slot1")
        finally:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @unittest.skipUnless(sys.platform == "linux", "the Mac path runs with Linux orphan reaping")
    def test_mac_run_stops_its_background_group_before_it_releases_the_slot(self) -> None:
        wrapper = """import os, pathlib, sys
from unittest import mock
from scripts.lib import build_folders, build_slots
from scripts.lib.descendants import adopt_descendants
adopt_descendants()
with build_slots.take_slot(pathlib.Path(sys.argv[1]), sys.argv[2], showrunner='builds',
                           unit='queue', work='proof', purpose='test') as slot:
    descriptor = build_folders.build_folder_lock_fd(slot.path)
    middle = os.fork()
    if middle == 0:
        with mock.patch.object(sys, 'platform', 'darwin'):
            status = build_folders.run_build_command(sys.argv[3:], path=slot.path,
                environment={**os.environ, **slot.env}, pass_fds=(descriptor,))
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
        with self.take() as reused:
            self.assertEqual(reused.path, self.slot_root / "widget-slot1")

    @unittest.skipUnless(sys.platform == "linux", "the test adopts the orphaned Git process")
    def test_git_keeps_the_slot_locked_after_its_caller_dies(self) -> None:
        _ = adopt_descendants()
        _ = write_config_fixture(self.settings, f"slots.{self.machine}.widget.max=1\n")
        real_git = shutil.which("git")
        assert real_git is not None
        binaries = self.root / "bin"
        binaries.mkdir()
        fake = binaries / "git"
        _ = fake.write_text(f"#!{sys.executable}\n" + """import json, os, socket, sys
arguments = sys.argv[1:]
command = ' '.join(arguments[2:4]) if arguments[2] == 'worktree' else arguments[2]
if command == os.environ['SLOT_GIT_OPERATION']:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.connect('\\0' + os.environ['SLOT_GIT_SOCKET'])
        peer.sendall((json.dumps({'pid': os.getpid(), 'command': command}) + '\\n').encode())
        peer.recv(1)
    raise SystemExit(0)
os.execv(os.environ['SLOT_REAL_GIT'], [os.environ['SLOT_REAL_GIT'], *arguments])
""")
        fake.chmod(0o755)
        checkout = Path(__file__).resolve().parents[2]
        path = self.slot_root / "widget-slot1"
        for operation in ("worktree add", "checkout", "clean", "worktree remove"):
            with self.subTest(operation=operation):
                if operation != "worktree add":
                    with self.take() as slot:
                        self.assertEqual(slot.path, path)
                    if operation == "worktree remove":
                        self.age_lease(path)
                address = f"slot-git-{os.getpid()}"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.settimeout(TEST_HANG_GUARD_SECS)
                    listener.bind("\0" + address)
                    listener.listen(1)
                    arguments = (("sweep",) if operation == "worktree remove" else
                                 (*self.run_arguments(), sys.executable, "-c", "pass"))
                    process = subprocess.Popen([sys.executable, "-m", "scripts.lib.build_slots", *arguments],
                                               cwd=checkout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                               env={**os.environ, "PYTHONPATH": str(checkout),
                                                    "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                                                    "SLOT_REAL_GIT": real_git, "SLOT_GIT_OPERATION": operation,
                                                    "SLOT_GIT_SOCKET": address})
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
                                with self.assertRaises(build_slots.SlotsFull):
                                    with self.take():
                                        self.fail("the slot was reused while Git still ran")
                            finally:
                                peer.sendall(b"x")
                                _ = os.waitpid(git_pid, 0)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                with self.take() as reused:
                    self.assertEqual(reused.path, path)


if __name__ == "__main__":
    _ = unittest.main()
