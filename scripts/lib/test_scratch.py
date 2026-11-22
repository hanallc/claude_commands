#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Tests for scratch.py: folders that belong to a task and go when it ends."""
from __future__ import annotations

import errno
import itertools
import json
import io
import fcntl
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from pathlib import Path
from threading import Event, current_thread
from typing import cast, override
from unittest import mock

from scripts.accounts import accounts
from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment
from scripts.production.cli_fixture import ImmutableGitSeed
from scripts.lib import build_folders, build_slots, cargo_output, copy_record, git_files, process_group, scratch, session_folder, time_limit
from scripts.lint import sweep

from scripts.lib import conf
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.message import sessions
from scripts.production import staff
from scripts.production.staff_test_support import (
    StaffTestCase, fixture_runtime_id, fixture_member, fixture_worker, isolated_staff_file,
    source_environment, isolated_source_readers,
)

MAKER_ID = fixture_runtime_id("scratch-Claude-session")

RUN = "run:2a858b68-0f9a-4af2-b3cf-a776e6b180cb"
ROW = "row:cargo-liner-C54"


def setUpModule() -> None:
    root = Path(tempfile.mkdtemp(prefix="scratch-test-module-")).resolve()
    unittest.addModuleCleanup(shutil.rmtree, root)
    _ = cast(object, unittest.enterModuleContext(mock.patch.dict(os.environ, config_environment(root))))


