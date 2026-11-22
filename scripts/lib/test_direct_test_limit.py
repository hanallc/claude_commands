"""Check the direct test limit against the real user systemd manager."""
from __future__ import annotations

import os
from pathlib import Path
from secrets import token_hex
import subprocess
import sys
import tempfile
import unittest
from typing import override

from scripts.lib.checkout_test_support import copy_test_runner, direct_test_fixture_environment, run_gate_test_fixture
from scripts.lib.python_startup.sitecustomize import startup_pythonpath, within_test_limit
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


REPO = Path(__file__).resolve().parents[2]
MODULE_RUN = REPO / "scripts/test_times/module_run.py"
CASE_LIMIT = 2
LIMIT_LINE = f"stopped at the {CASE_LIMIT}-second direct test limit; run longer tests with targeted_test.py --module"
CASE = """
import os, pathlib, sys, time, unittest

class Example(unittest.TestCase):
    def test_short(self):
        if sys.platform == 'linux':
            cgroup = pathlib.Path('/proc/self/cgroup').read_text().strip().removeprefix('0::')
            print('scope=' + pathlib.Path(cgroup).name, flush=True)
        print('pid=' + str(os.getpid()), flush=True)
        print('fixture stdout', flush=True)
        print('fixture stderr', file=sys.stderr, flush=True)

    def test_failure(self):
        self.fail('fixture failure')

    def test_long(self):
        print('fixture started', flush=True)
        time.sleep({seconds})
        print('fixture finished', flush=True)
"""
GATE_CASE = """
from direct_case import Example

def load_tests(loader, tests, pattern):
    return loader.loadTestsFromName("test_long", Example)
"""
GATE_FIXTURE = """
import os, sys
source = os.environ.get("DEVTOOLS_GATE_PARENT_SOURCE_FD", "")
print("gate source descriptor:", bool(source), flush=True)
os.environ["PYTHONPATH"] = sys.argv[3]
os.execv(sys.argv[1], sys.argv[1:])
"""
NESTED_TEST_PROBE = """
import os, sys, unittest

queries = []

def guard(event, arguments):
    if event == "subprocess.Popen" and sys.argv[2] == "direct":
        program, command, _, _ = arguments
        if program == "busctl" and command[-2:] == ["RuntimeMaxUSec", "KillSignal"]:
            queries.append(command)
            return
    if event == "subprocess.Popen" or event == "open" and str(arguments[0]).endswith(".conf"):
        raise AssertionError(f"Unexpected setup: {event} {arguments[0]}")

sys.addaudithook(guard)
unittest.defaultTestLoader.loadTestsFromTestCase(unittest.TestCase)
assert len(queries) == int(sys.argv[2] == "direct")
assert "PYTHONPATH" not in os.environ
assert os.environ["TMPDIR"] == sys.argv[1]
"""
NESTED_FIXTURE = """
import os, subprocess
from pathlib import Path

def run_nested(root, shim, probe, timeout, limit="gate"):
    environment = {name: os.environ[name] for name in (
        "PATH", "HOME", "CONF_DIR", "PYTHONDONTWRITEBYTECODE", "CLAUDE_DISABLE_DATACLASS_CODE_CACHE",
        "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
        if name in os.environ}
    environment.update(TMPDIR=str(root), XDG_CACHE_HOME=str(root / "cache"))
    result = subprocess.run(
        [shim, "-c", probe, str(root), limit], env=environment, start_new_session=True,
        capture_output=True, text=True, check=False, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
"""
DIRECT_FIXTURE_VARIABLES = (
    "BUILDLOG_DIR", "CLAUDE_DISABLE_DATACLASS_CODE_CACHE", "CLAUDE_PYTHON", "CONF_DIR", "CONF_READ_LOG",
    "DBUS_SESSION_BUS_ADDRESS", "GATE_PARENT_PID", "HOME", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
    "PYTHONWARNINGS", "TMPDIR", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
)


class DirectTestLimitTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str]

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.environment = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = (self.root / "direct_case.py").write_text(CASE.format(seconds=CASE_LIMIT + 1))
        self.environment = direct_test_fixture_environment(self.root, seconds=CASE_LIMIT)
        self.environment["HOME"] = str(self.root / "home")
        self.environment["PYTHONPATH"] = startup_pythonpath("")

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        if sys.platform == "linux" and within_test_limit():
            # The manager starts the direct fixture outside the parent test's limit.
            command = ["systemd-run", "--user", "--quiet", "--pipe", "--wait", "--collect",
                       "--property=Type=exec", f"--property=RuntimeMaxSec={TEST_HANG_GUARD_SECS}s",
                       f"--working-directory={self.root}",
                       *(f"--setenv={name}" for name in DIRECT_FIXTURE_VARIABLES if name in self.environment),
                       "--", *command]
        return subprocess.run(command, cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=TEST_HANG_GUARD_SECS, text=True, check=False)

    def run_case(self, name: str) -> subprocess.CompletedProcess[str]:
        return self.invoke(sys.executable, "-m", "unittest", f"direct_case.Example.{name}")

    def assert_direct_limit_result(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertIn("fixture started\n", result.stdout, result.stderr)
        if sys.platform != "linux":
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("fixture finished", result.stdout)
            return
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.splitlines().count(LIMIT_LINE), 1, result.stdout + result.stderr)
        self.assertNotIn("fixture finished", result.stdout)

    def test_plain_python_stops_at_the_direct_limit(self) -> None:
        self.assert_direct_limit_result(self.run_case("test_long"))

    @unittest.skipUnless(sys.platform == "linux", "Direct test scopes use Linux systemd")
    def test_scope_name_without_enforcement_cannot_bypass_the_direct_limit(self) -> None:
        for property_value in ("KillSignal=SIGKILL", "RuntimeMaxSec=90s"):
            with self.subTest(property=property_value):
                result = self.invoke(
                    "systemd-run", "--user", "--scope", "--quiet", "--collect",
                    "--unit", f"claude-direct-test-{token_hex(16)}",
                    "--property", "MemoryMax=2G", "--property", property_value,
                    "--", sys.executable, "-m", "unittest", "direct_case.Example.test_long",
                )
                self.assert_direct_limit_result(result)

    def test_short_direct_test_preserves_output_and_exit_status(self) -> None:
        self.environment["PYTHONWARNINGS"] = "default"
        result = self.invoke(sys.executable, "-c", """
import os, runpy
print(f"entry={os.getpid()}", flush=True)
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
""", "direct_case.Example.test_short")
        stdout, stderr = result.stdout, result.stderr
        self.assertEqual(result.returncode, 0, stdout + stderr)
        entry = next(line.removeprefix("entry=") for line in stdout.splitlines() if line.startswith("entry="))
        self.assertIn(f"pid={entry}\n", stdout)
        self.assertIn("fixture stdout\n", stdout)
        self.assertIn("fixture stderr\n", stderr)
        self.assertNotIn("ResourceWarning", stderr)
        if sys.platform == "linux":
            self.assertIn("scope=claude-direct-test-", stdout)
        result = self.run_case("test_failure")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture failure", result.stderr)
        self.assertNotIn(LIMIT_LINE, result.stderr)

    def test_live_gate_starts_its_module_without_the_direct_limit(self) -> None:
        case = self.root / "gate_case.py"
        _ = case.write_text(GATE_CASE)
        shim = str(REPO / "scripts/lib/py")
        run = run_gate_test_fixture(
            case, [shim, "-c", GATE_FIXTURE, shim, str(MODULE_RUN), str(self.root), case.name], self.environment)
        result = run.stdout
        self.assertEqual(run.returncode, 0, result)
        self.assertIn(f"gate source descriptor: {sys.platform == 'linux'}", result)
        self.assertIn("fixture finished", result)
        self.assertNotIn(LIMIT_LINE, result)
        self.assertFalse((self.root / "test-runs").exists())

    @unittest.skipUnless(sys.platform == "linux", "Gate ancestry uses Linux process records")
    def test_gate_descendant_keeps_its_environment_without_a_second_limit(self) -> None:
        case = self.root / "test_gate_child.py"
        shim = str(REPO / "scripts/lib/py")
        _ = case.write_text(NESTED_FIXTURE + f"""
import unittest
class GateChild(unittest.TestCase):
    def test_child(self):
        run_nested(Path({str(self.root)!r}), {shim!r}, {NESTED_TEST_PROBE!r}, {TEST_HANG_GUARD_SECS!r})
""")
        environment = {**self.environment, "PYTHONPATH": str(self.root)}
        result = run_gate_test_fixture(case, [shim, str(MODULE_RUN), str(self.root), case.name], environment)
        self.assertEqual(result.returncode, 0, result.stdout)

    @unittest.skipUnless(sys.platform == "linux", "Direct test scopes use Linux systemd")
    def test_direct_scope_descendant_keeps_its_environment_without_a_second_limit(self) -> None:
        shim = str(REPO / "scripts/lib/py")
        result = self.invoke(sys.executable, "-c", NESTED_FIXTURE + f"""
import unittest
unittest.defaultTestLoader.loadTestsFromTestCase(unittest.TestCase)
assert "claude-direct-test-" in Path("/proc/self/cgroup").read_text()
run_nested(Path({str(self.root)!r}), {shim!r}, {NESTED_TEST_PROBE!r}, {TEST_HANG_GUARD_SECS!r}, "direct")
""")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_live_non_gate_parent_does_not_bypass_the_limit(self) -> None:
        self.environment["GATE_PARENT_PID"] = str(os.getpid())
        self.assert_direct_limit_result(self.run_case("test_long"))


    def test_worker_script_starts_the_limit_at_the_runner(self) -> None:
        script = self.root / "worker.py"
        _ = script.write_text("from direct_case import Example\nfrom unittest import TextTestRunner\n"
                              + "TextTestRunner().run(Example('test_long'))\n")
        self.assert_direct_limit_result(self.invoke(sys.executable, str(script)))

    def test_loader_starts_the_limit_before_loading_tests(self) -> None:
        _ = (self.root / "slow_loader.py").write_text(
            f"import time\nprint('fixture started', flush=True)\ntime.sleep({CASE_LIMIT + 1})\n"
            + "print('fixture finished', flush=True)\n")
        self.assert_direct_limit_result(self.invoke(
            sys.executable, "-c", "import unittest; unittest.defaultTestLoader.loadTestsFromName('slow_loader')"))

    def test_suite_and_case_start_the_limit_without_a_runner(self) -> None:
        for expression in ("Example('test_long').run()", "unittest.TestSuite([Example('test_long')]).run(unittest.TestResult())"):
            with self.subTest(expression=expression):
                self.assert_direct_limit_result(self.invoke(
                    sys.executable, "-c", "import unittest; from direct_case import Example; " + expression))

    def test_importing_unittest_mock_does_not_start_the_limit(self) -> None:
        result = self.invoke(sys.executable, "-c", f"import unittest.mock, time; time.sleep({CASE_LIMIT + 1}); print('done')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "done\n")
        self.assertFalse((self.root / "test-runs").exists())

    def test_setup_records_a_config_read_from_another_thread(self) -> None:
        log = self.root / "config-reads"
        self.environment["CONF_READ_LOG"] = str(log)
        result = self.invoke(sys.executable, "-c", """
import threading
import unittest
from unittest.mock import patch

from scripts.lib import conf, testing_folders

start_test_run = testing_folders.start_test_run

def start_with_reader():
    reader = threading.Thread(target=conf.read, args=("parallel",))
    reader.start()
    reader.join()
    start_test_run()

with patch.object(testing_folders, "start_test_run", start_with_reader):
    unittest.defaultTestLoader.loadTestsFromName("direct_case.Example.test_short")
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("parallel", log.read_text().splitlines() if log.exists() else [])

    def test_failed_setup_preserves_the_child_config_log(self) -> None:
        log = self.root / "config-reads"
        _ = self.environment.pop("CONF_READ_LOG", None)
        result = self.invoke(sys.executable, "-c", """
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts.lib import conf, testing_folders

conf.read("before")
os.environ["CONF_READ_LOG"] = sys.argv[1]
with patch.object(testing_folders, "start_test_run", side_effect=OSError("setup failed")):
    try:
        unittest.defaultTestLoader.loadTestsFromName("direct_case.Example.test_short")
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("Setup did not fail")

subprocess.run([sys.executable, "-c", "from scripts.lib import conf; conf.read('child')"], check=True)
""", str(log))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("setup failed", result.stderr)
        self.assertEqual(log.read_text().splitlines() if log.exists() else [], ["child"])

    def test_macos_checkout_without_a_test_folder_needs_no_config_recorder(self) -> None:
        library = self.root / "scripts/lib"
        library.mkdir(parents=True)
        (library / "py").touch()
        _ = (library / "conf.py").write_text("# Configuration reader from an older checkout.\n")
        result = self.invoke(sys.executable, "-c", """
import sys
import unittest

sys.platform = "darwin"
unittest.defaultTestLoader.loadTestsFromName("direct_case.Example.test_short")
assert "scripts.lib.conf_reads" not in sys.modules
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_startup_hooks_share_one_direct_limit(self) -> None:
        copy = self.root / "copy"
        copy_test_runner(copy)
        hook = copy / "scripts/lib/python_startup/sitecustomize.py"
        with hook.open("a") as output:
            _ = output.write("\nif __name__ == 'sitecustomize': print('copy hook', flush=True)\n")
        self.environment["PYTHONPATH"] += os.pathsep + str(self.root)
        result = self.invoke(str(copy / "scripts/lib/py"), "-c", """
import sys, unittest
from direct_case import Example
state = sys.modules["_devtools_checkout"]
print("following hooks:", sum(hasattr(hook, "start_process") for hook in getattr(state, "startup_hooks", ())), flush=True)
print("unittest boundaries:", int(unittest.__dict__.get("_devtools_test_boundary", False)), flush=True)
unittest.TextTestRunner().run(Example("test_long"))
""")
        self.assertIn("copy hook\n", result.stdout, result.stderr)
        self.assertIn("following hooks: 1\n", result.stdout, result.stderr)
        self.assertIn("unittest boundaries: 1\n", result.stdout, result.stderr)
        self.assert_direct_limit_result(result)


    def test_limit_stops_a_child_that_creates_a_session(self) -> None:
        _ = (self.root / "child_case.py").write_text(f"""
import subprocess, sys, time, unittest

class ChildSession(unittest.TestCase):
    def test_child(self):
        subprocess.Popen(
            [sys.executable, '-c', "import time; time.sleep({CASE_LIMIT + 1}); print('child finished', flush=True)"],
            start_new_session=True)
        print('fixture started', flush=True)
        time.sleep({CASE_LIMIT + 1})
        print('fixture finished', flush=True)
""")
        result = self.invoke(sys.executable, "-c", """
from child_case import ChildSession
ChildSession("test_child").run()
""")
        self.assert_direct_limit_result(result)
        self.assertEqual("child finished" in result.stdout, sys.platform != "linux", result.stdout + result.stderr)
