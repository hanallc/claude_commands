"""Check the direct test boundary and its requests to user systemd."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from typing import override
from unittest import mock

from scripts.lib.checkout_test_support import (STANDALONE_TEST_STARTUP, WITHOUT_GATE_ANCESTRY, copy_test_runner,
                                              direct_test_fixture_environment, run_gate_test_fixture,
                                              without_user_test_manager, write_stdlib_tool)
from scripts.lib.python_startup import sitecustomize as startup
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


REPO = Path(__file__).resolve().parents[2]
MODULE_RUN = REPO / "scripts/test_times/module_run.py"
CASE_LIMIT = 2
LIMIT_LINE = (f"stopped at the {CASE_LIMIT}-second direct test limit. "
              "Run longer tests with copies.py run and gate.py --affected --module.")
LIMIT_REQUEST = f"scope requested: {CASE_LIMIT}"
FIXTURE_SCOPE = "claude-direct-test-" + "0" * 32 + ".scope"
CALLER_REGISTRY_PIN = "/home/worker/.claude/config/models.conf"
LIMIT_FIXTURE = STANDALONE_TEST_STARTUP + """
startup._user_test_manager_available = lambda: True
startup._adopt_direct_test_scope = lambda seconds: print(f"scope requested: {seconds}", flush=True)
"""
CASE = """
import os, sys, unittest

class Example(unittest.TestCase):
    def test_short(self):
        print('pid=' + str(os.getpid()), flush=True)
        print('fixture stdout', flush=True)
        print('fixture stderr', file=sys.stderr, flush=True)

    def test_failure(self):
        self.fail('fixture failure')

    def test_inherited_registry_pin_is_clear(self):
        self.assertNotIn('MODELS_CONFIG_FILE', os.environ)

    def test_registry_pin_set_in_a_test_stays(self):
        self.assertNotIn('MODELS_CONFIG_FILE', os.environ)
        pin = os.path.join(os.environ['HOME'], 'fixture-models.conf')
        os.environ['MODELS_CONFIG_FILE'] = pin
        unittest.defaultTestLoader.loadTestsFromTestCase(unittest.TestCase)
        self.assertEqual(os.environ.get('MODELS_CONFIG_FILE'), pin)

    def test_long(self):
        print('fixture started', flush=True)
        print('fixture finished', flush=True)

    def test_hang(self):
        import time
        print('fixture started', flush=True)
        time.sleep(60)
        print('fixture finished', flush=True)
"""
GATE_CASE = """
from direct_case import Example

def load_tests(loader, tests, pattern):
    return loader.loadTestsFromNames(("test_long", "test_inherited_registry_pin_is_clear"), Example)
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


class DirectTestLimitTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str]

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.environment = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = (self.root / "direct_case.py").write_text(CASE)
        self.environment = direct_test_fixture_environment(self.root, seconds=CASE_LIMIT)
        self.environment["HOME"] = str(self.root / "home")
        hooks = self.root / "startup"
        hooks.mkdir()
        _ = (hooks / "sitecustomize.py").write_text(LIMIT_FIXTURE)
        self.environment["PYTHONPATH"] = startup.startup_pythonpath(str(hooks))

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(arguments, cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=TEST_HANG_GUARD_SECS, text=True, check=False)

    def run_case(self, name: str) -> subprocess.CompletedProcess[str]:
        return self.invoke(sys.executable, "-m", "unittest", f"direct_case.Example.{name}")

    def assert_limit_before_fixture(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = [LIMIT_REQUEST] if sys.platform == "linux" else []
        expected.extend(("fixture started", "fixture finished"))
        relevant = [line for line in result.stdout.splitlines()
                    if line.startswith(("scope requested:", "fixture "))]
        self.assertEqual(relevant, expected)

    def test_plain_python_requests_the_direct_limit_before_the_test(self) -> None:
        self.assert_limit_before_fixture(self.run_case("test_long"))

    def test_unittest_clears_the_inherited_registry_pin(self) -> None:
        self.environment["MODELS_CONFIG_FILE"] = CALLER_REGISTRY_PIN
        result = self.invoke(sys.executable, "-c", """
import os, runpy, sys, unittest
assert os.environ["MODELS_CONFIG_FILE"] == sys.argv.pop(1)
runpy.run_module("unittest", run_name="__main__", alter_sys=True)
""", CALLER_REGISTRY_PIN, "direct_case.Example.test_inherited_registry_pin_is_clear")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_registry_pin_set_in_a_test_survives_later_boundary_calls(self) -> None:
        self.environment["MODELS_CONFIG_FILE"] = CALLER_REGISTRY_PIN
        result = self.run_case("test_registry_pin_set_in_a_test_stays")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_user_manager_runs_the_test_in_a_private_folder(self) -> None:
        self.environment = without_user_test_manager(self.root, self.environment)
        self.environment["PYTHONPATH"] = startup.startup_pythonpath("")
        result = self.invoke(sys.executable, "-c", STANDALONE_TEST_STARTUP + """
import os, unittest
case = unittest.defaultTestLoader.loadTestsFromName("direct_case.Example.test_short")
assert unittest.TextTestRunner().run(case).wasSuccessful()
print("folder=" + os.environ["TMPDIR"])
""")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        folder = Path(result.stdout.splitlines()[-1].removeprefix("folder="))
        self.assertIn("fixture stdout\n", result.stdout)
        self.assertEqual(folder.parent, self.root / "test-runs")
        self.assertFalse(folder.exists())

    @unittest.skipUnless(sys.platform == "linux", "Direct test limits use Linux process records")
    def test_no_user_manager_stops_a_long_test_at_the_direct_limit(self) -> None:
        self.environment = without_user_test_manager(self.root, self.environment)
        self.environment["PYTHONPATH"] = startup.startup_pythonpath("")
        result = self.invoke(sys.executable, "-c", STANDALONE_TEST_STARTUP + """
import unittest
case = unittest.defaultTestLoader.loadTestsFromName("direct_case.Example.test_hang")
unittest.TextTestRunner().run(case)
""")
        self.assertEqual(result.returncode, -signal.SIGKILL.value, result.stdout + result.stderr)
        self.assertIn("fixture started\n", result.stdout)
        self.assertNotIn("fixture finished", result.stdout)
        self.assertIn(LIMIT_LINE, result.stderr)

    @unittest.skipUnless(sys.platform == "linux", "Direct test limits use Linux process records")
    def test_failed_manager_check_stops_before_the_test(self) -> None:
        bus = self.root / "bus"
        bus.touch()
        write_stdlib_tool(self.root / "busctl", "import sys\nprint('fixture bus refused', file=sys.stderr)\nsys.exit(1)\n")
        self.environment.update(DBUS_SESSION_BUS_ADDRESS=f"unix:path={bus}",
                                PATH=os.pathsep.join((str(self.root), self.environment["PATH"])),
                                PYTHONPATH=startup.startup_pythonpath(""))
        result = self.invoke(sys.executable, "-c", STANDALONE_TEST_STARTUP + """
import unittest
case = unittest.defaultTestLoader.loadTestsFromName("direct_case.Example.test_long")
unittest.TextTestRunner().run(case)
""")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("direct test: could not check the user systemd manager: fixture bus refused", result.stderr)
        self.assertNotIn("fixture started", result.stdout)

    @unittest.skipUnless(sys.platform == "linux", "Direct test limits use Linux process records")
    @unittest.skipIf(os.geteuid() == 0, "Root can read a folder with mode 000.")
    def test_unreadable_bus_socket_stops_before_the_test(self) -> None:
        locked = self.root / "locked"
        locked.mkdir(mode=0)
        self.addCleanup(locked.chmod, 0o700)
        self.environment.update(DBUS_SESSION_BUS_ADDRESS=f"unix:path={locked / 'bus'}",
                                PYTHONPATH=startup.startup_pythonpath(""))
        result = self.invoke(sys.executable, "-c", STANDALONE_TEST_STARTUP + """
import unittest
case = unittest.defaultTestLoader.loadTestsFromName("direct_case.Example.test_long")
unittest.TextTestRunner().run(case)
""")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("direct test: [Errno 13] Permission denied", result.stderr)
        self.assertNotIn("fixture started", result.stdout)

    @unittest.skipUnless(sys.platform == "linux", "Direct test scopes use Linux systemd")
    def test_scope_name_without_enforcement_cannot_bypass_the_direct_limit(self) -> None:
        for properties in (f"t {2**64 - 1}\ni {signal.SIGKILL.value}\n", f"t 90000000\ni {signal.SIGTERM.value}\n"):
            with (self.subTest(properties=properties),
                  mock.patch.object(startup, "_verified_gate", return_value=False),
                  mock.patch.object(startup, "_descends_from_gate_module", return_value=False),
                  mock.patch.object(startup, "_cgroup", return_value=f"/fixture/{FIXTURE_SCOPE}"),
                  mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, properties))):
                self.assertFalse(startup.within_test_limit())

    @unittest.skipUnless(sys.platform == "linux", "Gate commands use Linux process records")
    def test_each_installed_gate_entry_point_is_a_gate_parent(self) -> None:
        home = self.root / "installed-home"
        production = home / ".claude/scripts/production"
        production.mkdir(parents=True)
        for name, expected in (("gate.py", True), ("merge_gate.py", True), ("targeted_test.py", True),
                               ("merge_checkpoint.py", True), ("other_tool.py", False)):
            entry = production / name
            _ = entry.write_text("import sys\nprint('ready', flush=True)\nsys.stdin.read()\n")
            with self.subTest(entry=name):
                process = subprocess.Popen([sys.executable, str(entry)], cwd=self.root,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                try:
                    # Popen can return before Linux publishes the child's command line.
                    assert process.stdout is not None
                    readable, _, _ = select.select([process.stdout], [], [], TEST_HANG_GUARD_SECS)
                    self.assertTrue(readable, "the gate fixture did not report readiness")
                    self.assertEqual(process.stdout.readline(), b"ready\n")
                    with mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir=str(home))):
                        recognized = startup._gate_command(str(process.pid))  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(recognized, expected)
                finally:
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

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
            self.assertEqual(stdout.splitlines().count(LIMIT_REQUEST), 1)
        result = self.run_case("test_failure")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture failure", result.stderr)
        self.assertNotIn(LIMIT_LINE, result.stderr)

    def test_live_gate_starts_its_module_without_the_direct_limit(self) -> None:
        case = self.root / "gate_case.py"
        _ = case.write_text(GATE_CASE)
        shim = str(REPO / "scripts/lib/py")
        manager_query = self.root / "manager-query"
        write_stdlib_tool(self.root / "busctl", f"from pathlib import Path\nPath({str(manager_query)!r}).touch()\n")
        environment = {**self.environment, "PYTHONPATH": startup.startup_pythonpath(""),
                       "MODELS_CONFIG_FILE": CALLER_REGISTRY_PIN,
                       "PATH": os.pathsep.join((str(self.root), self.environment["PATH"]))}
        run = run_gate_test_fixture(
            case, [shim, "-c", GATE_FIXTURE, shim, str(MODULE_RUN), str(self.root), case.name], environment)
        result = run.stdout
        self.assertEqual(run.returncode, 0, result)
        self.assertIn(f"gate source descriptor: {sys.platform == 'linux'}", result)
        self.assertIn("fixture finished", result)
        self.assertNotIn(LIMIT_LINE, result)
        self.assertFalse(manager_query.exists())
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
        properties = f"t {CASE_LIMIT * 1_000_000}\ni {signal.SIGKILL.value}\n"
        write_stdlib_tool(self.root / "busctl", f"print({properties!r}, end='')\n")
        self.environment["PATH"] = os.pathsep.join((str(self.root), self.environment["PATH"]))
        probe = WITHOUT_GATE_ANCESTRY + f"""
startup._cgroup = lambda pid: "/fixture/{FIXTURE_SCOPE}"
""" + NESTED_TEST_PROBE
        result = self.invoke(sys.executable, "-c", NESTED_FIXTURE + f"""
run_nested(Path({str(self.root)!r}), {shim!r}, {probe!r}, {TEST_HANG_GUARD_SECS!r}, "direct")
""")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_live_non_gate_parent_does_not_bypass_the_limit(self) -> None:
        self.environment["GATE_PARENT_PID"] = str(os.getpid())
        self.assert_limit_before_fixture(self.run_case("test_long"))


    def test_worker_script_starts_the_limit_at_the_runner(self) -> None:
        script = self.root / "worker.py"
        _ = script.write_text("from direct_case import Example\nfrom unittest import TextTestRunner\n"
                              + "TextTestRunner().run(Example('test_long'))\n")
        self.assert_limit_before_fixture(self.invoke(sys.executable, str(script)))

    def test_loader_starts_the_limit_before_loading_tests(self) -> None:
        _ = (self.root / "slow_loader.py").write_text(
            "print('fixture started', flush=True)\n"
            + "print('fixture finished', flush=True)\n")
        self.assert_limit_before_fixture(self.invoke(
            sys.executable, "-c", "import unittest; unittest.defaultTestLoader.loadTestsFromName('slow_loader')"))

    def test_suite_and_case_start_the_limit_without_a_runner(self) -> None:
        for expression in ("Example('test_long').run()", "unittest.TestSuite([Example('test_long')]).run(unittest.TestResult())"):
            with self.subTest(expression=expression):
                self.assert_limit_before_fixture(self.invoke(
                    sys.executable, "-c", "import unittest; from direct_case import Example; " + expression))

    def test_importing_unittest_mock_does_not_start_the_limit(self) -> None:
        result = self.invoke(sys.executable, "-c", "import unittest.mock; print('done')")
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
        self.assert_limit_before_fixture(result)


    def test_observer_requests_scope_deadline_and_reports_timeout(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(startup, "_cgroup", return_value="/user.slice/fixture.slice/test.service"),
              mock.patch.object(startup, "_start_transient_unit") as start,
              mock.patch.object(subprocess, "run") as run,
              mock.patch("secrets.token_hex", return_value="0" * 32),
              mock.patch("select.select", return_value=([], [], [])),
              mock.patch.object(startup, "_settle_direct_test_scope", return_value=True) as settle,
              mock.patch.object(os, "dup", return_value=12),
              mock.patch.object(os, "close"),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            startup._observe_direct_test(os.getpid(), CASE_LIMIT)  # pyright: ignore[reportPrivateUsage]
        start.assert_called_once_with(FIXTURE_SCOPE, (
            ("PIDFDs", "ah", "1", "12"),
            ("Slice", "s", "fixture.slice"),
            ("RuntimeMaxUSec", "t", str(CASE_LIMIT * 1_000_000)),
            ("KillSignal", "i", str(signal.SIGKILL.value)),
        ), (12,))
        run.assert_called_once_with(["systemctl", "--user", "start", "--", FIXTURE_SCOPE], check=True)
        settle.assert_called_once_with(FIXTURE_SCOPE)
        self.assertEqual(output.getvalue(), FIXTURE_SCOPE + "\n")
        self.assertEqual(errors.getvalue(), LIMIT_LINE + "\n")
