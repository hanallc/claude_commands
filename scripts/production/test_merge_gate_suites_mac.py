"""Mac routing and natedev verdicts for missing Mac passes."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, redirect_stdout, suppress
import io
import os
from pathlib import Path
import runpy
import select
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Literal, cast, final, override
from unittest import mock

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED
from scripts.direct.wait_for_test_state import wait_for_file, wait_until
from scripts.lib import process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import mac_gate
from scripts.production import mac_gate_process, merge_gate, targeted_test
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_routes import MacGateCancellation, ModuleCoverage, SuiteMode, TreeSelection
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.production.merge_gate_test_support import gate_suites


MAC_MODULE = "scripts.production.test_merge_gate_suites_mac"
HOOKS = "scripts/hooks/test_first.py"
LIB_FIRST = "scripts/lib/test_first.py"
LIB_SECOND = "scripts/lib/test_second.py"
MESSAGE = "scripts/message/test_first.py"


def route(repo: Path, *, mode: SuiteMode, full: bool, module_time_limit_s: float,
          selected: TreeSelection, cancellation: MacGateCancellation) -> merge_gate.GateRun | str:
    """Run a fake Mac route in the real child."""
    state = Path(os.environ["MERGE_GATE_TEST_STATE"])
    plan = cloud_gate.read(state / "mac-plan.json")
    cloud_gate.save(state / "mac-call.json", {
        "arguments": {"repo": str(repo), "mode": mode.value, "full": full, "limit": module_time_limit_s,
                      "paths": list(selected.paths), "tree": selected.tree},
        "pid": os.getpid(), "sid": os.getsid(0),
    })
    match plan["action"]:
        case "return":
            pass
        case "await-result":
            wait_for_file(state / "read-mac-result")
            readable, _, _ = select.select([cancellation.caller_fd], [], [], 0)
            assert not readable
        case "check-recovery":
            wait_for_file(state / "linux-registered")
            assert isinstance(merge_gate.ACTIVE_GATE_RECOVERY.get(), merge_gate.NoGateRecovery)
            _ = merge_gate.checked_git(repo, "rev-parse", "HEAD")
        case "cancel" | "hold":
            assert os.read(cancellation.caller_fd, 1) == b""
            (state / "cancelled").touch()
            if plan["action"] == "hold":
                _ = threading.Event().wait(TEST_HANG_GUARD_SECS)
        case "crash":
            os._exit(17)
        case "empty":
            os._exit(0)
        case "invalid":
            # The route command passes the result writer as its third argument.
            _ = os.write(int(sys.argv[3]), b"invalid JSON")
            os._exit(0)
        case _:
            raise ValueError("unknown Mac fixture action")
    return gate_box.decode_run(plan["gate"]) if "gate" in plan else ledger.text(plan, "miss")


TERM_PROBE = """
import os
from pathlib import Path
import signal
import sys
from threading import Event
from unittest.mock import patch

from scripts.production.gate_routes import SuiteMode, TreeSelection
from scripts.production.mac_gate_process import start_mac_gate
from scripts.production.merge_gate import ACTIVE_GATE_RECOVERY, GateRecovery, SharedCPUPool

state = Path(sys.argv[1])

