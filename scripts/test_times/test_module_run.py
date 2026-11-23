"""Compare the timed runner with unittest on isolated test modules."""

from __future__ import annotations

from scripts.lib.checkout_test_support import copy_test_runner

from collections.abc import Callable
from contextlib import ExitStack, chdir, redirect_stderr
from io import StringIO
from dataclasses import dataclass
import errno
from itertools import product
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
from typing import ClassVar, cast, override
import unittest
from unittest.mock import patch


from scripts.lib import conf
from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message.process_test_support import OwnedTestProcesses
from scripts.production import gate
from scripts.test_times import module_run
from scripts.test_times.gate_record import ModuleTimes
from scripts.test_times.module_run import FixtureMeasure, TestMeasure

RUNNER = Path(__file__).with_name("module_run.py").resolve()

OUTCOMES = '''\
import os
import unittest
import warnings
from fixture_root import VALUE

class Outcomes(unittest.TestCase):
    def test_pass(self) -> None:
        self.assertEqual(VALUE, 42)
        if os.environ.get("CHECK_REMOVED"):
            self.assertNotIn("TEST_TIMES_OUT", os.environ)

    def test_fail(self) -> None:
        self.fail("fixture failure")

    def test_error(self) -> None:
        raise RuntimeError("fixture error")

    @unittest.skip("fixture skip")
    def test_skip(self) -> None:
        self.fail("A skipped test must not run")

    @unittest.expectedFailure
    def test_expected_failure(self) -> None:
        self.fail("expected fixture failure")

    @unittest.expectedFailure
    def test_unexpected_success(self) -> None:
        pass

    def test_subtest_fail(self) -> None:
        with self.subTest(part="fail"):
            self.fail("subtest failure")
        with self.subTest(part="pass"):
            pass

    def test_subtest_error(self) -> None:
        with self.subTest(part="error"):
            raise ValueError("subtest error")
        with self.subTest(part="fail"):
            self.fail("second subtest failure")
        with self.subTest(part="skip"):
            self.skipTest("subtest skip")
        with self.subTest(part="pass"):
            pass

    def test_warning(self) -> None:
        warnings.warn("fixture warning", DeprecationWarning)

class BrokenClass(unittest.TestCase):
    @classmethod
    def setUpClass(cls: type["BrokenClass"]) -> None:
        raise RuntimeError("fixture class error")

    def test_unreached(self) -> None:
        pass
'''

SUCCESS = '''\
import unittest
from fixture_root import VALUE

class RootImport(unittest.TestCase):
    def test_value(self) -> None:
        self.assertEqual(VALUE, 42)
'''

DISCOVERY_BOUNDARIES = '''\
import os
import unittest

IMPORTED_OUTPUT = os.environ.pop("TEST_TIMES_OUT", None)

class DiscoveryBoundaries(unittest.TestCase):
    test_case: None = None

    def test_failure_with_test_case_attribute(self) -> None:
        self.fail("fixture attribute failure")

    def test_import_environment(self) -> None:
        self.assertIsNone(IMPORTED_OUTPUT, "Discovery must not receive the output path")
'''


ORPHAN_PROCESS = '''\
import os
import select
import signal
import sys
import unittest

class OrphanProcess(unittest.TestCase):
    def test_orphan(self):
        reader, writer = os.pipe()
        intermediate = os.fork()
        if intermediate == 0:
            os.close(reader)
            if os.fork() != 0:
                os._exit(0)
            os.setsid()
            os.write(writer, str(os.getpid()).encode())
            os.close(writer)
            for descriptor in (0, 1, 2):
                os.close(descriptor)
            while True:
                signal.pause()
        os.close(writer)
        pid = int(os.read(reader, 32))
        os.close(reader)
        os.waitpid(intermediate, 0)
        descriptor = os.pidfd_open(pid)
        try:
            print(pid, flush=True)
            sys.stdin.buffer.read(1)
            if __REAP__:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                reaped = select.poll()
                reaped.register(descriptor, select.POLLHUP)
                self.assertTrue(reaped.poll(__HANG_GUARD_MS__), "The orphan remains unreaped")
            if __FAIL__:
                self.fail("fixture failure after orphan launch")
        finally:
            os.close(descriptor)
'''


SENDER_REQUEST = '''\
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from scripts.message import session_send
from scripts.message import session_send_test_support

def setUpModule():
    previous_guard = os.environ.get("TEST_SENDER_GUARD")
    temporary = Path(os.environ["TMPDIR"])
    def check_cleanup():
        assert isinstance(session_send_test_support.refusal_marker, session_send_test_support.SenderGuardNotInstalled)
        assert os.environ.get("TEST_SENDER_GUARD") == previous_guard
        assert not list(temporary.glob("sender-guard-*"))
    unittest.addModuleCleanup(check_cleanup)
    session_send_test_support.forbid_real_sender()
    sys.addaudithook(last_guard)
    if MODE == "setup_exit":
        subprocess.run([python, str(real_sender), "--help"], check=False)
        raise SystemExit(0)

real_sender = Path(session_send.__file__).resolve().with_name("send.py")
python = str(real_sender.parents[1] / "lib/py")

# This safety hook permits only harmless probes and blocks network delivery.
def last_guard(event, arguments):
    if event in ("socket.connect", "socket.sendto", "socket.bind"):
        raise RuntimeError("the sender probe attempted network access")
    if event in ("subprocess.Popen", "os.posix_spawn", "os.exec"):
        command = arguments[1]
    elif event == "os.spawn":
        command = arguments[2]
    elif event == "os.system":
        command = arguments[0]
    else:
        return
    if str(Path(__file__).with_name("fake_sender.py")) not in str(command) and "--help" not in str(command):
        raise RuntimeError("the sender probe attempted a live command")


MODE = __MODE__

class SenderRequest(unittest.TestCase):
    def test_sender(self):
        if MODE == "fake":
            fake = Path(__file__).with_name("fake_sender.py")
            fake.write_text("print('fake sender ran')\\n")
            with mock.patch.object(session_send, "SEND_SCRIPT", fake):
                result = subprocess.run(session_send.session_send_argv("fixture"),
                                        capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "fake sender ran\\n")
        elif MODE == "cleared_environment":
            child_environment = os.environ.copy()
            marker = session_send_test_support.refusal_marker
            # This isolated process keeps its environment empty through runner completion.
            mock.patch.dict(os.environ, {}, clear=True).start()
            session_send_test_support.forbid_real_sender()
            self.assertIs(session_send_test_support.refusal_marker, marker)
            result = subprocess.run([python, str(real_sender), "--help"],
                                    env=child_environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.environ, {})
            self.assertIsInstance(marker, session_send_test_support.SenderRefusalMarker)
            self.assertEqual(marker.path.read_text(), "real sender refused\\n")
        elif MODE == "caught":
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run([python, str(real_sender), "--help"], check=True)
        else:
            if MODE == "cd":
                command = f"cd {shlex.quote(str(real_sender.parent))} && {shlex.quote(python)} send.py --help"
                directory = None
            elif MODE == "module":
                command = [python, "-m", "scripts.message.send", "--help"]
                directory = real_sender.parents[2]
            else:
                command = [python, str(real_sender), "--help"]
                directory = None
            result = subprocess.run(command, cwd=directory, shell=MODE == "cd", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

if MODE == "expected_failure":
    SenderRequest.test_sender = unittest.expectedFailure(SenderRequest.test_sender)
'''


