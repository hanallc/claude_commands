"""Test folder cleanup at normal exit and after a stopped test run."""
from __future__ import annotations

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

from scripts.lib import build_folders, testing_folders
from scripts.lib.checkout_test_support import (STANDALONE_TEST_STARTUP, direct_test_fixture_environment,
                                              run_gate_test_fixture, without_user_test_manager)
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
from scripts.lib import build_folders
class Fixture(unittest.TestCase):
    def test_folder(self):
        print('TMPDIR=' + os.environ['TMPDIR'], flush=True)
        self.assertNotIn('GATE_PARENT_PID', os.environ)
        self.assertNotIn('MERGE_GATE_PARENT_PID', os.environ)
"""
REPO = Path(__file__).resolve().parents[2]
MODULE_RUN = REPO / "scripts/test_times/module_run.py"
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

    def start(self, *options: str) -> tuple[subprocess.Popen[str], Path, Path, int]:
        environment = without_user_test_manager(self.root, self.environment())
        process = subprocess.Popen([str(SHIM), *options, "-B", "-c", DISCOVER_STANDALONE, "discover", "-s", str(self.root),
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
        child_source = """import os, pathlib, unittest
class Child(unittest.TestCase):
    def test_folder(self):
        path = pathlib.Path(os.environ['TMPDIR'])
        self.assertEqual(path.parent.name, 'test-runs')
        self.assertTrue(path.is_dir())
"""
        _ = (self.root / "test_child.py").write_text(child_source)
        source = GATE_FIXTURE + f"""        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temporary:
            child = subprocess.run(
                [{str(SHIM)!r}, '-m', 'unittest', 'discover', '-s', {str(self.root)!r}, '-p', 'test_child.py'],
                env={{**os.environ, 'TMPDIR': temporary}},
                capture_output=True, text=True, timeout={TEST_HANG_GUARD_SECS!r})
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
"""
        case = self.root / "test_gate_fixture.py"
        _ = case.write_text(source)
        environment = {**self.environment(), "PYTHONPATH": str(self.root)}
        run = run_gate_test_fixture(case, [str(SHIM), str(MODULE_RUN), str(self.root), case.name], environment)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn(f"TMPDIR={self.root}\n", run.stdout)
        self.assertFalse((self.root / "test-runs").exists())

    def test_a_run_without_a_build_log_keeps_its_log_in_the_run_folder(self) -> None:
        for caller in ("", str(self.root / "caller-buildlog")):
            with self.subTest(caller=caller), mock.patch.dict(os.environ, self.environment()):
                _ = os.environ.pop("BUILDLOG_DIR", None)
                if caller:
                    os.environ["BUILDLOG_DIR"] = caller
                with testing_folders.test_run_folder() as path:
                    self.assertEqual(os.environ["BUILDLOG_DIR"], caller or str(path / "buildlog"))
                self.assertEqual(os.environ.get("BUILDLOG_DIR"), caller or None)

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