class ScratchTests(unittest.TestCase):
    base: Path = Path()

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_") and key not in ("BASH_ENV", "STAFF_CONFIG")}
        fixture = copies_environment(self.base)
        _ = write_config_fixture(Path(fixture["CONF_DIR"]) / "build_folders.conf",
            f"roots.build={self.base / 'build'}\nroots.build.natedev={self.base / 'build'}\n"
            + f"roots.workspace={self.base / 'worktrees'}\n")
        self.enterContext(mock.patch.dict(os.environ, {
            **environment,
            **fixture,
            "HOME": str(self.base),
            copy_record.ROOT_ENV: str(self.base / "scratch"), scratch.VISIBLE_ROOT_ENV: str(self.base / "repro"),
            scratch.HOLDS_ENV: str(self.base / "holds.json"), copy_record.COPIES_ENV: str(self.base / "copies"),
            "CODEX_THREAD_ID": MAKER_ID, "STAFF_FILE": str(self.base / "staff.json"),
            # An absent session root: a run without the test-run folder must not reach the live root.
            session_folder.ROOT_ENV: str(self.base / "session")}, clear=True))
        _ = staff.ensure("showrunner", name="maker", runtime_id=MAKER_ID)
        def registered_plan(unit: str) -> Path:
            return self.base / "registered-plans" / f"{unit}.md"

        _ = self.enterContext(mock.patch.object(scratch, "registered_plan", side_effect=registered_plan))
        _ = self.unit_plan("maker", "### Phase 21 — Make a copy · status: todo\n")
        _ = self.unit_plan("next-unit", "### Phase 22 — Use a copy · status: todo\n")
        _ = self.unit_plan("builds", "### Phase 7 — Check a build · status: todo\n")

    def unit_plan(self, unit: str, text: str) -> Path:
        plan = self.base / "registered-plans" / f"{unit}.md"
        plan.parent.mkdir(exist_ok=True)
        _ = plan.write_text(text)
        return plan

    def git(self, repo: Path, *arguments: str) -> None:
        _ = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                           check=True, capture_output=True)

    def copy(self, name: str = "a", task: str = "gate:copy", **details: str) -> Path:
        copies = self.base / "copies"
        os.environ["SCRATCH_COPIES_ROOT"] = str(copies)
        self.addCleanup(os.environ.pop, "SCRATCH_COPIES_ROOT", None)
        path = copies / "show-maker" / name
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        self.register(path, task, **details)
        return path

    def register(self, path: Path, task: str, **details: str) -> None:
        scratch.register_copy(path, task=task, owner_unit="maker", source_path=str(path),
                              source_commit="a" * 40, copy_commit="b" * 40, target_mode="none",
                              seed_source="", borrows_from="", **details)

    def removal_attempt(self, path: Path, task: str) -> scratch.RemovedCopy | scratch.KeptFolder:
        try:
            return scratch.remove_task_copy(path, task, time.time())
        except (scratch.ScratchError, OSError) as error:
            return scratch.KeptFolder(path, str(error))

    def test_hold_and_task_end_keep_canaries_and_the_scratch_folder(self) -> None:
        task = "gate:fixture-holds"
        home = Path.home()
        paths = [scratch.new(task, "owned")]
        for parent in (home, home / "rust", home / "worktrees", home / ".claude"):
            canary = parent / "canary"
            canary.mkdir(parents=True)
            paths.append(canary)
        for path in paths:
            _ = (path / "keep").write_text("fixture data")
            self.assertEqual(scratch.hold(path, task, "fixture"), task)
        with (redirect_stderr(io.StringIO()) as errors,
              mock.patch.object(scratch, "remove_folder", side_effect=AssertionError("task end requested removal"))):
            self.assertEqual(scratch.main(["end", "--task", task]), 0)
        self.assertCountEqual(errors.getvalue().splitlines(), [
            f"kept for a manual removal list: {path} (scratch, task {task} ended)" for path in paths
        ])
        for path in paths:
            self.assertEqual((path / "keep").read_text(), "fixture data")
            self.assertGreater(scratch.holds()[str(path)]["ended"], 0)
        record = scratch.task_of(paths[0])
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.fields["ended_task"], task)

    def test_task_end_omits_missing_paths_and_keeps_saved_holds(self) -> None:
        task = scratch.worker_task(MAKER_ID)
        kept = self.base / "kept"
        kept.mkdir()
        canary = kept / "canary"
        _ = canary.write_text("fixture data")
        missing = self.base / "missing"
        link = self.base / "dangling-link"
        link.symlink_to(missing, target_is_directory=True)
        denied = self.base / "denied"
        denied.mkdir()
        saved_holds = {
            str(path): scratch.Hold(task=task, reason="fixture", maker=MAKER_ID, made=1.0, ended=2.0)
            for path in (kept, missing, link, denied)
        }
        scratch.write_holds(saved_holds)
        lstat = os.lstat

        def deny_one(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *,
                     dir_fd: int | None = None) -> os.stat_result:
            if os.fspath(path) == str(denied):
                raise PermissionError("path access denied")
            return lstat(path, dir_fd=dir_fd)

        with (mock.patch.object(os, "lstat", side_effect=deny_one),
              mock.patch.object(scratch, "remove_folder", side_effect=AssertionError("task end requested removal")),
              redirect_stderr(io.StringIO()) as errors):
            result = scratch.end(task)
        self.assertCountEqual([entry.path for entry in result], [kept, link, denied])
        self.assertCountEqual(errors.getvalue().splitlines(), [
            scratch.retained_task_folder_notice(entry.path, entry.reason) for entry in result
        ])
        self.assertEqual(scratch.holds(), saved_holds)
        self.assertEqual(canary.read_text(), "fixture data")
        self.assertTrue(link.is_symlink())
        self.assertFalse(missing.exists())

    def test_a_callers_folder_goes_in_its_session_folder_and_its_task_ends_it(self) -> None:
        sessions = self.base / "session"
        sessions.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {session_folder.ROOT_ENV: str(sessions)}))
        folder = sessions / "maker"
        (folder / session_folder.RUN).mkdir(parents=True)
        shot = scratch.new("gate:shot", "center-shot")
        second = scratch.new("gate:shot", "center-shot")
        unnamed = scratch.new("phase:maker/21")
        taken = scratch.new("gate:shot", session_folder.RUN)
        until = scratch.new(word="hana-shot-15751")
        loose = self.base / "loose"
        loose.mkdir()
        adopted = scratch.adopt(loose, "gate:shot", "adopted")
        self.assertEqual(session_folder.folder_for(sessions, MAKER_ID), folder)
        self.assertEqual([shot, second, unnamed, taken, until, adopted],
                         [folder / "center-shot", folder / "center-shot-2", folder / "phase-maker-21",
                          folder / "run-2", folder / "hana-shot-15751", folder / "adopted"])
        held = scratch.holds()
        self.assertEqual({path: hold["task"] for path, hold in held.items()}, {
            str(shot): "gate:shot", str(second): "gate:shot", str(unnamed): "phase:maker/21",
            str(taken): "gate:shot", str(adopted): "gate:shot"})
        result = scratch.end("gate:shot")
        self.assertCountEqual([entry.path for entry in result], [shot, second, taken, adopted])
        self.assertTrue(all(path.is_dir() for path in (shot, second, unnamed, taken, until, adopted)))
        self.assertEqual({name for name, hold in scratch.holds().items() if not hold["ended"]}, {str(unnamed)})
        self.assertEqual(scratch.new("gate:other", visible=False).parent, folder)
        self.assertFalse((self.base / "scratch").exists())

    def test_copy_removal_rechecks_child_leases_after_writing_its_intent(self) -> None:
        path = self.copy()
        child = path / "late"
        write_record = scratch.atomic_record

        def allocate_late(destination: Path, fields: object) -> None:
            write_record(destination, fields)
            if destination == scratch.removal_mark(path):
                child.mkdir()
                lock = self.enterContext((path / "late.lock").open("wb"))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with mock.patch.object(scratch, "atomic_record", side_effect=allocate_late):
            result = self.removal_attempt(path, "gate:copy")
        self.assertEqual(result, scratch.KeptFolder(path, scratch.BUSY_REASON))
        self.assertTrue(child.is_dir())
        self.assertTrue((path / "late.lock").is_file())

    def test_checkout_removal_saves_outputs_before_source_deletion_and_keeps_retry_evidence(self) -> None:
        checkout = self.base / "cargo-checkout"
        checkout.mkdir()
        manifest = checkout / "Cargo.toml"
        _ = manifest.write_text("private manifest")
        cache = self.base / "outside-cache"
        roots = cargo_output.CargoOutputRoots(checkout, checkout / "target", cache)
        calls: list[str] = []
        def clean(path: Path, saved: tuple[scratch.SavedCargoOutput, ...]) -> None:
            self.assertEqual(path, checkout)
            self.assertEqual(saved[0].roots, roots)
            self.assertTrue(scratch.cargo_removal_mark(checkout).exists())
            calls.append("cache")
        def interrupted(path: Path) -> None:
            self.assertEqual(calls, ["cache"])
            (path / "Cargo.toml").unlink()
            raise OSError("stopped source deletion")
        with (mock.patch.object(cargo_output, "read_cargo_output_roots", return_value=roots),
              mock.patch.object(scratch, "cargo_checkout_peers", return_value=()),
              mock.patch.object(scratch, "clean_cargo_outputs", side_effect=clean)):
            with mock.patch.object(scratch, "remove", side_effect=interrupted):
                with self.assertRaisesRegex(OSError, "stopped source"):
                    scratch.remove_with_cargo_output(checkout)
            self.assertTrue(scratch.cargo_removal_mark(checkout).exists())
            with mock.patch.object(cargo_output, "read_cargo_output_roots", side_effect=AssertionError("lost source")):
                scratch.remove_with_cargo_output(checkout)
        self.assertEqual(calls, ["cache", "cache"])
        self.assertFalse(checkout.exists())
        self.assertFalse(scratch.cargo_removal_mark(checkout).exists())
        self.assertFalse(scratch.removal_mark(checkout).exists())

    def test_removal_intent_rechecks_current_and_legacy_sources_before_retry(self) -> None:
        for version in ("current", "legacy"):
            for state in ("unchanged", "new-work", "partial-git", "partial-new-work"):
                with self.subTest(version=version, state=state):
                    task = f"gate:recovery-{version}-{state}"
                    path = self.copy(name=f"{version}-{state}", task=task)
                    with mock.patch.object(scratch, "remove", side_effect=OSError("stopped after intent")):
                        with self.assertRaisesRegex(OSError, "stopped after intent"):
                            _ = scratch.remove_task_copy(path, task, time.time())
                    mark = scratch.removal_mark(path)
                    fields = cast(dict[str, object], json.loads(mark.read_text()))
                    self.assertEqual(fields["removal_version"], scratch.REMOVAL_VERSION)
                    if version == "legacy":
                        for key in ("removal_version", "removal_repositories", "removal_files", "removal_intent_ns", "removal_ref_files"):
                            _ = fields.pop(key)
                        _ = mark.write_text(json.dumps(fields))
                    work = path / "new-work.txt"
                    if state == "new-work":
                        _ = work.write_text("keep new work after deletion intent")
                    elif state in ("partial-git", "partial-new-work"):
                        shutil.rmtree(path / ".git/objects")
                        if state == "partial-new-work":
                            _ = work.write_text("keep new work after partial deletion")
                    result = self.removal_attempt(path, task)
                    if state in ("unchanged", "partial-git"):
                        self.assertIsInstance(result, scratch.RemovedCopy)
                        self.assertFalse(path.exists())
                        self.assertFalse(mark.exists())
                    else:
                        self.assertIsInstance(result, scratch.KeptFolder)
                        assert isinstance(result, scratch.KeptFolder)
                        self.assertTrue(path.is_dir())
                        self.assertTrue(mark.is_file())
                        self.assertEqual(result.path, path)
                        if state == "new-work":
                            self.assertEqual(work.read_text(), "keep new work after deletion intent")
                        else:
                            self.assertIn("ambiguous source", result.reason)
                            self.assertIn(str(work), result.reason)

    def copy_with_published_head(self, name: str, task: str = "gate:copy") -> tuple[Path, Path]:
        path = self.copy(name=name, task=task)
        remote = self.base / f"{name}.git"
        self.git(self.base, "init", "--bare", "-q", str(remote))
        self.git(path, "commit", "-qm", "Published", "--allow-empty")
        self.git(path, "remote", "add", "origin", str(remote))
        self.git(path, "push", "-q", "origin", "HEAD:refs/heads/main")
        return path, remote

    def publish_next_commit(self, origin: Path) -> Path:
        publisher = self.base / (origin.stem + "-publisher")
        self.git(self.base, "clone", "-q", "-b", "main", str(origin), str(publisher))
        self.git(publisher, "commit", "-qm", "New remote tip", "--allow-empty")
        self.git(publisher, "push", "-q", "origin", "main")
        return publisher

    def git_object_files(self, repo: Path) -> dict[Path, bytes]:
        objects = repo / ".git/objects"
        return {file.relative_to(objects): file.read_bytes() for file in objects.rglob("*") if file.is_file()}

    def stall_remote_advertisement(self, clone: Path, address: Path) -> None:
        """Start a transport child that holds its socket until group cleanup."""
        transport = self.base / "upload-pack"
        _ = transport.write_text(
            f"#!{sys.executable}\n"
            + "import os, signal, socket\n"
            + "if os.fork() == 0:\n"
            + "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            + "    signal.alarm(15)\n"
            + "    with socket.socket(socket.AF_UNIX) as connection:\n"
            + f"        connection.connect({str(address)!r})\n"
            + "        assert os.environ['GIT_TERMINAL_PROMPT'] == '0'\n"
            + "        assert os.environ['GIT_ASKPASS'] == os.environ['SSH_ASKPASS'] == 'false'\n"
            + "        assert os.environ['SSH_ASKPASS_REQUIRE'] == 'never'\n"
            + "        assert os.environ['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'\n"
            + "        connection.sendall(b'R')\n"
            + "        connection.recv(1)\n"
            + "else:\n"
            + "    signal.alarm(15)\n"
            + "    signal.pause()\n")
        transport.chmod(0o700)
        self.git(clone, "config", "remote.origin.uploadpack", shlex.quote(str(transport)))

    def test_removal_accepts_a_detached_head_equal_to_an_advertised_tip(self) -> None:
        task = "gate:stale-remote"
        path, origin = self.copy_with_published_head("stale-remote", task)
        backup = self.base / "backup.git"
        self.git(self.base, "init", "--bare", "-q", str(backup))
        self.git(path, "remote", "add", "backup", os.path.relpath(backup, path))
        self.git(path, "checkout", "--detach", "-q")
        self.git(path, "commit", "-qm", "Published tag", "--allow-empty")
        self.git(path, "tag", "published")
        self.git(path, "push", "-q", str(backup), "refs/tags/published")
        self.git(backup, "tag", "-f", "-am", "Remote annotation", "published", "refs/tags/published^{commit}")
        annotation = subprocess.check_output(
            ["git", "-C", str(backup), "rev-parse", "refs/tags/published"], text=True).strip()
        self.assertNotEqual(subprocess.run(["git", "-C", str(path), "cat-file", "-e", annotation],
                                           check=False, capture_output=True).returncode, 0)
        self.git(path, "checkout", "--detach", "-q", "refs/remotes/origin/main")
        self.git(path, "commit", "-qm", "Published detached HEAD", "--allow-empty")
        self.git(path, "push", "-q", str(origin), "HEAD:refs/heads/main")
        self.assertEqual(subprocess.check_output(["git", "-C", str(path), "for-each-ref", "--contains", "HEAD"]), b"")
        refs = ["git", "-C", str(path), "for-each-ref"]
        previous_refs = subprocess.check_output(refs)
        previous_objects = self.git_object_files(path)
        fetch_head = path / ".git/FETCH_HEAD"
        _ = fetch_head.write_text("previous fetch\n")
        trace = path / ".git/removal.trace"
        packet_trace = path / ".git/removal-packets.trace"
        global_git = self.base / "trace.gitconfig"
        trace2 = {kind: path / ".git" / f"{kind}.trace" for kind in ("normal", "event", "perf")}
        for kind, destination in trace2.items():
            self.git(path, "config", "--file", str(global_git), f"trace2.{kind}Target", str(destination))
        traces = (trace, packet_trace, *trace2.values())
        for destination in traces:
            _ = destination.write_text("previous trace\n")
        scratch.busy_path(path).touch()
        previous_git_files = {file.relative_to(path): file.read_bytes()
                              for file in (path / ".git").rglob("*") if file.is_file()}
        remove = scratch.remove

        def check_then_remove(folder: Path) -> None:
            self.assertEqual((folder / ".git" / fetch_head.name).read_text(), "previous fetch\n")
            for destination in traces:
                self.assertEqual((folder / ".git" / destination.name).read_text(), "previous trace\n")
            self.assertNotIn("built-in: git fetch ", (folder / ".git" / trace.name).read_text())
            self.assertEqual({file.relative_to(folder): file.read_bytes()
                              for file in (folder / ".git").rglob("*") if file.is_file()}, previous_git_files)
            with mock.patch.dict(os.environ, {"GIT_TRACE": "0", "GIT_TRACE_PACKET": "0", "GIT_TRACE2": "0",
                                              "GIT_TRACE2_EVENT": "0", "GIT_TRACE2_PERF": "0"}):
                self.assertEqual(subprocess.check_output([*refs[:2], str(folder), *refs[3:]]), previous_refs)
                self.assertEqual(self.git_object_files(folder), previous_objects)
                self.git(folder, "merge-base", "--is-ancestor", "refs/remotes/origin/main", "HEAD")
            remove(folder)

        with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_GLOBAL": str(global_git),
                                           "GIT_TRACE": str(trace), "GIT_TRACE_PACKET": str(packet_trace)}),
              mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query,
              mock.patch.object(scratch, "remove", side_effect=check_then_remove)):
            self.assertIsInstance(self.removal_attempt(path, task), scratch.RemovedCopy)
        self.assertFalse(path.exists())
        self.assertEqual(query.call_count, 2)
        for remote in ("backup", "origin"):
            query.assert_any_call(["git", "-C", str(path), "ls-remote", "--", remote], deadline=mock.ANY, env=mock.ANY)
        self.assertIn("refs/tags/published^{}", subprocess.check_output(
            ["git", "ls-remote", str(backup)], text=True))

    def test_removal_keeps_a_head_below_a_remote_tip_that_is_absent_locally(self) -> None:
        task = "gate:missing-remote-tip"
        path, origin = self.copy_with_published_head("missing-remote-tip", task)
        self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
        publisher = self.publish_next_commit(origin)
        tip = subprocess.check_output(["git", "-C", str(publisher), "rev-parse", "HEAD"], text=True).strip()
        self.assertNotEqual(subprocess.run(["git", "-C", str(path), "cat-file", "-e", tip],
                                           check=False, capture_output=True).returncode, 0)
        previous_objects = self.git_object_files(path)
        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            result = self.removal_attempt(path, task)
        assert isinstance(result, scratch.KeptFolder)
        self.assertIn("advertised tips are missing locally", result.reason)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.git_object_files(path), previous_objects)

    def test_removal_keeps_an_unpushed_clone_after_a_remote_check(self) -> None:
        for cleanup in ("task", "terminal-workspace"):
            with self.subTest(cleanup=cleanup):
                task = f"gate:unpushed-{cleanup}"
                path, remote = self.copy_with_published_head(f"unpushed-{cleanup}", task)
                self.git(path, "commit", "-qm", "Only local", "--allow-empty")
                tracking = ["git", "-C", str(path), "rev-parse", "--verify", "refs/remotes/origin/main"]
                previous_tip = subprocess.check_output(tracking)

                if cleanup == "task":
                    with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
                        result = self.removal_attempt(path, task)
                    self.assertEqual(result, scratch.RemoteKeptFolder(path, f"{path}: it has commits that are on no remote"))
                    self.assertTrue(path.is_dir())
                    self.git(path, "rev-parse", "--verify", "refs/remotes/origin/main")
                    self.assertEqual(subprocess.check_output(tracking), previous_tip)
                    continue

                source = self.base / "workspace-source"
                self.git(self.base, "clone", "-qb", "main", str(remote), str(source))
                tip = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
                manifest = self.base / "crate-run.json"
                base = previous_tip.decode().strip()
                _ = manifest.write_text(json.dumps({
                    "version": 2, "canonical": str(source), "base": base,
                    "crates": {"saved": {"checkout": str(path), "stage": "abandoned"}},
                }))
                scratch.register_copy(path, task=task, owner_unit="maker", source_path=str(source),
                                      source_commit=base, copy_commit=tip, target_mode="none",
                                      seed_source="", borrows_from="", crate_run=str(manifest))
                with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}),
                      mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query,
                      mock.patch.object(copies, "rescue_workspace", wraps=copies.rescue_workspace) as rescue):
                    self.assertIsInstance(self.removal_attempt(path, task), scratch.RemovedCopy)
                query.assert_called_once()
                rescue.assert_called_once()
                self.assertFalse(path.exists())
                saved = subprocess.check_output(
                    ["git", "-C", str(source), "for-each-ref", "--contains", tip, "--format=%(refname)", "refs/rescue/"],
                    text=True)
                self.assertTrue(saved.strip())

    def test_removal_keeps_a_clone_when_the_remote_check_fails(self) -> None:
        for destination in ("missing", "self", "nested", "file", "symlink", "suffix"):
            with self.subTest(destination=destination):
                task = f"gate:failed-remote-{destination}"
                name = f"failed-remote-{destination}" + (".git" if destination == "suffix" else "")
                path, remote = self.copy_with_published_head(name, task)
                self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
                root = target = path
                if destination == "missing":
                    _ = remote.rename(self.base / "moved-origin.git")
                else:
                    url = "."
                    if destination == "nested":
                        root = scratch.new(task, "remote-parent")
                        path = root / "checkout"
                        self.git(root, "clone", "-qb", "main", str(remote), str(path))
                        self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
                        target = root / "remote.git"
                        _ = remote.rename(target)
                        url = "../remote.git"
                    elif destination == "file":
                        url = path.as_uri()
                    elif destination == "symlink":
                        alias = self.base / "remote-alias"
                        alias.symlink_to(path, target_is_directory=True)
                        url = str(alias)
                    elif destination == "suffix":
                        url = os.path.relpath(path.with_suffix(""), path)
                    self.git(path, "remote", "set-url", "origin", url)

                with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}),
                      mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query):
                    result = self.removal_attempt(root, task)
                self.assertIsInstance(result, scratch.KeptFolder)
                assert isinstance(result, scratch.KeptFolder)
                self.assertEqual(result.path, root)
                if destination == "missing":
                    self.assertIn("git cannot read remote 'origin': fatal:", result.reason)
                    self.assertIn(str(remote), result.reason)
                    query.assert_called_once()
                else:
                    self.assertEqual(result, scratch.RemoteInsideRemovalTree(
                        root, f"{path}: local remote 'origin' points inside the removal tree: {target}"))
                    query.assert_not_called()
                self.assertEqual(len(result.reason.splitlines()), 1)
                self.assertTrue(path.is_dir())
                self.assertFalse(scratch.removal_mark(root).exists())

    def test_local_remote_storage_must_survive_removal(self) -> None:
        for dependency in ("git directory", "common directory", "alternate", "nested alternate", "unreadable alternate",
                           "independent alternate"):
            with self.subTest(dependency=dependency):
                name = dependency.replace(" ", "-")
                path, remote = self.copy_with_published_head(name)
                self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
                git_dir = path / ".git"
                alternate = remote / "objects/info/alternates"
                if dependency == "git directory":
                    location = self.base / "external-checkout"
                    location.mkdir()
                    _ = (location / ".git").write_text(f"gitdir: {git_dir}\n")
                    update = f"private-rewrite:{name}"
                    self.git(path, "config", f"url.{location}.insteadOf", update)
                    self.git(path, "remote", "set-url", "origin", update)
                    target = git_dir
                elif dependency == "common directory":
                    _ = (remote / "commondir").write_text(f"{git_dir}\n")
                    target = git_dir
                else:
                    target = git_dir / "objects"
                    if dependency in ("nested alternate", "independent alternate"):
                        other = self.base / f"{name}-objects.git"
                        self.git(self.base, "init", "--bare", "-q", str(other))
                        _ = alternate.write_text(os.path.relpath(other / "objects", remote / "objects") + "\n")
                        alternate = other / "objects/info/alternates"
                    if dependency == "unreadable alternate":
                        os.mkfifo(alternate)
                    elif dependency != "independent alternate":
                        _ = alternate.write_text(f"{target}\n")
                previous = self.git_object_files(path)
                with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}),
                      mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query):
                    outcome = scratch.remove_folder(path)
                if dependency == "independent alternate":
                    self.assertEqual(outcome, scratch.RemovedCopy())
                    self.assertFalse(path.exists())
                    query.assert_called_once()
                else:
                    if dependency == "unreadable alternate":
                        self.assertEqual(outcome, scratch.RemoteStorageUnproven(
                            path, f"{path}: cannot verify local remote 'origin' storage: {alternate} is not a regular file"))
                    else:
                        self.assertEqual(outcome, scratch.RemoteInsideRemovalTree(
                            path, f"{path}: local remote 'origin' points inside the removal tree: {target}"))
                    query.assert_not_called()
                    self.assertEqual(self.git_object_files(path), previous)
                    self.assertFalse(scratch.removal_mark(path).exists())

    def test_removal_keeps_a_clone_when_its_named_remote_times_out(self) -> None:
        task = "gate:remote-timeout"
        path, _ = self.copy_with_published_head("remote-timeout", task)
        self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
        address = self.base / "remote.sock"
        self.stall_remote_advertisement(path, address)
        previous_objects = self.git_object_files(path)
        with (socket.socket(socket.AF_UNIX) as server,
              mock.patch.dict(os.environ, {**config_environment(self.base / "config"), "GIT_ALLOW_PROTOCOL": "file"})):
            for key in ("GIT_SSH_COMMAND", "GIT_SSH"):
                _ = os.environ.pop(key, None)
            _ = write_config_fixture(conf.path("scratch"), "removal_remote_timeout_seconds=2\n")
            server.bind(str(address))
            server.listen()
            server.settimeout(1)
            started = time.monotonic()
            result = self.removal_attempt(path, task)
            with server.accept()[0] as connection:
                connection.settimeout(1)
                self.assertEqual(connection.recv(1), b"R")
                self.assertEqual(connection.recv(1), b"")
        self.assertLess(time.monotonic() - started, 10)
        assert isinstance(result, scratch.KeptFolder)
        self.assertEqual(result.path, path)
        self.assertIn("git cannot read remote 'origin': ls-remote timed out (limit 2 s)", result.reason)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.git_object_files(path), previous_objects)

    def test_a_deleted_remote_branch_does_not_cover_commits_after_a_remote_check(self) -> None:
        task = "gate:deleted-remote-branch"
        path, remote = self.copy_with_published_head("deleted-remote-branch", task)
        self.git(path, "checkout", "-qb", "deleted")
        self.git(path, "commit", "-qm", "Only on the deleted branch", "--allow-empty")
        self.git(path, "push", "-q", "origin", "HEAD:refs/heads/deleted")
        self.git(remote, "update-ref", "-d", "refs/heads/deleted")
        self.git(path, "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/deleted")
        self.git(path, "checkout", "-q", "-")
        self.git(path, "commit", "-qm", "Published after the last fetch", "--allow-empty")
        self.git(path, "push", "-q", str(remote), "HEAD:refs/heads/main")
        trace = self.base / "git.trace"
        trace.touch()

        with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file", "GIT_TRACE": str(trace)}),
              mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query):
            result = self.removal_attempt(path, task)
        self.assertEqual(result, scratch.RemoteKeptFolder(path, f"{path}: it has commits that are on no remote"))
        query.assert_called_once()
        self.assertNotIn("built-in: git fetch ", trace.read_text())
        self.assertTrue(path.is_dir())
        self.git(path, "merge-base", "--is-ancestor", "refs/heads/deleted", "refs/remotes/origin/deleted")

    def test_a_remote_check_keeps_a_private_branch_and_does_not_apply_fetch_refspecs(self) -> None:
        task = "gate:force-refspec"
        path, _ = self.copy_with_published_head("force-refspec", task)
        self.git(path, "checkout", "-qb", "private")
        self.git(path, "commit", "-qm", "Only on the private branch", "--allow-empty")
        self.git(path, "checkout", "-q", "-")
        self.git(path, "config", "remote.origin.fetch", "+refs/heads/main:refs/heads/private")
        self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
        refs = ["git", "-C", str(path), "for-each-ref"]
        previous_refs = subprocess.check_output(refs)

        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            result = self.removal_attempt(path, task)
        self.assertEqual(result, scratch.RemoteKeptFolder(path, f"{path}: it has commits that are on no remote"))
        self.assertEqual(subprocess.check_output(refs), previous_refs)
        self.assertTrue(path.is_dir())

    def test_a_remote_check_uses_the_clones_ssh_command(self) -> None:
        task = "gate:configured-ssh"
        path, remote = self.copy_with_published_head("configured-ssh", task)
        self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
        ssh = self.base / "configured-ssh"
        _ = ssh.write_text(
            f"#!{sys.executable}\n"
            + "import os, sys\n"
            + "assert sys.argv[1] == 'example.invalid'\n"
            + "assert os.environ['SSH_ASKPASS_REQUIRE'] == 'never'\n"
            + f"os.execlp('git', 'git', 'upload-pack', {str(remote)!r})\n")
        ssh.chmod(0o700)
        self.git(path, "remote", "set-url", "origin", "ssh://example.invalid/repository")
        self.git(path, "config", "core.sshCommand", shlex.quote(str(ssh)))
        self.git(path, "config", "ssh.variant", "simple")
        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "ssh"}):
            for key in ("GIT_SSH_COMMAND", "GIT_SSH", "GIT_SSH_VARIANT"):
                _ = os.environ.pop(key, None)
            self.assertIsInstance(self.removal_attempt(path, task), scratch.RemovedCopy)
        self.assertFalse(path.exists())

    def test_a_remote_check_keeps_a_tag_that_appears_during_the_query(self) -> None:
        task = "gate:tag-during-query"
        path, _ = self.copy_with_published_head("tag-during-query", task)
        self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
        transport = self.base / "upload-pack-with-tag"
        _ = transport.write_text(
            f"#!{sys.executable}\n"
            + "import os, subprocess, sys\n"
            + f"repo = {str(path)!r}\n"
            + "git = ['git', '-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@t']\n"
            + "subprocess.run(git + ['checkout', '--detach', '-q'], check=True)\n"
            + "subprocess.run(git + ['commit', '-qm', 'Only on a new tag', '--allow-empty'], check=True)\n"
            + "subprocess.run(git + ['tag', 'private'], check=True)\n"
            + "subprocess.run(git + ['checkout', '-q', '-'], check=True)\n"
            + "os.execvp('git', ['git', 'upload-pack', *sys.argv[1:]])\n")
        transport.chmod(0o700)
        self.git(path, "config", "remote.origin.uploadpack", shlex.quote(str(transport)))

        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            result = self.removal_attempt(path, task)
        self.assertEqual(result, scratch.RemoteKeptFolder(path, f"{path}: it has commits that are on no remote"))
        self.git(path, "rev-parse", "--verify", "refs/tags/private")

    def test_cargo_recovery_rechecks_local_work_with_the_same_advertised_ids(self) -> None:
        for change in ("published", "private"):
            with self.subTest(change=change):
                path, _ = self.copy_with_published_head(change)
                self.git(path, "branch", "new-published-ref")
                self.stop_removal_after_intent(path, "current")
                self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
                self.git(path, "config", "test.retry", "true")
                trace = self.base / f"{change}.trace"
                trace.touch()
                roots = cargo_output.CargoOutputRoots(path, path / "target", self.base / "outside-output")
                saved = scratch.SavedCargoOutput(roots, (), (), time.time())

                def change_refs(repo: Path, _saved: tuple[scratch.SavedCargoOutput, ...]) -> None:
                    query.assert_called_once()
                    if change == "private":
                        self.git(repo, "checkout", "--detach", "-q")
                        self.git(repo, "commit", "-qm", "Only on a private branch", "--allow-empty")
                        self.git(repo, "branch", "private")
                        self.git(repo, "checkout", "-q", "-")

                with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file", "GIT_TRACE": str(trace)}),
                      mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query,
                      mock.patch.object(scratch, "save_cargo_outputs", return_value=(saved,)),
                      mock.patch.object(scratch, "clean_cargo_outputs", side_effect=change_refs)):
                    result = scratch.remove_folder(path)
                query.assert_called_once()
                self.assertNotIn("built-in: git fetch ", trace.read_text())
                if change == "private":
                    self.assertEqual(result, scratch.RemoteKeptFolder(path, f"{path}: it has commits that are on no remote"))
                    self.git(path, "rev-parse", "--verify", "refs/heads/private")
                    self.assertTrue(scratch.removal_mark(path).exists())
                else:
                    self.assertEqual(result, scratch.RemovedCopy())
                    self.assertFalse(path.exists())

    def test_task_end_reports_a_folder_once_when_its_hold_names_the_same_copy(self) -> None:
        task = "gate:held-copy"
        path = self.copy(task=task)
        _ = scratch.hold(path, task, "The folder and the hold name the same copy")
        with redirect_stderr(io.StringIO()) as report:
            result = scratch.end(task)
        self.assertEqual(result, [scratch.KeptFolder(path, f"task {task} ended")])
        self.assertEqual(report.getvalue().splitlines(),
                         [f"kept for a manual removal list: {path} (scratch, task {task} ended)"])
        self.assertTrue(path.is_dir())
        self.assertGreater(scratch.holds()[str(path)]["ended"], 0)

    def test_the_portable_runner_stops_the_remote_group_after_removal_is_interrupted(self) -> None:
        task = "gate:stopped-remote"
        path, _ = self.copy_with_published_head("stopped-remote", task)
        self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
        address = self.base / "stopped.sock"
        self.stall_remote_advertisement(path, address)
        scratch.busy_path(path).touch()
        previous_files = set(path.rglob("*"))
        previous_objects = self.git_object_files(path)
        env = {**os.environ, **config_environment(self.base / "config"), "GIT_ALLOW_PROTOCOL": "file",
               "GIT_ASKPASS": "unused", "SSH_ASKPASS": "unused", "SSH_ASKPASS_REQUIRE": "force"}
        with mock.patch.dict(os.environ, env):
            _ = write_config_fixture(conf.path("scratch"), "removal_remote_timeout_seconds=3\n")
        for key in ("GIT_SSH_COMMAND", "GIT_SSH"):
            _ = env.pop(key, None)
        script = """import sys
import time
from pathlib import Path
from scripts.lib import scratch

scratch.remove_task_copy(Path(sys.argv[1]), sys.argv[2], time.time())
"""
        command = [sys.executable, "-c", script, str(path), task]

        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(address))
            server.listen()
            server.settimeout(5)
            with subprocess.Popen(command, env=env, start_new_session=True, stderr=subprocess.PIPE) as removal:
                try:
                    with server.accept()[0] as conn:
                        conn.settimeout(7)
                        self.assertEqual(conn.recv(1), b"R")
                        removal.send_signal(signal.SIGINT)
                        self.assertEqual(removal.wait(timeout=5), -signal.SIGINT)
                        self.assertEqual(conn.recv(1), b"")
                finally:
                    if removal.returncode is None:
                        process_group.send_process_group(removal.pid, signal.SIGKILL)
        self.assertTrue(path.is_dir())
        self.assertFalse(scratch.removal_mark(path).exists())
        self.assertEqual(set(path.rglob("*")), previous_files)
        self.assertEqual(self.git_object_files(path), previous_objects)

    def test_removal_dry_run_keeps_a_copy_that_needs_a_remote_check(self) -> None:
        path, _ = self.copy_with_published_head("dry-remote")
        self.git(path, "update-ref", "-d", "refs/remotes/origin/main")
        trace = self.base / "git.trace"
        trace.touch()
        with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file", "GIT_TRACE": str(trace)}),
              mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query):
            outcome = scratch.remove_folder(path, dry_run=True)
        self.assertIsInstance(outcome, scratch.KeptFolder)
        assert isinstance(outcome, scratch.KeptFolder)
        self.assertIn("a remote check is needed", outcome.reason)
        self.assertNotIn("built-in: git fetch ", trace.read_text())
        self.assertNotIn("built-in: git ls-remote ", trace.read_text())
        query.assert_not_called()
        self.assertTrue(path.is_dir())
        self.assertFalse(scratch.removal_mark(path).exists())

    def test_removal_skips_the_remote_check_when_tracking_refs_cover_the_clone(self) -> None:
        task = "gate:current-remote"
        path, remote = self.copy_with_published_head("current-remote", task)
        _ = remote.rename(self.base / "unavailable-origin.git")
        trace = self.base / "git.trace"
        trace.touch()
        self.git(path, "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main")
        with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file", "GIT_TRACE": str(trace)}),
              mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query):
            self.assertIsInstance(self.removal_attempt(path, task), scratch.RemovedCopy)
        self.assertNotIn("built-in: git fetch ", trace.read_text())
        self.assertNotIn("built-in: git ls-remote ", trace.read_text())
        query.assert_not_called()
        self.assertFalse(path.exists())

    def test_removal_skips_the_remote_check_for_a_linked_worktree_with_an_unpushed_head(self) -> None:
        source, _ = self.copy_with_published_head("linked-source")
        path = self.base / "linked"
        self.git(source, "worktree", "add", "--detach", "-q", str(path))
        self.git(path, "commit", "-qm", "Only in the worktree", "--allow-empty")
        trace = self.base / "git.trace"
        trace.touch()
        with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file", "GIT_TRACE": str(trace)}),
              mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query):
            result = scratch.remove_folder(path)
        self.assertEqual(result, scratch.KeptFolder(path, f"{path}: it has commits that are on no remote"))
        self.assertNotIn("built-in: git fetch ", trace.read_text())
        self.assertNotIn("built-in: git ls-remote ", trace.read_text())
        query.assert_not_called()
        self.assertTrue(path.is_dir())

    def test_worktree_registration_cleanup_holds_the_transient_lease(self) -> None:
        source, _ = self.copy_with_published_head("linked-source")
        path = self.base / "linked"
        self.git(source, "worktree", "add", "--detach", "-q", str(path))
        lock = path.with_suffix(".lock")
        checks: list[Path] = []

        def check_registration_removal(*args: object, **kwargs: object) -> object:
            if (args and args[0] == ["git", "--git-dir", str(source / ".git"), "worktree", "remove",
                                    "--force", "--force", str(path)] and kwargs.get("check") is False):
                with build_folders.folder_removal_lease(path, wait=False) as lease:
                    self.assertIsInstance(lease, build_folders.RemovalLockHeld)
                checks.append(path)
            return cast(object, mock.DEFAULT)

        self.assertFalse(lock.exists())
        with mock.patch.object(subprocess, "run", wraps=subprocess.run, side_effect=check_registration_removal):
            self.assertEqual(scratch.remove_folder(path), scratch.RemovedCopy())

        self.assertEqual(checks, [path])
        self.assertFalse(path.exists())
        self.assertFalse(lock.exists())
        self.assertFalse((source / ".git/worktrees" / path.name).exists())

    def test_retry_keeps_a_new_commit_held_only_by_a_new_tag(self) -> None:
        for reference in ("new-work", "HEAD"):
            with self.subTest(reference=reference):
                task = f"gate:commit-after-intent-{reference}"
                path, _ = self.copy_with_published_head(reference, task)
                _ = (path / "tracked").write_text("original")
                self.git(path, "add", "tracked")
                self.git(path, "commit", "-qm", "Create the fixture")
                self.git(path, "push", "-q", "origin", "HEAD:refs/heads/main")
                with mock.patch.object(scratch, "remove", side_effect=OSError("stop after intent")):
                    self.assertIsInstance(self.removal_attempt(path, task), scratch.KeptFolder)
                self.git(path, "checkout", "--detach", "-q")
                _ = (path / "tracked").write_text("new tagged work")
                self.git(path, "commit", "-qam", "Keep the new work")
                if reference == "new-work":
                    self.git(path, "tag", "new-work")
                    self.git(path, "checkout", "-q", "-")
                else:
                    self.assertEqual(subprocess.run(
                        ["git", "-C", str(path), "for-each-ref", "--contains", "HEAD"],
                        capture_output=True, text=True, check=True).stdout, "")
                result = self.removal_attempt(path, task)
                self.assertIsInstance(result, scratch.KeptFolder)
                assert isinstance(result, scratch.KeptFolder)
                self.assertIn("commits that are on no remote", result.reason)
                self.assertTrue(path.is_dir())
                self.assertTrue(scratch.removal_mark(path).is_file())
                self.assertEqual(subprocess.run(["git", "-C", str(path), "show", f"{reference}:tracked"],
                                 capture_output=True, text=True, check=True).stdout, "new tagged work")

    def test_legacy_retry_keeps_new_work_beside_a_repository_with_deleted_git(self) -> None:
        task = "gate:legacy-two-repositories"
        path = scratch.new(task, "two-repositories")
        for name in ("first", "second"):
            repo = path / name
            repo.mkdir()
            self.git(repo, "init", "-q")
            _ = (repo / "source").write_text("source")
            self.git(repo, "add", "source")
            self.git(repo, "commit", "-qm", "Create the fixture")
            self.git(repo, "remote", "add", "origin", str(self.base / "origin"))
            self.git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        scratch.atomic_record(scratch.removal_mark(path), {"task": task})
        shutil.rmtree(path / "first/.git")
        work = path / "first/new-work"
        _ = work.write_text("keep this work")
        result = self.removal_attempt(path, task)
        self.assertIsInstance(result, scratch.KeptFolder)
        assert isinstance(result, scratch.KeptFolder)
        self.assertIn(str(work), result.reason)
        self.assertEqual(work.read_text(), "keep this work")
        self.assertTrue((path / "second/.git").is_dir())
        self.assertTrue(scratch.removal_mark(path).is_file())

    def test_absent_source_recovery_keeps_a_busy_source_and_finishes_the_next_source(self) -> None:
        first = self.copy(name="a-busy")
        second = self.copy(name="b-ready")
        for path in (first, second):
            scratch.atomic_record(scratch.cargo_removal_mark(path), [])
            with mock.patch.object(scratch, "finish_removal", side_effect=OSError("stop before completion")):
                with self.assertRaisesRegex(OSError, "stop before completion"):
                    scratch.remove_marked(path)
        lock = first.with_name(first.name + ".lock")
        with lock.open("a") as handle, redirect_stderr(io.StringIO()) as errors:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            kept = scratch.clear_finished_copy_removals(first.parent.parent)
        self.assertEqual([item.path for item in kept], [first])
        self.assertIn(f"kept {first}", errors.getvalue())
        self.assertTrue(scratch.removal_mark(first).is_file())
        self.assertTrue(scratch.cargo_removal_mark(first).is_file())
        self.assertFalse(scratch.removal_mark(second).exists())
        self.assertFalse(scratch.cargo_removal_mark(second).exists())

    def test_absent_sources_finish_cargo_recovery_before_task_evidence(self) -> None:
        for entry in ("task", "selected", "scan"):
            with self.subTest(entry=entry):
                task = f"gate:finish-{entry}"
                path = self.copy(name=entry, task=task)
                cargo_mark = scratch.cargo_removal_mark(path)
                task_mark = scratch.removal_mark(path)
                saved_fields: dict[str, object] = {"workspace_root": str(path),
                    "target_directory": str(path / "target"), "build_directory": str(self.base / f"cache-{entry}"),
                    "other_checkouts": [], "shared_output_roots": [], "saved_at": 1.0}
                scratch.atomic_record(cargo_mark, [saved_fields])
                with (mock.patch.object(scratch, "clean_cargo_outputs"),
                      mock.patch.object(scratch, "finish_removal", side_effect=OSError("stopped before completion"))):
                    result = self.removal_attempt(path, task)
                self.assertIsInstance(result, scratch.KeptFolder)
                self.assertFalse(path.exists())
                self.assertTrue(cargo_mark.is_file())
                self.assertTrue(task_mark.is_file())
                with mock.patch.object(scratch, "clean_cargo_outputs", side_effect=OSError("Cargo recovery refused")):
                    result = self.removal_attempt(path, task)
                self.assertIsInstance(result, scratch.KeptFolder)
                self.assertTrue(cargo_mark.is_file())
                self.assertTrue(task_mark.is_file())
                events: list[str] = []
                unlink = Path.unlink

                def observe_unlink(mark: Path, *, missing_ok: bool = False) -> None:
                    if mark in (cargo_mark, task_mark):
                        events.append("cargo" if mark == cargo_mark else "task")
                        if mark == cargo_mark:
                            self.assertTrue(task_mark.is_file())
                    unlink(mark, missing_ok=missing_ok)

                pending = scratch.pending_removal(path)
                assert isinstance(pending, copy_record.Registered)
                with (mock.patch.object(scratch, "clean_cargo_outputs"),
                      mock.patch.object(Path, "unlink", observe_unlink)):
                    if entry == "task":
                        self.assertIsInstance(self.removal_attempt(path, task), scratch.RemovedCopy)
                    elif entry == "selected":
                        self.assertTrue(scratch.end_selected_copy(path, pending, time.time(), validate=lambda: None))
                    else:
                        _ = scratch.clear_finished_copy_removals(path.parent.parent)
                self.assertEqual(events, ["cargo", "task"])
                self.assertFalse(cargo_mark.exists())
                self.assertFalse(task_mark.exists())

    def test_refusals_before_source_deletion_leave_no_removal_mark(self) -> None:
        for stage in ("trust", "cargo-save", "cargo-clean"):
            with self.subTest(stage=stage):
                path = self.copy(name=stage, task="gate:pre-deletion-refusal")
                def trust_field(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
                    assert isinstance(record, copy_record.Registered)
                    return {**record.fields, "claude_trust_folder": str(path)}

                _ = scratch.update_record(path, trust_field)
                delete = mock.Mock()
                trust = (accounts.FolderTrustFailed(("trust refused",)) if stage == "trust"
                         else accounts.FolderTrustComplete())
                with (mock.patch.object(accounts, "set_claude_folder_trust", return_value=trust),
                      mock.patch.object(scratch, "save_cargo_outputs", return_value=(),
                                        side_effect=OSError("cargo-save refused") if stage == "cargo-save" else None),
                      mock.patch.object(scratch, "clean_cargo_outputs",
                                        side_effect=scratch.CargoOutputKept("cargo-clean refused") if stage == "cargo-clean" else None),
                      mock.patch.object(scratch, "remove", delete)):
                    with self.assertRaisesRegex((scratch.ScratchError, OSError), f"{stage} refused"):
                        scratch.remove_marked(path)
                delete.assert_not_called()
                self.assertTrue(path.is_dir())
                self.assertFalse(scratch.removal_mark(path).exists())
                self.assertIsInstance(scratch.task_of(path), copy_record.Registered)

    def test_invalid_cargo_removal_marks_report_an_oserror_and_keep_the_source(self) -> None:
        checkout = self.base / "invalid-mark-checkout"
        checkout.mkdir()
        mark = scratch.cargo_removal_mark(checkout)
        for contents in (b"{broken", b"\xff"):
            with self.subTest(contents=contents):
                _ = mark.write_bytes(contents)
                with mock.patch.object(scratch, "remove", side_effect=AssertionError("source must remain")):
                    with self.assertRaisesRegex(OSError, "invalid Cargo removal record"):
                        scratch.remove_with_cargo_output(checkout)
                self.assertTrue(checkout.exists())
                self.assertEqual(mark.read_bytes(), contents)

    def test_contained_cargo_output_needs_no_external_cleanup(self) -> None:
        checkout = self.base / "cargo-checkout"
        (checkout / "target" / "debug").mkdir(parents=True)
        _ = (checkout / "Cargo.toml").write_text("private manifest")
        roots = cargo_output.CargoOutputRoots(checkout, checkout / "target", checkout / "target")
        with (mock.patch.object(cargo_output, "read_cargo_output_roots", return_value=roots),
              mock.patch.object(scratch, "cargo_checkout_peers", return_value=()),
              mock.patch.object(subprocess, "run", side_effect=AssertionError("external guard was unnecessary"))):
            scratch.remove_with_cargo_output(checkout)
        self.assertFalse(checkout.exists())

    def test_a_test_clone_and_its_build_output_end_with_the_task(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        _ = (source / "asset").write_text("saved source")
        self.git(source, "add", "asset")
        self.git(source, "commit", "-qm", "source")

        with mock.patch.object(copies, "initialize_git", wraps=copies.initialize_git) as initialize:
            checkout = copies.clone(source, "gate:hana-view", "camera")
        initialize.assert_called_once_with(checkout, source.resolve())

        self.assertTrue(checkout.is_relative_to(self.base / "scratch"))
        self.assertEqual((checkout / "asset").read_text(), "saved source")
        self.assertTrue((checkout / ".git").is_dir())
        self.assertFalse((checkout / scratch.BUSY).exists())
        record = scratch.task_of(checkout)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:hana-view")
        self.assertEqual(record.fields["source_path"], str(source.resolve()))
        self.assertIs(record.fields["test_hana_clone"], True)
        target = checkout / "target"
        target.mkdir()
        _ = (target / "build").write_text("disposable build output")
        self.assertEqual([entry.path for entry in scratch.end("gate:hana-view")], [checkout])
        self.assertTrue(checkout.is_dir())
        self.assertEqual((source / "asset").read_text(), "saved source")
        self.assertEqual([entry.path for entry in scratch.end("gate:hana-view")], [checkout])

    def test_a_failed_clone_keeps_its_partial_files_after_task_end(self) -> None:
        def interrupted_clone(checkout: Path, source: Path) -> None:
            self.assertEqual(source, self.base / "source")
            with scratch.judgment_lock(checkout) as available:
                self.assertFalse(available)
            _ = (checkout / "partial").write_text("partial clone")
            record = scratch.task_of(checkout)
            assert isinstance(record, copy_record.Registered)
            self.assertEqual(record.task, "gate:clone-failure")
            raise copies.CopyError("interrupted clone")

        with mock.patch.object(copies, "initialize_git", side_effect=interrupted_clone):
            with self.assertRaisesRegex(copies.CopyError, "clone failed"):
                _ = copies.clone(self.base / "source", "gate:clone-failure")
        paths = [item.path for item in scratch.folders() if item.task == "gate:clone-failure"]
        self.assertEqual(len(paths), 1)
        self.assertTrue((paths[0] / "partial").exists())
        self.assertEqual([entry.path for entry in scratch.end("gate:clone-failure")], paths)
        self.assertTrue(paths[0].is_dir())

    def test_main_ends_a_session_clone_and_reports_each_retained_clone(self) -> None:
        from scripts.accounts import accounts

        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        session = self.base / "session" / "maker"
        (session / session_folder.RUN).mkdir(parents=True)
        for state in ("clean", "unsaved", "busy", "until"):
            with self.subTest(state=state):
                task = "until:2000-01-01T0000Z" if state == "until" else f"gate:session-{state}"
                with (mock.patch.object(accounts, "source_has_claude_trust", return_value=True),
                      mock.patch.object(accounts, "set_claude_folder_trust") as trust):
                    checkout = copies.clone(source, task, state)
                    self.assertEqual(checkout.parent, session)
                    sidecars = [checkout.with_name(checkout.name + suffix) for suffix in (".lease.json", ".lock")]
                    self.assertTrue(all(path.is_file() for path in sidecars))
                    other = checkout.with_name(checkout.name + ".lease.json.0123456789abcdef.tmp")
                    _ = other.write_text("keep this file")
                    if state == "unsaved":
                        _ = (checkout / "work").write_text("keep this work")
                    output, error = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(error):
                        if state == "busy":
                            with copies.claim_tree(checkout, create=False) as claim:
                                self.assertIs(claim, copies.ClaimState.ACQUIRED)
                                code = scratch.main(["end", "--task", task])
                        else:
                            code = scratch.main(["end", "--task", task])
                    self.assertEqual(code, 0)
                    self.assertEqual(output.getvalue(), "")
                    self.assertTrue(checkout.is_dir())
                    self.assertTrue(all(path.is_file() for path in sidecars))
                    if state != "until":
                        self.assertIn(f"kept for a manual removal list: {checkout} (scratch, task {task} ended)", error.getvalue())
                        self.assertGreater(scratch.holds()[str(checkout)]["ended"], 0)
                    self.assertEqual(trust.call_args_list, [mock.call(checkout, trusted=True)])
                    self.assertEqual(other.read_text(), "keep this file")

    def test_removal_keeps_the_sidecars_of_a_possible_borrowed_source(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        clone = copies.clone(source, "gate:upstream", "upstream")
        legacy = self.base / "outside-both-roots"
        self.git(source, "clone", "--shared", "-q", str(clone), str(legacy))
        worktree = copy_record.copies_root() / "review-pool" / "review"
        worktree.parent.mkdir(parents=True)
        self.git(legacy, "worktree", "add", "--detach", str(worktree))

        result = self.removal_attempt(clone, "gate:upstream")

        self.assertEqual(result, scratch.KeptFolder(
            clone, f"repair record {worktree / copy_record.RECORD}: cannot rule out borrowed objects"))
        self.assertTrue(clone.is_dir())
        self.assertTrue(clone.with_name(clone.name + ".lock").is_file())
        self.assertTrue(clone.with_name(clone.name + ".lease.json").is_file())
        self.git(worktree, "cat-file", "-e", "HEAD^{commit}")

    def test_removal_completes_when_a_sidecar_unlink_fails(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        unlink = os.unlink
        for suffix in (".lease.json", ".lock"):
            with self.subTest(suffix=suffix):
                task = f"gate:sidecar-{suffix[1:]}"
                clone = copies.clone(source, task)
                lease = clone.with_name(clone.name + ".lease.json")
                lock = clone.with_name(clone.name + ".lock")
                selected = clone.name + suffix
                mark = scratch.removal_mark(clone)

                def refuse(name: str | bytes | os.PathLike[str] | os.PathLike[bytes], *,
                           dir_fd: int | None = None) -> None:
                    if dir_fd is not None and name == selected:
                        self.assertFalse(clone.exists())
                        self.assertFalse(mark.exists())
                        with lock.open("rb") as contender, self.assertRaises(BlockingIOError):
                            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        raise PermissionError("sidecar unlink denied")
                    unlink(name, dir_fd=dir_fd)

                with mock.patch.object(os, "unlink", side_effect=refuse):
                    self.assertIsInstance(self.removal_attempt(clone, task), scratch.RemovedCopy)
                self.assertFalse(clone.exists())
                self.assertFalse(mark.exists())
                self.assertEqual(lease.is_file(), suffix == ".lease.json")
                self.assertEqual(lock.is_file(), suffix == ".lock")

    def test_ending_a_session_hold_keeps_a_busy_or_borrowed_descendant(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        session = self.base / "session" / "maker"
        (session / session_folder.RUN).mkdir(parents=True)
        child = copies.clone(source, "gate:descendant", "child")
        self.assertEqual(child.parent, session)
        task = "gate:container"
        scratch.write_holds({**scratch.holds(), str(session): scratch.Hold(
            task=task, reason="old session hold", maker="", made=0.0, ended=0.0)})
        sibling = session / "keep.txt"
        _ = sibling.write_text("keep")
        with scratch.busy_locks(child, create=True) as idle:
            self.assertTrue(idle)
            result = scratch.end(task)
        self.assertEqual(result, [scratch.KeptFolder(session, f"task {task} ended")])

        linked = self.base / "outside-the-session"
        self.git(child, "worktree", "add", "--detach", "-q", str(linked))
        scratch.write_record(linked, "gate:borrower", time.time())
        record = scratch.task_of(linked)
        assert isinstance(record, copy_record.Registered)
        scratch.atomic_record(linked / copy_record.RECORD, {**record.fields, "borrows_from": str(child / ".git")})
        _ = scratch.hold(linked, "gate:borrower", "Keep the borrower")
        result = scratch.end(task)
        self.assertEqual(result, [scratch.KeptFolder(session, f"task {task} ended")])
        self.git(linked, "cat-file", "-e", "HEAD^{commit}")
        self.assertEqual(sibling.read_text(), "keep")
        self.assertIn(str(session), scratch.holds())

    def test_recursive_removal_keeps_a_fifo_record_and_closes_its_descriptor(self) -> None:
        task = "gate:container"
        parent = scratch.new(task, "container")
        clone = parent / "clone"
        clone.mkdir(parents=True)
        self.git(clone, "init", "-q")
        _ = scratch.hold(parent, task, "Keep the container")
        program = """import errno, os, sys
from pathlib import Path
from unittest.mock import patch
from scripts.lib import scratch

parent, record = map(Path, sys.argv[1:3])
task = sys.argv[3]
open_path = os.open
descriptors = []

def track(path, flags, *args, **kwargs):
    if path == record:
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_NONBLOCK
    descriptor = open_path(path, flags, *args, **kwargs)
    if path == record:
        descriptors.append(descriptor)
    return descriptor

with patch.object(os, "open", track):
    result = scratch.remove_task_copy(parent, task, 1.0)
assert isinstance(result, scratch.KeptFolder), result
assert parent.is_dir()
assert str(record) in result.reason, result
assert "expected a regular file" in result.reason, result
assert descriptors
for descriptor in descriptors:
    try:
        os.fstat(descriptor)
    except OSError as error:
        assert error.errno == errno.EBADF
    else:
        raise AssertionError("The record descriptor is still open.")
"""
        for folder in (parent / "held", self.base / "other-hold"):
            with self.subTest(folder=folder):
                folder.mkdir()
                scratch.write_holds({**scratch.holds(), str(folder): scratch.Hold(
                    task="gate:borrower", reason="old hold", maker="", made=0.0, ended=0.0)})
                record = folder / copy_record.RECORD
                os.mkfifo(record)
                run = subprocess.run([sys.executable, "-c", program, str(parent), str(record), task],
                                     capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                record.unlink()
                self.assertTrue(scratch.release(folder))

    def test_retry_keeps_a_worktree_with_unreadable_external_metadata(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "-q", "--allow-empty", "-m", "Create the fixture")
        worktree = self.base / "linked"
        self.git(source, "worktree", "add", "-q", "-b", "side", str(worktree))
        with (mock.patch.object(scratch, "remove", side_effect=OSError("stop after removal intent")),
              self.assertRaisesRegex(OSError, "stop after removal intent")):
            _ = scratch.remove_folder(worktree)

        settings = conf.read("scratch")
        pending = scratch.pending_removal(worktree, scratch_settings=settings)
        self.assertIsInstance(pending, copy_record.Registered)
        assert isinstance(pending, copy_record.Registered)
        for name in ("commondir", "HEAD"):
            with self.subTest(name=name):
                metadata = source / ".git" / "worktrees" / worktree.name / name
                contents = metadata.read_bytes()
                metadata.unlink()
                metadata.mkdir()
                kept = scratch.KeptFolder(worktree, f"{metadata}: expected a regular file")
                self.assertEqual(scratch.recovery_state(worktree, pending, scratch.BusyMarkScope(),
                                                       scratch_settings=settings), kept)
                self.assertEqual(scratch.remove_folder(worktree, source_deletion=scratch.SourceDeletion.RUN), kept)
                self.assertTrue(worktree.is_dir())
                self.assertTrue(scratch.removal_mark(worktree).is_file())
                metadata.rmdir()
                _ = metadata.write_bytes(contents)

        self.assertEqual(scratch.remove_folder(worktree, source_deletion=scratch.SourceDeletion.RUN),
                         scratch.RemovedCopy())
        self.assertFalse(worktree.exists())

    def checkout_with_removal_intent(self, version: str, name: str) -> Path:
        path = self.copy(name=f"{version}-{name}")
        self.add_saved_work_and_loose_ref(path)
        self.stop_removal_after_intent(path, version)
        return path

    def add_saved_work_and_loose_ref(self, path: Path) -> None:
        _ = (path / "tracked").write_text("saved work")
        self.git(path, "add", "tracked")
        self.git(path, "commit", "-qm", "Create the fixture")
        self.git(path, "remote", "add", "origin", str(self.base / "origin"))
        self.git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git(path, "pack-refs", "--all")
        self.git(path, "update-ref", "refs/heads/loose", "HEAD")

    def stop_removal_after_intent(self, path: Path, version: str) -> None:
        with (mock.patch.object(scratch, "remove", side_effect=OSError("stop after removal intent")),
              self.assertRaisesRegex(OSError, "stop after removal intent")):
            _ = scratch.remove_folder(path)
        if version == "legacy":
            mark = scratch.removal_mark(path)
            fields = cast(dict[str, object], json.loads(mark.read_text()))
            for key in ("removal_version", "removal_repositories", "removal_files", "removal_intent_ns", "removal_ref_files"):
                _ = fields.pop(key)
            _ = mark.write_text(json.dumps(fields))

    def assert_retry_keeps_metadata(self, path: Path, metadata: Path) -> None:
        with mock.patch.object(scratch, "remove", side_effect=AssertionError("source deletion started")) as delete:
            result = scratch.remove_folder(path)
            delete.assert_not_called()
        self.assertIsInstance(result, scratch.KeptFolder)
        assert isinstance(result, scratch.KeptFolder)
        self.assertEqual(result.path, path)
        self.assertIn(str(metadata), result.reason)
        self.assertTrue(path.is_dir())
        self.assertTrue(scratch.removal_mark(path).is_file())

    def set_removal_mark_times(self, path: Path, *, intent: int, mtime: int) -> None:
        mark = scratch.removal_mark(path)
        fields = cast(dict[str, object], json.loads(mark.read_text()))
        fields["removal_intent_ns"] = intent
        _ = mark.write_text(json.dumps(fields))
        os.utime(mark, ns=(mtime, mtime))

    def test_retry_checks_internal_metadata_in_a_descendant_checkout(self) -> None:
        for version in ("current", "legacy"):
            with self.subTest(version=version):
                path = self.base / version
                child = path / "checkout"
                child.mkdir(parents=True)
                self.git(child, "init", "-q")
                _ = (child / "tracked").write_text("saved work")
                self.git(child, "add", "tracked")
                self.git(child, "commit", "-qm", "Create the fixture")
                self.git(child, "remote", "add", "origin", str(self.base / "origin"))
                self.git(child, "update-ref", "refs/remotes/origin/main", "HEAD")
                self.stop_removal_after_intent(path, version)

                index = child / ".git/index"
                index.unlink()
                index.mkdir()
                self.assert_retry_keeps_metadata(path, index)
                self.assertTrue(index.is_dir())

    def test_retry_keeps_a_ref_directory_replaced_by_a_symlink_during_scan(self) -> None:
        path = self.checkout_with_removal_intent("current", "ref-directory-swap")
        namespace = path / ".git/refs/heads"
        moved = self.base / "moved-refs"
        empty = self.base / "empty-refs"
        empty.mkdir()
        open_entry, scan = os.open, os.scandir

        def replace_namespace() -> None:
            _ = namespace.rename(moved)
            namespace.symlink_to(empty, target_is_directory=True)

        def open_during_swap(name: str | Path, flags: int, mode: int = 0o777, *,
                             dir_fd: int | None = None) -> int:
            if name == namespace.name and dir_fd is not None and flags & os.O_DIRECTORY:
                parent = namespace.parent.stat()
                descriptor = os.fstat(dir_fd)
                self.assertEqual((parent.st_dev, parent.st_ino), (descriptor.st_dev, descriptor.st_ino))
                required = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
                self.assertEqual(flags & required, required)
                replace_namespace()
            return open_entry(name, flags, mode, dir_fd=dir_fd)

        def scan_during_swap(folder: Path | str | int = "."):
            if folder == namespace:
                replace_namespace()
            return scan(folder)

        with (mock.patch.object(os, "open", side_effect=open_during_swap),
              mock.patch.object(os, "scandir", side_effect=scan_during_swap)):
            self.assert_retry_keeps_metadata(path, namespace)
        self.assertTrue(namespace.is_symlink())
        self.assertTrue((moved / "loose").is_file())

    def test_retry_keeps_a_ref_directory_replaced_by_a_different_directory(self) -> None:
        path = self.checkout_with_removal_intent("current", "ref-directory-replacement")
        namespace = path / ".git/refs/heads"
        loose = namespace / "loose"
        loose.unlink()
        loose.mkdir()
        moved = self.base / "moved-refs"
        replacement = self.base / "replacement-refs"
        replacement.mkdir()
        open_entry = os.open

        def open_during_swap(name: str | Path, flags: int, mode: int = 0o777, *,
                             dir_fd: int | None = None) -> int:
            if name == namespace.name and dir_fd is not None and flags & os.O_DIRECTORY:
                _ = namespace.rename(moved)
                _ = replacement.rename(namespace)
            return open_entry(name, flags, mode, dir_fd=dir_fd)

        with mock.patch.object(os, "open", side_effect=open_during_swap):
            self.assert_retry_keeps_metadata(path, namespace)
        self.assertTrue((moved / "loose").is_dir())
        self.assertEqual(list(namespace.iterdir()), [])

    def test_retry_keeps_checkout_when_a_ref_ancestor_changes_during_scan(self) -> None:
        path = self.checkout_with_removal_intent("current", "ref-ancestor-swap")
        metadata = path / ".git"
        refs = metadata / "refs"
        moved = self.base / "moved-git-directory"
        replacement = self.base / "replacement-git-directory"
        (replacement / "refs").mkdir(parents=True)
        open_entry = os.open

        def open_during_swap(name: str | Path, flags: int, mode: int = 0o777, *,
                             dir_fd: int | None = None) -> int:
            if name in (refs, refs.name) and flags & os.O_DIRECTORY:
                if dir_fd is not None:
                    self.assertTrue(os.path.samestat(metadata.stat(), os.fstat(dir_fd)))
                _ = metadata.rename(moved)
                metadata.symlink_to(replacement, target_is_directory=True)
            return open_entry(name, flags, mode, dir_fd=dir_fd)

        with (mock.patch.object(os, "open", side_effect=open_during_swap),
              mock.patch.object(scratch, "remove", side_effect=AssertionError("source deletion started")) as delete):
            result = scratch.remove_folder(path)
            delete.assert_not_called()
        self.assertEqual(result, scratch.KeptFolder(path, f"{metadata}: directory changed during inspection"))
        self.assertTrue(path.is_dir())
        self.assertTrue(scratch.removal_mark(path).is_file())
        self.assertTrue((moved / "refs/heads/loose").is_file())

    def test_retry_accepts_a_symbolic_ref_with_a_ref_target(self) -> None:
        path = self.copy(name="symbolic-ref")
        self.add_saved_work_and_loose_ref(path)
        link = path / ".git/refs/heads/alias"
        link.symlink_to("refs/heads/loose")
        self.git(path, "symbolic-ref", "refs/heads/alias")
        self.stop_removal_after_intent(path, "current")
        with mock.patch.object(scratch, "recovery_state", wraps=scratch.recovery_state) as inspect:
            self.assertEqual(scratch.remove_folder(path), scratch.RemovedCopy())
        inspect.assert_called_once()
        self.assertFalse(path.exists())
        self.assertFalse(scratch.removal_mark(path).exists())

    def test_retry_keeps_a_symbolic_ref_added_or_changed_after_the_intent(self) -> None:
        for version in ("current", "legacy"):
            for change in ("add", "replace", "published alias"):
                with self.subTest(version=version, change=change):
                    path = self.copy(name=f"{version}-symbolic-ref-{change}")
                    self.add_saved_work_and_loose_ref(path)
                    link = path / ".git/refs/heads/alias"
                    if change == "replace":
                        link.symlink_to("refs/remotes/origin/main")
                    self.stop_removal_after_intent(path, version)
                    if version == "legacy":
                        last_file_change = max(scratch.removal_source_files(path).values())
                        os.utime(scratch.removal_mark(path), ns=(last_file_change, last_file_change))

                    if change == "published alias":
                        self.git(path, "symbolic-ref", "refs/heads/alias", "refs/heads/loose")
                        self.assert_retry_keeps_metadata(path, link)
                        self.assertEqual(link.read_text(), "ref: refs/heads/loose\n")
                    else:
                        link.unlink(missing_ok=True)
                        link.symlink_to("refs/heads/loose")
                        self.git(path, "symbolic-ref", "refs/heads/alias")
                        self.assert_retry_keeps_metadata(path, link)
                        self.assertEqual(os.readlink(link), "refs/heads/loose")

    def test_retry_keeps_a_symbolic_ref_with_the_removal_marks_timestamp(self) -> None:
        for mark_delay in (0, 10_000_000_000):
            with self.subTest(mark_delay=mark_delay):
                path = self.checkout_with_removal_intent("current", f"coarse-symbolic-ref-{mark_delay}")
                link = path / ".git/refs/heads/alias"
                link.symlink_to("refs/heads/loose")
                stamp = link.lstat().st_ctime_ns
                intent = stamp + 1
                self.set_removal_mark_times(path, intent=intent, mtime=stamp + mark_delay)
                if mark_delay:
                    self.assertGreater(scratch.removal_mark(path).stat().st_mtime_ns, intent)
                self.assert_retry_keeps_metadata(path, link)

    def test_final_removal_scan_checks_metadata_added_after_the_first_scan(self) -> None:
        scan_metadata = git_files.check_internal_removal_entries
        for version in ("current", "legacy"):
            for change in ("index directory", "regular ref", "symbolic ref"):
                with self.subTest(version=version, change=change):
                    path = self.checkout_with_removal_intent(version, change.replace(" ", "-"))
                    mark = scratch.removal_mark(path)
                    last_file_change = max(scratch.removal_source_files(path).values())
                    if version == "legacy":
                        os.utime(mark, ns=(last_file_change, last_file_change))
                    fields = cast(dict[str, object], json.loads(mark.read_text()))
                    stored_intent = fields.get("removal_intent_ns", mark.stat().st_mtime_ns)
                    assert isinstance(stored_intent, int)
                    intent: int = stored_intent
                    index = path / ".git/index"
                    ref = path / ".git/refs/heads/added"
                    oid = (ref.parent / "loose").read_text()

                    def mutate(checkouts: tuple[git_files.Checkout, ...],
                               previous: git_files.RecordedRemovalFiles | git_files.LegacyRemovalFiles, *,
                               removal_root: Path) -> None:
                        scan_metadata(checkouts, previous, removal_root=removal_root)
                        if change == "index directory":
                            index.unlink()
                            index.mkdir()
                        elif change == "regular ref":
                            _ = ref.write_text(oid)
                        else:
                            ref.symlink_to("refs/heads/loose")
                        if change != "index directory":
                            self.assertGreater(ref.lstat().st_ctime_ns, intent)

                    with mock.patch.object(git_files, "check_internal_removal_entries", side_effect=mutate):
                        if change == "index directory":
                            self.assert_retry_keeps_metadata(path, index)
                            self.assertTrue(index.is_dir())
                        else:
                            pending = scratch.pending_removal(path)
                            assert isinstance(pending, copy_record.Registered)
                            state = scratch.recovery_state(path, pending, scratch.BusyMarkScope(),
                                                           scratch_settings=conf.read("scratch"))
                            if isinstance(state, scratch.KeptFolder):
                                self.assertIn(str(ref), state.reason)
                            else:
                                self.assertEqual(state, scratch.RemovalRetry.RECHECK)
                        self.assertTrue(mark.is_file())

    def test_retry_validates_source_again_after_cargo_cleanup(self) -> None:
        for version in ("current", "legacy"):
            for has_outputs in (True, False):
                with self.subTest(version=version, has_outputs=has_outputs):
                    path = self.copy(name=f"{version}-after-cargo-{has_outputs}")
                    if not has_outputs:
                        _ = (path / "Cargo.toml").write_text("[workspace]\n")
                        self.git(path, "add", "Cargo.toml")
                    self.add_saved_work_and_loose_ref(path)
                    with mock.patch.object(scratch, "save_cargo_outputs", return_value=()):
                        self.stop_removal_after_intent(path, version)
                    index = path / ".git/index"
                    reference = path / ".git/refs/heads/late"
                    roots = cargo_output.CargoOutputRoots(path, path / "target", self.base / "outside-output")
                    saved = (scratch.SavedCargoOutput(roots, (), (), time.time()),) if has_outputs else ()

                    def prepare_cargo(_path: Path) -> tuple[scratch.SavedCargoOutput, ...]:
                        if not has_outputs:
                            reference.symlink_to("refs/heads/loose")
                        return saved

                    def change_metadata(_path: Path, _saved: tuple[scratch.SavedCargoOutput, ...]) -> None:
                        if has_outputs:
                            index.unlink()
                            index.mkdir()

                    with (mock.patch.object(scratch, "save_cargo_outputs", side_effect=prepare_cargo),
                          mock.patch.object(scratch, "clean_cargo_outputs", side_effect=change_metadata)):
                        self.assert_retry_keeps_metadata(path, index if has_outputs else reference)
                    if has_outputs:
                        self.assertTrue(index.is_dir())
                    else:
                        self.assertEqual(os.readlink(reference), "refs/heads/loose")

    def test_retry_removes_checkout_below_a_search_only_ancestor(self) -> None:
        ancestor = self.base / "search-only"
        path = ancestor / "readable" / "checkout"
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        self.add_saved_work_and_loose_ref(path)
        self.stop_removal_after_intent(path, "current")

        self.addCleanup(ancestor.chmod, ancestor.stat().st_mode & 0o7777)
        ancestor.chmod(0o111)
        self.assertEqual(scratch.remove_folder(path), scratch.RemovedCopy())
        self.assertFalse(path.exists())
        self.assertFalse(scratch.removal_mark(path).exists())

    def test_retry_keeps_checkout_with_a_symbolic_ref_to_an_outside_path(self) -> None:
        path = self.checkout_with_removal_intent("current", "outside-ref")
        link = path / ".git/refs/heads/alias"
        outside = self.base / "outside"
        _ = outside.write_text("Keep this file.")
        for target in (str(outside), "refs/../outside"):
            with self.subTest(target=target):
                link.symlink_to(target)
                self.assert_retry_keeps_metadata(path, link)
                self.assertEqual(outside.read_text(), "Keep this file.")
                link.unlink()

    def test_legacy_retry_keeps_standard_namespace_with_exact_ref_evidence(self) -> None:
        for evidence in ("reflog", "packed"):
            with self.subTest(evidence=evidence):
                path = self.copy(name=evidence)
                self.git(path, "symbolic-ref", "HEAD", "refs/heads/original")
                self.git(path, "commit", "-q", "--allow-empty", "-m", "Create the fixture")
                self.git(path, "remote", "add", "origin", str(self.base / "origin"))
                self.git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
                self.git(path, "switch", "-q", "--detach")
                self.git(path, "branch", "-D", "original")
                namespace = path / ".git/refs/heads"
                if namespace.is_dir():
                    namespace.rmdir()
                self.git(path, "update-ref", "--create-reflog", "refs/heads", "HEAD")
                log = path / ".git/logs/refs/heads"
                self.assertTrue(log.is_file())
                if evidence == "packed":
                    self.git(path, "pack-refs", "--all", "--no-prune")
                    log.unlink()
                self.assertTrue(namespace.is_file())
                self.stop_removal_after_intent(path, "legacy")
                namespace.unlink()
                namespace.mkdir()
                self.assert_retry_keeps_metadata(path, namespace)

    def test_legacy_retry_removes_checkout_with_a_fresh_empty_tags_namespace(self) -> None:
        path = self.checkout_with_removal_intent("legacy", "empty-tags")
        self.assertEqual(list((path / ".git/refs/tags").iterdir()), [])
        self.assertEqual(scratch.remove_folder(path), scratch.RemovedCopy())
        self.assertFalse(path.exists())
        self.assertFalse(scratch.removal_mark(path).exists())

    def test_retry_keeps_internal_metadata_replaced_with_a_directory(self) -> None:
        names = ("HEAD", "index", "packed-refs", "refs/heads/loose")
        for version in ("current", "legacy"):
            for name in names:
                with self.subTest(version=version, name=name):
                    path = self.checkout_with_removal_intent(version, Path(name).name)
                    mark = scratch.removal_mark(path)
                    fields = cast(dict[str, object], json.loads(mark.read_text()))
                    intent = fields.get("removal_intent_ns", mark.stat().st_mtime_ns)
                    assert isinstance(intent, int)
                    metadata = path / ".git" / name
                    original = self.base / f"{version}-{metadata.name}-original"
                    _ = metadata.rename(original)
                    metadata.mkdir()
                    self.assertTrue(all(ctime <= intent for ctime in scratch.removal_source_files(path).values()))

                    with mock.patch.object(scratch, "remove", side_effect=AssertionError("source deletion started")) as delete:
                        result = scratch.remove_folder(path)
                        delete.assert_not_called()
                    self.assertIsInstance(result, scratch.KeptFolder)
                    assert isinstance(result, scratch.KeptFolder)
                    self.assertEqual(result.path, path)
                    self.assertIn(str(metadata), result.reason)
                    self.assertTrue(path.is_dir())
                    self.assertTrue(mark.is_file())

                    metadata.rmdir()
                    _ = original.rename(metadata)
                    if version == "legacy" and name != "index":
                        self.assert_retry_keeps_metadata(path, metadata)
                        mark.unlink()
                    self.assertEqual(scratch.remove_folder(path), scratch.RemovedCopy())
                    self.assertFalse(path.exists())
                    self.assertFalse(mark.exists())

    def test_retry_keeps_internal_head_replaced_with_a_fifo(self) -> None:
        program = """import sys
from pathlib import Path
from unittest.mock import patch
from scripts.lib import scratch

path = Path(sys.argv[1])
with patch.object(scratch, "remove", side_effect=AssertionError("source deletion started")) as delete:
    result = scratch.remove_folder(path)
    delete.assert_not_called()
assert isinstance(result, scratch.KeptFolder), result
assert str(path / ".git/HEAD") in result.reason, result
assert path.is_dir()
assert scratch.removal_mark(path).is_file()
"""
        for version in ("current", "legacy"):
            for kind in ("fifo", "link"):
                with self.subTest(version=version, kind=kind):
                    path = self.checkout_with_removal_intent(version, kind)
                    head = path / ".git/HEAD"
                    head.unlink()
                    if kind == "fifo":
                        os.mkfifo(head)
                    else:
                        pipe = self.base / f"{version}-head-pipe"
                        os.mkfifo(pipe)
                        head.symlink_to(pipe)
                    run = subprocess.run([sys.executable, "-c", program, str(path)],
                                         capture_output=True, text=True, timeout=10)
                    self.assertEqual(run.returncode, 0, run.stderr)

    def test_retry_recovers_when_interrupted_deletion_removed_internal_head(self) -> None:
        for version in ("current", "legacy"):
            with self.subTest(version=version):
                path = self.checkout_with_removal_intent(version, "absent-head")
                mark = scratch.removal_mark(path)
                (path / ".git/HEAD").unlink()
                self.assertEqual(scratch.remove_folder(path), scratch.RemovedCopy())
                self.assertFalse(path.exists())
                self.assertFalse(mark.exists())

    def test_retry_keeps_an_emptied_tracked_busy_mark(self) -> None:
        repo = self.base / "checkout"
        repo.mkdir()
        self.git(repo, "init", "-q")
        mark = repo / scratch.BUSY
        _ = mark.write_text("Tracked work\n")
        self.git(repo, "add", mark.name)
        self.git(repo, "commit", "-qm", "Save the tracked file")
        self.git(repo, "remote", "add", "origin", str(self.base / "origin"))
        self.git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

        with (mock.patch.object(scratch, "remove", side_effect=OSError("stop after removal intent")),
              self.assertRaisesRegex(OSError, "stop after removal intent")):
            _ = scratch.remove_folder(repo)
        self.assertTrue(scratch.removal_mark(repo).is_file())
        _ = mark.write_bytes(b"")
        stamp = mark.stat().st_ctime_ns
        self.set_removal_mark_times(repo, intent=stamp + 1, mtime=stamp)

        result = scratch.remove_folder(repo)
        self.assertEqual(result, scratch.KeptFolder(repo, f"{repo}: it has uncommitted changes"))
        self.assertEqual(mark.read_bytes(), b"")
        self.assertTrue(scratch.removal_mark(repo).is_file())

    def test_descendant_check_returns_held_locks_or_a_kept_folder(self) -> None:
        parent = self.base / "container"
        child = parent / "checkout"
        child.mkdir(parents=True)
        self.git(child, "init", "-q")
        settings = conf.read("scratch")
        with ExitStack() as locks:
            checked = scratch.lock_descendant_clones(parent, locks, scratch.BusyMarkScope(),
                                                      create=True, scratch_settings=settings)
            self.assertIsInstance(checked, scratch.DescendantLocksHeld)
            with scratch.busy_locks(child, create=False) as idle:
                self.assertFalse(idle)
        with scratch.busy_locks(child, create=False) as idle, ExitStack() as locks:
            self.assertTrue(idle)
            checked = scratch.lock_descendant_clones(parent, locks, scratch.BusyMarkScope(),
                                                      create=False, scratch_settings=settings)
            self.assertEqual(checked, scratch.KeptFolder(parent, f"{child}: {scratch.BUSY_REASON}"))

    def test_recursive_removal_keeps_nonregular_busy_marks(self) -> None:
        parent = self.base / "container"
        child = parent / "checkout"
        child.mkdir(parents=True)
        self.git(child, "init", "-q")
        mark = scratch.busy_path(child)
        target = self.base / "link-target"
        target.touch()
        for kind in ("pipe", "folder", "link"):
            with self.subTest(kind=kind):
                if kind == "pipe":
                    os.mkfifo(mark)
                elif kind == "folder":
                    mark.mkdir()
                else:
                    mark.symlink_to(target)
                program = (
                    "import sys; from pathlib import Path; from scripts.lib import scratch; "
                    "result = scratch.remove_folder(Path(sys.argv[1])); print(result)"
                )
                run = subprocess.run([sys.executable, "-c", program, str(parent)],
                                     capture_output=True, text=True, timeout=10, check=True)
                self.assertIn(scratch.BUSY_REASON, run.stdout)
                self.assertIn(str(child), run.stdout)
                self.assertTrue(child.is_dir())
                if kind == "folder":
                    mark.rmdir()
                else:
                    mark.unlink()
        self.assertEqual(target.read_bytes(), b"")

    def test_recursive_removal_accepts_a_linked_descendant_with_a_git_file(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        task = "gate:container"
        parent = scratch.new(task, "container")
        child = parent / "checkout"
        self.git(source, "worktree", "add", "--detach", "-q", str(child))
        self.assertTrue((child / ".git").is_file())
        self.assertIsInstance(scratch.remove_folder(parent), scratch.RemovedCopy)
        self.assertFalse(parent.exists())
        self.git(source, "cat-file", "-e", "HEAD^{commit}")

    def test_busy_mark_scope_reuses_its_parent_lock_only_in_the_same_scope(self) -> None:
        parent = self.base / "container"
        child = parent / "checkout"
        child.mkdir(parents=True)
        scope = scratch.BusyMarkScope()
        with scratch.busy_locks(parent, create=True, scope=scope) as idle:
            self.assertTrue(idle)
            with scratch.busy_locks(child, create=True) as independent:
                self.assertFalse(independent)
            with scratch.busy_locks(child, create=True, scope=scope) as shared:
                self.assertTrue(shared)
            with scratch.busy_locks(parent, create=False) as independent:
                self.assertFalse(independent)
        self.assertEqual(scope.held, {})
        with scratch.busy_locks(child, create=False, scope=scope) as idle:
            self.assertTrue(idle)

    def test_removal_keeps_the_sidecars_and_record_in_a_replacement_parent(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        task = "gate:new-parent"
        clone = copies.clone(source, task)
        parent = clone.parent
        self.assertTrue(parent.is_relative_to(self.base))
        old_parent = self.base / "old-parent"
        mark = scratch.removal_mark(clone)
        lease = clone.with_name(clone.name + ".lease.json")
        lock = clone.with_name(clone.name + ".lock")
        unlink = os.unlink

        def replace_parent(name: str | bytes | os.PathLike[str] | os.PathLike[bytes], *,
                           dir_fd: int | None = None) -> None:
            unlink(name, dir_fd=dir_fd)
            if name == mark and dir_fd is None:
                _ = parent.rename(old_parent)
                parent.mkdir()
                for path in (mark, lease, lock):
                    _ = path.write_text("replacement")

        with mock.patch.object(os, "unlink", side_effect=replace_parent):
            self.assertIsInstance(self.removal_attempt(clone, task), scratch.RemovedCopy)

        self.assertFalse((old_parent / clone.name).exists())
        self.assertFalse((old_parent / mark.name).exists())
        for path in (lease, lock):
            self.assertTrue((old_parent / path.name).is_file())
        for path in (mark, lease, lock):
            self.assertEqual(path.read_text(), "replacement")

    def test_end_reports_held_work_and_continues_after_a_record_error(self) -> None:
        failed = scratch.new("gate:report", "failed")
        clean = scratch.new("gate:report", "clean")
        held = self.base / "held"
        held.mkdir()
        self.git(held, "init", "-q")
        _ = (held / "work").write_text("keep")
        scratch.write_holds({str(held): scratch.Hold(task="gate:report", reason="old hold",
                                                   maker="", made=0.0, ended=0.0)})
        finish = scratch.record_task_end

        def fail_one(path: Path, task: str, moment: float, *, scratch_settings: conf.ConfigRead) -> None:
            if path == failed:
                raise PermissionError("record denied")
            finish(path, task, moment, scratch_settings=scratch_settings)

        with mock.patch.object(scratch, "record_task_end", side_effect=fail_one):
            result = scratch.end("gate:report")
        self.assertTrue(clean.is_dir())
        self.assertEqual({folder.path for folder in result}, {clean, failed, held})
        self.assertIn("record denied", next(folder.reason for folder in result if folder.path == failed))
        self.assertIn("task gate:report ended", next(folder.reason for folder in result if folder.path == held))
        self.assertTrue(scratch.holds()[str(held)]["ended"])
        self.assertEqual((held / "work").read_text(), "keep")

    def test_main_reports_a_session_clone_after_its_task_transfer(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        session = self.base / "session" / "maker"
        (session / session_folder.RUN).mkdir(parents=True)
        checkout = copies.clone(source, "gate:old", "transfer")
        scratch.hand_over(checkout, "next-unit", "gate:new")
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = scratch.main(["end", "--task", "gate:new"])
        self.assertEqual(code, 0, error.getvalue())
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue().splitlines(), [
            f"kept for a manual removal list: {checkout} (scratch, task gate:new ended)",
        ])
        self.assertTrue(checkout.is_dir())
        self.assertGreater(scratch.holds()[str(checkout)]["ended"], 0)

    def test_standing_checkout_creates_a_stamped_cache_the_sweep_accepts(self) -> None:
        seed = ImmutableGitSeed.create(self.base, "main", "Scratch tests", "scratch@example.invalid",
                                       {"Cargo.toml": "[workspace]\n", "source.txt": "style source\n"})
        started = time.time()
        with mock.patch.dict(os.environ, {"HOME": str(self.base)}):
            with copies.standing("style", seed.checkout, copies.CommittedSource("HEAD")) as checkout:
                target = (checkout / "target").resolve()
            self.assertTrue(checkout.exists())
            self.assertEqual(build_folders.build_folder_state(checkout), "free")
            # Supply build output, but let copies supply every cache marker.
            fingerprint = target / "debug" / ".fingerprint" / "sample-0123456789abcdef"
            fingerprint.mkdir(parents=True)
            _ = (fingerprint / "bin-sample.json").write_text("{}")
            self.assertFalse((target / sweep.RUSTC_INFO).exists())
            self.assertTrue(sweep.is_cargo_target(str(target)), "The sweep must accept the standing cache")
            self.assertGreaterEqual((target / sweep.USE_STAMP).stat().st_mtime, started)

    def test_exported_busy_descriptors_keep_checkout_locked_in_command(self) -> None:
        path = self.base / "held-checkout"
        path.mkdir()
        descriptors: list[int] = []
        with scratch.busy_locks(path, create=True, inherited_fds=descriptors) as acquired:
            self.assertTrue(acquired)
            self.assertTrue(descriptors)
            refused: list[int] = []
            with scratch.busy_locks(path, create=False, inherited_fds=refused) as idle:
                self.assertFalse(idle)
                self.assertEqual(refused, [])
            process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, pass_fds=descriptors)
        try:
            with scratch.busy_locks(path, create=False) as idle:
                self.assertFalse(idle)
        finally:
            _, errors = process.communicate(input=b"x", timeout=10)
        self.assertEqual(process.returncode, 0, errors)
        with scratch.busy_locks(path, create=False) as idle:
            self.assertTrue(idle)

    def test_blocking_busy_acquisition_waits_then_takes_released_mark(self) -> None:
        path = self.base / "held-checkout"
        path.mkdir()
        attempted, acquired = Event(), Event()

        def take() -> bool:
            attempted.set()
            with scratch.busy_locks(path, create=False, wait=True) as idle:
                acquired.set()
                return idle

        with ThreadPoolExecutor(max_workers=1) as executor:
            with scratch.busy_locks(path, create=True) as idle:
                self.assertTrue(idle)
                future = executor.submit(take)
                self.assertTrue(attempted.wait(timeout=10))
                self.assertFalse(acquired.wait(timeout=0.02))
            self.assertTrue(future.result(timeout=10))

    def test_target_uses_the_loaded_namer_after_import_access_ends(self) -> None:
        probe = """import builtins, sys
from scripts.lib import scratch
original_import = builtins.__import__
def load(name, *args, **kwargs):
    if name == 'scripts.lib.build_folders':
        raise ImportError('helper imports are closed after startup')
    return original_import(name, *args, **kwargs)
builtins.__import__ = load
print(scratch.target('gate:proof', 'tree', False))
"""
        result = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", probe], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).parent, copy_record.root())
        self.assertRegex(Path(result.stdout.strip()).name, r"^gate-proof-tree-[a-z0-9]{6}$")

    def test_copy_record_with_a_legacy_shared_target_still_loads(self) -> None:
        path = self.copy()
        record_path = path / copy_record.RECORD
        fields = cast(dict[str, object], json.loads(record_path.read_text()))
        fields["target_mode"] = "shared"
        fields["shared_target"] = str(self.base / "old-build-folder")
        _ = record_path.write_text(json.dumps(fields))

        record = scratch.task_of(path)

        self.assertIsInstance(record, copy_record.Registered)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:copy")
        self.assertEqual(record.fields, fields)
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.assertFalse((self.base / "old-build-folder").exists())

    def test_record_and_lifetime_results_distinguish_missing_bad_and_external_end(self) -> None:
        plain = self.base / "notes.txt"
        _ = plain.write_text("scratch output")
        for path in (self.base, plain):
            with self.subTest(path=path.name):
                self.assertIsInstance(scratch.task_of(path), copy_record.NotRegistered)
        _ = (self.base / copy_record.RECORD).write_text("bad json")
        bad = scratch.task_of(self.base)
        self.assertIsInstance(bad, copy_record.UnreadableRecord)
        assert isinstance(bad, copy_record.UnreadableRecord)
        self.assertIn(str(self.base / copy_record.RECORD), bad.reason)
        with self.subTest(path="folder with an unsearchable parent"):
            if os.geteuid() == 0:
                self.skipTest("root ignores the parent permission mode")
            parent = self.base / "unsearchable"
            folder = parent / "folder"
            folder.mkdir(parents=True)
            parent.chmod(0o600)
            try:
                self.assertIsInstance(scratch.task_of(folder), copy_record.UnreadableRecord)
            finally:
                parent.chmod(0o700)
        self.assertIsInstance(scratch.task_end("until:bad"), scratch.Unknown)
        self.assertIsInstance(scratch.task_end("gate:outside"), scratch.Live)
        self.assertIsInstance(scratch.task_end("until:2000-01-01T0000Z"), scratch.Ended)

    def test_copy_record_is_atomic_and_excludes_only_its_two_metadata_files(self) -> None:
        path = self.copy()
        self.git(path, "init", "-q")
        scratch.register_copy(path, task="gate:copy", owner_unit="maker", source_path="/source",
                              source_commit="a" * 40, copy_commit="b" * 40, target_mode="seed",
                              seed_source="/seed", borrows_from="/source/.git", land_target="origin/builds")
        fields = cast(dict[str, object], json.loads((path / copy_record.RECORD).read_text()))
        self.assertEqual({key: fields[key] for key in ("source_path", "source_commit", "copy_commit", "target_mode",
                         "seed_source", "borrows_from", "owner_unit", "task", "land_target")},
                         {"source_path": "/source", "source_commit": "a" * 40, "copy_commit": "b" * 40,
                          "target_mode": "seed", "seed_source": "/seed", "borrows_from": "/source/.git",
                          "owner_unit": "maker", "task": "gate:copy", "land_target": "origin/builds"})
        _ = (path / ".claude-busy").touch()
        self.assertIsNone(scratch.repo_problem(path))
        _ = (path / "ordinary").write_text("keep me")
        self.assertEqual(scratch.repo_problem(path), "it has uncommitted changes")
        before = (path / copy_record.RECORD).read_bytes()
        with mock.patch.object(os, "replace", side_effect=OSError("injected record stop")):
            with self.assertRaises(OSError):
                scratch.hand_over(path, "next-unit", "gate:next-unit")
        self.assertEqual((path / copy_record.RECORD).read_bytes(), before)
        scratch.hand_over(path, "next-unit", "gate:next-unit")
        self.assertIsInstance(scratch.task_of(path), copy_record.Registered)

    def test_phase_lifetime_reads_the_open_phase_list_and_fails_closed(self) -> None:
        run = self.base / "run"
        run.mkdir()
        progress = run / "progress_history_state.json"
        path = self.copy(task="phase:maker/21", run_dir=str(run), phase_id="21")
        state = {"open_phases": [{"id": "21", "status": "active"}, {"id": "22", "status": "active"}]}
        _ = progress.write_text(json.dumps(state))
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.assertEqual([entry.path for entry in scratch.end("phase:maker/21")], [path])
        _ = progress.write_text("bad")
        unknown = scratch.lifetime(path)
        self.assertIsInstance(unknown, scratch.Unknown)
        self.assertEqual([entry.path for entry in scratch.end("phase:maker/21")], [path])
        _ = progress.write_text(json.dumps({"phase": {"id": "21", "status": "finished"}}))
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        self.assertEqual([entry.path for entry in scratch.end("phase:maker/21")], [path])
        self.assertTrue(path.is_dir())

    def test_a_reopened_phase_keeps_its_copy(self) -> None:
        run = self.base / "run"
        run.mkdir()
        task = "phase:maker/21"
        path = self.copy(task=task, run_dir=str(run), phase_id="21")
        state = {
            "phase": {"id": "21", "status": "completed", "instance_id": "old"},
            "open_phases": [{"id": "21", "status": "active", "instance_id": "new"}],
        }
        _ = (run / "progress_history_state.json").write_text(json.dumps(state))

        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.assertEqual([entry.path for entry in scratch.end(task)], [path])
        self.assertTrue(path.is_dir())

    def test_an_existing_unknown_phase_task_can_update_and_end(self) -> None:
        path = self.base / "scratch" / "legacy-phase"
        path.mkdir(parents=True)
        task = "phase:maker/ci-icon"
        scratch.write_record(path, task, 100.0)

        scratch.renew(path, now=200.0)

        record = scratch.task_of(path)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, task)
        self.assertEqual(record.fields["renewed"], 200.0)
        self.assertEqual([entry.path for entry in scratch.end(task)], [path])
        self.assertTrue(path.is_dir())

    def test_unknown_phase_is_refused_before_creation_or_hand_over(self) -> None:
        path = self.copy()
        before = (path / copy_record.RECORD).read_bytes()
        for operation in ("new", "adopt", "hand-over"):
            with self.subTest(operation=operation):
                loose = self.base / "loose"
                loose.mkdir(exist_ok=True)
                _ = (loose / "work").write_text("keep this work")
                with self.assertRaises(scratch.ScratchError) as refusal:
                    if operation == "new":
                        _ = scratch.new("phase:maker/ci-icon")
                    elif operation == "adopt":
                        _ = scratch.adopt(loose, "phase:maker/ci-icon")
                    else:
                        scratch.hand_over(path, "maker", "phase:maker/ci-icon")
                message = str(refusal.exception)
                for form in ("ci-icon", "phase", "gate:<id>", "--until <when>"):
                    self.assertIn(form, message)
                self.assertEqual((path / copy_record.RECORD).read_bytes(), before)
                self.assertEqual((loose / "work").read_text(), "keep this work")
                self.assertFalse((self.base / "scratch").exists())

    def test_real_phase_ids_come_from_the_registered_plan(self) -> None:
        _ = self.unit_plan("maker", (
            "### Phase 21 — Finished work (abcdef12)\n\n"
            "### Phase 21 Review\n\n"
            "### Phase 22a — Split work · status: todo\n"))
        caller_plan = self.base / "checkout" / "docs" / "plans" / "maker-unit.md"
        caller_plan.parent.mkdir(parents=True)
        _ = caller_plan.write_text("### Phase 99 — Caller-only phase · status: todo\n")
        old_cwd = Path.cwd()
        os.chdir(caller_plan.parents[2])
        self.addCleanup(os.chdir, old_cwd)
        for phase in ("21", "22a"):
            with self.subTest(phase=phase):
                task = f"phase:maker/{phase}"
                path = scratch.new(task)
                self.assertTrue(path.is_dir())
                record = scratch.task_of(path)
                assert isinstance(record, copy_record.Registered)
                self.assertEqual(record.task, task)
                self.assertEqual(record.fields["phase_id"], phase)
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.new("phase:maker/99")

    def test_phase_creation_without_a_readable_registered_plan_names_the_cause(self) -> None:
        plan = self.unit_plan("maker", "### Phase 21 — Work · status: todo\n")
        for cause in ("unknown unit", "missing plan", "invalid encoding"):
            with self.subTest(cause=cause):
                unit = "unregistered" if cause == "unknown unit" else "maker"
                if cause == "missing plan":
                    plan.unlink()
                elif cause == "invalid encoding":
                    _ = plan.write_bytes(b"\xff")
                with self.assertRaises(scratch.ScratchError) as refusal:
                    _ = scratch.new(f"phase:{unit}/21")
                message = str(refusal.exception)
                self.assertIn(unit, message)
                self.assertIn("plan", message.lower())
                if cause != "unknown unit":
                    self.assertIn(str(plan), message)
                self.assertFalse((self.base / "scratch").exists())

    def test_hand_over_recovers_after_a_process_stops_before_record_replace(self) -> None:
        path = self.copy()
        program = ("import os,sys; from pathlib import Path; from scripts.lib import scratch; "
                   "os.replace=lambda *args: os._exit(71); scratch.hand_over(Path(sys.argv[1]), 'next-unit', 'gate:next-unit')")
        stopped = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", program, str(path)], check=False, capture_output=True)
        self.assertEqual(stopped.returncode, 71, stopped.stderr)
        scratch.hand_over(path, "next-unit", "gate:next-unit")
        record = scratch.task_of(path)
        self.assertIsInstance(record, copy_record.Registered)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:next-unit")
        self.assertEqual(record.fields["owner_unit"], "next-unit")
        self.assertIsNone(scratch.repo_problem(path))

    def test_landed_lifetime_requires_target_ancestry_and_saved_work(self) -> None:
        path = self.copy(task="landed:maker/work", land_target="origin/main")
        self.git(path, "init", "-q")
        self.git(path, "commit", "-qm", "base", "--allow-empty")
        remote = self.base / "origin.git"
        self.git(self.base, "init", "--bare", "-q", str(remote))
        self.git(path, "remote", "add", "origin", str(remote))
        self.git(path, "push", "-q", "origin", "HEAD:refs/heads/work")
        self.assertIsInstance(scratch.lifetime(path), scratch.Unknown)
        self.git(path, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        _ = (path / "edit").write_text("unsaved")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        (path / "edit").unlink()
        self.git(path, "checkout", "-qb", "side")
        self.git(path, "commit", "-qm", "unpushed", "--allow-empty")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)

    def test_landed_task_removal_checks_the_remote_after_dry_run_keeps_the_clone(self) -> None:
        task = "landed:maker/work"
        path, remote = self.copy_with_published_head("landed-remote", task)
        self.git(path, "push", "-q", "origin", "HEAD:refs/heads/work")
        self.git(path, "commit", "-qm", "Published after the last fetch", "--allow-empty")
        self.git(path, "push", "-q", str(remote), "HEAD:refs/heads/main")
        trace = self.base / "git.trace"
        trace.touch()
        with (mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file", "GIT_TRACE": str(trace)}),
              mock.patch.object(time_limit, "capture", wraps=time_limit.capture) as query):
            outcome = scratch.remove_folder(path, scratch.TaskRemoval(task, time.time()), dry_run=True)
            self.assertIsInstance(outcome, scratch.KeptFolder)
            assert isinstance(outcome, scratch.KeptFolder)
            self.assertIn("a remote check is needed", outcome.reason)
            self.assertNotIn("built-in: git fetch ", trace.read_text())
            query.assert_not_called()
            self.assertIsInstance(self.removal_attempt(path, task), scratch.RemovedCopy)
        self.assertFalse(path.exists())
        query.assert_called_once()
        self.assertNotIn("built-in: git fetch ", trace.read_text())

    def test_phase_end_survives_a_stop_when_the_recorder_moves_to_the_next_phase(self) -> None:
        run = self.base / "run"
        run.mkdir()
        progress = run / "progress_history_state.json"
        path = self.copy(task="phase:maker/21", run_dir=str(run), phase_id="21")
        _ = progress.write_text(json.dumps({"phase": {"id": "21", "status": "finished"}}))
        with mock.patch.object(scratch, "remove", side_effect=OSError("injected removal stop")):
            result = self.removal_attempt(path, "phase:maker/21")
        self.assertEqual(result, scratch.KeptFolder(path, "injected removal stop"))
        _ = progress.write_text(json.dumps({"open_phases": [{"id": "22", "status": "active"}]}))
        self.assertIsInstance(self.removal_attempt(path, "phase:maker/21"), scratch.RemovedCopy)

    def test_hand_over_keeps_parent_path_and_child_alternates_then_child_goes_first(self) -> None:
        parent = self.copy("parent")
        self.git(parent, "init", "-q")
        self.git(parent, "commit", "-qm", "base", "--allow-empty")
        child = parent.parent / "child"
        self.git(parent, "clone", "--shared", str(parent), str(child))
        scratch.register_copy(child, task="gate:child", owner_unit="child", source_path=str(parent),
                              source_commit="a" * 40, copy_commit="b" * 40, target_mode="none",
                              seed_source="", borrows_from=str(parent / ".git"))
        alternate = child / ".git/objects/info/alternates"
        before = alternate.read_text()
        scratch.hand_over(parent, "next-unit", "gate:next-unit")
        registered = scratch.task_of(parent)
        self.assertIsInstance(registered, copy_record.Registered)
        assert isinstance(registered, copy_record.Registered)
        self.assertEqual(registered.task, "gate:next-unit")
        self.assertEqual(registered.fields["owner_unit"], "next-unit")
        self.assertEqual(alternate.read_text(), before)
        self.assertTrue(Path(before.strip()).is_dir())
        self.assertEqual([entry.path for entry in scratch.end("gate:next-unit")], [parent])
        self.assertIsInstance(self.removal_attempt(child, "gate:child"), scratch.RemovedCopy)
        # Save the parent's own commits before allowing its removal.
        self.git(parent, "remote", "add", "origin", str(self.base / "origin"))
        self.git(parent, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertIsInstance(self.removal_attempt(parent, "gate:next-unit"), scratch.RemovedCopy)

    def test_shared_holders_and_crashed_holder_control_scratch_end(self) -> None:
        path = self.copy()
        with scratch.copy_lock(path), scratch.copy_lock(path):
            self.assertIsInstance(scratch.lifetime(path), scratch.Live)
            self.assertEqual([entry.path for entry in scratch.end("gate:copy")], [path])
        program = ("import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_SH); "
                   "print('ready',flush=True); sys.stdin.read()")
        process: subprocess.Popen[str] = subprocess.Popen([sys.executable, "-c", program, str(scratch.busy_path(path))],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert process.stdout is not None
            self.assertEqual(cast(str, process.stdout.readline()).strip(), "ready")
            self.assertEqual([entry.path for entry in scratch.end("gate:copy")], [path])
        finally:
            process.kill()
            _ = process.communicate(timeout=10)
        self.assertIsInstance(self.removal_attempt(path, "gate:copy"), scratch.RemovedCopy)

    def test_a_hold_cannot_bypass_a_copys_busy_lock_or_unknown_lifetime(self) -> None:
        path = self.copy()
        _ = scratch.hold(path, "gate:copy", "copy run")
        with scratch.copy_lock(path):
            self.assertEqual([entry.path for entry in scratch.end("gate:copy")], [path])
        self.assertTrue(path.is_dir())
        _ = (path / copy_record.RECORD).write_text("bad record")
        self.assertEqual([entry.path for entry in scratch.end("gate:copy")], [path])
        self.assertTrue(path.is_dir())

    def test_missing_child_record_cannot_release_a_possible_borrowed_parent(self) -> None:
        parent = self.copy("parent", task="until:2000-01-01T0000Z")
        child = parent.parent / "child"
        child.mkdir()
        objects = child / ".git/objects/info"
        objects.mkdir(parents=True)
        _ = (objects / "alternates").write_text(str(parent / ".git/objects") + "\n")
        state = scratch.lifetime(parent)
        self.assertIsInstance(state, scratch.Unknown)
        self.assertEqual([entry.path for entry in scratch.end("until:2000-01-01T0000Z")], [parent])
        self.assertTrue(parent.is_dir())

    def test_an_ordinary_worktree_subfolder_does_not_keep_a_task_clone(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        task = "until:2000-01-01T0000Z"
        clone = copies.clone(source, task)
        worktree = copy_record.copies_root() / "ordinary-worktree"
        worktree.parent.mkdir(exist_ok=True)
        self.git(source, "worktree", "add", "--detach", str(worktree))
        settings = worktree / ".claude"
        settings.mkdir()
        _ = (settings / "settings.local.json").write_text("{}")

        self.assertIsInstance(scratch.record_lifetime(clone, time.time()), scratch.Ended)
        self.assertIsInstance(self.removal_attempt(clone, task), scratch.RemovedCopy)
        self.assertTrue(settings.is_dir())

    def test_an_unregistered_plain_folder_in_a_copies_pair_keeps_the_source(self) -> None:
        source = self.copy("source", task="until:2000-01-01T0000Z")
        candidate = source.parent / "candidate"
        candidate.mkdir()

        self.assertEqual(scratch.record_lifetime(source, time.time()),
                         scratch.Unknown(f"repair record {candidate / copy_record.RECORD}: cannot rule out borrowed objects"))

    def ordinary_worktree(self) -> Path:
        source = self.base / "worktree-source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        worktree = copy_record.copies_root() / "ordinary-worktree"
        self.git(source, "worktree", "add", "--detach", str(worktree))
        return worktree

    def test_an_unregistered_repository_with_a_common_directory_keeps_the_source(self) -> None:
        source = self.copy("source", task="until:2000-01-01T0000Z")
        candidate = self.ordinary_worktree() / "candidate"
        candidate.mkdir()
        _ = (candidate / "HEAD").write_text("ref: refs/heads/main\n")
        _ = (candidate / "commondir").write_text(str(source / ".git") + "\n")

        self.assertEqual(scratch.record_lifetime(source, time.time()),
                         scratch.Unknown(f"repair record {candidate / copy_record.RECORD}: cannot rule out borrowed objects"))

    def test_an_unregistered_git_repository_keeps_a_possible_borrowed_source(self) -> None:
        source = self.copy("source", task="until:2000-01-01T0000Z")
        worktree = self.ordinary_worktree()
        for options in ((), ("--bare",)):
            with self.subTest(options=options), tempfile.TemporaryDirectory(dir=worktree) as temporary:
                candidate = Path(temporary)
                self.git(candidate, "init", "-q", *options)
                state = scratch.record_lifetime(source, time.time())
                self.assertIsInstance(state, scratch.Unknown)
                assert isinstance(state, scratch.Unknown)
                self.assertIn(str(candidate / copy_record.RECORD), state.reason)

    def test_an_unrecorded_shared_clone_in_a_second_pair_keeps_its_source(self) -> None:
        root = copy_record.worktree_root()
        source = root / "known-pair/source"
        borrower = root / "unrecorded-pair/borrower"
        source.mkdir(parents=True)
        borrower.parent.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        scratch.atomic_record(source / copy_record.RECORD, {"task": ROW})
        self.git(root, "clone", "--shared", str(source), str(borrower))
        with mock.patch.dict(os.environ, {copy_record.COPIES_ENV: str(root)}):
            self.assertEqual(list(scratch.legacy_copy_pairs(root)), [source.parent])
            self.assertIn(borrower, list(scratch.registered_copy_paths()))
            result = scratch.end(ROW)
        self.assertEqual(result, [scratch.KeptFolder(source, f"task {ROW} ended")])
        self.assertTrue((source / ".git/objects").is_dir())

    def test_unreadable_git_metadata_keeps_a_possible_borrowed_source(self) -> None:
        source = self.copy("source", task="until:2000-01-01T0000Z")
        candidate = self.ordinary_worktree() / "candidate"
        candidate.mkdir()
        git = candidate / ".git"
        git.mkdir()
        candidate_inode = candidate.stat().st_ino
        stat_entry = os.stat

        def unreadable(path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int, *,
                       dir_fd: int | None = None, follow_symlinks: bool = True) -> os.stat_result:
            if path == ".git" and dir_fd is not None and os.fstat(dir_fd).st_ino == candidate_inode:
                raise PermissionError(f"{git}: Git metadata is unreadable")
            return stat_entry(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        with mock.patch.object(os, "stat", unreadable):
            state = scratch.record_lifetime(source, time.time())

        self.assertIsInstance(state, scratch.Unknown)
        assert isinstance(state, scratch.Unknown)
        self.assertIn(str(git), state.reason)

    def test_git_entry_links_keep_a_possible_borrowed_source(self) -> None:
        source = self.copy("source", task="until:2000-01-01T0000Z")
        candidate = self.ordinary_worktree() / "candidate"
        candidate.mkdir()
        for name in (".git", "HEAD", "objects", "commondir"):
            with self.subTest(name=name):
                entry = candidate / name
                entry.symlink_to("missing")
                self.assertEqual(scratch.record_lifetime(source, time.time()),
                                 scratch.Unknown(f"repair record {candidate / copy_record.RECORD}: cannot rule out borrowed objects"))
                entry.unlink()

    def test_a_directory_link_added_during_the_borrow_check_keeps_the_source(self) -> None:
        source = self.copy("source", task="until:2000-01-01T0000Z")
        pair = self.ordinary_worktree()
        candidate = pair / "candidate"
        candidate.mkdir()
        self.assertIsInstance(scratch.record_lifetime(source, time.time()), scratch.Ended)
        open_directory = os.open
        moved = self.base / "moved"

        for replaced, open_path in ((candidate, candidate.name), (pair, pair)):
            with self.subTest(replaced=replaced):
                def replace_with_link(path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                                      flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                    if path == open_path:
                        _ = replaced.rename(moved)
                        replaced.symlink_to(moved, target_is_directory=True)
                    return open_directory(path, flags, mode, dir_fd=dir_fd)

                with mock.patch.object(os, "open", replace_with_link):
                    state = scratch.record_lifetime(source, time.time())

                self.assertTrue(replaced.is_symlink())
                self.assertIsInstance(state, scratch.Unknown)
                replaced.unlink()
                _ = moved.rename(replaced)

    def test_registered_borrow_evidence_keeps_the_source(self) -> None:
        source = self.copy("source", task="until:2000-01-01T0000Z")
        borrower = source.parent / "borrower"
        borrower.mkdir()
        self.git(borrower, "init", "-q")
        scratch.atomic_record(borrower / copy_record.RECORD,
                              {"task": "gate:borrower", "borrows_from": str(source / ".git")})

        self.assertEqual(scratch.record_lifetime(source, time.time()),
                         scratch.Live(f"borrowed: {borrower} still uses its git objects"))

    def test_outside_checkout_does_not_inspect_the_copies_root(self) -> None:
        copies = self.base / "unrelated-copies"
        resolve = Path.resolve

        def resolve_private(path: Path, strict: bool = False) -> Path:
            self.assertNotEqual(path, copies, "An outside checkout must not inspect the copies registry")
            return resolve(path, strict)

        with (mock.patch.dict(os.environ, {"SCRATCH_COPIES_ROOT": str(copies)}),
              mock.patch.object(Path, "resolve", resolve_private), scratch.copy_lock(self.base)):
            self.assertFalse((self.base / ".claude-busy").exists())

    def test_symlinked_copies_root_registers_and_locks_the_real_copy(self) -> None:
        path = self.copy()
        alias = self.base / "tmp-link"
        alias.symlink_to(self.base / "copies", target_is_directory=True)
        with mock.patch.dict(os.environ, {copy_record.COPIES_ENV: str(alias)}):
            linked = alias / "show-maker" / "a"
            self.assertEqual(copy_record.copy_folder(linked), path)
            scratch.register_copy(linked, task="gate:copy", owner_unit="maker", source_path=str(self.base),
                                  source_commit="a", copy_commit="b", target_mode="none", seed_source="", borrows_from="")
            with scratch.copy_lock(linked), scratch.busy_path(path).open("r") as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_hand_over_clears_end_evidence_and_other_tasks_evidence_is_ignored(self) -> None:
        path = self.copy()
        with scratch.copy_lock(path):
            self.assertEqual([entry.path for entry in scratch.end("gate:copy")], [path])
        scratch.hand_over(path, "next-unit", "gate:next")
        record = scratch.task_of(path)
        assert isinstance(record, copy_record.Registered)
        self.assertNotIn("ended", record.fields)
        scratch.atomic_record(path / copy_record.RECORD, {**record.fields, "ended": 123, "ended_task": "gate:copy"})
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)

    def test_a_busy_tasks_end_evidence_survives_until_the_last_holder_exits(self) -> None:
        path = self.copy()
        with scratch.copy_lock(path):
            self.assertEqual([entry.path for entry in scratch.end("gate:copy")], [path])
            self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        record = scratch.task_of(path)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.fields["ended_task"], "gate:copy")

    def test_folders_omits_a_sibling_removed_after_its_record_was_read(self) -> None:
        vanished = scratch.new("gate:vanished", "proof")
        remaining = scratch.new("gate:remaining", "proof")
        task_of = scratch.task_of

        def remove_after_read(path: Path, *, scratch_settings: conf.ConfigRead,
        ) -> copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord:
            record = task_of(path, scratch_settings=scratch_settings)
            if path == vanished:
                (path / copy_record.RECORD).unlink()
                path.rmdir()
            return record

        with mock.patch.object(scratch, "task_of", side_effect=remove_after_read):
            paths = [folder.path for folder in scratch.folders()]
        self.assertNotIn(vanished, paths)
        self.assertIn(remaining, paths)
        self.assertFalse(vanished.exists())

    def test_end_rechecks_task_after_a_hand_over_between_snapshot_and_lock(self) -> None:
        path = self.copy(task="gate:old")
        snapshot = scratch.folders()

        def transfer(*, scratch_settings: conf.ConfigRead) -> list[scratch.Folder]:
            scratch.hand_over(path, "next-unit", "gate:next", scratch_settings=scratch_settings)
            return snapshot

        with mock.patch.object(scratch, "folders", side_effect=transfer):
            self.assertEqual([entry.path for entry in scratch.end("gate:old")], [path])
        self.assertTrue(path.is_dir())
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)

    def test_legacy_container_hold_cannot_select_other_tasks(self) -> None:
        path = self.copy(task="until:2000-01-01T0000Z")
        for held in (path.parent, path.parent.parent, self.base):
            with self.subTest(held=held):
                _ = scratch.hold(held, "gate:ancestor", "copy container")
                scratch.write_holds({str(held): scratch.Hold(
                    task="gate:ancestor", reason="old container", maker="", made=0.0, ended=0.0)})
                result = scratch.end("gate:ancestor")
                self.assertEqual([entry.path for entry in result], [held])
                self.assertTrue(path.is_dir())

    def test_end_resumes_after_the_record_was_unlinked_during_removal(self) -> None:
        path = self.copy()

        def stopped_removal(folder: Path) -> None:
            (folder / copy_record.RECORD).unlink()
            raise OSError("stopped after record unlink")

        with mock.patch.object(scratch, "remove", side_effect=stopped_removal):
            result = self.removal_attempt(path, "gate:copy")
        self.assertEqual(result, scratch.KeptFolder(path, "stopped after record unlink"))
        self.assertIsInstance(self.removal_attempt(path, "gate:copy"), scratch.RemovedCopy)
        self.assertFalse(path.exists())

    def test_all_recorder_terminal_states_end_a_phase(self) -> None:
        run = self.base / "run"
        run.mkdir()
        path = self.copy(task="phase:maker/21", run_dir=str(run), phase_id="21")
        for status in ("completed", "stopped", "error"):
            with self.subTest(status=status):
                _ = (run / "progress_history_state.json").write_text(json.dumps(
                    {"phase": {"id": "21", "status": status, "finished_at": 123}}))
                self.assertIsInstance(scratch.lifetime(path), scratch.Ended)

    def test_landing_uses_fresh_source_refs_instead_of_stale_copy_refs(self) -> None:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "commit", "--allow-empty", "-qm", "base")
        self.git(source, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git(source, "commit", "--allow-empty", "-qm", "work")
        self.git(source, "update-ref", "refs/remotes/origin/work", "HEAD")
        path = self.copy(task="landed:maker/work", land_target="origin/main")
        record = scratch.task_of(path)
        assert isinstance(record, copy_record.Registered)
        scratch.atomic_record(path / copy_record.RECORD, {**record.fields, "source_path": str(source)})
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.git(source, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        os.rename(source, self.base / "gone")
        state = scratch.lifetime(path)
        self.assertIsInstance(state, scratch.Unknown)
        assert isinstance(state, scratch.Unknown)
        self.assertIn("source", state.reason)

    def test_hand_over_validates_the_complete_new_task_without_replacing_its_id(self) -> None:
        path = self.copy()
        for task in ("run:run-id", "gate:gate-id", "row:next-C9", "until:2099-01-01T0000Z", "phase:next-unit/22"):
            with self.subTest(task=task):
                scratch.hand_over(path, "next-unit", task)
                record = scratch.task_of(path)
                assert isinstance(record, copy_record.Registered)
                self.assertEqual(record.task, task)
                self.assertEqual(record.fields["owner_unit"], "next-unit")
        before = (path / copy_record.RECORD).read_bytes()
        with self.assertRaises(scratch.ScratchError):
            scratch.hand_over(path, "next-unit", "row:invalid")
        self.assertEqual((path / copy_record.RECORD).read_bytes(), before)

    def test_lifetime_judgment_does_not_create_a_busy_mark(self) -> None:
        path = self.copy()
        (path / copy_record.RECORD).unlink()
        self.assertIsInstance(scratch.lifetime(path), scratch.Unknown)
        self.assertFalse(scratch.busy_path(path).exists())

    def test_a_held_lease_is_busy_for_judgment_and_removal(self) -> None:
        request = build_folders.UniqueBuildFolder("leased-", build_folders.ExplicitBuildParent(self.base))
        with build_folders.temporary_build_folder(request) as path:
            for lock in (scratch.removal_lock, scratch.judgment_lock):
                with self.subTest(lock=lock.__name__), lock(path) as idle:
                    self.assertFalse(idle)
            self.assertFalse(scratch.busy_path(path).exists())
            self.assertEqual(build_folders.build_folder_state(path), "held")

    def test_a_held_folders_lock_and_lease_record_are_busy_until_the_lease_is_free(self) -> None:
        request = build_folders.UniqueBuildFolder("leased-", build_folders.ExplicitBuildParent(self.base))
        with build_folders.temporary_build_folder(request) as path:
            records = [path.with_name(path.name + suffix) for suffix in (".lock", ".lease.json")]
            for record, lock in itertools.product(records, (scratch.removal_lock, scratch.judgment_lock)):
                with self.subTest(record=record.name, lock=lock.__name__), lock(record) as idle:
                    self.assertFalse(idle)
        residue = self.base / "ended-run.lease.json"
        residue.touch()
        with scratch.removal_lock(residue) as idle:
            self.assertTrue(idle)

    def test_removal_finds_a_held_lease_below_the_folder_but_judgment_does_not_walk(self) -> None:
        parent = self.base / "runs"
        nested = parent / "a" / "b"
        nested.mkdir(parents=True)
        request = build_folders.UniqueBuildFolder("run-", build_folders.ExplicitBuildParent(nested))
        with build_folders.temporary_build_folder(request) as held:
            with scratch.removal_lock(parent) as idle:
                self.assertFalse(idle)
            self.assertFalse(scratch.busy_path(parent).exists())
            with scratch.judgment_lock(parent) as idle:
                self.assertTrue(idle)
            self.assertEqual(build_folders.build_folder_state(held), "held")
        (nested / "unrelated.lock").touch()
        with scratch.removal_lock(parent) as idle:
            self.assertTrue(idle)

    def test_removal_holds_a_free_lease_until_it_ends(self) -> None:
        path = self.base / "free-lease"
        path.mkdir()
        lease_lock = path.with_name(path.name + ".lock")
        lease_lock.touch()
        with scratch.removal_lock(path) as idle:
            self.assertTrue(idle)
            with lease_lock.open("a") as allocator, self.assertRaises(BlockingIOError):
                fcntl.flock(allocator, fcntl.LOCK_SH | fcntl.LOCK_NB)
        self.assertEqual(build_folders.build_folder_state(path), "free")

    def test_judgment_of_an_unmanaged_folder_creates_no_lease_lock(self) -> None:
        path = self.base / "unmanaged"
        path.mkdir()
        with scratch.judgment_lock(path) as idle:
            self.assertTrue(idle)
        self.assertEqual(sorted(entry.name for entry in self.base.iterdir() if entry.name.startswith("unmanaged")),
                         ["unmanaged"])

    def test_removal_claims_an_absent_busy_mark_before_a_late_holder_can_start(self) -> None:
        path = self.copy()
        self.assertFalse(scratch.busy_path(path).exists())
        with scratch.removal_lock(path) as idle:
            self.assertTrue(idle)
            with scratch.busy_path(path).open("a") as late_holder:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(late_holder, fcntl.LOCK_SH | fcntl.LOCK_NB)
            scratch.remove_marked(path)
        self.assertFalse(path.exists())

    def test_holder_cannot_restart_work_after_removal_unlinks_its_busy_mark(self) -> None:
        path = self.copy()
        remove = scratch.remove

        def remove_after_late_holder_attempt(folder: Path) -> None:
            scratch.busy_path(folder).unlink()
            with self.assertRaises(scratch.ScratchError):
                with scratch.copy_lock(folder):
                    self.fail("a holder entered a copy whose removal had started")
            remove(folder)

        with scratch.removal_lock(path) as idle:
            self.assertTrue(idle)
            with mock.patch.object(scratch, "remove", side_effect=remove_after_late_holder_attempt):
                scratch.remove_marked(path)
        self.assertFalse(path.exists())

    def test_record_writers_cannot_restore_the_task_read_before_a_hand_over(self) -> None:
        for operation in ("renew", "register"):
            with self.subTest(operation=operation):
                path = self.copy(operation, task="gate:old")
                read = Event()
                resume = Event()
                publish = scratch.atomic_record
                flock = fcntl.flock
                caller = current_thread()

                def paused_publish(destination: Path, fields: object) -> None:
                    if destination == path / copy_record.RECORD and current_thread() is not caller:
                        read.set()
                        if not resume.wait(10):
                            raise AssertionError("record writer did not resume")
                    publish(destination, fields)

                def no_wait(descriptor: int, mode: int) -> None:
                    flock(descriptor, mode | fcntl.LOCK_NB)

                def write() -> None:
                    if operation == "renew":
                        scratch.renew(path, 123.0)
                    else:
                        self.register(path, "gate:old")

                with ThreadPoolExecutor(max_workers=1) as worker, mock.patch.object(scratch, "atomic_record", paused_publish):
                    writing = worker.submit(write)
                    transferred = False
                    try:
                        self.assertTrue(read.wait(10), "writer did not reach record publication")
                        # A writer that owns the record lock makes this transfer retry.
                        # Otherwise the transfer finishes while the writer has old fields.
                        with mock.patch.object(fcntl, "flock", no_wait):
                            try:
                                scratch.hand_over(path, "next-unit", "gate:next")
                                transferred = True
                            except BlockingIOError:
                                pass
                    finally:
                        resume.set()
                    writing.result(timeout=10)
                if not transferred:
                    scratch.hand_over(path, "next-unit", "gate:next")
                record = scratch.task_of(path)
                assert isinstance(record, copy_record.Registered)
                self.assertEqual(record.task, "gate:next")
                self.assertEqual(record.fields["owner_unit"], "next-unit")
                self.assertEqual(scratch.end("gate:old"), [])
                self.assertTrue(path.is_dir())

    def test_waiting_record_writer_relocks_a_replacement_and_preserves_its_hand_over(self) -> None:
        path = self.copy(task="gate:old")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        directory = os.fstat(descriptor)
        old_identity = (directory.st_dev, directory.st_ino)
        flock = fcntl.flock
        flock(descriptor, fcntl.LOCK_EX)
        waiting = Event()
        publishing = Event()
        resume = Event()
        caller = current_thread()
        locked: list[tuple[int, int]] = []
        publish = scratch.atomic_record

        def observed_flock(lock: int, mode: int) -> None:
            if current_thread() is caller:
                flock(lock, mode | fcntl.LOCK_NB)
                return
            held = os.fstat(lock)
            identity = (held.st_dev, held.st_ino)
            if identity == old_identity:
                waiting.set()
            flock(lock, mode)
            locked.append(identity)

        def paused_publish(destination: Path, fields: object) -> None:
            if destination == path / copy_record.RECORD and current_thread() is not caller:
                publishing.set()
                if not resume.wait(10):
                    raise AssertionError("waiting record writer did not resume")
            publish(destination, fields)

        with (ThreadPoolExecutor(max_workers=1) as worker,
              mock.patch.object(fcntl, "flock", observed_flock),
              mock.patch.object(scratch, "atomic_record", paused_publish)):
            writing = worker.submit(self.register, path, "gate:old")
            transferred = False
            try:
                self.assertTrue(waiting.wait(10), "writer did not open the old directory")
                scratch.remove(path)
                self.assertEqual(self.copy(task="gate:replacement"), path)
                replacement = path.lstat()
                replacement_identity = (replacement.st_dev, replacement.st_ino)
                self.assertNotEqual(replacement_identity, old_identity)
                flock(descriptor, fcntl.LOCK_UN)
                self.assertTrue(publishing.wait(10), "writer did not reach record publication")
                try:
                    scratch.hand_over(path, "next-unit", "gate:next")
                    transferred = True
                except BlockingIOError:
                    pass
            finally:
                flock(descriptor, fcntl.LOCK_UN)
                resume.set()
            writing.result(timeout=10)
        if not transferred:
            scratch.hand_over(path, "next-unit", "gate:next")
        self.assertIn(replacement_identity, locked, "writer must lock the replacement directory")
        record = scratch.task_of(path)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:next")
        self.assertEqual(record.fields["owner_unit"], "next-unit")
        self.assertEqual(scratch.end("gate:old"), [])
        self.assertTrue(path.is_dir())

    def test_reused_copy_path_does_not_inherit_an_old_removal_certificate(self) -> None:
        path = self.copy(task="gate:old")
        # Keep the removed directory's inode allocated while reusing its name.
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        remove = scratch.remove

        def stopped_after_tree_removal(folder: Path) -> None:
            remove(folder)
            raise OSError("stopped before removal certificate unlink")

        with mock.patch.object(scratch, "remove", stopped_after_tree_removal):
            result = self.removal_attempt(path, "gate:old")
        self.assertEqual(result, scratch.KeptFolder(path, "stopped before removal certificate unlink"))
        self.assertFalse(path.exists())
        self.assertTrue(scratch.removal_mark(path).is_file())
        reused = self.copy(task="gate:new")
        self.assertEqual(reused, path)
        self.assertFalse(scratch.removal_mark(path).exists())
        _ = (path / "ordinary").write_text("new owner's work")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        scratch.hand_over(path, "next-unit", "gate:next")
        self.assertEqual(scratch.end("gate:old"), [])
        self.assertTrue((path / "ordinary").is_file())

    def test_reused_copy_path_retains_unfinished_cargo_recovery(self) -> None:
        path = self.copy(task="gate:old")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        with mock.patch.object(scratch, "finish_removal", side_effect=OSError("stopped before completion")):
            self.assertIsInstance(self.removal_attempt(path, "gate:old"), scratch.KeptFolder)
        self.assertFalse(path.exists())
        cargo_mark = scratch.cargo_removal_mark(path)
        scratch.atomic_record(cargo_mark, [])
        with self.assertRaisesRegex(scratch.ScratchError, "previous copy needs Cargo recovery"):
            _ = self.copy(task="gate:new")
        self.assertTrue(path.is_dir())
        self.assertTrue(scratch.removal_mark(path).is_file())
        self.assertTrue(cargo_mark.is_file())

    def test_hand_over_replaces_phase_context_and_clears_it_for_an_external_task(self) -> None:
        old_run = self.base / "old-run"
        new_run = self.base / "new-run"
        old_run.mkdir()
        new_run.mkdir()
        for run, phase, status in ((old_run, "21", "completed"), (new_run, "22", "active")):
            record = {"id": phase, "status": status}
            state: dict[str, object] = {"open_phases": [record]} if status == "active" else {"phase": record}
            _ = (run / "progress_history_state.json").write_text(json.dumps(state))
        path = self.copy(task="phase:maker/21", run_dir=str(old_run), phase_id="21", land_target="origin/old")
        self.assertIsInstance(scratch.lifetime(path), scratch.Ended)
        scratch.hand_over(path, "next-unit", "phase:next-unit/22")
        self.assertIsInstance(scratch.lifetime(path), scratch.Unknown)
        scratch.hand_over(path, "next-unit", "phase:next-unit/22", run_dir=str(new_run))
        record = scratch.task_of(path)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.fields["run_dir"], str(new_run))
        self.assertEqual(record.fields["phase_id"], "22")
        self.assertIsInstance(scratch.lifetime(path), scratch.Live)
        self.assertEqual(scratch.end("phase:maker/21"), [])
        scratch.hand_over(path, "next-unit", "gate:next")
        record = scratch.task_of(path)
        assert isinstance(record, copy_record.Registered)
        for field in ("run_dir", "phase_id", "land_target", "ended", "ended_task"):
            self.assertNotIn(field, record.fields)

    def test_external_tasks_say_they_wait_for_an_end_instead_of_a_landing(self) -> None:
        for task in (RUN, ROW, "gate:copy"):
            with self.subTest(task=task):
                state = scratch.task_end(task)
                assert isinstance(state, scratch.Live)
                self.assertIn("has not ended", state.why)
                self.assertNotIn("landed", state.why)

    def test_same_until_value_gives_each_folder_a_task_that_ends_only_that_folder(self) -> None:
        first = scratch.new(scratch.parsed_until("2099-01-01 09:30"))
        second = scratch.new(scratch.parsed_until("2099-01-01 09:30"))
        _ = (first / "work").write_text("first work")
        _ = (second / "work").write_text("second work")
        first_record = scratch.task_of(first)
        second_record = scratch.task_of(second)
        assert isinstance(first_record, copy_record.Registered)
        assert isinstance(second_record, copy_record.Registered)
        self.assertNotEqual(first_record.task, second_record.task)
        bare_task = first_record.task.partition("/")[0]
        self.assertEqual(second_record.task.partition("/")[0], bare_task)

        self.assertEqual(scratch.end(bare_task), [])
        self.assertEqual((first / "work").read_text(), "first work")
        self.assertEqual((second / "work").read_text(), "second work")
        self.assertEqual([entry.path for entry in scratch.end(first_record.task)], [first])
        self.assertTrue(first.is_dir())
        self.assertEqual((second / "work").read_text(), "second work")
        self.assertEqual(scratch.task_of(second), second_record)

    def test_default_folders_created_at_the_same_time_have_different_tasks(self) -> None:
        first = scratch.task_of(scratch.new(now=1_000_020.0))
        second = scratch.task_of(scratch.new(now=1_000_020.0))
        assert isinstance(first, copy_record.Registered)
        assert isinstance(second, copy_record.Registered)
        self.assertNotEqual(first.task, second.task)

    def test_until_task_with_an_id_keeps_the_deadline_of_the_bare_task(self) -> None:
        bare = "until:2099-01-01T0930Z"
        bare_end = scratch.task_end(bare, now=9e12)
        assert isinstance(bare_end, scratch.Ended)
        task = bare + "/private-task"
        self.assertIsInstance(scratch.task_end(task, now=bare_end.at - 1), scratch.Live)
        ended = scratch.task_end(task, now=bare_end.at)
        assert isinstance(ended, scratch.Ended)
        self.assertEqual(ended.at, bare_end.at)
        self.assertIsInstance(scratch.task_end("until:bad/private-task"), scratch.Unknown)

    def test_until_folder_name_holds_only_the_word(self) -> None:
        path = scratch.new("until:2099-01-01T0930Z/private-task", word="proof")
        self.assertRegex(path.name, r"^proof-[a-z0-9]+$")

    def test_no_task_lasts_a_day_and_until_ends_at_its_time(self) -> None:
        path = scratch.new(now=1_000_000.0)
        record = scratch.task_of(path)
        self.assertIsInstance(record, copy_record.Registered)
        assert isinstance(record, copy_record.Registered)
        task = record.task
        deadline = scratch.task_end(task, 1_000_000.0 + conf.read("scratch").whole("default_life_seconds"))
        self.assertIsInstance(deadline, scratch.Ended)
        assert isinstance(deadline, scratch.Ended)
        self.assertEqual(deadline.at, 1_000_000.0 + conf.read("scratch").whole("default_life_seconds") - 40)
        self.assertFalse(scratch.ended(task, 1_000_000.0))
        self.assertTrue(scratch.ended(task, 1_000_000.0 + conf.read("scratch").whole("default_life_seconds")))
        self.assertFalse(scratch.ended(RUN, 9e12))
        self.assertTrue(scratch.parsed_until("2026-10-28 09:30").startswith("until:2026-10-28T"))
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.parsed_until("tomorrow")

    def test_default_task_lifetime_reads_the_private_config_at_use(self) -> None:
        with mock.patch.dict(os.environ, config_environment(self.base / "config")):
            for lifetime in (3600, 7200):
                with self.subTest(lifetime=lifetime):
                    _ = write_config_fixture(conf.path("scratch"), f"default_life_seconds={lifetime}\n")
                    path = scratch.new(now=1_000_020.0)
                    record = scratch.task_of(path)
                    self.assertIsInstance(record, copy_record.Registered)
                    assert isinstance(record, copy_record.Registered)
                    self.assertFalse(scratch.ended(record.task, 1_000_020.0 + lifetime - 1))
                    self.assertTrue(scratch.ended(record.task, 1_000_020.0 + lifetime))

    def test_repository_scan_reads_each_setting_once_across_recursive_folders(self) -> None:
        root = self.base / "repos"
        repositories = [root / name / "nested" for name in ("first", "second")]
        for repo in repositories:
            (repo / ".git").mkdir(parents=True)
        with (
            mock.patch.dict(os.environ, config_environment(self.base / "config")),
            mock.patch("scripts.lib.scratch.conf.ConfigRead.whole", side_effect=conf.read("scratch").whole) as whole,
            mock.patch("scripts.lib.scratch.conf.ConfigRead.words", side_effect=conf.read("scratch").words) as words,
        ):
            self.assertEqual(scratch.repos(root), repositories)
        self.assertEqual(whole.call_args_list, [mock.call("repo_depth")])
        self.assertEqual(words.call_args_list, [mock.call("repo_skip")])

    def test_repository_scan_depth_reads_the_private_config_at_use(self) -> None:
        root = self.base / "repos"
        nested = root / "first/second"
        (nested / ".git").mkdir(parents=True)
        with mock.patch.dict(os.environ, config_environment(self.base / "config")):
            _ = write_config_fixture(conf.path("scratch"), "repo_depth=1\n")
            self.assertEqual(scratch.repos(root), [])
            _ = write_config_fixture(conf.path("scratch"), "repo_depth=2\n")
            self.assertEqual(scratch.repos(root), [nested])

    def test_new_makes_a_recorded_folder_for_its_task(self) -> None:
        path = scratch.new(RUN, "gate")
        self.assertEqual(path.parent, self.base / "scratch")
        self.assertRegex(path.name, r"^run-2a858b68-0f9a-4af2-b3cf-a776e6b180cb-gate-[a-z0-9]{6}$")
        record = cast(dict[str, object], json.loads((path / copy_record.RECORD).read_text()))
        self.assertEqual((record["task"], record["maker"]), (RUN, MAKER_ID))

    def test_worker_folders_end_together_under_the_recorded_worker_task(self) -> None:
        parent = self.base / "worker-tmp"
        with (isolated_staff_file(self.base / "worker-staff.json", {}),
              mock.patch.dict(os.environ, source_environment(self.base / "sources")),
              isolated_source_readers(), mock.patch.dict(os.environ, {"TMPDIR": str(parent)})):
            showrunner = fixture_member("showrunner", name="sdlc")
            unit = fixture_member("unit", name="hierarchies", parent=showrunner.id)
            worker = fixture_worker("impl", unit.id)
            peer = fixture_worker("review", unit.id)
            made: list[Path] = []
            peer_paths: list[Path] = []
            jobs = ((worker, ""), (worker, "clone"), (peer, ""))
            program = Path(__file__).resolve().parents[2] / "scripts/lib/py"

            def create(job: tuple[staff.StaffRow, str]) -> subprocess.CompletedProcess[str]:
                owner, name = job
                return subprocess.run([str(program), "-m", "scripts.lib.scratch", "new", "--worker",
                                       *(["--name", name] if name else [])], capture_output=True, text=True,
                                      env={**os.environ, "CODEX_THREAD_ID": owner.current_session}, timeout=30)

            with ThreadPoolExecutor(max_workers=len(jobs)) as processes:
                results = list(processes.map(create, jobs))
            for (owner, name), result in zip(jobs, results, strict=True):
                with self.subTest(owner=owner.name, name=name):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    path = Path(result.stdout.strip())
                    self.assertEqual(path.parent, parent)
                    self.assertRegex(path.name, rf"^sdlc-hierarchies-{owner.name}-[a-z0-9]{{6}}" + ("-clone$" if name else "$"))
                    record = scratch.task_of(path)
                    self.assertIsInstance(record, copy_record.Registered)
                    assert isinstance(record, copy_record.Registered)
                    self.assertEqual((record.task, record.fields["maker"]), (scratch.worker_task(owner.id), owner.id))
                    if owner == worker:
                        _ = (path / "trial.txt").write_text("worker output")
                        made.append(path)
                    else:
                        peer_paths.append(path)
                        _ = (path / "peer.txt").write_text("peer output")
            kept = peer_paths[0]
            # Task end uses the recorded paths after TMPDIR changes.
            task = scratch.worker_task(worker.id)
            output, report = io.StringIO(), io.StringIO()
            with (mock.patch.dict(os.environ, {"TMPDIR": str(self.base / "next-tmp")}),
                  redirect_stdout(output), redirect_stderr(report)):
                self.assertEqual(scratch.main(["end", "--task", task]), 0)
            self.assertEqual(output.getvalue(), "")
            self.assertCountEqual(report.getvalue().splitlines(), [
                f"kept for a manual removal list: {path} (scratch, task {task} ended)" for path in made
            ])
            self.assertCountEqual(parent.iterdir(), [kept, *made])
            holds = scratch.holds()
            for path in made:
                self.assertEqual((path / "trial.txt").read_text(), "worker output")
                record = scratch.task_of(path)
                assert isinstance(record, copy_record.Registered)
                self.assertEqual(record.task, task)
                self.assertTrue(scratch.task_has_end_evidence(record))
                self.assertEqual(holds[str(path)]["ended"], record.fields["ended"])
            self.assertEqual((kept / "peer.txt").read_text(), "peer output")
            peer_record = scratch.task_of(kept)
            assert isinstance(peer_record, copy_record.Registered)
            self.assertEqual(peer_record.task, scratch.worker_task(peer.id))
            self.assertFalse(scratch.task_has_end_evidence(peer_record))
            self.assertEqual(holds[str(kept)]["ended"], 0)

    def test_worker_creation_refuses_a_caller_without_a_worker_row(self) -> None:
        for identity in (MAKER_ID, fixture_runtime_id("unknown-worker")):
            with self.subTest(identity=identity), mock.patch.dict(os.environ, {"CODEX_THREAD_ID": identity}), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(scratch.main(["new", "--worker"]), 2)
                self.assertEqual(errors.getvalue().splitlines(), ["scratch: --worker requires the caller's worker staff row"])

    def test_a_failed_record_write_removes_the_folder_new_made(self) -> None:
        with (
            mock.patch.object(scratch, "write_record", side_effect=OSError(errno.ENOSPC, "no space")),
            self.assertRaises(OSError),
        ):
            _ = scratch.new(RUN, "gate")
        self.assertEqual(list((self.base / "scratch").iterdir()), [])

    def test_a_visible_repro_goes_where_cargo_port_lists_it(self) -> None:
        self.assertEqual(scratch.new(ROW, "demo", visible=True), self.base / "repro" / "cargo-liner-C54-demo")
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.new(RUN, "x", visible=True)

    def test_a_malformed_task_or_name_is_refused(self) -> None:
        for task in ("run", "session:abc", "row:cargo-liner", "row:a/C1", "phase:"):
            with self.assertRaises(scratch.ScratchError, msg=task):
                _ = scratch.new(task)
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.new(RUN, "../out")

    def test_end_marks_only_the_selected_task_folders(self) -> None:
        mine = [scratch.new(ROW, "demo", visible=True), scratch.new(ROW, "copy")]
        other = scratch.new("phase:builds/7")
        loose = self.base / "scratch" / "loose"
        loose.mkdir()
        self.assertCountEqual([entry.path for entry in scratch.end(ROW)], mine)
        for path in mine:
            self.assertTrue(path.is_dir())
            record = scratch.task_of(path)
            assert isinstance(record, copy_record.Registered)
            self.assertEqual(record.fields["ended_task"], ROW)
        self.assertTrue(other.is_dir())
        self.assertTrue(loose.is_dir())

    def test_stopped_removal_keeps_its_task_record_until_the_rerun_finishes(self) -> None:
        path = scratch.new(ROW, "stopped", visible=True)
        first, second = path / "a", path / "b"
        _ = first.write_text("first child")
        _ = second.write_text("second child")
        record = (path / copy_record.RECORD).read_bytes()
        other = scratch.new(RUN, "kept")
        real_unlink = os.unlink
        real_scandir = os.scandir

        class RecordFirstEntries:
            def __init__(self, entries: list[os.DirEntry[str]]) -> None:
                self.entries: list[os.DirEntry[str]] = entries

            def __iter__(self) -> Iterator[os.DirEntry[str]]:
                return iter(self.entries)

            def __enter__(self) -> Iterator[os.DirEntry[str]]:
                return iter(self.entries)

            def __exit__(self, *error: object) -> None:
                pass

        def record_first(directory: int | str | os.PathLike[str]) -> RecordFirstEntries:
            with real_scandir(directory) as entries:
                return RecordFirstEntries(sorted(entries, key=lambda entry: (entry.name != copy_record.RECORD, entry.name)))

        def stop_after_child(child: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
            real_unlink(child, dir_fd=dir_fd)
            if Path(child).name == first.name:
                raise KeyboardInterrupt

        with (mock.patch.object(os, "scandir", side_effect=record_first),
              mock.patch.object(os, "unlink", side_effect=stop_after_child),
              self.assertRaises(KeyboardInterrupt)):
            _ = self.removal_attempt(path, ROW)
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file())
        self.assertEqual((path / copy_record.RECORD).read_bytes(), record)
        self.assertIn(path, [folder.path for folder in scratch.folders() if folder.task == ROW])
        self.assertIsInstance(self.removal_attempt(path, ROW), scratch.RemovedCopy)
        self.assertFalse(path.exists())
        self.assertFalse(scratch.removal_mark(path).exists())
        self.assertTrue(other.is_dir())

    def test_removal_rerun_finds_the_task_after_its_record_or_folder_is_deleted(self) -> None:
        for index, boundary in enumerate(("record removed", "before rmdir", "after rmdir")):
            with self.subTest(boundary=boundary):
                path = scratch.new(ROW, f"final-stop-{index}", visible=True)
                other = scratch.new(RUN, "kept")
                real_unlink = os.unlink
                real_rmdir = os.rmdir
                claimed = path.stat()

                def stop_after_record(child: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
                    real_unlink(child, dir_fd=dir_fd)
                    if boundary == "record removed" and Path(child).name == copy_record.RECORD:
                        raise KeyboardInterrupt

                def stop_at_folder(folder: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
                    source = os.path.samestat(claimed, os.stat(folder, dir_fd=dir_fd, follow_symlinks=False))
                    if source and boundary == "before rmdir":
                        raise KeyboardInterrupt
                    real_rmdir(folder, dir_fd=dir_fd)
                    if source and boundary == "after rmdir":
                        raise KeyboardInterrupt

                with (mock.patch.object(os, "unlink", side_effect=stop_after_record),
                      mock.patch.object(os, "rmdir", side_effect=stop_at_folder),
                      self.assertRaises(KeyboardInterrupt)):
                    _ = self.removal_attempt(path, ROW)
                self.assertFalse((path / copy_record.RECORD).exists())
                self.assertEqual(path.exists(), boundary != "after rmdir")
                self.assertIn(path, [folder.path for folder in scratch.folders() if folder.task == ROW])
                self.assertIsInstance(self.removal_attempt(path, ROW), scratch.RemovedCopy)
                self.assertFalse(path.exists())
                self.assertNotIn(path, [folder.path for folder in scratch.folders()])
                self.assertTrue(other.is_dir())
                self.assertEqual(list(path.parent.iterdir()), [])

    def test_end_keep_predicate_preserves_folders_and_holds_until_the_command_finishes(self) -> None:
        kept = scratch.new(RUN, "live")
        completed = scratch.new(RUN, "old")
        held = scratch.new(RUN, "held-command")
        _ = scratch.hold(held, RUN, "command cwd")
        before = scratch.holds()[str(held)].copy()
        other = scratch.new(ROW, "other")
        result = scratch.end(RUN, keep=lambda path: path in (kept, held))
        self.assertCountEqual([entry.path for entry in result], [kept, held, completed])
        self.assertTrue(kept.is_dir())
        self.assertTrue(held.is_dir())
        self.assertTrue(other.is_dir())
        self.assertEqual(scratch.holds()[str(held)], before)
        self.assertCountEqual([entry.path for entry in scratch.end(RUN)], [kept, held, completed])
        self.assertTrue(completed.is_dir())
        self.assertGreater(scratch.holds()[str(held)]["ended"], 0)
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.end("invalid-task", keep=lambda path: True)

    def test_scratch_removal_keeps_a_managed_worktree_and_numbered_copy(self) -> None:
        source_root = copy_record.worktree_root()
        self.enterContext(mock.patch.dict(os.environ, {copy_record.COPIES_ENV: str(source_root)}))
        workspace = build_folders.worktree_path("hana", "demo", "edit")
        output = build_folders.output_path("hana", "demo", "edit")
        numbered = build_folders.copy_path("hana", "demo", 1)
        legacy = source_root / "old-pair/old-copy"
        for path in (workspace, output, numbered, legacy):
            path.mkdir(parents=True)
        scratch.atomic_record(legacy / copy_record.RECORD, {"task": "gate:legacy"})
        self.assertEqual(list(scratch.legacy_copy_pairs(source_root)), [legacy.parent])
        self.assertEqual(copy_record.copy_folder(legacy / "src"), legacy)
        self.assertIsNone(copy_record.copy_folder(workspace))
        self.assertIsNone(copy_record.copy_folder(numbered))
        (workspace / "target").symlink_to(output, target_is_directory=True)
        alias = self.base / "old-worktree"
        alias.symlink_to(workspace, target_is_directory=True)
        for path in (workspace, workspace.parent, numbered, alias):
            self.assertIsInstance(scratch.remove_folder(path), scratch.KeptFolder)
            self.assertTrue(path.is_dir())
        with self.assertRaisesRegex(scratch.ScratchError, "worktrees.py make"):
            _ = scratch.adopt(workspace, ROW)
        self.assertTrue((workspace / "target").is_symlink())
        self.assertTrue(output.is_dir())

    def numbered_copy_source(self) -> Path:
        source = self.base / "source"
        source.mkdir()
        self.git(source, "init", "-q")
        self.git(source, "remote", "add", "origin", str(self.base / "unused-origin/source"))
        self.git(source, "commit", "--allow-empty", "-qm", "source")
        return source

    def test_a_numbered_copy_keeps_its_source_and_uses_its_own_lease(self) -> None:
        source = self.numbered_copy_source()
        with build_slots.take_copy(source, "HEAD", showrunner="maker", unit="demo", work="test",
                                   purpose="test lease") as copy:
            with scratch.copy_lock(copy.path):
                self.assertEqual(build_folders.build_folder_state(copy.path), "held")
            with self.assertRaisesRegex(scratch.ScratchError, "copy lease owns this record"):
                _ = scratch.update_record(copy.path, lambda _: {"task": ROW})
            for held in (copy.path, copy.path / "target"):
                with self.assertRaisesRegex(scratch.ScratchError, "the numbered copy lease owns its lifetime"):
                    _ = scratch.hold(held, ROW, "explicit cleanup")
            self.assertIsInstance(scratch.remove_folder(copy.path), scratch.KeptFolder)
        with scratch.copy_lock(copy.path):
            self.assertEqual(build_folders.build_folder_state(copy.path), "held")
        kept = scratch.remove_folder(source)
        self.assertIsInstance(kept, scratch.KeptFolder)
        assert isinstance(kept, scratch.KeptFolder)
        self.assertIn(str(copy.path), kept.reason)
        with scratch.locked_holds() as holds:
            holds[str(copy.path)] = scratch.Hold(task=ROW, maker="old owner", reason="old hold", made=0.0, ended=0.0)
            scratch.write_holds(holds)
        ended = scratch.end(ROW)
        self.assertEqual(ended, [scratch.KeptFolder(copy.path, f"task {ROW} ended")])
        self.assertTrue(copy.path.exists())
        self.assertEqual(scratch.remove_folder(copy.path),
                         scratch.KeptFolder(copy.path, "the numbered copy requires manual review"))
        self.assertTrue(copy.path.exists())
        self.assertTrue((source / ".git/objects").is_dir())

    def test_an_unreadable_numbered_copy_keeps_git_sources(self) -> None:
        source = self.numbered_copy_source()
        with build_slots.take_copy(source, "HEAD", showrunner="maker", unit="demo", work="test",
                                   purpose="test lease") as copy:
            pass
        _ = copy.path.with_name(copy.path.name + ".lease.json").write_text("invalid")
        kept = scratch.remove_folder(source)
        self.assertIsInstance(kept, scratch.KeptFolder)
        assert isinstance(kept, scratch.KeptFolder)
        self.assertIn("cannot check numbered copy source", kept.reason)
        self.assertTrue((source / ".git/objects").is_dir())

    def test_task_end_keeps_a_managed_checkout(self) -> None:
        workspace = build_folders.worktree_path("hana", "demo", "edit")
        workspace.mkdir(parents=True)
        _ = scratch.hold(workspace, ROW, "keep the managed checkout")
        data = workspace / "saved"
        _ = data.write_text("keep")
        self.assertEqual(scratch.end(ROW), [scratch.KeptFolder(workspace, f"task {ROW} ended")])
        self.assertEqual(data.read_text(), "keep")
        self.assertGreater(scratch.holds()[str(workspace)]["ended"], 0)

    def test_adopt_moves_a_loose_folder_in(self) -> None:
        loose = self.base / "rust" / "c54demo"
        (loose / "src").mkdir(parents=True)
        path = scratch.adopt(loose, ROW, "demo", visible=True)
        self.assertFalse(loose.exists())
        self.assertTrue((path / "src").is_dir())
        record = scratch.task_of(path)
        self.assertIsInstance(record, copy_record.Registered)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, ROW)

    def test_renew_moves_the_records_time(self) -> None:
        path = scratch.new(RUN)
        scratch.renew(path, now=1_000.0)
        self.assertEqual((path / copy_record.RECORD).stat().st_mtime, 1_000.0)
        with self.assertRaises(scratch.ScratchError):
            scratch.renew(self.base)

    def test_task_end_preserves_the_git_worktree_record(self) -> None:
        source = self.base / "source"
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

        def git(*arguments: str) -> None:
            _ = subprocess.run(["git", "-C", str(source), *arguments], check=True, capture_output=True,
                               env=environment)

        source.mkdir()
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        path = scratch.target(RUN, "wt", False)
        git("worktree", "add", "-q", str(path))
        scratch.write_record(path, RUN, 0.0)
        _ = (path / "work").write_text("keep this work")
        retained = scratch.end(RUN)
        self.assertEqual(retained[0].path, path)
        self.assertEqual((path / "work").read_text(), "keep this work")
        (path / "work").unlink()
        _ = scratch.end(RUN)
        self.assertTrue(path.is_dir())
        listed = subprocess.run(["git", "-C", str(source), "worktree", "list"], check=True, capture_output=True,
                                text=True, env=environment).stdout
        self.assertIn(str(path), listed)


if __name__ == "__main__":
    _ = unittest.main()


class NativeScratchMakerTests(StaffTestCase):
    def test_scratch_owner_uses_the_exact_process_registry_when_claude_exports_no_session_id(self) -> None:
        identity = fixture_runtime_id("scratch-exact-Claude-pid")
        _ = staff.ensure("showrunner", name="maker", runtime_id=identity)
        registry = Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / f"{os.getpid()}.json"
        self.write_json(registry, {"pid": os.getpid(), "procStart": sessions.process_start(os.getpid()),
                                  "sessionId": identity, "name": "same-display-name",
                                  "messagingSocketPath": str(self.root / "private.sock"), "updatedAt": 1})
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}):
            self.assertEqual(scratch.maker(), identity)
        self.write_json(registry, {"pid": os.getpid(), "procStart": "another-process-generation",
                                  "sessionId": identity, "name": "same-display-name",
                                  "messagingSocketPath": str(self.root / "private.sock"), "updatedAt": 1})
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}):
            self.assertEqual(scratch.maker(), "")

    def test_scratch_and_hold_keep_the_fixed_maker_after_a_session_handoff(self) -> None:
        owner = staff.ensure("showrunner", name="maker", runtime_id=MAKER_ID)
        current = staff.native_id(fixture_runtime_id("scratch-replacement-thread"))
        _ = staff.update_current_session(owner.current_session, current)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with mock.patch.dict(os.environ, {
            "CODEX_THREAD_ID": current, copy_record.ROOT_ENV: str(self.root / "scratch"),
            scratch.HOLDS_ENV: str(self.root / "holds.json"),
        }):
            path = scratch.new(RUN, "handoff")
            record = scratch.task_of(path)
            self.assertIsInstance(record, copy_record.Registered)
            assert isinstance(record, copy_record.Registered)
            self.assertEqual(record.fields["maker"], owner.id)
            held = scratch.new("gate:maker-handoff", "held")
            _ = scratch.hold(held, "gate:maker-handoff", "retain the fixed owner")
            self.assertEqual(scratch.holds()[str(held)]["maker"], owner.id)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": owner.id}):
            self.assertEqual(scratch.maker(), "")
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_unpublished_runtime_has_no_scratch_owner_and_creates_no_staff_store(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": MAKER_ID}):
            self.assertEqual(scratch.maker(), "")
        self.assertFalse(self.staff_file.exists())
