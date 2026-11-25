"""Test folder cleanup at normal exit and after a stopped test run."""
from __future__ import annotations

import atexit
from collections.abc import Callable
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from typing import cast, override
from unittest import mock

from scripts.lib import build_folders, session_folder, testing_folders, unit_runs
from scripts.lib.checkout_test_support import (STANDALONE_TEST_STARTUP, copy_test_runner,
                                              direct_test_fixture_environment, run_gate_test_fixture,
                                              without_user_test_manager)
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


SHIM = Path(__file__).with_name("py")
FIXTURE = """import json, os, pathlib, sys, unittest, uuid
class Fixture(unittest.TestCase):
    def test_folder(self):
        path = pathlib.Path(os.environ['TMPDIR']) / ('leftover-' + uuid.uuid4().hex)
        path.mkdir()
        (path / 'output').write_text('test output')
        print(json.dumps([os.environ['TMPDIR'], str(path), os.getpid()]), flush=True)
        sys.stdin.read(1)
"""
GATE_FIXTURE = """import os, subprocess, tempfile, unittest
from pathlib import Path
from scripts.lib import build_folders
class Fixture(unittest.TestCase):
    def test_folder(self):
        print('TMPDIR=' + os.environ['TMPDIR'], flush=True)
        self.assertNotIn('GATE_PARENT_PID', os.environ)
        self.assertNotIn('MERGE_GATE_PARENT_PID', os.environ)
        for name in ('CONF_READ_LOG', 'SCRATCH_ROOT', 'ASTRA_REVIEW_DIR'):
            self.assertEqual(Path(os.environ[name]).parent.parent, Path(os.environ['TMPDIR']))
"""
DISCOVER_STANDALONE = STANDALONE_TEST_STARTUP + """
import runpy
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
"""


