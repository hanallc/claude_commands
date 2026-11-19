"""Mac routing and natedev verdicts for missing Mac passes."""

from __future__ import annotations

from contextlib import redirect_stdout, suppress
import io
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import cast, final, override
from unittest import mock

from scripts.aws_target import cloud_gate, ledger
from scripts.direct.wait_for_test_state import wait_until
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_routes import AssignedModules, MacGateCancellation, ModuleCoverage
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.production.merge_gate_test_support import gate_suites


MAC_MODULE = "scripts.mac_test.mac_gate"
HOOKS = "scripts/hooks/test_first.py"
LIB_FIRST = "scripts/lib/test_first.py"
LIB_SECOND = "scripts/lib/test_second.py"
MESSAGE = "scripts/message/test_first.py"

TERM_PROBE = """
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event
from unittest.mock import patch

from scripts.production.gate_routes import SuiteMode, TreeSelection
from scripts.production.merge_gate import ACTIVE_GATE_RECOVERY, GateRecovery, SharedCPUPool, start_mac_gate

state = Path(sys.argv[1])

class FakeMac:
    def route(self, *args, cancellation, **options):
        reader = cancellation.caller_fd
        watch = 'import os, sys; from pathlib import Path; ' \\
                'assert os.read(int(sys.argv[1]), 1) == b""; Path(sys.argv[2]).touch()'
        with subprocess.Popen([sys.executable, '-c', watch, str(reader), str(state / 'cancelled')],
                              pass_fds=(reader,), start_new_session=True):
            (state / 'watching').touch()
        return 'fixture cancelled'

with start_mac_gate(FakeMac(), state, mode=SuiteMode.MERGE_GATE, full=False,
                    module_time_limit_s=1, selected=TreeSelection(('scripts/lib/test_first.py',), 'tree')):
    if sys.argv[2] == 'protected':
        reader, writer = os.pipe()
        os.write(writer, b'Q')
        recovery = GateRecovery(state / 'recovery.json', str(state), os.getpid(), 'fixture')
        ACTIVE_GATE_RECOVERY.set(recovery)
        save = recovery.save

        def pause_before_save():
            (state / 'protected').touch()
            assert sys.stdin.buffer.read(1) == b'r'
            assert signal.SIGTERM in signal.sigpending()
            save()

        with patch.object(recovery, 'save', pause_before_save):
            SharedCPUPool(reader, reader).try_acquire()
    Event().wait()
"""


