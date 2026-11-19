"""Mac routing and natedev verdicts for missing Mac passes."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from typing import cast, final, override
from unittest import mock

from scripts.aws_target import cloud_gate
from scripts.direct.wait_for_test_state import wait_until
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import merge_gate
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.production.merge_gate_test_support import gate_suites


MAC_MODULE = "scripts.mac_test.mac_gate"
HOOKS = "scripts/hooks/test_first.py"
LIB_FIRST = "scripts/lib/test_first.py"
LIB_SECOND = "scripts/lib/test_second.py"
MESSAGE = "scripts/message/test_first.py"


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
        route.assert_called_once_with(self.checkout, mode=mode, full=False,
                                      module_time_limit_s=merge_gate.DEFAULT_MODULE_TIME_LIMIT_S,
                                      selected=merge_gate.TreeSelection(paths, tree))

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
                self.assertNotIn(failed, run.executions)
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

    @gate_suites("direct", "hooks", "message")
    def test_targeted_gate_splits_only_affected_modules_and_runs_types_once(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "merge_gate.conf", "scan_roots=\n")
        baseline = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue(baseline.passed, self.output.getvalue())
        self.cloud_route.reset_mock()
        self.clear_gate_events()
        for path in (HOOKS, MESSAGE):
            module = self.checkout / path
            self.write(module, module.read_text() + "\n# Change the module.\n")
        self.read_modules.return_value = ("scripts/direct/test_first.py", HOOKS)
        self.mac_route.return_value = "fixture Mac busy"
        self.cloud_route.return_value = merge_gate.GateRun(
            (merge_gate.GateJobResult("message", "test_first.py", 0, 1, ""),), 1, 0)
        mode = merge_gate.SuiteMode.TARGETED_TEST
        run = merge_gate.run_gate_suites(self.checkout, ("scripts",), mode=mode)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assertIn("affected: 2 of 3 modules", self.output.getvalue())
        self.assert_routed(self.mac_route, HOOKS, mode=mode)
        self.assert_routed(self.cloud_route, MESSAGE, mode=mode)
        self.assertEqual([row["suite"] for row in self.module_events() if row["event"] == "start"], ["hooks"])
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)

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