@dataclass(frozen=True)
class UnwrittenFixtureTimes:
    """The fixture exited before the runner wrote its times."""


@dataclass(frozen=True)
class FixtureRun:
    timed: subprocess.CompletedProcess[str]
    standard: subprocess.CompletedProcess[str]
    recorded_times: ModuleTimes | UnwrittenFixtureTimes
    standard_sender_folders: list[str]
    timed_sender_folders: list[str]

    @property
    def times(self) -> ModuleTimes:
        if isinstance(self.recorded_times, UnwrittenFixtureTimes):
            raise AssertionError("The fixture exited without a timing report")
        return self.recorded_times


def read_times(path: Path) -> ModuleTimes:
    """Read output without using the product's reader."""
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise AssertionError("The runner must write a JSON object")
    return cast(ModuleTimes, cast(object, value))


def normalized_output(text: str) -> str:
    return re.sub(r"Ran (\d+) tests? in [0-9.]+s", r"Ran \1 tests in <time>s", text)


def run_fixture(root: Path, name: str, source: str) -> FixtureRun:
    folder = root / name
    folder.mkdir()
    _ = (folder / "test_fixture.py").write_text(textwrap.dedent(source), encoding="utf-8")
    output = root / f"{name}.json"
    _ = output.write_text("stale output\n", encoding="utf-8")
    env = dict(os.environ)
    for key in ("TEST_TIMES_OUT", "PYTHONPATH", "PYTHONWARNINGS", "TEST_SENDER_GUARD"):
        _ = env.pop(key, None)
    temporary = root / f"{name}_temporary"
    temporary.mkdir()
    env.update({**interpreter_cache_environment(root / "cache"), "HOME": str(root / "home"), "BUILDLOG_DIR": str(root / "buildlog"),
                "CONF_DIR": str(root / "config"), "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join((str(root), str(folder))), "TMPDIR": str(temporary)})
    args = [f"{name}.test_fixture"]
    standard = subprocess.run(
        [str(RUNNER.parents[1] / "lib/py"), "-m", "unittest", *args], cwd=root, env=env,
        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
    )
    standard_sender_folders = [path.name for path in temporary.glob("sender-guard-*")]
    timed = subprocess.run(
        [str(RUNNER.parents[1] / "lib/py"), str(RUNNER), str(folder), "test_fixture.py"], cwd=root,
        env={**env, "TEST_TIMES_OUT": str(output), "CHECK_REMOVED": "1"},
        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
    )
    timed_sender_folders = [path.name for path in temporary.glob("sender-guard-*")]
    times = UnwrittenFixtureTimes() if output.read_text() == "stale output\n" else read_times(output)
    return FixtureRun(timed, standard, times, standard_sender_folders, timed_sender_folders)


class LiveConfigGuardTests(unittest.TestCase):
    def test_child_guard_allows_private_cleanup_and_relative_open(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        protected, private, alias = (root / name for name in ("config", "private", "alias"))
        protected.mkdir()
        private.mkdir()
        alias.symlink_to(protected, target_is_directory=True)
        log = root / "violations"
        child = """
import os
import shutil

assert os.open in os.supports_dir_fd
assert shutil.rmtree.avoids_symlink_attacks
fd = os.open("../private", os.O_RDONLY)
os.close(os.open("fixture", os.O_WRONLY | os.O_CREAT, dir_fd=fd))
os.close(fd)
shutil.rmtree("../private")
try:
    shutil.rmtree("../alias")
except OSError:
    pass
else:
    raise AssertionError("rmtree accepted a symlink")
"""
        with module_run.guard_live_config(protected, root, log):
            result = subprocess.run([sys.executable, "-c", child], cwd=protected, capture_output=True, text=True,
                                    timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(private.exists())
        self.assertTrue(alias.is_symlink())
        self.assertFalse(log.exists())

    def test_child_guard_preserves_checkout_startup_and_next_hook(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        library = RUNNER.parents[1] / "lib"
        startup = root / "guard"
        startup.mkdir()
        following = root / "following"
        following.mkdir()
        _ = (following / "sitecustomize.py").write_text(
            "import os\nos.environ['CONFIG_TEST_NEXT_HOOK'] = 'loaded'\n", encoding="utf-8")
        child = """
import os
import runpy
import sys

runpy.run_path(sys.argv[1])
print(os.environ['CONFIG_TEST_NEXT_HOOK'])
"""
        environment = {"PYTHONPATH": os.pathsep.join((str(library / "python_startup"), str(following))),
                       "CLAUDE_PYTHON": sys.executable}
        with patch.dict(os.environ, environment), module_run.guard_live_config(
                root / "config", startup, root / "violations"):
            for program in (sys.executable, str(library / "py")):
                with self.subTest(program=program):
                    result = subprocess.run([program, "-c", child, str(library / "package_root.py")], cwd=root,
                                            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "loaded\n")
                    self.assertEqual(result.stderr, "")

    def test_runner_rejects_a_caught_write_or_read_in_a_python_child(self) -> None:
        for access in ('Path(sys.argv[1]).write_text("changed")', "Path(sys.argv[1]).read_text()"):
            with self.subTest(access=access):
                # Each run keeps its hook in this process, so each run has its own root.
                root = Path(self.enterContext(tempfile.TemporaryDirectory()))
                protected = root / "fixture-home" / ".claude" / "config"
                protected.mkdir(parents=True)
                config = protected / "console.conf"
                _ = config.write_text("original\n")
                child = f"""
from pathlib import Path
import sys
try:
    {access}
except AssertionError:
    print("refused")
"""
                children: list[subprocess.CompletedProcess[str]] = []

                def fixture_test() -> None:
                    shim = Path(module_run.__file__).resolve().parents[1] / "lib" / "py"
                    for program in (sys.executable, str(shim)):
                        children.append(subprocess.run([program, "-c", child, str(config)], cwd=root,
                                                       capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS))

                suite = unittest.TestSuite([unittest.FunctionTestCase(fixture_test)])
                with (patch.object(module_run, "live_config_folder", return_value=protected),
                      patch.dict(os.environ, {"CLAUDE_PYTHON": sys.executable}),
                      patch.object(sys, "argv", ["module_run.py", str(root), "test_*.py"]),
                      patch.object(unittest.defaultTestLoader, "loadTestsFromNames", return_value=suite),
                      redirect_stderr(StringIO())):
                    with self.assertRaisesRegex(AssertionError, "Test attempted to read or write live configuration"):
                        module_run.run_module(module_run.NoConfigReadLog(), root, root / "violations")
                self.assertEqual([(child.returncode, child.stdout, child.stderr) for child in children],
                                 [(0, "refused\n", ""), (0, "refused\n", "")])
                self.assertEqual(config.read_text(), "original\n")

    def test_guard_refuses_reads_in_the_live_folder_and_allows_others(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        protected = root / "config"
        protected.mkdir()
        config = protected / "console.conf"
        _ = config.write_text("live\n")
        private = root / "private.conf"
        _ = private.write_text("private\n")
        (root / "alias").symlink_to(protected, target_is_directory=True)
        (root / "link.conf").symlink_to(config)
        (root / "nested").mkdir()
        # The AWS guest links the live folder to the config/ of the checkout under test.
        checkout = root / "checkout" / "config"
        checkout.mkdir(parents=True)
        tested = checkout / "gate.conf"
        _ = tested.write_text("checkout\n")
        linked = root / "home" / ".claude" / "config"
        linked.parent.mkdir(parents=True)
        linked.symlink_to(checkout, target_is_directory=True)

        def relative() -> str:
            with chdir(root):
                return Path("config/console.conf").read_text()

        def relative_from_the_folder() -> str:
            with chdir(protected):
                return Path("console.conf").read_text()

        def relative_open(mode: str) -> Callable[[], str]:
            def access() -> str:
                with chdir(root), open("config/console.conf", mode, encoding="utf-8"):
                    return ""
            return access

        def checkout_write() -> str:
            _ = tested.write_text("changed\n")
            return ""

        # Each row: label, live folder, access, and the text the access returns, or None when the guard refuses it.
        rows: tuple[tuple[str, Path, Callable[[], str], str | None], ...] = (
            ("absolute", protected, config.read_text, None),
            ("relative", protected, relative, None),
            ("relative from the folder", protected, relative_from_the_folder, None),
            ("parent step", protected, (root / "nested" / ".." / "config" / "console.conf").read_text, None),
            ("folder link", protected, (root / "alias" / "console.conf").read_text, None),
            ("file link", protected, (root / "link.conf").read_text, None),
            ("bytes", protected, lambda: open(os.fsencode(config), encoding="utf-8").read(), None),
            ("relative r+", protected, relative_open("r+"), None),
            ("relative a", protected, relative_open("a"), None),
            ("elsewhere", protected, private.read_text, "private\n"),
            ("checkout read through a home link", linked, tested.read_text, "checkout\n"),
            ("checkout write through a home link", linked, checkout_write, None),
        )
        for index, (label, folder, access, expected) in enumerate(rows):
            with self.subTest(label):
                # Each guard keeps its hook in this process, so each row has its own startup folder.
                startup = root / f"startup-{index}"
                startup.mkdir()
                log = root / f"violations-{index}"
                guard = module_run.guard_live_config(folder, startup, log, checkout_config=checkout)
                if expected is None:
                    with self.assertRaisesRegex(AssertionError, "set CONF_DIR to a fixture folder"):
                        with guard:
                            with self.assertRaisesRegex(AssertionError, "read or write live configuration"):
                                _ = access()
                    # The log holds the absolute path, also for a relative open.
                    self.assertTrue(log.read_text().startswith(str(root) + os.sep))
                else:
                    with guard:
                        self.assertEqual(access(), expected)
                    self.assertFalse(log.exists())
        self.assertEqual((config.read_text(), tested.read_text()), ("live\n", "checkout\n"))

    def test_private_run_roots_copies_the_test_owned_config(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        checkout = RUNNER.parents[2] / "config"
        owned = root / "owned"
        owned.mkdir()
        _ = (owned / "console.conf").write_text("page.list.owner=test-owned\n")

        def owned_path(stem: str) -> Path:
            return owned / f"{stem}.conf"

        sources = {path: path.read_bytes() for path in (checkout / "console.conf", owned / "console.conf")}
        fixture = root / "fixture"
        rows: tuple[tuple[str, dict[str, str]], ...] = (("no CONF_DIR", {}), ("set CONF_DIR", {"CONF_DIR": str(fixture)}))
        for index, (label, environment) in enumerate(rows):
            with self.subTest(label), patch.object(conf, "test_config_path", side_effect=owned_path), patch.dict(os.environ):
                _ = os.environ.pop("CONF_DIR", None)
                os.environ.update(environment)
                module = root / f"module-{index}"
                module.mkdir()
                with module_run.private_run_roots(module_run.GateModuleLease(module)) as run_root:
                    folder = Path(os.environ["CONF_DIR"])
                    if environment:
                        self.assertEqual(folder, fixture)
                        self.assertFalse((run_root / "config").exists())
                    else:
                        self.assertEqual(folder, run_root / "config")
                        self.assertEqual((folder / "console.conf").read_text(), "page.list.owner=test-owned\n")
                        self.assertEqual((folder / "gate.conf").read_bytes(), (checkout / "gate.conf").read_bytes())
                        self.assertEqual(os.readlink(folder / "merge_gate.conf"), "gate.conf")
                        _ = (folder / "console.conf").write_text("changed\n")
                self.assertEqual(os.environ.get("CONF_DIR"), environment.get("CONF_DIR"))
                self.assertEqual(list(module.iterdir()), [])
        self.assertEqual({path: path.read_bytes() for path in sources}, sources)

    def test_child_guard_expires_when_the_runner_removes_the_hook(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        protected = root / "config"
        protected.mkdir()
        config = protected / "console.conf"
        _ = config.write_text("original\n")
        child = """
from pathlib import Path
import sys

hook, config = map(Path, sys.argv[1:])
try:
    config.write_text("blocked")
except AssertionError:
    print("refused")
hook.unlink()
config.write_text("expired")
"""
        log = root / "violations"
        with self.assertRaisesRegex(AssertionError, "Test attempted to read or write live configuration"):
            with module_run.guard_live_config(protected, root, log):
                result = subprocess.run([sys.executable, "-c", child, str(root / "sitecustomize.py"), str(config)],
                                        cwd=root, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "refused\n")
        self.assertEqual(config.read_text(), "expired")
        self.assertFalse((root / "sitecustomize.py").exists())

    def test_guard_allows_mkdir_on_a_private_symlink(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        protected = root / "config"
        protected.mkdir()
        alias = root / "alias"
        alias.symlink_to(protected, target_is_directory=True)
        log = root / "violations"
        with module_run.guard_live_config(protected, root, log):
            alias.mkdir(exist_ok=True)
        self.assertTrue(alias.is_symlink())
        self.assertFalse(log.exists())

    def test_guard_preserves_file_exists_for_a_private_link_target(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        protected = root / "config"
        protected.mkdir()
        alias = root / "alias"
        alias.symlink_to(protected, target_is_directory=True)
        source = root / "source"
        source.touch()
        log = root / "violations"
        with module_run.guard_live_config(protected, root, log):
            with self.assertRaises(FileExistsError):
                os.symlink(source, alias)
            with self.assertRaises(FileExistsError):
                os.link(source, alias)
        self.assertEqual(alias.readlink(), protected)
        self.assertFalse(log.exists())

    def test_guard_resolves_symlinks_before_parent_traversal(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        protected = root / "config"
        protected.mkdir()
        private = root / "private"
        nested = private / "nested"
        nested.mkdir(parents=True)
        (private / protected.name).mkdir()
        alias = root / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        through_alias = alias / ".." / protected.name
        log = root / "violations"
        with module_run.guard_live_config(protected, root, log):
            _ = (through_alias / "file").write_text("private")
            (through_alias / "directory").mkdir()
        self.assertEqual((private / protected.name / "file").read_text(), "private")
        self.assertTrue((private / protected.name / "directory").is_dir())
        self.assertFalse(log.exists())

    def test_guard_preserves_nofollow_error_for_a_private_symlink(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        protected = root / "config"
        protected.mkdir()
        config = protected / "console.conf"
        config.touch()
        alias = root / "alias"
        alias.symlink_to(config)
        log = root / "violations"
        with module_run.guard_live_config(protected, root, log):
            with self.assertRaises(OSError) as error:
                os.close(os.open(alias, os.O_WRONLY | os.O_NOFOLLOW))
            self.assertEqual(error.exception.errno, errno.ELOOP)
        self.assertFalse(log.exists())

    def test_guard_preserves_exclusive_create_error_for_a_private_symlink(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        protected = root / "config"
        protected.mkdir()
        config = protected / "console.conf"
        config.touch()
        alias = root / "alias"
        alias.symlink_to(config)
        log = root / "violations"
        with module_run.guard_live_config(protected, root, log):
            with self.assertRaises(FileExistsError):
                os.close(os.open(alias, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        self.assertFalse(log.exists())

    def test_guard_allows_rename_and_removal_of_a_private_link_to_live_config(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        protected = root / "config"
        protected.mkdir()
        config = protected / "console.conf"
        _ = config.write_text("original\n")
        private = root / "private"
        private.mkdir()
        alias = private / "console.conf"
        alias.symlink_to(config)
        log = root / "violations"
        with module_run.guard_live_config(protected, root, log):
            renamed = private / "renamed"
            _ = alias.rename(renamed)
            self.assertTrue(renamed.is_symlink())
            renamed.unlink()
            renamed.symlink_to(config)
            _ = alias.write_text("private")
            _ = alias.replace(renamed)
            self.assertEqual(renamed.read_text(), "private")
            renamed.unlink()
            private.rmdir()
        self.assertFalse(os.path.lexists(alias))
        self.assertFalse((root / "sitecustomize.py").exists())
        self.assertFalse(log.exists())
        directory_alias = root / "alias"
        directory_alias.symlink_to(protected, target_is_directory=True)
        empty = protected / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(AssertionError, "Test attempted to read or write live configuration"):
            with module_run.guard_live_config(protected, root, log):
                with self.assertRaises(AssertionError):
                    _ = (directory_alias / config.name).rename(root / "moved")
                with self.assertRaises(AssertionError):
                    _ = directory_alias.rename(protected / "moved")
                with self.assertRaises(AssertionError):
                    (directory_alias / config.name).unlink()
                with self.assertRaises(AssertionError):
                    (directory_alias / empty.name).rmdir()
                with self.assertRaises(AssertionError):
                    (directory_alias / "new-directory").mkdir()
                with self.assertRaises(AssertionError):
                    (directory_alias / "new-link").symlink_to(config)
        self.assertEqual(config.read_text(), "original\n")
        self.assertTrue(empty.is_dir())

    def test_runner_refuses_live_config_writes_even_if_the_test_catches_them(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = root / "fixture-home"
        protected = home / ".claude" / "config"
        protected.mkdir(parents=True)
        console = protected / "console.conf"
        _ = console.write_text("page.list.owner=original\n")
        private = root / "private.conf"
        alias = root / "alias"
        alias.symlink_to(protected, target_is_directory=True)

        def fixture_test() -> None:
            _ = private.write_text("page.list.owner=fixture\n")
            with self.assertRaises(AssertionError):
                _ = private.replace(console)

        def discover(_names: list[str]) -> unittest.TestSuite:
            with self.assertRaises(AssertionError):
                _ = (alias / "console.conf").write_text("page.list.owner=fixture\n")
            return unittest.TestSuite([unittest.FunctionTestCase(fixture_test)])

        with (patch.object(module_run, "live_config_folder", return_value=protected),
              patch.object(sys, "argv", ["module_run.py", str(root), "test_*.py"]),
              patch.object(unittest.defaultTestLoader, "loadTestsFromNames", side_effect=discover),
              redirect_stderr(StringIO())):
            with self.assertRaisesRegex(AssertionError, "Test attempted to read or write live configuration"):
                module_run.run_module(module_run.NoConfigReadLog(), root, root / "violations")

        self.assertEqual(console.read_text(), "page.list.owner=original\n")
        self.assertEqual(private.read_text(), "page.list.owner=fixture\n")


class ModuleRunnerTests(unittest.TestCase):
    outcomes: ClassVar[FixtureRun]
    empty: ClassVar[FixtureRun]
    success: ClassVar[FixtureRun]
    discovery_result: ClassVar[subprocess.CompletedProcess[str]]
    discovery_output: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        _ = (root / "fixture_root.py").write_text("VALUE = 42\n", encoding="utf-8")
        cls.outcomes = run_fixture(root, "outcomes", OUTCOMES)
        cls.empty = run_fixture(root, "empty", "# This module has no tests.\n")
        cls.success = run_fixture(root, "success", SUCCESS)
        folder = root / "discovery_boundaries"
        folder.mkdir()
        _ = (folder / "test_boundaries.py").write_text(DISCOVERY_BOUNDARIES, encoding="utf-8")
        cls.discovery_output = root / "discovery.json"
        env = {**os.environ, "TEST_TIMES_OUT": str(cls.discovery_output),
               "HOME": str(root / "home"), "CONF_DIR": str(root / "config"),
               "BUILDLOG_DIR": str(root / "buildlog"), "PYTHONDONTWRITEBYTECODE": "1"}
        env["PYTHONPATH"] = os.pathsep.join((str(root), str(folder)))
        cls.discovery_result = subprocess.run(
            [str(RUNNER.parents[1] / "lib/py"), str(RUNNER), str(folder), "test_boundaries.py"], cwd=root, env=env,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def test_config_reads_include_python_and_shell_children(self) -> None:
        from scripts.lib import build_folders

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _ = (root / "fixture_root.py").write_text("VALUE = 42\n")
            config = root / "config"
            config.mkdir()
            for stem in ("alpha", "beta", "gamma"):
                _ = (config / (stem + ".conf")).write_text("value=one\n")
            repository = RUNNER.parents[2]
            source = f"""import os, subprocess, sys, unittest
from scripts.lib import conf
conf.read('alpha').text('value')
class Reads(unittest.TestCase):
    def test_reads(self):
        conf.read('alpha').text('value')
        subprocess.run([sys.executable, '-c', "from scripts.lib import conf; conf.read('gamma').text('value')"],
                       env={{**os.environ, 'PYTHONPATH': {str(repository / 'scripts/lib/python_startup')!r}}}, check=True)
        subprocess.run(['/bin/bash', '-c', 'source "$1"; conf_value beta value', 'fixture',
                       {str(repository / 'scripts/lib/conf.sh')!r}], check=True, capture_output=True)
"""
            with self.subTest(runner="standalone"):
                fixture = run_fixture(root, "config_reads", source)
                self.assertEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
                self.assertEqual(fixture.times.get("config_reads"), ["alpha", "beta", "gamma"])

            for gate_case in ("absolute", "relative with read-only scratch"):
                with self.subTest(runner=gate_case), ExitStack() as cleanup:
                    parent = root / "jobs"
                    parent.mkdir(exist_ok=True)
                    module = cleanup.enter_context(build_folders.tool_folder("test-module", parent))
                    relative = gate_case != "absolute"
                    gate_root = Path(os.path.relpath(module)) if relative else module
                    fixture_path = root / "test_gate_reads.py"
                    _ = fixture_path.write_text(source + f"""
        import json
        from pathlib import Path
        module = Path(os.environ['TMPDIR'])
        self.assertNotIn('GATE_PARENT_PID', os.environ)
        self.assertNotIn('MERGE_GATE_PARENT_PID', os.environ)
        self.assertNotIn('MERGE_GATE_MODULE_DIR', os.environ)
        paths = [Path(os.environ[name]) for name in ('SCRATCH_ROOT', 'ASTRA_REVIEW_DIR', 'CONF_READ_LOG')]
        scratch, review, config_log = paths
        for folder in (scratch, review):
            folder.mkdir()
            (folder / 'output').write_text('module output')
            self.assertEqual((folder / 'output').read_text(), 'module output')
        self.assertEqual(set(config_log.read_text().splitlines()), {{'alpha', 'beta', 'gamma'}})
        self.assertEqual(list(module.glob('*.lease.json')), [])
        print('runner files ' + json.dumps([str(path.relative_to(module)) for path in paths]))
        if {relative!r}:
            retained = module / 'scratch' / 'nested'
            retained.mkdir(parents=True)
            (retained / 'output').write_text('read-only scratch output')
            retained.chmod(0o500)
""", encoding="utf-8")
                    extra = {**os.environ, **interpreter_cache_environment(root / "cache"),
                             "CONF_DIR": str(config), "PYTHONPATH": str(root)}
                    env = gate.test_module_environment(gate_root, extra)
                    shim = gate.python_shim(repository)
                    job = gate.start_gate_job(
                        "test_times", fixture_path, [str(shim), str(RUNNER), str(root), fixture_path.name],
                        repository, env, gate_root / "output.log")
                    try:
                        status = job.process.wait(timeout=TEST_HANG_GUARD_SECS)
                    except subprocess.TimeoutExpired:
                        gate.kill_job_group(job)
                        raise
                    runner_entries = sorted(path.name for path in module.glob("test-run-roots*"))
                    runner_entries.extend(path.name for path in module.glob("config-reads*"))
                    retained = module / "scratch" / "nested" / "output"
                    scratch_output = retained.read_text() if relative and status == 0 else ""
                    job.folder_cleanup = cleanup.pop_all()
                    result = gate.finish_gate_job(job, status)
                    self.assertEqual(status, 0, result.output)
                    self.assertEqual(result.returncode, 0, result.output)
                    self.assertIn("runner files ", result.output)
                    self.assertEqual(runner_entries, [])
                    if relative:
                        self.assertEqual(scratch_output, "read-only scratch output")
                    self.assertIsInstance(result.times, dict)
                    self.assertEqual(cast(ModuleTimes, result.times).get("config_reads"), ["alpha", "beta", "gamma"])
                    self.assertEqual(list(parent.iterdir()), [])

    def test_folder_scans_record_repository_listings_and_skip_imports_and_children(self) -> None:
        cases: tuple[tuple[str, str, list[str]], ...] = (
            ("scandir", "with os.scandir(ROOT / 'listed') as entries: list(entries)", ["listed"]),
            ("listdir", "os.listdir(ROOT / 'listed')", ["listed"]),
            ("bytes", "os.listdir(os.fsencode(ROOT / 'listed'))", ["listed"]),
            ("relative", "os.listdir('listed')", ["listed"]),
            ("root", "os.listdir(ROOT)", ["."]),
            ("read file", """(ROOT / 'listed/item.txt').read_text()
with open(ROOT / 'listed/item.txt', 'rb') as source: source.read()
descriptor = os.open(ROOT / 'listed/item.txt', os.O_RDONLY)
try:
    os.read(descriptor, 16)
finally:
    os.close(descriptor)""", []),
            ("read outside", "(OUTSIDE / 'outside.txt').read_text()", []),
            ("read config", "(ROOT / 'config/limits.conf').read_text()", []),
            ("read cache", "(ROOT / '__pycache__/saved.bin').read_bytes()", []),
            ("write modes", """for mode in ('a', 'w', 'r+'):
    with open(ROOT / 'listed/item.txt', mode) as target: target.write('fixture')""", []),
            ("child read", "subprocess.run([sys.executable, '-c', \"from pathlib import Path; Path('listed/item.txt').read_text()\"], cwd=ROOT, check=True)", []),
            ("patch isolation", """listed = str(ROOT / 'listed')
outside = str(OUTSIDE)
with mock.patch.object(os.path, 'realpath', wraps=os.path.realpath) as realpath, \\
     mock.patch.object(os.path, 'join', wraps=os.path.join) as join, \\
     mock.patch.object(os.path, 'dirname', wraps=os.path.dirname) as dirname:
    os.listdir(listed)
    os.listdir(outside)
    self.assertEqual(realpath.call_count, 0)
    self.assertEqual(join.call_count, 0)
    self.assertEqual(dirname.call_count, 0)""", ["listed"]),
            ("listdir descriptor", """descriptor = os.open(ROOT / 'listed', os.O_RDONLY)
try:
    os.listdir(descriptor)
finally:
    os.close(descriptor)""", ["listed"]),
            ("scandir descriptor", """descriptor = os.open(ROOT / 'listed', os.O_RDONLY)
try:
    with os.scandir(descriptor) as entries: list(entries)
finally:
    os.close(descriptor)""", ["listed"]),
            ("outside removal", """path = OUTSIDE / 'removal-probe'
path.mkdir()
shutil.rmtree(path)""", []),
            ("git prefixes", "subprocess.check_output(['git', 'ls-files', '-z', '--', 'scripts/', 'commands/', 'docs/'], cwd=ROOT)",
             ["commands/", "docs/", "scripts/"]),
            ("git C", "subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '--', 'docs/'], cwd=OUTSIDE)", ["docs/"]),
            ("git subdirectory", "subprocess.check_output(['git', '-C', str(ROOT / 'listed'), 'ls-files', '--', '../docs/'])", ["docs/"]),
            ("git all", "subprocess.check_output(['git', 'ls-files'], cwd=ROOT)", ["/"]),
            ("git root magic", "subprocess.check_output(['git', 'ls-files', '--', ':/docs'], cwd=ROOT)", ["/"]),
            ("git empty magic", "subprocess.check_output(['git', 'ls-files', '--', ':'], cwd=ROOT)", ["/"]),
            ("git exclude magic", "subprocess.check_output(['git', 'ls-files', '--', ':!docs'], cwd=ROOT)", ["/"]),
            ("import system", "import import_only", []),
            ("outside", "os.listdir(OUTSIDE)", []),
            ("child", "subprocess.run([sys.executable, '-c', \"import os; os.listdir('listed')\"], cwd=ROOT, check=True)", []),
        )
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            root = outside / "checkout"
            library = root / "scripts/lib"
            runner = root / "scripts/test_times/module_run.py"
            copy_test_runner(root)
            for package in (root / "scripts", library, root / "tests", root / "import_only"):
                package.mkdir(exist_ok=True)
                _ = (package / "__init__.py").write_text("")
            for folder in ("listed", "commands", "docs"):
                target = root / folder
                target.mkdir()
                _ = (target / "item.txt").write_text("fixture\n")
            _ = (root / "config/limits.conf").write_text("value=fixture\n")
            (root / "__pycache__").mkdir()
            _ = (root / "__pycache__/saved.bin").write_bytes(b"fixture")
            _ = (outside / "outside.txt").write_text("fixture\n")
            environment = {**os.environ, "HOME": str(outside / "home"), "PYTHONPATH": str(library / "python_startup"),
                           "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
            for arguments in (("init", "--quiet"), ("add", "listed", "commands", "docs")):
                _ = subprocess.run(["git", *arguments], cwd=root, env=environment, check=True,
                                   capture_output=True, timeout=TEST_HANG_GUARD_SECS)
            for name, action, expected in cases:
                with self.subTest(case=name):
                    output = outside / "times.json"
                    source = ("import os, shutil, subprocess, sys, unittest\nfrom pathlib import Path\nfrom unittest import mock\n"
                              + f"ROOT = Path({str(root)!r})\nOUTSIDE = Path({str(outside)!r})\n"
                              + "class Listing(unittest.TestCase):\n    def test_listing(self):\n"
                              + textwrap.indent(action, "        ") + "\n")
                    _ = (root / "tests/test_listing.py").write_text(source)
                    result = subprocess.run([sys.executable, str(runner), str(root / "tests"), "test_listing.py"],
                                            cwd=root, env={**environment, "TEST_TIMES_OUT": str(output)},
                                            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(read_times(output).get("folder_scans"), expected)
                    self.assertEqual(read_times(output).get("content_reads"), ["listed/item.txt"] if name == "read file" else [])

    def sender_fixture(self, mode: str) -> FixtureRun:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            _ = (config / "lint.conf").write_text("# range: 1..\nsweep_sender_timeout_seconds=17\n")
            source = SENDER_REQUEST
            fixture = run_fixture(root, "sender", source.replace("__MODE__", repr(mode)))
            if mode != "setup_exit" and isinstance(fixture.recorded_times, UnwrittenFixtureTimes):
                raise AssertionError("The sender fixture exited without a timing report")
            self.assertEqual(fixture.standard_sender_folders, [], fixture.standard.stderr)
            self.assertEqual(fixture.timed_sender_folders, [], fixture.timed.stderr)
            return fixture

    def test_real_sender_request_fails_the_module_with_one_diagnostic(self) -> None:
        fixture = self.sender_fixture("uncaught")

        self.assertNotEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertNotEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        diagnostic = "test_fixture.py: started the real scripts/message/send.py"
        self.assertEqual(fixture.timed.stderr.splitlines().count(diagnostic), 1, fixture.timed.stderr)
        self.assertNotIn("the tested guard allowed the real sender", fixture.timed.stderr)
        self.assertEqual(fixture.times["tests_run"], 1)
        self.assertEqual(fixture.times["failures"], 1)

    def test_sender_from_its_folder_or_as_a_module_fails_with_one_diagnostic(self) -> None:
        for mode in ("cd", "module", "setup_exit"):
            with self.subTest(mode=mode):
                fixture = self.sender_fixture(mode)
                self.assertNotEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
                self.assertEqual(fixture.timed.returncode, 1, fixture.timed.stderr)
                diagnostic = "test_fixture.py: started the real scripts/message/send.py"
                self.assertEqual(fixture.timed.stderr.splitlines().count(diagnostic), 1, fixture.timed.stderr)
                self.assertIn("REFUSED: real scripts/message/send.py is forbidden in tests", fixture.timed.stderr)
                self.assertNotIn("the sender probe attempted", fixture.timed.stderr)
                if mode == "setup_exit":
                    self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)

    def test_caught_sender_refusal_still_fails_the_module(self) -> None:
        for mode in ("caught", "expected_failure"):
            with self.subTest(mode=mode):
                fixture = self.sender_fixture(mode)

                self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
                self.assertNotEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
                diagnostic = "test_fixture.py: started the real scripts/message/send.py"
                self.assertEqual(fixture.timed.stderr.splitlines().count(diagnostic), 1, fixture.timed.stderr)
                self.assertNotIn("the tested guard allowed the real sender", fixture.timed.stderr)
                self.assertEqual(fixture.times["tests_run"], 1)
                self.assertEqual(fixture.times["failures"], 0)
                self.assertEqual(fixture.times["errors"], 0)
                expected_status = "expected_failure" if mode == "expected_failure" else "pass"
                self.assertEqual(fixture.times["tests"][0]["status"], expected_status)

    def test_cleared_environment_keeps_the_refusal_marker_and_fails_the_module(self) -> None:
        fixture = self.sender_fixture("cleared_environment")

        self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertEqual(fixture.times["tests_run"], 1)
        self.assertEqual(fixture.times["tests"][0]["status"], "pass")
        self.assertNotEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        diagnostic = "test_fixture.py: started the real scripts/message/send.py"
        self.assertEqual(fixture.timed.stderr.splitlines().count(diagnostic), 1, fixture.timed.stderr)
        self.assertNotIn("the sender probe attempted", fixture.timed.stderr)

    def test_fake_sender_module_passes_without_a_refusal_diagnostic(self) -> None:
        fixture = self.sender_fixture("fake")

        self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        self.assertNotIn("started the real scripts/message/send.py", fixture.timed.stderr)
        self.assertEqual(fixture.times["tests_run"], 1)
        self.assertEqual(fixture.times["tests"][0]["status"], "pass")

    def test_normal_test_case_attribute_keeps_the_failure_entry(self) -> None:
        self.assertEqual(self.discovery_result.returncode, 1, self.discovery_result.stderr)
        self.assertTrue(self.discovery_output.is_file(), self.discovery_result.stderr)
        rows = read_times(self.discovery_output)["tests"]
        failures = [row for row in rows if row["test"] == "DiscoveryBoundaries.test_failure_with_test_case_attribute"]
        self.assertEqual(len(failures), 1, rows)
        self.assertEqual(failures[0]["status"], "fail")

    def test_discovery_import_does_not_receive_the_output_path(self) -> None:
        self.assertTrue(self.discovery_output.is_file(), self.discovery_result.stderr)
        times = read_times(self.discovery_output)
        imported = [row for row in times["tests"] if row["test"] == "DiscoveryBoundaries.test_import_environment"]
        self.assertEqual(len(imported), 1, times)
        self.assertEqual(imported[0]["status"], "pass")
        self.assertEqual(times["tests_run"], 2)
        self.assertEqual(times["failures"], 1)

    def test_none_fixtures_keep_the_standard_runner_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = run_fixture(Path(directory), "none_fixtures", """import unittest
setUpModule = None
tearDownModule = None
class Fixtures(unittest.TestCase):
    setUpClass = None
    tearDownClass = None
    def test_runs(self):
        pass
""")
        self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        self.assertEqual(normalized_output(fixture.timed.stderr), normalized_output(fixture.standard.stderr))
        self.assertEqual(fixture.times["tests_run"], 1)

    def test_a_class_without_an_imported_module_keeps_the_standard_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = run_fixture(Path(directory), "absent_module", """import unittest
class Fixtures(unittest.TestCase):
    __module__ = "absent_module"
    def test_runs(self):
        pass
""")
        self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        self.assertEqual(normalized_output(fixture.timed.stderr), normalized_output(fixture.standard.stderr))
        self.assertEqual(fixture.times["tests_run"], 1)

    def test_successful_class_fixture_has_its_own_cpu_measure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = """import time
import unittest

module_value = 0

def setUpModule():
    global module_value
    module_value = 7

class FixtureCPU(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        deadline = time.process_time() + 0.15
        while time.process_time() < deadline:
            pass
        cls.value = module_value

    def test_value(self):
        self.assertEqual(self.value, 7)
"""
            fixture = run_fixture(root, "fixture_cpu", source)
            self.assertEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
            self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
            report = cast(dict[str, object], cast(object, fixture.times))
            fixtures = cast(list[FixtureMeasure], report["fixtures"])
            setup = next(row for row in fixtures if row["test"] == "setUpClass (test_fixture.FixtureCPU)")
            test = cast(TestMeasure, fixture.times["tests"][0])
            self.assertEqual(setup["kind"], "fixture")
            self.assertEqual(test["kind"], "test")
            self.assertGreater(setup["cpu_s"], 0.1)
            self.assertGreater(setup["cpu_s"], test["cpu_s"])
            self.assertGreater(setup["s"], 0)
            self.assertEqual(fixture.times["tests_run"], 1)
            self.assertEqual(fixture.times["tests_s"], test["s"])
            self.assertTrue(any(row["test"] == "setUpModule (test_fixture)" for row in fixtures))

    def test_missing_arguments_print_one_usage_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "HOME": str(root / "home"), "CONF_DIR": str(root / "config"),
                   "BUILDLOG_DIR": str(root / "buildlog")}
            _ = env.pop("TEST_TIMES_OUT", None)
            result = subprocess.run(
                [sys.executable, str(RUNNER)], cwd=root, env=env,
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, "usage: module_run.py <start_dir> <pattern>\n")

    def test_output_and_exit_status_match_unittest(self) -> None:
        for fixture, status in ((self.outcomes, 1), (self.empty, 5), (self.success, 0)):
            with self.subTest(status=status):
                self.assertEqual(fixture.standard.returncode, status, fixture.standard.stderr)
                self.assertEqual(fixture.timed.returncode, fixture.standard.returncode, fixture.timed.stderr)
                self.assertEqual(fixture.timed.stdout, fixture.standard.stdout)
                self.assertEqual(normalized_output(fixture.timed.stderr), normalized_output(fixture.standard.stderr))
        self.assertIn("DeprecationWarning: fixture warning", self.outcomes.timed.stderr)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "scripts/test_times/module_run.py"
            copy_test_runner(root)
            folder = root / "scripts" / "fixture"
            folder.mkdir(parents=True)
            _ = (folder / "sibling.py").write_text("VALUE = 42\n", encoding="utf-8")
            _ = (folder / "test_package_name.py").write_text(
                "import sys, unittest\n"
                + "from scripts.fixture import sibling\n"
                + "class PackageImport(unittest.TestCase):\n"
                + "    def test_package_name(self):\n"
                + "        self.assertEqual(__name__, 'scripts.fixture.test_package_name')\n"
                + "        self.assertNotIn('test_package_name', sys.modules)\n"
                + "        self.assertEqual(sibling.VALUE, 42)\n", encoding="utf-8")
            output = root / "times.json"
            environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(folder), str(root))),
                           "TEST_TIMES_OUT": str(output)}
            for working_directory in (root, folder, Path("/home")):
                with self.subTest(case="package name", cwd=working_directory):
                    result = subprocess.run(
                        [str(root / "scripts/lib/py"), str(runner), str(folder), "test_package_name.py"],
                        cwd=working_directory, env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual([row["test"] for row in read_times(output)["tests"]],
                                     ["PackageImport.test_package_name"])

    def test_each_parent_has_one_outcome_with_error_precedence(self) -> None:
        rows = self.outcomes.times["tests"]
        by_name = {row["test"]: row for row in rows}
        expected = {
            "Outcomes.test_pass": "pass", "Outcomes.test_fail": "fail",
            "Outcomes.test_error": "error", "Outcomes.test_skip": "skip",
            "Outcomes.test_expected_failure": "expected_failure",
            "Outcomes.test_unexpected_success": "unexpected_success",
            "Outcomes.test_subtest_fail": "fail", "Outcomes.test_subtest_error": "error",
            "Outcomes.test_warning": "pass", "setUpClass (test_fixture.BrokenClass)": "error",
        }
        self.assertEqual({name: row["status"] for name, row in by_name.items()}, expected)
        self.assertEqual(len(rows), len(expected), "Subtests must share their parent entry")
        self.assertEqual(by_name["Outcomes.test_skip"]["s"], 0.0)
        self.assertEqual(by_name["setUpClass (test_fixture.BrokenClass)"]["s"], 0.0)
        self.assertEqual(self.outcomes.times["tests_run"], 9)
        self.assertEqual(self.outcomes.times["failures"], 3)
        self.assertEqual(self.outcomes.times["errors"], 3)
        self.assertEqual(self.outcomes.times["skipped"], 2)

    def test_totals_include_all_test_times_and_empty_discovery(self) -> None:
        for fixture in (self.outcomes, self.empty, self.success):
            with self.subTest(status=fixture.timed.returncode):
                times = fixture.times
                for value in (times["wall_s"], times["cpu_s"], times["tests_s"]):
                    self.assertIsInstance(value, (int, float))
                    self.assertTrue(math.isfinite(value))
                    self.assertGreaterEqual(value, 0.0)
                for row in times["tests"]:
                    self.assertTrue(math.isfinite(row["s"]))
                    self.assertGreaterEqual(row["s"], 0.0)
                    if row["status"] != "skip" and " (" not in row["test"]:
                        self.assertGreater(row["s"], 0.0, row["test"])
                self.assertAlmostEqual(times["tests_s"], sum(row["s"] for row in times["tests"]))
        self.assertGreater(self.outcomes.times["tests_s"], 0.0)
        self.assertGreater(self.success.times["tests_s"], 0.0)
        self.assertEqual(self.empty.times["tests"], [])
        self.assertEqual(self.empty.times["tests_run"], 0)
        self.assertEqual(self.success.times["tests_run"], 1)
        self.assertEqual([row["test"] for row in self.success.times["tests"]], ["RootImport.test_value"])

    def test_output_write_failure_keeps_the_test_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ = (root / "test_fixture.py").write_text(textwrap.dedent(SUCCESS), encoding="utf-8")
            _ = (root / "fixture_root.py").write_text("VALUE = 42\n", encoding="utf-8")
            output = root / "output"
            output.mkdir()
            env = {**os.environ, "TEST_TIMES_OUT": str(output), "HOME": str(root / "home"),
                   "CONF_DIR": str(root / "config"), "BUILDLOG_DIR": str(root / "buildlog")}
            env["PYTHONPATH"] = str(root)
            result = subprocess.run(
                [str(RUNNER.parents[1] / "lib/py"), str(RUNNER), str(root), "test_fixture.py"], cwd=root, env=env,
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            warnings = [line for line in result.stderr.splitlines() if line.startswith("test times not written:")]
            self.assertEqual(len(warnings), 1, result.stderr)
            self.assertTrue(output.is_dir())
            self.assertEqual(list(root.glob(".output.*")), [])


@unittest.skipUnless(sys.platform == "linux", "Descendant adoption uses Linux prctl and pidfds")
class ModuleProcessCleanupTests(unittest.TestCase):
    def test_gate_parent_death_keeps_the_sigkill_status_without_a_traceback(self) -> None:
        from scripts.lib import build_folders

        cases = ("standalone", "unleased TMPDIR", "gate")
        parent_names = ("GATE_PARENT_PID", "MERGE_GATE_PARENT_PID")
        for runner_case, parent_name in product(cases, parent_names):
            with self.subTest(runner=runner_case, parent_name=parent_name), ExitStack() as cleanup:
                root = Path(cleanup.enter_context(tempfile.TemporaryDirectory())).resolve()
                processes = OwnedTestProcesses()
                environment = processes.environment({**os.environ, "HOME": str(root / "home"),
                    "CONF_DIR": str(root / "config"), "BUILDLOG_DIR": str(root / "buildlog"),
                    "PYTHONPATH": str(root), "TMPDIR": str(root)})
                _ = environment.pop("TEST_TIMES_OUT", None)
                _ = environment.pop("MERGE_GATE_MODULE_DIR", None)
                _ = (root / "test_parent.py").write_text("""import os, signal, unittest
from pathlib import Path
from scripts.lib import conf_reads
class ParentDeath(unittest.TestCase):
    def test_wait(self):
        for name in ('SCRATCH_ROOT', 'ASTRA_REVIEW_DIR'):
            folder = Path(os.environ[name])
            folder.mkdir()
            (folder / 'output').write_text(name)
        conf_reads.record('ready')
        print('ready', flush=True)
        signal.pause()
""", encoding="utf-8")
                module = root / "module"
                if runner_case == "gate":
                    module = cleanup.enter_context(build_folders.tool_folder("module", root))
                    environment = gate.test_module_environment(module, environment)
                elif runner_case == "unleased TMPDIR":
                    module.mkdir()
                    environment.update(TMPDIR=str(module), TEST_TIMES_OUT=str(module / "test-times.json"))
                _ = cleanup.callback(processes.finish)
                parent = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                    env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                processes.track(parent)
                for name in parent_names:
                    _ = environment.pop(name, None)
                environment[parent_name] = str(parent.pid)
                runner = subprocess.Popen([sys.executable, str(RUNNER), str(root), "test_parent.py"],
                    env=environment, cwd=root, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                processes.track(runner)
                output = runner.stdout
                assert output is not None
                self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0])
                self.assertEqual(cast(str, output.readline()), "ready\n")
                if runner_case == "gate":
                    for folder, name in (("scratch", "SCRATCH_ROOT"), ("astra", "ASTRA_REVIEW_DIR")):
                        self.assertEqual((module / "test-run-roots" / folder / "output").read_text(), name)
                    self.assertEqual((module / "config-reads" / "reads").read_text(), "ready\n")
                elif runner_case == "unleased TMPDIR":
                    lease = cast(dict[str, object], json.loads((module / "test-run-roots.lease.json").read_text()))
                    self.assertEqual(lease["pid"], runner.pid)
                parent.send_signal(signal.SIGTERM)
                self.assertEqual(parent.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGTERM)
                stdout, stderr = runner.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(runner.returncode, -signal.SIGKILL, stdout + stderr)
                self.assertNotIn("Traceback", stderr)
                if runner_case != "standalone":
                    self.assertEqual(list(module.glob("test-run-roots*")), [])
                if runner_case == "gate":
                    self.assertEqual(list(module.glob("config-reads*")), [])

    def test_gate_parent_death_before_bootstrap_stops_the_module(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        processes = OwnedTestProcesses()
        self.addCleanup(processes.finish)
        environment = processes.environment({**os.environ, "HOME": str(root / "home"),
            "CONF_DIR": str(root / "config"), "BUILDLOG_DIR": str(root / "buildlog")})
        source = fr"""import os, signal, subprocess, sys
reader, writer = os.pipe()
prefix = 'import os; os.write(' + str(writer) + ', b"R"); os.close(' + str(writer) + '); os.read(0, 1)\n'
child = subprocess.Popen([sys.executable, '-c', prefix + {gate.SUITE_BOOTSTRAP!r},
    sys.executable, '-c', 'import signal; signal.pause()'],
    env={{**os.environ, 'GATE_PARENT_PID': str(os.getpid())}},
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    pass_fds=(writer,), start_new_session=True)
os.close(writer)
os.read(reader, 1)
os.close(reader)
print(child.pid, flush=True)
signal.pause()
"""
        parent = subprocess.Popen([sys.executable, "-c", source], env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.track(parent)
        output = parent.stdout
        assert output is not None
        self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0])
        child = int(cast(str, output.readline()))
        descriptor = os.pidfd_open(child)
        self.addCleanup(os.close, descriptor)
        parent.send_signal(signal.SIGTERM)
        stdout, stderr = parent.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(parent.returncode, -signal.SIGTERM, stdout + stderr)
        self.assertTrue(select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)[0],
                        "The module survived a gate that ended before bootstrap setup")

    def test_ignored_sigchld_keeps_the_test_process_status_and_inheritance(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (root / "test_signal.py").write_text(
            """import signal, unittest
class InheritedSignal(unittest.TestCase):
    def test_sigchld(self):
        self.assertEqual(signal.getsignal(signal.SIGCHLD), signal.SIG_IGN)
""",
            encoding="utf-8")
        environment = {**os.environ, "HOME": str(root / "home"), "CONF_DIR": str(root / "config"),
                       "BUILDLOG_DIR": str(root / "buildlog")}
        for name in ("TEST_TIMES_OUT", "PYTHONPATH"):
            _ = environment.pop(name, None)
        environment["PYTHONPATH"] = str(root)
        launcher = ("import os, signal, sys; signal.signal(signal.SIGCHLD, signal.SIG_IGN); "
                    "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])")
        for arguments, status in (([], 2), ([str(root), "test_signal.py"], 0)):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, "-c", launcher, str(RUNNER), *arguments],
                    cwd=root, env=environment, capture_output=True, text=True, check=False,
                    timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)

    def run_orphan(self, *, fail: bool = False, terminate: bool = False, reap: bool = False) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        processes = OwnedTestProcesses()
        self.addCleanup(processes.finish)
        source = (ORPHAN_PROCESS.replace("__REAP__", repr(reap)).replace("__FAIL__", repr(fail))
                  .replace("__HANG_GUARD_MS__", str(TEST_HANG_GUARD_SECS * 1000)))
        _ = (root / "test_orphan.py").write_text(source, encoding="utf-8")
        environment = processes.environment({**os.environ, "HOME": str(root / "home"),
            "CONF_DIR": str(root / "config"), "BUILDLOG_DIR": str(root / "buildlog"),
            "PYTHONDONTWRITEBYTECODE": "1"})
        for name in ("TEST_TIMES_OUT", "PYTHONPATH"):
            _ = environment.pop(name, None)
        environment["PYTHONPATH"] = str(root)
        process: subprocess.Popen[str] = subprocess.Popen([str(RUNNER.parents[1] / "lib/py"), str(RUNNER), str(root), "test_orphan.py"],
            cwd=root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        processes.track(process)
        output = process.stdout
        self.assertIsNotNone(output)
        if output is None:
            raise AssertionError("The runner has no output pipe")
        self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0], "The orphan did not start")
        line = cast(str, output.readline())
        self.assertTrue(line.strip().isdigit(), line)
        descriptor = os.pidfd_open(int(line))
        self.addCleanup(os.close, descriptor)
        if terminate:
            send_process_group(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(input="x", timeout=TEST_HANG_GUARD_SECS)
        expected = -signal.SIGTERM if terminate else 1 if fail else 0
        self.assertEqual(process.returncode, expected, stdout + stderr)
        # POLLHUP means the process was reaped, rather than left as a zombie.
        observer = select.poll()
        observer.register(descriptor, select.POLLHUP)
        self.assertTrue(observer.poll(0), "The orphan remains alive or unreaped")

    def test_normal_end_kills_and_reaps_a_detached_orphan(self) -> None:
        self.run_orphan()

    def test_failed_test_kills_and_reaps_a_detached_orphan(self) -> None:
        self.run_orphan(fail=True)

    def test_sigterm_to_the_runner_group_kills_and_reaps_a_detached_orphan(self) -> None:
        self.run_orphan(terminate=True)

    def test_a_running_test_sees_its_killed_orphan_reaped(self) -> None:
        self.run_orphan(reap=True)