@final
class MacGateSuitesTests(GateSuitesFixture):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.read_modules = mock.Mock(return_value=())
        self.mac_route = mock.Mock()
        self.cloud_route = mock.Mock(return_value="fixture cloud unavailable")
        self.output = io.StringIO()

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(sys.modules, {
            MAC_MODULE: SimpleNamespace(read_modules=self.read_modules, route=self.mac_route),
        })))
        _ = cast(object, self.enterContext(mock.patch.object(cloud_gate, "route", self.cloud_route)))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        _ = self.enterContext(redirect_stdout(self.output))

    def assert_routed(self, route: mock.Mock, *paths: str,
                      mode: merge_gate.SuiteMode = merge_gate.SuiteMode.MERGE_GATE) -> None:
        tree, _ = cloud_gate.working_tree(self.checkout)
        selection = AssignedModules(paths, tree) if route is self.cloud_route else merge_gate.TreeSelection(paths, tree)
        cancellation = {"cancellation": mock.ANY} if route is self.mac_route else {}
        route.assert_called_once_with(self.checkout, mode=mode, full=False,
                                      module_time_limit_s=merge_gate.DEFAULT_MODULE_TIME_LIMIT_S,
                                      selected=selection, **cancellation)

    @gate_suites("lib")
    def test_cli_entry_keeps_remote_assignments_and_mac_passes(self) -> None:
        self.read_modules.return_value = (LIB_SECOND,)
        first = merge_gate.GateJobResult("lib", "test_first.py", 0, 1, "")
        second = merge_gate.GateJobResult("lib", "test_second.py", 0, 1, "")
        self.cloud_route.return_value = merge_gate.GateRun((first,), 1, 0)
        self.mac_route.return_value = merge_gate.GateRun((second,), 1, 0)
        script = str(merge_gate.__file__)
        with mock.patch.object(sys, "argv", [script, "--tests-only", "--repo", str(self.checkout)]):
            with self.assertRaises(SystemExit) as exited:
                _ = runpy.run_path(script, run_name="__main__")
        self.assertEqual(exited.exception.code, 0, self.output.getvalue())
        self.assert_routed(self.cloud_route, LIB_FIRST)
        self.assert_routed(self.mac_route, LIB_SECOND)
        self.assertEqual(self.module_events(), [])

    @gate_suites("lib")
    def test_natedev_starts_while_the_mac_route_waits(self) -> None:
        self.read_modules.return_value = (LIB_SECOND,)
        mac_finished = threading.Event()

        def mac_result(*_arguments: object, **_options: object) -> merge_gate.GateRun:
            wait_until(lambda: (self.state / "modules.jsonl").exists(), self.state)
            mac_finished.set()
            return merge_gate.GateRun((merge_gate.GateJobResult("lib", "test_second.py", 0, 1, ""),), 1, 0)

        def cloud_result(*_arguments: object, **_options: object) -> str:
            self.assertIs(threading.current_thread(), threading.main_thread())
            self.assertFalse(mac_finished.is_set())
            return "fixture cloud unavailable"

        self.mac_route.side_effect = mac_result
        self.cloud_route.side_effect = cloud_result
        run = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assert_routed(self.mac_route, LIB_SECOND)
        self.assert_routed(self.cloud_route, LIB_FIRST)
        self.assertEqual([row["module"] for row in self.module_events() if row["event"] == "start"], ["test_first.py"])
        self.assertEqual([job.module for job in run.jobs], ["test_first.py", "test_second.py"])

    @gate_suites("hooks", "lib")
    def test_mac_miss_uses_the_natedev_verdict_and_keeps_mac_passes(self) -> None:
        self.read_modules.return_value = (LIB_FIRST, LIB_SECOND)
        hooks = merge_gate.GateJobResult("hooks", "test_first.py", 0, 1, "")
        passed = merge_gate.GateJobResult("lib", "test_second.py", 0, 1, "")
        self.cloud_route.return_value = merge_gate.GateRun((hooks,), 1, 0)
        for mac_failure, local_failure in (("failure", False), ("time limit", True)):
            with self.subTest(mac_failure=mac_failure, local_failure=local_failure):
                self.clear_gate_events()
                red = self.state / "red-lib"
                if local_failure:
                    self.write(red, "red")
                report = "Mac time limit" if mac_failure == "time limit" else ""
                failed = merge_gate.GateJobResult("lib", "test_first.py", 1, 1, "fixture Mac red", killed_for=report)
                self.mac_route.return_value = merge_gate.GateRun(
                    (failed, passed), 1, 0, executions=(failed, passed), time_limit_report=report)
                run = merge_gate.run_gate_suites(self.checkout)
                self.assertEqual(run.passed, not local_failure, self.output.getvalue())
                self.assertEqual(run.time_limit_report, "")
                self.assertEqual({row["module"] for row in self.module_events() if row["event"] == "start"},
                                 {"test_first.py"})
                self.assertNotIn(failed, run.jobs)
                self.assertIn(failed, run.executions)
                self.assertIn(passed, run.jobs)
                self.assertIn(hooks, run.jobs)

    @gate_suites("hooks", "lib")
    def test_lost_mac_run_sends_all_its_modules_to_natedev(self) -> None:
        self.read_modules.return_value = (LIB_FIRST, LIB_SECOND)
        self.mac_route.return_value = "fixture Mac result lost"
        self.cloud_route.return_value = merge_gate.GateRun(
            (merge_gate.GateJobResult("hooks", "test_first.py", 0, 1, ""),), 1, 0)
        run = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assert_routed(self.mac_route, LIB_FIRST, LIB_SECOND)
        self.assert_routed(self.cloud_route, HOOKS)
        self.assertCountEqual([row["module"] for row in self.module_events() if row["event"] == "start"],
                              ["test_first.py", "test_second.py"])
        self.assertIn("Mac gate uses natedev: fixture Mac result lost", self.output.getvalue())

    @gate_suites("lib")
    def test_lost_mac_time_and_both_linux_stop_reports_reach_the_result(self) -> None:
        self.read_modules.return_value = (LIB_FIRST,)
        self.mac_route.return_value = "fixture Mac result lost"
        self.cloud_route.return_value = merge_gate.GateRun((), 3, 0, time_limit_report="AWS time limit")
        local = merge_gate.GateRun((), 4, 0, time_limit_report="natedev time limit")
        with (mock.patch.object(time, "monotonic", side_effect=[10.0, 18.0]),
              mock.patch.object(merge_gate, "run_suites", return_value=local)):
            run = merge_gate.run_gate_suites(self.checkout)
        self.assertEqual(run.seconds, 12.0)
        self.assertFalse(run.passed)
        self.assertEqual(run.time_limit_report, "AWS time limit\nnatedev time limit")

    @gate_suites("lib")
    def test_linux_exception_and_sigint_cancel_without_joining_the_mac_worker(self) -> None:
        self.read_modules.return_value = (LIB_SECOND,)
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                started = threading.Event()
                cancelled = threading.Event()
                release = threading.Event()
                finished = threading.Event()

                def mac_result(*_arguments: object, cancellation: MacGateCancellation, **_options: object) -> str:
                    started.set()
                    try:
                        self.assertEqual(os.read(cancellation.caller_fd, 1), b"")
                        cancelled.set()
                        self.assertTrue(release.wait(10))
                        return "fixture cancelled"
                    finally:
                        finished.set()

                def linux_error(*_arguments: object, **_options: object) -> str:
                    self.assertTrue(started.wait(10))
                    if interrupt:
                        signal.raise_signal(signal.SIGINT)
                    raise GateRefused("fixture Linux exception")

                self.mac_route.side_effect = mac_result
                self.cloud_route.side_effect = linux_error
                try:
                    with self.assertRaises(KeyboardInterrupt if interrupt else GateRefused):
                        _ = merge_gate.run_gate_suites(self.checkout)
                    self.assertTrue(cancelled.wait(10))
                    self.assertFalse(finished.is_set())
                finally:
                    release.set()
                    self.assertTrue(finished.wait(10))

    def assert_sigterm_closes_the_mac_pipe(self, *, protected: bool) -> None:
        source = Path(__file__).resolve().parents[2]
        mode = "protected" if protected else "idle"
        with subprocess.Popen([sys.executable, "-c", TERM_PROBE, str(self.state), mode], cwd=source,
                              stdin=subprocess.PIPE) as gate:
            try:
                wait_until((self.state / "watching").exists, self.state, processes=(gate,))
                if protected:
                    wait_until((self.state / "protected").exists, self.state, processes=(gate,))
                gate.terminate()
                if protected:
                    assert gate.stdin is not None
                    with suppress(BrokenPipeError):
                        _ = os.write(gate.stdin.fileno(), b"r")
                self.assertEqual(gate.wait(timeout=10), -signal.SIGTERM)
                wait_until((self.state / "cancelled").exists, self.state)
            finally:
                if gate.poll() is None:
                    gate.kill()

    @gate_suites("lib")
    def test_sigterm_closes_the_mac_cancellation_pipe_on_process_exit(self) -> None:
        self.assert_sigterm_closes_the_mac_pipe(protected=False)

    @gate_suites("lib")
    def test_sigterm_waits_for_linux_to_record_its_cpu_slot(self) -> None:
        self.assert_sigterm_closes_the_mac_pipe(protected=True)
        path = self.state / "recovery.json"
        self.assertTrue(path.exists(), "Linux lost its CPU slot record")
        token, = ledger.rows(cloud_gate.read(path), "tokens")
        self.assertEqual(token["value"], b"Q".hex())

    @gate_suites("lib")
    def test_only_linux_writes_recovery_during_concurrent_registration(self) -> None:
        self.read_modules.return_value = (LIB_SECOND,)
        recovery = merge_gate.GateRecovery(self.state / "recovery.json", str(self.checkout), os.getpid(), "fixture")
        register = threading.Barrier(2)
        writers: list[threading.Thread] = []
        save = recovery.save

        def record_writer() -> None:
            writers.append(threading.current_thread())
            save()

        def mac_result(*_arguments: object, **_options: object) -> merge_gate.GateRun:
            _ = register.wait(timeout=10)
            _ = merge_gate.checked_git(self.checkout, "rev-parse", "HEAD")
            return merge_gate.GateRun((merge_gate.GateJobResult("lib", "test_second.py", 0, 1, ""),), 1, 0)

        def linux_result(*_arguments: object, **_options: object) -> merge_gate.GateRun:
            _ = register.wait(timeout=10)
            recovery.add_group(os.getpid())
            return merge_gate.GateRun((merge_gate.GateJobResult("lib", "test_first.py", 0, 1, ""),), 1, 0)

        self.mac_route.side_effect = mac_result
        self.cloud_route.side_effect = linux_result
        owner = merge_gate.ACTIVE_GATE_RECOVERY.set(recovery)
        try:
            with mock.patch.object(recovery, "save", record_writer):
                run = merge_gate.run_gate_suites(self.checkout)
        finally:
            merge_gate.ACTIVE_GATE_RECOVERY.reset(owner)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assertEqual(set(writers), {threading.main_thread()})
        saved = cloud_gate.read(recovery.path)
        self.assertEqual(set(ledger.mapping(saved["groups"])), {str(os.getpid())})

    @gate_suites("hooks")
    def test_empty_list_preserves_the_old_cloud_and_local_calls_and_output(self) -> None:
        local = merge_gate.GateRun((merge_gate.GateJobResult("hooks", "test_first.py", 0, 1, ""),), 1, 0)
        with mock.patch.object(merge_gate, "run_suites", return_value=local) as run_suites:
            for mac in ("missing", "empty"):
                with self.subTest(mac=mac), redirect_stdout(io.StringIO()) as output:
                    run_suites.reset_mock()
                    self.cloud_route.reset_mock()
                    if mac == "missing":
                        with mock.patch.dict(sys.modules, {MAC_MODULE: None}):
                            run = merge_gate.run_gate_suites(self.checkout)
                    else:
                        run = merge_gate.run_gate_suites(self.checkout)
                    self.assertIs(run, local)
                    self.cloud_route.assert_called_once_with(
                        self.checkout, mode=merge_gate.SuiteMode.MERGE_GATE, full=False,
                        module_time_limit_s=merge_gate.DEFAULT_MODULE_TIME_LIMIT_S, selected=merge_gate.ALL_MODULES)
                    run_suites.assert_called_once_with(
                        self.checkout, (), merge_gate.DEFAULT_MODULE_TIME_LIMIT_S,
                        source="tests-only", full=False, mode=merge_gate.SuiteMode.MERGE_GATE)
                    self.assertEqual(output.getvalue(), "Cloud gate uses natedev: fixture cloud unavailable\n")
        self.mac_route.assert_not_called()

    @gate_suites("lib")
    def test_later_gate_cannot_reuse_a_pass_for_one_module_in_the_same_suite(self) -> None:
        self.read_modules.return_value = (LIB_SECOND,)
        self.mac_route.return_value = merge_gate.GateRun(
            (merge_gate.GateJobResult("lib", "test_second.py", 0, 1, ""),), 1, 0)
        marker, _ = self.write_nightly_red_marker()
        first = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue(first.passed, self.output.getvalue())
        self.assertEqual(self.pass_records(), [])
        self.assertEqual(self.gate_run_records()[-1]["selection"], "subset")
        self.assertTrue(marker.exists())
        marker.unlink()
        self.clear_gate_events()
        self.read_modules.return_value = ()
        second = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue(second.passed, self.output.getvalue())
        self.assertFalse(second.tests_reused)
        self.assert_all_modules_started()
        record, = self.pass_records()
        self.assertEqual(record["kind"], "full")
        self.clear_gate_events()
        assigned = merge_gate.run_suites(self.checkout, selected=merge_gate.SelectedModules((LIB_FIRST,)))
        self.assertFalse(assigned.tests_reused)
        self.assertIs(assigned.coverage, ModuleCoverage.SUBSET)
        self.assertEqual([row["module"] for row in self.module_events() if row["event"] == "start"], ["test_first.py"])
        self.assertEqual(self.pass_records(), [record])

    @gate_suites("direct", "hooks", "message")
    def test_targeted_gate_splits_only_affected_modules_and_runs_types_once(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "merge_gate.conf", "scan_roots=\n")
        baseline = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue(baseline.passed, self.output.getvalue())
        passes = self.pass_records()
        self.cloud_route.reset_mock()
        self.clear_gate_events()
        for path in (HOOKS, MESSAGE):
            module = self.checkout / path
            self.write(module, module.read_text() + "\n# Change the module.\n")
        _ = self.commit(self.checkout, "Change two test modules")
        tree = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
        self.read_modules.return_value = ("scripts/direct/test_first.py", HOOKS)
        self.mac_route.return_value = "fixture Mac busy"
        self.cloud_route.return_value = merge_gate.GateRun(
            (merge_gate.GateJobResult("message", "test_first.py", 0, 1, ""),), 1, 0)
        mode = merge_gate.SuiteMode.TARGETED_TEST
        with mock.patch.object(merge_gate, "read_time_limit", wraps=merge_gate.read_time_limit) as limit:
            run = merge_gate.run_gate_suites(self.checkout, ("scripts",), mode=mode)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assertIn("affected: 2 of 3 modules", self.output.getvalue())
        self.assert_routed(self.mac_route, HOOKS, mode=mode)
        self.assert_routed(self.cloud_route, MESSAGE, mode=mode)
        self.assertEqual([row["suite"] for row in self.module_events() if row["event"] == "start"], ["hooks"])
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)
        self.assertEqual(limit.call_count, 2)
        self.assertEqual([row.args[3] for row in limit.call_args_list], [True, True])
        self.assertEqual(self.pass_records(), passes)
        self.clear_gate_events()
        self.read_modules.return_value = ()
        self.cloud_route.return_value = "fixture cloud unavailable"
        later = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue(later.passed, self.output.getvalue())
        self.assertFalse(later.tests_reused)
        self.assert_all_modules_started()
        self.assertEqual(self.pass_records()[-1]["tree"], tree)

    @gate_suites("hooks")
    def test_all_mac_modules_skip_aws_and_run_local_types(self) -> None:
        self.read_modules.return_value = (HOOKS,)
        self.mac_route.return_value = merge_gate.GateRun(
            (merge_gate.GateJobResult("hooks", "test_first.py", 0, 1, ""),), 1, 0)
        run = merge_gate.run_gate_suites(self.checkout, ("scripts",))
        self.assertTrue(run.passed, self.output.getvalue())
        self.assert_routed(self.mac_route, HOOKS)
        self.cloud_route.assert_not_called()
        self.assertEqual(self.module_events(), [])
        self.assertEqual([job.kind for job in run.jobs],
                         [merge_gate.GateJobKind.TYPE_CHECK, merge_gate.GateJobKind.TEST_MODULE])
