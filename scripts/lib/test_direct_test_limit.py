"""Check the direct test limit against the real user systemd manager."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import override

from scripts.lib.checkout_test_support import copy_test_runner, direct_test_fixture_environment, run_gate_test_fixture
from scripts.lib.python_startup.sitecustomize import startup_pythonpath
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
        return subprocess.run([*arguments], cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
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

    def test_short_direct_test_preserves_output_and_exit_status(self) -> None:
        self.environment["PYTHONWARNINGS"] = "default"
        command = [sys.executable, "-m", "unittest", "direct_case.Example.test_short"]
        with subprocess.Popen(command, cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertIn(f"pid={process.pid}\n", stdout)
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
