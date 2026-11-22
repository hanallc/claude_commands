"""Gate routing and the Mac route process."""

from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout, suppress
from dataclasses import replace
import io
import os
from pathlib import Path
import runpy
import select
import signal
import subprocess
import sys
import threading
from typing import Literal, cast, final, override
from unittest import mock

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client, admission_records, admission_wire, execution_identity, placement
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.build_targets import mac_linux
from scripts.buildlog.store import host_name
from scripts.direct.wait_for_test_state import wait_for_file, wait_until
from scripts.lib import conf, process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import claim_state, mac_gate
from scripts.production import mac_gate_process, gate, targeted_test
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_routes import MacGateCancellation, ModuleCoverage, SuiteMode, TreeSelection
from scripts.production.gate_suites_test_support import GateSuitesFixture
from scripts.production.gate_test_support import gate_suites


MAC_MODULE = "scripts.production.test_gate_suites_mac"
HOOKS = "scripts/hooks/test_first.py"
LIB_FIRST = "scripts/lib/test_first.py"
LIB_SECOND = "scripts/lib/test_second.py"
MESSAGE = "scripts/message/test_first.py"


def route(repo: Path, *, mode: SuiteMode, full: bool, module_time_limit_s: float,
          selected: TreeSelection, cancellation: MacGateCancellation) -> gate.GateRun | str:
    """Run a fake Mac route in the real child."""
    state = Path(os.environ["GATE_TEST_STATE"])
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
            assert isinstance(gate.ACTIVE_GATE_RECOVERY.get(), gate.NoGateRecovery)
            _ = gate.checked_git(repo, "rev-parse", "HEAD")
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
from scripts.production.gate import ACTIVE_GATE_RECOVERY, GateRecovery, SharedCPUPool

state = Path(sys.argv[1])