class TestRunFolderTests(unittest.TestCase):
    root: Path = Path()
    fixture_environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = (self.root / "test_fixture.py").write_text(FIXTURE)
        self.fixture_environment = direct_test_fixture_environment(self.root, seconds=15)

    def environment(self) -> dict[str, str]:
        return self.fixture_environment.copy()

    def start(self, *options: str, shim: Path = SHIM) -> tuple[subprocess.Popen[str], Path, Path, int]:
        environment = without_user_test_manager(self.root, self.environment())
        process = subprocess.Popen([str(shim), *options, "-B", "-c", DISCOVER_STANDALONE, "discover", "-s", str(self.root),
                                    "-p", "test_fixture.py"],
                                   cwd=self.root, env=environment, text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, process)
        assert process.stdout is not None
        with selectors.DefaultSelector() as ready:
            _ = ready.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(TEST_HANG_GUARD_SECS), "The test did not report its folder.")
        line = cast(str, process.stdout.readline())
        if not line:
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.fail("The test exited before it reported its folder: " + output + errors)
        folder, child, pid = cast(tuple[str, str, int], json.loads(line))
        return process, Path(folder), Path(child), pid

    def stop(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def checkout_shim(self, checkout: Path) -> Path:
        copy_test_runner(checkout)
        return checkout / "scripts/lib/py"

    def assert_run_uses_output(self, shim: Path, output_folder: Path) -> None:
        process, folder, child, _ = self.start(shim=shim)
        self.assertTrue(child.is_dir())
        output, errors = process.communicate("x", timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(folder.parent, output_folder / "test-runs")
        self.assertFalse(folder.exists())
        self.assertEqual(list(folder.parent.iterdir()), [])

    def test_a_worktree_run_uses_its_output_and_removes_its_run_folder(self) -> None:
        checkout = self.root / "worktrees/claude/verify/work"
        output = self.root / "build/claude/verify/work"
        shim = self.checkout_shim(checkout)
        output.mkdir(parents=True)
        (checkout / "target").symlink_to(output, target_is_directory=True)
        self.assert_run_uses_output(shim, output)

    def test_a_worktree_run_keeps_state_in_each_session_root(self) -> None:
        checkout = self.root / "worktrees/claude/verify/work"
        output = self.root / "build/claude/verify/work"
        shim = self.checkout_shim(checkout)
        output.mkdir(parents=True)
        (checkout / "target").symlink_to(output, target_is_directory=True)
        current = self.root / "session"
        former = self.root / "former"
        roots = {session_folder.ROOT_ENV: str(current), session_folder.FORMER_ROOT_ENV: str(former)}
        for session in (current / "verify", former / "abc-123"):
            with self.subTest(session=session):
                scratchpad = session / "claude-1000" / "note.txt"
                scratchpad.parent.mkdir(parents=True)
                _ = scratchpad.write_text("session state")
                record = session_folder.FolderRecord(owner="verify", session_id="abc-123")
                session_folder.write_record(session, record)
                with mock.patch.dict(self.fixture_environment, {**roots, "TMPDIR": str(session)}):
                    self.assert_run_uses_output(shim, output)
                self.assertFalse((session / "test-runs").exists())
                self.assertEqual(scratchpad.read_text(), "session state")
                self.assertEqual(session_folder.read_record(session), record)

    def test_an_unassigned_checkout_refuses_session_and_shared_build_parents(self) -> None:
        checkout = self.root / "unassigned"
        checkout.mkdir()
        build = self.root / "build"
        current = self.root / "session"
        former = self.root / "former"
        unit_root = self.root / "unit"
        former_unit_root = self.root / "delegate"
        old_run = build / "test-runs" / "r-old"
        alias = self.root / "session-alias"
        alias.symlink_to(current / "verify", target_is_directory=True)
        roots = {session_folder.ROOT_ENV: str(current), session_folder.FORMER_ROOT_ENV: str(former)}
        self.enterContext(mock.patch.multiple(unit_runs, RUN_ROOT=unit_root, FORMER_RUN_ROOT=former_unit_root))
        for inherited, marker in ((current / "verify", ""), (former / "abc-123", ""),
                                  (unit_runs.run_temp_path(unit_root / "run-id"), ""),
                                  (unit_runs.run_temp_path(former_unit_root / "run-id"), ""),
                                  (build, ""), (old_run, str(old_run)), (alias, "")):
            with self.subTest(inherited=inherited), mock.patch.dict(os.environ, {
                **self.environment(), **roots, "TMPDIR": str(inherited), testing_folders.RUN_FOLDER_ENV: marker,
            }):
                with self.assertRaisesRegex(build_folders.InvalidFolderForm, "require an assigned build output"):
                    _ = testing_folders.prepare_test_runs_parent(checkout)
        for parent in (current, former, unit_root, former_unit_root, build):
            self.assertFalse(parent.exists())

    def test_a_numbered_copy_run_uses_its_own_target(self) -> None:
        checkout = self.root / "build/claude/verify/7"
        self.assert_run_uses_output(self.checkout_shim(checkout), checkout / "target")

    def test_a_legacy_main_run_moves_when_it_gets_its_model_link(self) -> None:
        checkout = self.root / "home/.claude"
        shim = self.checkout_shim(checkout)
        self.assert_run_uses_output(shim, self.root)
        outside = self.root / "outside"
        outside.mkdir()
        target = checkout / "target"
        target.symlink_to(outside, target_is_directory=True)
        self.assert_run_uses_output(shim, self.root)
        self.assertFalse((outside / "test-runs").exists())
        target.unlink()
        output = self.root / "build/claude/main"
        output.mkdir(parents=True)
        target.symlink_to(output, target_is_directory=True)
        self.assert_run_uses_output(shim, output)

    def test_direct_run_disables_bytecode_for_the_test_and_its_child(self) -> None:
        case = self.root / "test_bytecode.py"
        _ = case.write_text(f"""import subprocess, sys, unittest
class Bytecode(unittest.TestCase):
    def test_child(self):
        child = subprocess.check_output(
            [sys.executable, "-c", "import os; print(os.environ.get('PYTHONDONTWRITEBYTECODE'))"],
            text=True, timeout={TEST_HANG_GUARD_SECS!r})
        self.assertEqual(child.strip(), "1")
        self.assertTrue(sys.dont_write_bytecode)
""")
        environment = without_user_test_manager(self.root, self.environment())
        _ = environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment["PYTHONPYCACHEPREFIX"] = str(self.root / "bytecode")
        result = subprocess.run([str(SHIM), "-c", DISCOVER_STANDALONE, "discover", "-s", str(self.root), "-p", case.name],
                                cwd=self.root, env=environment, capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bootstrap_removes_the_run_folder_on_normal_exit(self) -> None:
        for options in ((), ("--isolated",)):
            with self.subTest(options=options):
                process, folder, child, _ = self.start(*options)
                self.assertEqual(folder.parent, self.root / "test-runs")
                self.assertTrue(child.is_relative_to(folder))
                self.assertEqual(build_folders.build_folder_state(folder), "held")
                output, errors = process.communicate("x", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertFalse(folder.exists())
                self.assertEqual(list(folder.parent.iterdir()), [])

    def test_the_next_run_removes_killed_runs_and_keeps_a_live_run(self) -> None:
        live, live_folder, live_child, _ = self.start()
        for number in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(number=number):
                stopped, folder, child, pid = self.start()
                self.assertTrue(live_child.is_dir())
                os.kill(pid, number)
                _ = stopped.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(stopped.returncode, -number)
                self.assertTrue(child.is_dir())
                self.assertEqual(build_folders.build_folder_state(folder), "free")
                following, following_folder, _, _ = self.start()
                self.assertFalse(folder.exists())
                self.assertFalse(folder.with_name(folder.name + ".lease.json").exists())
                self.assertFalse(folder.with_name(folder.name + ".lock").exists())
                self.assertEqual(build_folders.build_folder_state(live_folder), "held")
                self.assertTrue(live_child.is_dir())
                output, errors = following.communicate("x", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(following.returncode, 0, output + errors)
                self.assertFalse(following_folder.exists())
        output, errors = live.communicate("x", timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(live.returncode, 0, output + errors)
        self.assertEqual(list(live_folder.parent.iterdir()), [])

    def test_a_gate_job_keeps_its_folder_when_a_test_imports_the_maker(self) -> None:
        # On Linux the child finds the gate module among its ancestors and keeps the folder that the
        # gate job gives it. On other systems the child cannot read its ancestors, so it is a direct
        # test and makes its own run folder. The timed runner keeps its folders in the gate's TMPDIR,
        # also when the checkout has a model output.
        checkout = self.root / "worktrees/claude/verify/gate"
        output = self.root / "build/claude/verify/gate"
        shim = self.checkout_shim(checkout)
        output.mkdir(parents=True)
        (checkout / "target").symlink_to(output, target_is_directory=True)
        child_source = """import os, pathlib, sys, unittest
class Child(unittest.TestCase):
    def test_folder(self):
        path = pathlib.Path(os.environ['TMPDIR'])
        given = pathlib.Path(os.environ['FIXTURE_GIVEN_TMPDIR'])
        if sys.platform == 'linux':
            self.assertEqual(path, given)
        else:
            self.assertEqual(path.parent, given / 'test-runs')
        self.assertTrue(path.is_dir())
"""
        _ = (self.root / "test_child.py").write_text(child_source)
        source = GATE_FIXTURE + f"""        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temporary:
            child = subprocess.run(
                [{str(SHIM)!r}, '-m', 'unittest', 'discover', '-s', {str(self.root)!r}, '-p', 'test_child.py'],
                env={{**os.environ, 'TMPDIR': temporary, 'FIXTURE_GIVEN_TMPDIR': temporary}},
                capture_output=True, text=True, timeout={TEST_HANG_GUARD_SECS!r})
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
"""
        case = checkout / "test_gate_fixture.py"
        _ = case.write_text(source)
        environment = {**self.environment(), "PYTHONPATH": str(self.root)}
        arguments = [str(shim), str(checkout / "scripts/test_times/module_run.py"), str(checkout), case.name]
        run = run_gate_test_fixture(case, arguments, environment)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn(f"TMPDIR={self.root}\n", run.stdout)
        self.assertFalse((self.root / "test-runs").exists())
        self.assertFalse((output / "test-runs").exists())

    def test_a_run_without_a_build_log_keeps_its_log_in_the_run_folder(self) -> None:
        for caller in ("", str(self.root / "caller-buildlog")):
            finishes: list[Callable[[], None]] = []
            with (self.subTest(caller=caller), mock.patch.dict(os.environ, self.environment()),
                  mock.patch.object(atexit, "register", finishes.append)):
                _ = os.environ.pop("BUILDLOG_DIR", None)
                if caller:
                    os.environ["BUILDLOG_DIR"] = caller
                # The targeted gate holds a run folder and keeps the caller's build log.
                with testing_folders.test_run_folder():
                    self.assertEqual(os.environ.get("BUILDLOG_DIR"), caller or None)
                testing_folders.start_test_run()
                path = Path(os.environ[testing_folders.RUN_FOLDER_ENV])
                self.assertEqual(os.environ["BUILDLOG_DIR"], caller or str(path / "buildlog"))
                finish, = finishes
                finish()
                self.assertEqual(os.environ.get("BUILDLOG_DIR"), caller or None)
                self.assertFalse(path.exists())

    def test_a_run_has_an_absent_session_root_and_restores_the_callers(self) -> None:
        for caller in ("", str(self.root / "caller-session")):
            with self.subTest(caller=caller), mock.patch.dict(os.environ, self.environment()):
                _ = os.environ.pop("SESSION_TEMP_ROOT", None)
                if caller:
                    os.environ["SESSION_TEMP_ROOT"] = caller
                with testing_folders.test_run_folder() as path:
                    self.assertEqual(os.environ["SESSION_TEMP_ROOT"], str(path / "session"))
                    self.assertFalse((path / "session").exists())
                self.assertEqual(os.environ.get("SESSION_TEMP_ROOT"), caller or None)

    def test_the_context_resets_tempfile_and_restores_the_callers_environment(self) -> None:
        with mock.patch.dict(os.environ, self.environment()), mock.patch.object(tempfile, "tempdir", str(self.root)):
            with testing_folders.test_run_folder() as path:
                self.assertEqual(tempfile.gettempdir(), str(path))
                child = subprocess.run([sys.executable, "-c", "import os; print(os.environ['TMPDIR'])"],
                                       text=True, capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(child.stdout.strip(), str(path))
                with testing_folders.test_run_folder() as nested:
                    self.assertEqual(nested.parent, path.parent)
                    self.assertNotEqual(nested, path)
                    self.assertEqual(build_folders.build_folder_state(path), "held")
                self.assertEqual(os.environ["TMPDIR"], str(path))
            self.assertEqual(os.environ["TMPDIR"], str(self.root))
            self.assertEqual(tempfile.tempdir, str(self.root))
            self.assertFalse(path.exists())