with start_mac_gate(sys.argv[3], state, mode=SuiteMode.TARGETED_TEST, full=False,
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
        self.cloud_route = mock.Mock(return_value="fixture cloud unavailable")
        self.output = io.StringIO()

    @override
    def setUp(self) -> None:
        super().setUp()
        info_exclude = self.checkout / ".git/info/exclude"
        with info_exclude.open("a") as exclusions:
            _ = exclusions.write(f"\n/{mac_gate.MODULE_LIST}\n")
        self.exclude_modules()
        _ = self.enterContext(mock.patch.object(mac_gate_process, "MAC_GATE_MODULE", MAC_MODULE))
        _ = cast(object, self.enterContext(mock.patch.dict(sys.modules, {
            MAC_MODULE: SimpleNamespace(read_modules=mac_gate.read_modules),
        })))
        _ = cast(object, self.enterContext(mock.patch.object(cloud_gate, "route", self.cloud_route)))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        _ = self.enterContext(redirect_stdout(self.output))

    def set_mac_result(self, result: merge_gate.GateRun | str, *,
                       action: Literal["return", "await-result", "check-recovery", "cancel", "hold", "crash",
                                       "empty", "invalid"] = "return") -> None:
        (self.state / "mac-call.json").unlink(missing_ok=True)
        (self.state / "cancelled").unlink(missing_ok=True)
        plan = {"miss": result} if isinstance(result, str) else {"gate": gate_box.encode_run(result)}
        cloud_gate.save(self.state / "mac-plan.json", {**plan, "action": action})

    def exclude_modules(self, *paths: str) -> None:
        self.write(self.checkout / mac_gate.MODULE_LIST,
                   mac_gate.MODULE_LIST_HEADER + "".join(path + "\n" for path in paths))

    def assert_mac_routed(self, *paths: str) -> None:
        tree, _ = cloud_gate.working_tree(self.checkout)
        call = cloud_gate.read(self.state / "mac-call.json")
        self.assertEqual(call["arguments"], {
            "repo": str(self.checkout.resolve()), "mode": SuiteMode.TARGETED_TEST.value, "full": False,
            "limit": merge_gate.DEFAULT_MODULE_TIME_LIMIT_S, "paths": list(paths), "tree": tree,
        })

    def start_fixture_mac(self) -> AbstractContextManager[mac_gate_process.MacGateProcess]:
        return mac_gate_process.start_mac_gate(MAC_MODULE, self.checkout, mode=SuiteMode.TARGETED_TEST,
                                              full=False, module_time_limit_s=1,
                                              selected=TreeSelection((LIB_FIRST,), "tree"))

    @gate_suites("lib")
    def test_merge_gate_cli_uses_the_cloud_route_before_natedev_fallback(self) -> None:
        self.exclude_modules(LIB_FIRST)
        script = str(merge_gate.__file__)
        with (mock.patch.object(mac_gate_process, "load_mac_gate_route") as load_mac,
              mock.patch.object(sys, "argv", [script, "--tests-only", "--repo", str(self.checkout)]),
              self.assertRaises(SystemExit) as exited):
            _ = runpy.run_path(script, run_name="__main__")
        self.assertEqual(exited.exception.code, 0, self.output.getvalue())
        self.assert_all_modules_started()
        load_mac.assert_not_called()
        self.cloud_route.assert_called_once()
        self.assertIs(cast(object, self.cloud_route.call_args.kwargs["selected"]), merge_gate.ALL_MODULES)
        self.assertIs(cast(object, self.cloud_route.call_args.kwargs["mode"]), SuiteMode.MERGE_GATE)

    @gate_suites("lib")
    def test_targeted_test_cli_keeps_remote_assignments_and_mac_passes(self) -> None:
        self.exclude_modules(LIB_FIRST)
        second = merge_gate.GateJobResult("lib", "test_second.py", 0, 1, "")
        self.set_mac_result(merge_gate.GateRun((second,), 1, 0))
        status = targeted_test.main(["--repo", str(self.checkout)])
        self.assertEqual(status, 0, self.output.getvalue())
        self.cloud_route.assert_not_called()
        self.assert_mac_routed(LIB_SECOND)
        self.assertEqual([row["module"] for row in self.module_events() if row["event"] == "start"], ["test_first.py"])

    @gate_suites("lib")
    def test_targeted_gate_waits_for_the_mac_result_after_natedev_finishes(self) -> None:
        self.exclude_modules(LIB_FIRST)
        mac_output = "fixture Mac output\n" * 65_536
        mac_job = merge_gate.GateJobResult("lib", "test_second.py", 0, 1, mac_output)
        self.set_mac_result(merge_gate.GateRun((mac_job,), 1, 0), action="await-result")
        threads = set(threading.enumerate())
        run_suites = merge_gate.run_suites
        read_result = mac_gate_process.MacGateProcess.result

        def local_result(*_arguments: object, **_options: object) -> merge_gate.GateRun:
            self.assertIs(threading.current_thread(), threading.main_thread())
            wait_for_file(self.state / "mac-call.json")
            self.assertEqual(set(threading.enumerate()), threads)
            call = cloud_gate.read(self.state / "mac-call.json")
            self.assertNotEqual(call["pid"], os.getpid())
            self.assertEqual(call["sid"], call["pid"])
            return run_suites(self.checkout, selected=merge_gate.SelectedModules((LIB_FIRST,)))

        def release_mac(child: mac_gate_process.MacGateProcess) -> merge_gate.GateRun | mac_gate_process.MacGateMiss:
            self.assertEqual([row["event"] for row in self.module_events()], ["start", "finish"])
            (self.state / "read-mac-result").touch()
            return read_result(child)

        with (mock.patch.object(merge_gate, "run_suites", side_effect=local_result),
              mock.patch.object(mac_gate_process.MacGateProcess, "result", release_mac)):
            run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assert_mac_routed(LIB_SECOND)
        self.cloud_route.assert_not_called()
        self.assertEqual([row["module"] for row in self.module_events() if row["event"] == "start"], ["test_first.py"])
        self.assertEqual([job.module for job in run.jobs], ["test_first.py", "test_second.py"])
        self.assertEqual(run.jobs[1].output, mac_output)

    @gate_suites("hooks", "lib")
    def test_mac_miss_uses_the_natedev_verdict_and_keeps_mac_passes(self) -> None:
        self.exclude_modules(HOOKS)
        passed = merge_gate.GateJobResult("lib", "test_second.py", 0, 1, "")
        for mac_failure, local_failure in (("failure", False), ("time limit", True)):
            with self.subTest(mac_failure=mac_failure, local_failure=local_failure):
                self.clear_gate_events()
                red = self.state / "red-lib"
                if local_failure:
                    self.write(red, "red")
                report = "Mac time limit" if mac_failure == "time limit" else ""
                failed = merge_gate.GateJobResult("lib", "test_first.py", 1, 1, "fixture Mac red", killed_for=report)
                mac = merge_gate.GateRun((failed, passed), 1, 0, executions=(failed, passed), time_limit_report=report)
                self.set_mac_result(mac)
                mac_failed, mac_passed = gate_box.decode_run(gate_box.encode_run(mac)).jobs
                run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
                self.assertEqual(run.passed, not local_failure, self.output.getvalue())
                self.assertEqual(run.time_limit_report, "")
                self.assertEqual({row["suite"] for row in self.module_events() if row["event"] == "start"},
                                 {"hooks", "lib"})
                self.assertNotIn(mac_failed, run.jobs)
                self.assertIn(mac_failed, run.executions)
                self.assertIn(mac_passed, run.jobs)
                self.assertIn("hooks", [job.suite for job in run.jobs])
                self.assertFalse((self.state / "cancelled").exists())

    @gate_suites("hooks", "lib")
    def test_lost_mac_run_sends_all_its_modules_to_natedev(self) -> None:
        self.exclude_modules(HOOKS)
        self.set_mac_result("fixture Mac result lost")
        run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assert_mac_routed(LIB_FIRST, LIB_SECOND)
        self.cloud_route.assert_not_called()
        self.assertCountEqual([(row["suite"], row["module"]) for row in self.module_events()
                               if row["event"] == "start"],
                              [("hooks", "test_first.py"), ("lib", "test_first.py"), ("lib", "test_second.py")])
        self.assertIn("Mac gate uses natedev: fixture Mac result lost", self.output.getvalue())

    @gate_suites("lib")
    def test_lost_mac_time_and_both_linux_stop_reports_reach_the_result(self) -> None:
        self.exclude_modules(LIB_SECOND)
        self.set_mac_result("fixture Mac result lost")
        first = merge_gate.GateRun((), 3, 0, time_limit_report="First natedev time limit")
        local = merge_gate.GateRun((), 4, 0, time_limit_report="natedev time limit")
        miss = mac_gate_process.MacGateMiss("fixture Mac result lost", 8)
        with (mock.patch.object(mac_gate_process.MacGateProcess, "result", return_value=miss),
              mock.patch.object(merge_gate, "run_suites", side_effect=(first, local))):
            run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertEqual(run.seconds, 12.0)
        self.assertFalse(run.passed)
        self.assertEqual(run.time_limit_report, "First natedev time limit\nnatedev time limit")

    @gate_suites("lib")
    def test_linux_exception_and_sigint_kill_and_reap_the_mac_child(self) -> None:
        self.exclude_modules(LIB_FIRST)
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                self.set_mac_result("fixture cancelled", action="hold")

                def linux_error(*_arguments: object, **_options: object) -> merge_gate.GateRun:
                    wait_for_file(self.state / "mac-call.json")
                    if interrupt:
                        signal.raise_signal(signal.SIGINT)
                    raise GateRefused("fixture Linux exception")

                with (mock.patch.object(merge_gate, "run_suites", side_effect=linux_error),
                      mock.patch.object(process_group, "send_process_group",
                                        wraps=process_group.send_process_group) as stop_group):
                    with self.assertRaises(KeyboardInterrupt if interrupt else GateRefused):
                        _ = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
                pid = ledger.instant(cloud_gate.read(self.state / "mac-call.json"), "pid")
                stop_group.assert_any_call(pid, signal.SIGKILL)
                with self.assertRaises(ChildProcessError):
                    _ = os.waitpid(pid, os.WNOHANG)

    @gate_suites("lib")
    def test_context_exit_cancels_and_reaps_the_mac_child(self) -> None:
        self.set_mac_result("fixture cancelled", action="cancel")
        with self.start_fixture_mac() as mac:
            wait_for_file(self.state / "mac-call.json")
        self.assertTrue((self.state / "cancelled").exists())
        self.assertEqual(mac.process.returncode, 0)

    @gate_suites("lib")
    def test_unusable_mac_child_results_give_a_miss(self) -> None:
        cases: tuple[tuple[Literal["crash", "empty", "invalid"], str], ...] = (
            ("crash", "exited with status 17"),
            ("empty", "returned no result"),
            ("invalid", "returned an invalid result"),
        )
        for case, reason in cases:
            with self.subTest(case=case):
                self.set_mac_result("fixture miss", action=case)
                with self.start_fixture_mac() as mac:
                    result = mac.result()
                    assert isinstance(result, mac_gate_process.MacGateMiss)
                    self.assertIn(reason, result.reason)

    def assert_sigterm_closes_the_mac_pipe(self, *, protected: bool) -> None:
        source = Path(__file__).resolve().parents[2]
        mode = "protected" if protected else "idle"
        self.set_mac_result("fixture cancelled", action="cancel")
        with subprocess.Popen([sys.executable, "-c", TERM_PROBE, str(self.state), mode, MAC_MODULE], cwd=source,
                              stdin=subprocess.PIPE) as gate:
            try:
                wait_until((self.state / "mac-call.json").exists, self.state, processes=(gate,))
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
    def test_sigterm_leaves_no_mac_result_folder(self) -> None:
        temp_root = self.state / "mac-route-temp"
        temp_root.mkdir()
        with mock.patch.dict(os.environ, {"TMPDIR": str(temp_root)}):
            self.assert_sigterm_closes_the_mac_pipe(protected=False)
        pid = ledger.instant(cloud_gate.read(self.state / "mac-call.json"), "pid")
        self.assert_process_stopped(pid)
        self.assertEqual(list(temp_root.glob("mac-gate-result-*")), [])

    @gate_suites("lib")
    def test_sigterm_waits_for_linux_to_record_its_cpu_slot(self) -> None:
        self.assert_sigterm_closes_the_mac_pipe(protected=True)
        path = self.state / "recovery.json"
        self.assertTrue(path.exists(), "Linux lost its CPU slot record")
        token, = ledger.rows(cloud_gate.read(path), "tokens")
        self.assertEqual(token["value"], b"Q".hex())

    @gate_suites("lib")
    def test_only_linux_writes_recovery_during_concurrent_registration(self) -> None:
        self.exclude_modules(LIB_FIRST)
        recovery = merge_gate.GateRecovery(self.state / "recovery.json", str(self.checkout), os.getpid(), "fixture")
        writers: list[threading.Thread] = []
        save = recovery.save

        def record_writer() -> None:
            writers.append(threading.current_thread())
            save()

        def linux_result(*_arguments: object, **_options: object) -> merge_gate.GateRun:
            recovery.add_group(os.getpid())
            (self.state / "linux-registered").touch()
            return merge_gate.GateRun((merge_gate.GateJobResult("lib", "test_first.py", 0, 1, ""),), 1, 0)

        self.set_mac_result(merge_gate.GateRun((merge_gate.GateJobResult("lib", "test_second.py", 0, 1, ""),), 1, 0),
                            action="check-recovery")
        owner = merge_gate.ACTIVE_GATE_RECOVERY.set(recovery)
        try:
            with (mock.patch.object(recovery, "save", record_writer),
                  mock.patch.object(merge_gate, "run_suites", side_effect=linux_result)):
                run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        finally:
            merge_gate.ACTIVE_GATE_RECOVERY.reset(owner)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assertEqual(self.module_events(), [])
        self.assertEqual(set(writers), {threading.main_thread()})
        saved = cloud_gate.read(recovery.path)
        self.assertEqual(set(ledger.mapping(saved["groups"])), {str(os.getpid())})

    @gate_suites("hooks")
    def test_missing_mac_route_preserves_the_cloud_fallback(self) -> None:
        local = merge_gate.GateRun((merge_gate.GateJobResult("hooks", "test_first.py", 0, 1, ""),), 1, 0)
        with (mock.patch.object(merge_gate, "run_suites", return_value=local) as run_suites,
              mock.patch.dict(sys.modules, {MAC_MODULE: None})):
            run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertIs(run, local)
        tree, _ = cloud_gate.working_tree(self.checkout)
        self.cloud_route.assert_called_once_with(
            self.checkout, mode=SuiteMode.TARGETED_TEST, full=False,
            module_time_limit_s=merge_gate.DEFAULT_MODULE_TIME_LIMIT_S, selected=TreeSelection((HOOKS,), tree),
            gate_job=GATE_JOB_NOT_RECORDED)
        run_suites.assert_called_once_with(self.checkout, (), merge_gate.DEFAULT_MODULE_TIME_LIMIT_S,
                                           source="tests-only", full=False, mode=SuiteMode.TARGETED_TEST,
                                           route_again=mock.ANY)
        self.assertTrue(callable(cast(object, run_suites.call_args.kwargs["route_again"])))
        self.assertIn("Cloud gate uses natedev: fixture cloud unavailable", self.output.getvalue())
        self.assertFalse((self.state / "mac-call.json").exists())

    @gate_suites("hooks")
    def test_cloud_retry_selects_the_tree_changed_during_the_local_wait(self) -> None:
        original_tree, _ = cloud_gate.working_tree(self.checkout)
        cloud_result = merge_gate.GateRun((), 1, 0)
        added = "scripts/hooks/test_retry.py"

        def wait_locally(_repo: Path, *_args: object, route_again: Callable[[], None], **_options: object) -> merge_gate.GateRun:
            self.write(self.checkout / added, (self.checkout / HOOKS).read_text())
            route_again()
            self.fail("the successful retry must exit the local wait")

        self.cloud_route.side_effect = ["fixture cloud unavailable", cloud_result]
        with (mock.patch.object(merge_gate, "run_suites", side_effect=wait_locally),
              mock.patch.dict(sys.modules, {MAC_MODULE: None})):
            run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)

        updated_tree, _ = cloud_gate.working_tree(self.checkout)
        self.assertNotEqual(original_tree, updated_tree)
        self.assertIs(run, cloud_result)
        self.assertEqual([call.kwargs["selected"] for call in self.cloud_route.call_args_list],
                         [TreeSelection((HOOKS,), original_tree), TreeSelection((HOOKS, added), updated_tree)])

    @gate_suites("hooks")
    def test_missing_exclude_file_preserves_the_cloud_route(self) -> None:
        excluded = self.checkout / mac_gate.MODULE_LIST
        excluded.unlink()
        hooks = merge_gate.GateJobResult("hooks", "test_first.py", 0, 1, "")
        self.cloud_route.return_value = merge_gate.GateRun((hooks,), 1, 0)
        is_file = Path.is_file

        def avoid_probe(path: Path, *, follow_symlinks: bool = True) -> bool:
            self.assertNotEqual(path, excluded, "Read the exclude file without checking it first")
            return is_file(path, follow_symlinks=follow_symlinks)

        with mock.patch.object(Path, "is_file", avoid_probe):
            run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertEqual(run.jobs, (hooks,))
        self.cloud_route.assert_called_once()
        self.assertFalse((self.state / "mac-call.json").exists())

    @gate_suites("lib")
    def test_mac_queue_refusal_records_and_reuses_the_full_natedev_pass(self) -> None:
        self.set_mac_result("fixture Mac queue refusal")
        first = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(first.passed, self.output.getvalue())
        self.assertIs(first.coverage, ModuleCoverage.COMPLETE)
        self.assert_mac_routed(LIB_FIRST, LIB_SECOND)
        self.assert_all_modules_started()
        self.assertIn("Mac gate uses natedev: fixture Mac queue refusal", self.output.getvalue())
        record, = self.pass_records()
        self.assertEqual(record["kind"], "full")
        self.clear_gate_events()
        self.set_mac_result("fixture Mac queue refusal")
        second = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(second.tests_reused, self.output.getvalue())
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.pass_records(), [record])
        self.assertFalse((self.state / "mac-call.json").exists())

    @gate_suites("lib")
    def test_gate_with_exclusions_reuses_the_full_pass_before_starting_a_subset(self) -> None:
        self.exclude_modules(LIB_FIRST)
        local = merge_gate.run_suites(self.checkout)
        self.assertTrue(local.passed, self.output.getvalue())
        record, = self.pass_records()
        self.clear_gate_events()
        self.set_mac_result("fixture Mac queue refusal")

        run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)

        self.assertTrue(run.tests_reused, self.output.getvalue())
        self.assertIs(run.coverage, ModuleCoverage.COMPLETE)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.pass_records(), [record])
        self.assertFalse((self.state / "mac-call.json").exists())

    @gate_suites("lib")
    def test_later_gate_cannot_reuse_a_pass_for_one_module_in_the_same_suite(self) -> None:
        self.exclude_modules(LIB_FIRST)
        self.set_mac_result(merge_gate.GateRun(
            (merge_gate.GateJobResult("lib", "test_second.py", 0, 1, ""),), 1, 0))
        marker, _ = self.write_nightly_red_marker()
        first = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(first.passed, self.output.getvalue())
        self.assertEqual(self.pass_records(), [])
        self.assertEqual(self.gate_run_records()[-1]["selection"], "subset")
        self.assertTrue(marker.exists())
        marker.unlink()
        self.clear_gate_events()
        self.exclude_modules(LIB_FIRST, LIB_SECOND)
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
        self.exclude_modules("scripts/direct/test_first.py", HOOKS, MESSAGE)
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
        self.exclude_modules("scripts/direct/test_first.py", MESSAGE)
        self.set_mac_result("fixture Mac queue refusal")
        mode = merge_gate.SuiteMode.TARGETED_TEST
        with mock.patch.object(merge_gate, "read_time_limit", wraps=merge_gate.read_time_limit) as limit:
            run = merge_gate.run_gate_suites(self.checkout, ("scripts",), mode=mode)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assertIn("affected: 2 of 3 modules", self.output.getvalue())
        self.assertIn("Mac gate uses natedev: fixture Mac queue refusal", self.output.getvalue())
        self.assert_mac_routed(HOOKS)
        self.cloud_route.assert_not_called()
        self.assertEqual([row["suite"] for row in self.module_events() if row["event"] == "start"], ["message", "hooks"])
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)
        self.assertEqual(limit.call_count, 2)
        self.assertEqual([row.args[3] for row in limit.call_args_list], [True, True])
        self.assertEqual(self.pass_records(), passes)
        self.clear_gate_events()
        self.exclude_modules("scripts/direct/test_first.py", HOOKS, MESSAGE)
        later = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue(later.passed, self.output.getvalue())
        self.assertFalse(later.tests_reused)
        self.assert_all_modules_started()
        self.assertEqual(self.pass_records()[-1]["tree"], tree)

    @gate_suites("lib")
    def test_empty_exclude_list_sends_all_modules_to_the_mac(self) -> None:
        jobs = (merge_gate.GateJobResult("lib", "test_first.py", 0, 1, ""),
                merge_gate.GateJobResult("lib", "test_second.py", 0, 1, ""))
        self.set_mac_result(merge_gate.GateRun(jobs, 1, 0))
        run = merge_gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assert_mac_routed(LIB_FIRST, LIB_SECOND)
        self.cloud_route.assert_not_called()
        self.assertEqual(self.module_events(), [])
        self.assertEqual([job.kind for job in run.jobs],
                         [merge_gate.GateJobKind.TYPE_CHECK, merge_gate.GateJobKind.TEST_MODULE,
                          merge_gate.GateJobKind.TEST_MODULE])

    @gate_suites("hooks", "message")
    def test_all_excluded_affected_modules_reject_a_checkout_edit_during_the_run(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "merge_gate.conf", "scan_roots=\n")
        baseline = merge_gate.run_suites(self.checkout)
        self.assertTrue(baseline.passed, self.output.getvalue())
        self.clear_gate_events()
        selected = self.checkout / HOOKS
        self.write(selected, selected.read_text() + f"""
class CheckoutEdit(unittest.TestCase):
    def test_edit_the_checkout(self):
        with Path({MESSAGE!r}).open("a") as output:
            output.write("\\n# The checkout changed during the test.\\n")
""")
        _ = self.commit(self.checkout, "Add a test that edits the checkout")
        self.exclude_modules(HOOKS)
        with mock.patch.object(mac_gate_process, "start_mac_gate") as start_mac:
            with self.assertRaisesRegex(GateRefused, "The checkout changed during the split gate"):
                _ = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        start_mac.assert_not_called()
        self.cloud_route.assert_not_called()
        self.assertEqual([row["suite"] for row in self.module_events() if row["event"] == "start"], ["hooks"])
        self.assertIn("# The checkout changed during the test.", (self.checkout / MESSAGE).read_text())