with start_mac_gate(sys.argv[3], state, mode=SuiteMode.TARGETED, full=False,
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
        self.placement_status = mock.Mock(side_effect=AssertionError("Unexpected placement status query."))
        self.output = io.StringIO()

    @override
    def setUp(self) -> None:
        super().setUp()
        info_exclude = self.checkout / ".git/info/exclude"
        with info_exclude.open("a") as exclusions:
            _ = exclusions.write(f"\n/{mac_gate.MODULE_LIST}\n")
        self.exclude_modules()
        _ = self.enterContext(mock.patch.object(mac_gate_process, "MAC_GATE_MODULE", MAC_MODULE))
        _ = self.enterContext(mock.patch.object(mac_gate_process, "load_mac_gate_route",
                                                side_effect=AssertionError("The gate loaded the Mac route")))
        _ = cast(object, self.enterContext(mock.patch.object(cloud_gate, "route", self.cloud_route)))
        _ = cast(object, self.enterContext(mock.patch.object(admission_client, "placement_status", self.placement_status)))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **self.environment, "BUILDLOG_PLACEMENT_FILE": "", "BUILDLOG_PLACEMENT_EPISODE": "episode",
        })))
        _ = self.enterContext(redirect_stdout(self.output))

    def set_mac_result(self, result: gate.GateRun | str, *,
                       action: Literal["return", "await-result", "check-recovery", "cancel", "hold", "crash",
                                       "empty", "invalid"] = "return") -> None:
        (self.state / "mac-call.json").unlink(missing_ok=True)
        (self.state / "cancelled").unlink(missing_ok=True)
        plan = {"miss": result} if isinstance(result, str) else {"gate": gate_box.encode_run(result)}
        cloud_gate.save(self.state / "mac-plan.json", {**plan, "action": action})

    def exclude_modules(self, *paths: str) -> None:
        self.write(self.checkout / mac_gate.MODULE_LIST,
                   mac_gate.MODULE_LIST_HEADER + "".join(path + "\n" for path in paths))

    def assert_targeted_cloud_selection(
        self, *paths: str, gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED,
    ) -> None:
        tree, _ = cloud_gate.working_tree(self.checkout)
        self.cloud_route.assert_called_once_with(
            self.checkout, mode=SuiteMode.TARGETED, full=False,
            module_time_limit_s=gate.DEFAULT_MODULE_TIME_LIMIT_S, selected=TreeSelection(paths, tree),
            gate_job=gate_job)
        self.assertFalse((self.state / "mac-call.json").exists())

    def start_fixture_mac(self) -> AbstractContextManager[mac_gate_process.MacGateProcess]:
        return mac_gate_process.start_mac_gate(MAC_MODULE, self.checkout, mode=SuiteMode.TARGETED,
                                              full=False, module_time_limit_s=1,
                                              selected=TreeSelection((LIB_FIRST,), "tree"))

    def gate_assignment(self, target: str) -> placement.PlacementAssignment:
        pid = os.getpid()
        start = admission_records.process_start_time(pid)
        assert start is not None
        destination = placement.PlacementDestination("ssh", target, "fixture-host", claim_state.WHOLE_MAC)
        return placement.PlacementAssignment("episode", "attempt", destination,
            execution_identity.SourceKeyKnown("fixture-source"), placement.SubmittingOwner(host_name(), pid, start),
            NoOwningSession(), 1.0)

    def write_gate_assignment(self, assignment: placement.PlacementAssignment) -> Path:
        path = self.state / "gate-assignment.json"
        admission_wire.write_assignment(path, admission_wire.Assignment(assignment))
        return path

    def set_placement_status(self, assignment: placement.PlacementAssignment,
                             state: placement.AssignmentState) -> None:
        self.placement_status.side_effect = None
        self.placement_status.return_value = admission_wire.PlacementStatusReply(
            admission_wire.KnownPlacementStatus(assignment, state, placement.NoProviderClaim()))

    def test_mac_linux_runs_the_whole_gate_when_enabled_and_placed(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        types = gate.GateJobResult("types", "basedpyright", 0, 1, "", kind=gate.GateJobKind.TYPE_CHECK)
        result = gate.GateRun((gate.GateJobResult("hooks", "test_first.py", 0, 1, ""), types), 1, 0)
        job = StepJob("integrate", unit="fixture")
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route", return_value=result) as guest,
              mock.patch.object(gate, "run_suites") as local):
            run = gate.run_gate_suites(self.checkout, ("scripts",), module_time_limit_s=30, full=True, gate_job=job)
        self.assertIs(run, result)
        guest.assert_called_once_with(self.checkout, mode=SuiteMode.GATE, full=True, module_time_limit_s=30,
                                     selected=gate.ALL_MODULES, gate_job=job, type_dirs=("scripts",))
        self.placement_status.assert_called_once_with("episode", "attempt", wait_for_service=False)
        self.cloud_route.assert_not_called()
        local.assert_not_called()

    @gate_suites("lib")
    def test_mac_linux_checks_types_in_the_guest_when_the_tests_are_reused(self) -> None:
        self.assertTrue(gate.run_suites(self.checkout).passed, self.output.getvalue())
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        tree, _ = cloud_gate.working_tree(self.checkout)
        types = gate.GateJobResult("types", "basedpyright", 0, 1, "", kind=gate.GateJobKind.TYPE_CHECK)
        result = gate.GateRun((types,), 1, 0, selection_certified=True)
        job = StepJob("test-affected")
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route", return_value=result) as guest,
              mock.patch.object(gate, "run_suites") as local):
            run = gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED, gate_job=job)
        self.assertIs(run, result)
        guest.assert_called_once_with(self.checkout, mode=SuiteMode.TARGETED, full=False,
                                     module_time_limit_s=gate.DEFAULT_MODULE_TIME_LIMIT_S,
                                     selected=TreeSelection((), tree), gate_job=job, type_dirs=("scripts",))
        self.cloud_route.assert_not_called()
        local.assert_not_called()

    @gate_suites("lib")
    def test_mac_linux_reuses_the_pass_without_starting_a_runner(self) -> None:
        self.assertTrue(gate.run_suites(self.checkout).passed, self.output.getvalue())
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route") as guest,
              mock.patch.object(gate, "run_suites") as local):
            run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, gate_job=StepJob("test-affected"))
        self.assertTrue(run.passed)
        self.assertTrue(run.tests_reused)
        self.assertEqual(run.jobs, ())
        self.assertIn("tests reused from", self.output.getvalue())
        guest.assert_not_called()
        self.cloud_route.assert_not_called()
        local.assert_not_called()

    def test_mac_linux_enabled_without_its_placement_uses_the_cloud(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        other = self.write_gate_assignment(self.gate_assignment("mac"))
        result = gate.GateRun((), 1, 0, selection_certified=True)
        self.cloud_route.return_value = result
        for path in ("", str(other)):
            with (self.subTest(path=path), mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": path}),
                  mock.patch.object(mac_linux, "route") as guest):
                self.assertIs(gate.run_gate_suites(self.checkout), result)
                guest.assert_not_called()
        self.assertEqual(self.cloud_route.call_count, 2)

    def test_mac_linux_off_refuses_the_current_assignment(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=off\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route") as guest,
              mock.patch.object(gate, "run_suites") as local, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(gate.main(["--tests-only", "--repo", str(self.checkout)]), 1)
        self.assertIn("Mac Linux guest gate refused with build_targets.conf enabled.mac-linux=off", errors.getvalue())
        guest.assert_not_called()
        self.cloud_route.assert_not_called()
        local.assert_not_called()

    def test_mac_linux_off_without_a_current_assignment_uses_cloud_and_natedev(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=off\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.ClosedAssignment("replaced"))
        result = gate.GateRun((), 2, 0, selection_certified=True)
        with (mock.patch.object(mac_linux, "route") as guest,
              mock.patch.object(gate, "run_suites", return_value=result) as local):
            for location in ("", str(path)):
                with mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": location}):
                    self.assertIs(gate.run_gate_suites(self.checkout, gate_job=StepJob("test-all")), result)
            guest.assert_not_called()
        self.assertEqual(local.call_count, 2)
        self.assertEqual(local.call_args_list[0], local.call_args_list[1])
        self.assertEqual(self.cloud_route.call_count, 2)
        self.assertEqual(self.cloud_route.call_args_list[0], self.cloud_route.call_args_list[1])
        self.assertEqual(self.output.getvalue().splitlines(), ["Cloud gate uses natedev: fixture cloud unavailable"] * 2)

    def test_non_guest_placement_skips_the_target_configuration(self) -> None:
        path = self.write_gate_assignment(self.gate_assignment("mac"))
        result = gate.GateRun((), 1, 0, selection_certified=True)
        self.cloud_route.return_value = result
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(conf, "read", wraps=conf.read) as read_config,
              mock.patch.object(mac_linux, "route") as guest):
            self.assertIs(gate.run_gate_suites(self.checkout, gate_job=StepJob("test-all")), result)
        self.assertFalse(any(call.args[0] == "build_targets" for call in read_config.call_args_list))
        self.cloud_route.assert_called_once()
        guest.assert_not_called()

    def test_old_attempt_goes_to_the_cloud(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        old = replace(self.gate_assignment("mac-linux"), attempt="old-attempt")
        path = self.write_gate_assignment(old)
        self.set_placement_status(old, placement.ClosedAssignment("replaced"))
        result = gate.GateRun((), 1, 0, selection_certified=True)
        self.cloud_route.return_value = result
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route") as guest):
            self.assertIs(gate.run_gate_suites(self.checkout, gate_job=StepJob("test-all")), result)
        self.placement_status.assert_called_once_with("episode", "old-attempt", wait_for_service=False)
        self.cloud_route.assert_called_once()
        guest.assert_not_called()

    def test_other_owner_or_episode_goes_to_the_cloud(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        result = gate.GateRun((), 1, 0, selection_certified=True)
        self.cloud_route.return_value = result
        for other in (replace(assignment, owner=replace(assignment.owner, start_time="previous-process")),
                      replace(assignment, episode="other-episode")):
            path = self.write_gate_assignment(other)
            with (self.subTest(assignment=other), mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
                  mock.patch.object(mac_linux, "route") as guest):
                self.assertIs(gate.run_gate_suites(self.checkout, gate_job=StepJob("test-all")), result)
                guest.assert_not_called()
        self.assertEqual(self.cloud_route.call_count, 2)
        self.placement_status.assert_not_called()

    def test_placement_query_overflow_goes_to_the_cloud(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        path = self.write_gate_assignment(self.gate_assignment("mac-linux"))
        self.placement_status.side_effect = OverflowError("fixture socket timeout overflow")
        result = gate.GateRun((), 1, 0, selection_certified=True)
        self.cloud_route.return_value = result
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route") as guest):
            self.assertIs(gate.run_gate_suites(self.checkout, gate_job=StepJob("test-all")), result)
        self.placement_status.assert_called_once_with("episode", "attempt", wait_for_service=False)
        self.cloud_route.assert_called_once()
        guest.assert_not_called()

    def test_invalid_assignment_goes_to_the_cloud(self) -> None:
        path = self.state / "gate-assignment.json"
        nested = sys.getrecursionlimit() + 1
        result = gate.GateRun((), 1, 0, selection_certified=True)
        self.cloud_route.return_value = result
        for payload in (b"[" * nested + b"0" + b"]" * nested, b" " * (admission_wire.ASSIGNMENT_MAX_BYTES + 1)):
            _ = path.write_bytes(payload)
            with (self.subTest(size=len(payload)),
                  mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
                  mock.patch.object(mac_linux, "route") as guest):
                self.assertIs(gate.run_gate_suites(self.checkout, gate_job=StepJob("test-all")), result)
                guest.assert_not_called()
        self.assertEqual(self.cloud_route.call_count, 2)
        self.placement_status.assert_not_called()

    def test_mac_linux_refusal_ends_the_gate(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        reason = "mac-linux: fixture guest unavailable"
        for mode, verb in ((SuiteMode.GATE, "test-all"), (SuiteMode.TARGETED, "test-affected")):
            with (self.subTest(mode=mode), mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
                  mock.patch.object(mac_linux, "route", return_value=reason) as guest,
                  mock.patch.object(gate, "run_suites") as local):
                with self.assertRaisesRegex(gate.MacLinuxGateRefused, f"Mac Linux guest gate failed: {reason}"):
                    _ = gate.run_gate_suites(self.checkout, mode=mode, full=True, gate_job=StepJob(verb))
                guest.assert_called_once()
                self.cloud_route.assert_not_called()
                local.assert_not_called()
        self.assertNotIn("falls back", self.output.getvalue())

    def test_merge_gate_cli_reports_the_mac_linux_refusal(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        for reason in ("mac-linux: fixture guest unavailable",
                       "mac-linux: guest unreachable: fixture-guest did not confirm the run ended within 2 seconds"):
            with (self.subTest(reason=reason), mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
                  mock.patch.object(mac_linux, "route", return_value=reason) as guest,
                  mock.patch.object(gate, "run_suites") as local, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(gate.main(["--tests-only", "--repo", str(self.checkout)]), 1)
            self.assertIn(f"REFUSED: Mac Linux guest gate failed: {reason}", errors.getvalue())
            guest.assert_called_once()
            self.cloud_route.assert_not_called()
            local.assert_not_called()

    def test_merge_gate_cli_reports_failed_mac_linux_jobs(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        jobs = (gate.GateJobResult("lib", "test_first.py", 1, 1, "first failure"),
                gate.GateJobResult("lib", "test_second.py", 2, 1, "second failure"),
                gate.GateJobResult("hooks", "test_first.py", 0, 1, "passed job"))
        result = mac_linux.MacLinuxGateRun(jobs, 3, 0, bundle_bytes=10, bundle_form=mac_linux.BundleForm.FIRST,
                                          bundle_reason=mac_linux.BundleReason.FIRST_RUN, module_skips={})
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route", return_value=result) as guest,
              mock.patch.object(gate, "run_suites") as local, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(gate.main(["--tests-only", "--repo", str(self.checkout)]), 1)
        output = errors.getvalue()
        self.assertIn("Mac Linux guest gate failed", output)
        self.assertIn("lib/test_first.py exit=1", output)
        self.assertIn("lib/test_second.py exit=2", output)
        self.assertIn("first failure", output)
        self.assertIn("second failure", output)
        self.assertNotIn("hooks/test_first.py", output)
        guest.assert_called_once()
        self.cloud_route.assert_not_called()
        local.assert_not_called()

    @gate_suites("lib")
    def test_merge_gate_cli_uses_the_cloud_route_before_natedev_fallback(self) -> None:
        self.exclude_modules(LIB_FIRST)
        script = str(gate.__file__)
        with (mock.patch.object(mac_gate_process, "load_mac_gate_route") as load_mac,
              mock.patch.object(sys, "argv", [script, "--tests-only", "--repo", str(self.checkout)]),
              self.assertRaises(SystemExit) as exited):
            _ = runpy.run_path(script, run_name="__main__")
        self.assertEqual(exited.exception.code, 0, self.output.getvalue())
        self.assert_all_modules_started()
        load_mac.assert_not_called()
        self.cloud_route.assert_called_once()
        self.assertIs(cast(object, self.cloud_route.call_args.kwargs["selected"]), gate.ALL_MODULES)
        self.assertIs(cast(object, self.cloud_route.call_args.kwargs["mode"]), SuiteMode.GATE)

    @gate_suites("lib")
    def test_targeted_test_cli_sends_the_whole_selection_to_the_cloud(self) -> None:
        self.exclude_modules(LIB_FIRST)
        jobs = (gate.GateJobResult("lib", "test_first.py", 0, 1, ""),
                gate.GateJobResult("lib", "test_second.py", 0, 1, ""))
        self.cloud_route.return_value = gate.GateRun(jobs, 1, 0)
        with mock.patch.object(gate, "run_suites") as local:
            status = targeted_test.main(["--repo", str(self.checkout)])
        self.assertEqual(status, 0, self.output.getvalue())
        self.assert_targeted_cloud_selection(LIB_FIRST, LIB_SECOND, gate_job=StepJob("test-affected"))
        local.assert_not_called()

    @gate_suites("lib")
    def test_mac_child_returns_large_output(self) -> None:
        mac_output = "fixture Mac output\n" * 65_536
        mac_job = gate.GateJobResult("lib", "test_first.py", 0, 1, mac_output)
        self.set_mac_result(gate.GateRun((mac_job,), 1, 0), action="await-result")
        threads = set(threading.enumerate())
        with self.start_fixture_mac() as child:
            wait_for_file(self.state / "mac-call.json")
            self.assertEqual(set(threading.enumerate()), threads)
            call = cloud_gate.read(self.state / "mac-call.json")
            self.assertNotEqual(call["pid"], os.getpid())
            self.assertEqual(call["sid"], call["pid"])
            (self.state / "read-mac-result").touch()
            run = child.result()
        assert isinstance(run, gate.GateRun), run
        job, = run.jobs
        self.assertEqual(job.output, mac_output)

    @gate_suites("hooks", "lib")
    def test_cloud_miss_uses_the_natedev_verdict_for_the_whole_selection(self) -> None:
        self.exclude_modules(HOOKS)
        paths = (HOOKS, LIB_FIRST, LIB_SECOND)
        red = self.state / "red-lib"
        for local_failure in (True, False):
            with self.subTest(local_failure=local_failure):
                self.clear_gate_events()
                self.cloud_route.reset_mock()
                if local_failure:
                    self.write(red, "red")
                else:
                    red.unlink()
                run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED)
                self.assertEqual(run.passed, not local_failure, self.output.getvalue())
                self.assert_targeted_cloud_selection(*paths)
                self.assertCountEqual([f"scripts/{job.suite}/{job.module}" for job in run.jobs], paths)

    @gate_suites("hooks", "lib")
    def test_full_targeted_gate_uses_the_cloud_before_natedev(self) -> None:
        self.exclude_modules(HOOKS)
        run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, full=True)
        self.assertTrue(run.passed, self.output.getvalue())
        self.cloud_route.assert_called_once_with(
            self.checkout, mode=SuiteMode.TARGETED, full=True,
            module_time_limit_s=gate.DEFAULT_MODULE_TIME_LIMIT_S, selected=gate.ALL_MODULES,
            gate_job=GATE_JOB_NOT_RECORDED)
        self.assert_all_modules_started()
        self.assertIn("Cloud gate uses natedev: fixture cloud unavailable", self.output.getvalue())

    @gate_suites("lib")
    def test_cloud_stop_report_reaches_the_result_without_running_local_types(self) -> None:
        self.exclude_modules(LIB_SECOND)
        stopped = gate.GateRun((), 3, 0, time_limit_report="Cloud time limit")
        self.cloud_route.return_value = stopped
        with mock.patch.object(gate, "run_suites") as local:
            run = gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED)
        self.assertIs(run, stopped)
        self.assertFalse(run.passed)
        self.assertEqual(run.time_limit_report, "Cloud time limit")
        self.assert_targeted_cloud_selection(LIB_FIRST, LIB_SECOND)
        local.assert_not_called()

    @gate_suites("lib")
    def test_mac_process_context_kills_and_reaps_on_exception_and_sigint(self) -> None:
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                self.set_mac_result("fixture cancelled", action="hold")
                with mock.patch.object(process_group, "send_process_group",
                                       wraps=process_group.send_process_group) as stop_group:
                    with (self.assertRaises(KeyboardInterrupt if interrupt else GateRefused),
                          self.start_fixture_mac()):
                        wait_for_file(self.state / "mac-call.json")
                        if interrupt:
                            signal.raise_signal(signal.SIGINT)
                        raise GateRefused("fixture Linux exception")
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
        recovery = gate.GateRecovery(self.state / "recovery.json", str(self.checkout), os.getpid(), "fixture")
        writers: list[threading.Thread] = []
        save = recovery.save

        def record_writer() -> None:
            writers.append(threading.current_thread())
            save()

        self.set_mac_result(gate.GateRun((gate.GateJobResult("lib", "test_first.py", 0, 1, ""),), 1, 0),
                            action="check-recovery")
        owner = gate.ACTIVE_GATE_RECOVERY.set(recovery)
        try:
            with (mock.patch.object(recovery, "save", record_writer),
                  self.start_fixture_mac() as child):
                recovery.add_group(os.getpid())
                (self.state / "linux-registered").touch()
                run = child.result()
        finally:
            gate.ACTIVE_GATE_RECOVERY.reset(owner)
        assert isinstance(run, gate.GateRun), run
        self.assertTrue(run.passed, self.output.getvalue())
        self.assertEqual(self.module_events(), [])
        self.assertEqual(set(writers), {threading.main_thread()})
        saved = cloud_gate.read(recovery.path)
        self.assertEqual(set(ledger.mapping(saved["groups"])), {str(os.getpid())})

    @gate_suites("hooks")
    def test_missing_mac_route_preserves_the_cloud_fallback(self) -> None:
        local = gate.GateRun((gate.GateJobResult("hooks", "test_first.py", 0, 1, ""),), 1, 0)
        with (mock.patch.object(gate, "run_suites", return_value=local) as run_suites,
              mock.patch.dict(sys.modules, {MAC_MODULE: None})):
            run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED)
        self.assertIs(run, local)
        tree, _ = cloud_gate.working_tree(self.checkout)
        self.cloud_route.assert_called_once_with(
            self.checkout, mode=SuiteMode.TARGETED, full=False,
            module_time_limit_s=gate.DEFAULT_MODULE_TIME_LIMIT_S, selected=TreeSelection((HOOKS,), tree),
            gate_job=GATE_JOB_NOT_RECORDED)
        run_suites.assert_called_once_with(self.checkout, (), gate.DEFAULT_MODULE_TIME_LIMIT_S,
                                           source="tests-only", full=False, mode=SuiteMode.TARGETED)
        self.assertIn("Cloud gate uses natedev: fixture cloud unavailable", self.output.getvalue())
        self.assertFalse((self.state / "mac-call.json").exists())

    @gate_suites("hooks")
    def test_missing_exclude_file_preserves_the_cloud_route(self) -> None:
        excluded = self.checkout / mac_gate.MODULE_LIST
        excluded.unlink()
        hooks = gate.GateJobResult("hooks", "test_first.py", 0, 1, "")
        self.cloud_route.return_value = gate.GateRun((hooks,), 1, 0)
        is_file = Path.is_file

        def avoid_probe(path: Path, *, follow_symlinks: bool = True) -> bool:
            self.assertNotEqual(path, excluded, "Read the exclude file without checking it first")
            return is_file(path, follow_symlinks=follow_symlinks)

        with mock.patch.object(Path, "is_file", avoid_probe):
            run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED)
        self.assertEqual(run.jobs, (hooks,))
        self.cloud_route.assert_called_once()
        self.assertFalse((self.state / "mac-call.json").exists())

    @gate_suites("lib")
    def test_cloud_refusal_records_and_reuses_the_full_natedev_pass(self) -> None:
        first = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED)
        self.assertTrue(first.passed, self.output.getvalue())
        self.assertIs(first.coverage, ModuleCoverage.COMPLETE)
        self.assert_targeted_cloud_selection(LIB_FIRST, LIB_SECOND)
        self.assert_all_modules_started()
        self.assertIn("Cloud gate uses natedev: fixture cloud unavailable", self.output.getvalue())
        record, = self.pass_records()
        self.assertEqual(record["kind"], "full")
        self.clear_gate_events()
        self.cloud_route.reset_mock()
        second = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED)
        self.assertTrue(second.tests_reused, self.output.getvalue())
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.pass_records(), [record])
        self.cloud_route.assert_not_called()
        self.assertFalse((self.state / "mac-call.json").exists())

    @gate_suites("lib")
    def test_gate_with_exclusions_reuses_the_full_pass_before_starting_a_subset(self) -> None:
        self.exclude_modules(LIB_FIRST)
        local = gate.run_suites(self.checkout)
        self.assertTrue(local.passed, self.output.getvalue())
        record, = self.pass_records()
        self.clear_gate_events()
        self.set_mac_result("fixture Mac queue refusal")

        run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED)

        self.assertTrue(run.tests_reused, self.output.getvalue())
        self.assertIs(run.coverage, ModuleCoverage.COMPLETE)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.pass_records(), [record])
        self.assertFalse((self.state / "mac-call.json").exists())

    @gate_suites("lib")
    def test_later_gate_cannot_reuse_a_pass_for_one_module_in_the_same_suite(self) -> None:
        marker, _ = self.write_nightly_red_marker()
        first = gate.run_suites(self.checkout, selected=gate.SelectedModules((LIB_FIRST,)))
        self.assertTrue(first.passed, self.output.getvalue())
        self.assertEqual(self.pass_records(), [])
        self.assertEqual(self.gate_run_records()[-1]["selection"], "subset")
        self.assertTrue(marker.exists())
        marker.unlink()
        self.clear_gate_events()
        second = gate.run_gate_suites(self.checkout)
        self.assertTrue(second.passed, self.output.getvalue())
        self.assertFalse(second.tests_reused)
        self.assert_all_modules_started()
        record, = self.pass_records()
        self.assertEqual(record["kind"], "full")
        self.clear_gate_events()
        assigned = gate.run_suites(self.checkout, selected=gate.SelectedModules((LIB_FIRST,)))
        self.assertFalse(assigned.tests_reused)
        self.assertIs(assigned.coverage, ModuleCoverage.SUBSET)
        self.assertEqual([row["module"] for row in self.module_events() if row["event"] == "start"], ["test_first.py"])
        self.assertEqual(self.pass_records(), [record])

    @gate_suites("direct", "hooks", "message")
    def test_targeted_gate_sends_all_affected_modules_to_the_cloud_and_runs_types_once(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "gate.conf", "scan_roots=\n")
        self.exclude_modules("scripts/direct/test_first.py", MESSAGE)
        baseline = gate.run_gate_suites(self.checkout)
        self.assertTrue(baseline.passed, self.output.getvalue())
        passes = self.pass_records()
        self.cloud_route.reset_mock()
        self.clear_gate_events()
        for path in (HOOKS, MESSAGE):
            module = self.checkout / path
            self.write(module, module.read_text() + "\n# Change the module.\n")
        _ = self.commit(self.checkout, "Change two test modules")
        tree = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
        jobs = (gate.GateJobResult("hooks", "test_first.py", 0, 1, ""),
                gate.GateJobResult("message", "test_first.py", 0, 1, ""))
        self.cloud_route.return_value = gate.GateRun(jobs, 1, 0)
        with mock.patch.object(gate, "read_time_limit", wraps=gate.read_time_limit) as limit:
            run = gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assertIn("affected: 2 of 3 modules", self.output.getvalue())
        self.assert_targeted_cloud_selection(HOOKS, MESSAGE)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(run.jobs[:2], jobs)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)
        limit.assert_called_once()
        self.assertTrue(limit.call_args.args[3])
        self.assertEqual(self.pass_records(), passes)
        self.clear_gate_events()
        self.cloud_route.return_value = "fixture cloud unavailable"
        later = gate.run_gate_suites(self.checkout)
        self.assertTrue(later.passed, self.output.getvalue())
        self.assertFalse(later.tests_reused)
        self.assert_all_modules_started()
        self.assertEqual(self.pass_records()[-1]["tree"], tree)

    @gate_suites("lib")
    def test_empty_exclude_list_sends_all_modules_to_the_cloud(self) -> None:
        jobs = (gate.GateJobResult("lib", "test_first.py", 0, 1, ""),
                gate.GateJobResult("lib", "test_second.py", 0, 1, ""))
        self.cloud_route.return_value = gate.GateRun(jobs, 1, 0)
        run = gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED)
        self.assertTrue(run.passed, self.output.getvalue())
        self.assert_targeted_cloud_selection(LIB_FIRST, LIB_SECOND)
        self.assertEqual(self.module_events(), [])
        self.assertEqual([job.kind for job in run.jobs],
                         [gate.GateJobKind.TEST_MODULE, gate.GateJobKind.TEST_MODULE, gate.GateJobKind.TYPE_CHECK])

    @gate_suites("hooks", "message")
    def test_cloud_receives_affected_modules_when_all_are_excluded_from_the_mac(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "gate.conf", "scan_roots=\n")
        baseline = gate.run_suites(self.checkout)
        self.assertTrue(baseline.passed, self.output.getvalue())
        self.clear_gate_events()
        selected = self.checkout / HOOKS
        self.write(selected, selected.read_text() + "\n# Change the module.\n")
        _ = self.commit(self.checkout, "Change one test module")
        self.exclude_modules(HOOKS)
        job = gate.GateJobResult("hooks", "test_first.py", 0, 1, "")
        completed = gate.GateRun((job,), 1, 0)
        self.cloud_route.return_value = completed
        run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED)
        self.assertIs(run, completed)
        self.assert_targeted_cloud_selection(HOOKS)
        self.assertEqual(self.module_events(), [])
