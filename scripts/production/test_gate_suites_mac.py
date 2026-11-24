"""Gate routing and the Mac route process."""

from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout, suppress
from dataclasses import replace
from functools import partial
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
from scripts.build_hold import build_hold
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client, admission_records, admission_wire, execution_identity, first_step, placement, progress_profile
from scripts.build_queue import test_placement as placement_fixture
from scripts.build_queue.admission_test_support import Exchange, FakeClock, ScriptedTransport, reply_bytes
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.build_targets import mac_linux, target_config
from scripts.buildlog.store import host_name
from scripts.direct.wait_for_test_state import wait_for_file, wait_until
from scripts.lib import conf, process_group, scratch
from scripts.lib.build_slots import SlotQueuePosition
from scripts.build_queue.placement import MAC_LINUX_NO_ANSWER
from scripts.lib.build_steps import CLAIM_FAILURE_SUFFIX, MAC_LINUX_PRELAUNCH_REFUSED_STATUS, gate_job_for_command
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import claim_state, mac_gate
from scripts.production import mac_gate_process, gate, targeted_test
from scripts.copies import copies
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_routes import MacGateCancellation, ModuleCoverage, SuiteMode, TreeSelection
from scripts.production.gate_suites_test_support import GateSuitesFixture
from scripts.production.gate_test_support import gate_suites


MAC_MODULE = "scripts.production.test_gate_suites_mac"
HOOKS = "scripts/hooks/test_first.py"
LIB_FIRST = "scripts/lib/test_first.py"
LIB_SECOND = "scripts/lib/test_second.py"
MESSAGE = "scripts/message/test_first.py"
LIVE_PLACEMENT_STATUS = admission_client.placement_status


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


def queue_release(queue: placement_fixture.PlacementFixture, reservation: str, status: int,
                  _timing: admission_wire.CleanupTiming,
                  ending: admission_wire.ReleaseEnding) -> admission_wire.Ok | admission_wire.Refused:
    """Send a release to the fixture queue in place of the admission socket."""
    ended, = queue.connect(admission_wire.ReleaseRequest(
        admission_client.request_id(), reservation, status, ending)).replies()
    assert isinstance(ended, (admission_wire.Ok, admission_wire.Refused)), ended
    return ended


def queue_cancel(queue: placement_fixture.PlacementFixture, request: admission_wire.CancelPlacement
                 ) -> admission_wire.Ok:
    """Send a placement cancel to the fixture queue in place of the admission socket."""
    cancelled, = queue.connect(request).replies()
    assert isinstance(cancelled, admission_wire.Ok), cancelled
    return cancelled


def checked_target(mode: SuiteMode, gate_job: StepJob) -> gate.GateTarget:
    """Return the target of the placement that the gate checks. The check changes no queue state."""
    return gate.placement_target(gate.current_gate_placement(mode, gate_job))


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
            **self.environment, "BUILDLOG_PLACEMENT_EPISODE": "episode",
        })))
        _ = os.environ.pop("BUILDLOG_PLACEMENT_FILE", None)
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

    def gate_assignment(self, target: str, *,
                        provider: Literal["ssh", "scaler"] = "ssh") -> placement.PlacementAssignment:
        pid = os.getpid()
        start = admission_records.process_start_time(pid)
        assert start is not None
        destination = placement.PlacementDestination(provider, target, "fixture-host", claim_state.WHOLE_MAC)
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

    def assert_gate_refuses_placement(self, path: Path | str, reason: str) -> None:
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route") as guest,
              mock.patch.object(gate, "run_suites") as local, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(gate.main(["--tests-only", "--repo", str(self.checkout)]), 1)
        self.assertIn("REFUSED: " + reason, errors.getvalue())
        self.cloud_route.assert_not_called()
        guest.assert_not_called()
        local.assert_not_called()

    def test_gate_follows_the_assignment_from_a_two_machine_request(self) -> None:
        command = ("gate.py", "--tests-only")
        job = StepJob("test-all")
        local_result = gate.GateRun((), 1, 0, selection_certified=True)
        guest_result = gate.GateRun((), 2, 0, selection_certified=True)
        for expected, eligibility in (("mac-linux", placement.AllowedHost()),
                                      ("natedev", placement.TemporaryExclusion("disk"))):
            with self.subTest(target=expected), placement_fixture.PlacementFixture() as queue:
                _ = write_config_fixture(queue.root / "build_targets.conf", "targets=mac-linux,natedev\nenabled.mac-linux=on\n")
                _ = write_config_fixture(queue.root / "mac_linux.conf", "# range: 1..\nprobe_timeout_s=3\n")
                request = placement_fixture.placement_request(queue)
                assert isinstance(request.local, admission_wire.LocalAdmissionContext)
                assert isinstance(request.profile, admission_records.StepProfile)
                sidecar = str(queue.root / "placement.cgroup")
                guest_target = placement.PlacementDestination("ssh", "mac-linux", "fixture-mac", claim_state.WHOLE_MAC)
                request = replace(request, profile=replace(request.profile, step="gate"), local=replace(
                    request.local, argv=command, sidecar=sidecar),
                    constraints=replace(request.constraints, command=command,
                    repository=str(self.checkout), candidates=(placement.PlacementCandidate(guest_target),
                                                               placement_fixture.local_candidate(queue))))
                _ = placement_fixture.publish_candidate(queue, request, eligibility)
                reply, = queue.connect(request).replies()
                assert isinstance(reply, (admission_wire.Assignment, admission_wire.LocalPlacementGranted)), reply
                assignment = reply.assignment
                self.assertEqual(assignment.destination.target, expected)
                status, = queue.connect(admission_wire.PlacementStatusRequest(assignment.episode, assignment.attempt)).replies()
                assert isinstance(status, admission_wire.PlacementStatusReply), status
                self.placement_status.side_effect = None
                self.placement_status.return_value = status
                path = self.state / "gate-assignment.json"
                admission_wire.write_assignment(path, reply)
                owner = build_hold.ProcessIdentity(assignment.owner.pid, assignment.owner.start_time)
                with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path),
                                                  "BUILDLOG_PLACEMENT_EPISODE": assignment.episode,
                                                  "BUILDLOG_PLACEMENT_SIDECAR": sidecar}),
                      mock.patch.object(build_hold, "caller_processes", return_value=frozenset({owner})),
                      mock.patch.object(gate, "gate_job_from_process", return_value=job),
                      mock.patch.object(admission_client, "release", side_effect=partial(queue_release, queue)) as released,
                      mock.patch.object(mac_linux, "route", return_value=guest_result) as guest,
                      mock.patch.object(gate, "run_suites", return_value=local_result) as local):
                    result = gate.run_gate_suites(self.checkout, gate_job=job,
                                                  target=checked_target(SuiteMode.GATE, job))
                if expected == "mac-linux":
                    self.assertIs(result, guest_result)
                    guest.assert_called_once()
                    local.assert_not_called()
                    released.assert_not_called()
                else:
                    assert isinstance(reply, admission_wire.LocalPlacementGranted)
                    self.assertIs(result, local_result)
                    guest.assert_not_called()
                    local.assert_called_once()
                    released.assert_called_once_with(reply.grant.reservation, 0, reply.grant.cleanup_timing, "result")
                self.cloud_route.assert_not_called()

    def natedev_grant(self, queue: placement_fixture.PlacementFixture, targets: tuple[str, ...]
                      ) -> tuple[admission_wire.PlacementRequest, admission_wire.LocalPlacementGranted]:
        """Place a gate that names these targets with the guest busy, so natedev takes it."""
        command = ("gate.py", "--tests-only")
        _ = write_config_fixture(queue.root / "build_targets.conf", "targets=mac-linux,natedev\nenabled.mac-linux=on\n")
        _ = write_config_fixture(queue.root / "mac_linux.conf", "# range: 1..\nprobe_timeout_s=3\n")
        request = placement_fixture.placement_request(queue)
        assert isinstance(request.local, admission_wire.LocalAdmissionContext)
        assert isinstance(request.profile, admission_records.StepProfile)
        guest = placement.PlacementCandidate(
            placement.PlacementDestination("ssh", "mac-linux", "fixture-mac", claim_state.WHOLE_MAC))
        named = tuple(candidate for candidate in (guest, placement_fixture.local_candidate(queue))
                      if candidate.destination.target in targets)
        request = replace(request, profile=replace(request.profile, step="gate"), local=replace(
            request.local, argv=command, sidecar=str(queue.root / "placement.cgroup")),
            constraints=replace(request.constraints, command=command,
            repository=str(self.checkout), candidates=named))
        if guest in named:
            _ = placement_fixture.publish_candidate(queue, request, placement.TemporaryExclusion("disk"))
        reply, = queue.connect(request).replies()
        assert isinstance(reply, admission_wire.LocalPlacementGranted), reply
        self.placement_status.side_effect = partial(self.fixture_status, queue)
        return request, reply

    def fixture_status(self, queue: placement_fixture.PlacementFixture, episode: str, attempt: str
                       ) -> admission_wire.PlacementStatusReply:
        """Read the placement status from the fixture queue in place of the admission socket."""
        status, = queue.connect(admission_wire.PlacementStatusRequest(episode, attempt)).replies()
        assert isinstance(status, admission_wire.PlacementStatusReply), status
        return status

    def hand_off_a_natedev_gate(self, queue: placement_fixture.PlacementFixture, targets: tuple[str, ...]
                                ) -> tuple[admission_wire.LocalPlacementGranted, first_step.FirstStepAdmission]:
        """Place a natedev gate, then hand it off as copies.py does."""
        request, reply = self.natedev_grant(queue, targets)
        path = self.state / "gate-assignment.json"
        admission_wire.write_assignment(path, reply)
        admitted = first_step.FirstStepAdmission(SlotQueuePosition(reply.assignment.episode, reply.assignment.since),
            first_step.GatePlacement(reply, request), path)
        admitted.handoff(self.checkout)
        self.assertEqual(self.queue_state(reply), placement.CurrentAssignment())
        return reply, admitted

    def queue_state(self, reply: admission_wire.LocalPlacementGranted) -> placement.AssignmentState:
        status = admission_client.placement_status(reply.assignment.episode, reply.assignment.attempt).status
        assert isinstance(status, admission_wire.KnownPlacementStatus), status
        return status.state

    def assert_reservation_ended(self, reply: admission_wire.LocalPlacementGranted) -> None:
        self.assertEqual(self.queue_state(reply), placement.ClosedAssignment("local reservation has ended"))

    def run_handed_off_gate(self, targets: tuple[str, ...], *arguments: str, mode_flag: str = "--tests-only"
                            ) -> tuple[int, str, admission_wire.LocalPlacementGranted, mock.MagicMock, mock.MagicMock]:
        """Hand off a natedev gate, then run gate.main with this mode flag and arguments as the gate process does."""
        argv = [mode_flag, "--repo", str(self.checkout), *arguments]
        run_real = mock.Mock(wraps=gate.run_suites)
        with placement_fixture.PlacementFixture() as queue, mock.patch.dict(os.environ):
            def run_local(repo: Path, *arguments: object, **options: object) -> gate.GateRun:
                self.assertEqual(self.queue_state(reply), placement.CurrentAssignment())
                grant = cast(gate.LocalGateAssignment, options["local_grant"])
                self.assertEqual((grant.grant, grant.owner), (reply.grant, reply.assignment.owner))
                run = cast(gate.GateRun, run_real(repo, *arguments, **options))
                self.assertEqual(self.queue_state(reply), placement.CurrentAssignment())
                return run

            with mock.patch.object(admission_client, "release", side_effect=partial(queue_release, queue)) as released:
                reply, _ = self.hand_off_a_natedev_gate(queue, targets)
                released.assert_not_called()
                owner = build_hold.ProcessIdentity(reply.assignment.owner.pid, reply.assignment.owner.start_time)
                with (mock.patch.object(build_hold, "caller_processes", return_value=frozenset({owner})),
                      mock.patch.object(gate, "gate_job_from_process",
                                        return_value=gate_job_for_command(["gate.py", *argv])),
                      mock.patch.object(mac_linux, "route") as guest,
                      mock.patch.object(gate, "_gate_job_parent", return_value=self.fixture_root.parent),
                      mock.patch.object(gate, "gate_reservation") as second_reservation,
                      mock.patch.object(gate, "run_suites", side_effect=run_local) as local,
                      redirect_stderr(io.StringIO()) as errors):
                    status = gate.main(argv)
                second_reservation.assert_not_called()
            guest.assert_not_called()
            self.cloud_route.assert_not_called()
            return status, errors.getvalue(), reply, released, local

    def test_a_gate_that_names_natedev_and_mac_linux_runs_on_natedev_after_the_handoff(self) -> None:
        status, errors, reply, released, local = self.run_handed_off_gate(("natedev", "mac-linux"))
        self.assertEqual((status, errors), (0, ""), self.output.getvalue())
        released.assert_called_once_with(reply.grant.reservation, 0, reply.grant.cleanup_timing, "result")
        local.assert_called_once()

    def test_a_targeted_gate_that_names_only_natedev_runs_on_natedev_after_the_handoff(self) -> None:
        status, errors, reply, released, local = self.run_handed_off_gate(("natedev",), mode_flag="--affected")
        self.assertEqual((status, errors), (0, ""), self.output.getvalue())
        released.assert_called_once_with(reply.grant.reservation, 0, reply.grant.cleanup_timing, "result")
        local.assert_called_once()

    @gate_suites("lib")
    def test_natedev_suites_publish_progress_to_the_granted_sidecar(self) -> None:
        publish = progress_profile.publish_counter
        seen: list[progress_profile.ProgressCounter] = []
        sidecars: set[Path] = set()

        def read_progress(path: Path, counter: progress_profile.ProgressCounter) -> None:
            sidecar = Path(os.environ["BUILDLOG_PLACEMENT_SIDECAR"])
            sidecars.add(sidecar)
            publish(path, counter)
            self.assertEqual(progress_profile.read_counter(sidecar), counter)
            seen.append(counter)

        with mock.patch.object(progress_profile, "publish_counter", side_effect=read_progress):
            status, errors, _, _, _ = self.run_handed_off_gate(("natedev",))
        self.assertEqual((status, errors), (0, ""), self.output.getvalue())
        self.assertEqual(seen[0].done, 0)
        self.assertEqual(seen[-1].done, seen[-1].total)
        sidecar, = sidecars
        self.assertIsInstance(progress_profile.read_counter(sidecar), progress_profile.NoCounter)

    @gate_suites("lib")
    def test_a_named_module_gate_keeps_the_natedev_reservation_until_it_finishes(self) -> None:
        status, errors, reply, released, local = self.run_handed_off_gate(
            ("natedev", "mac-linux"), "--module", "lib/test_first.py")
        self.assertEqual((status, errors), (0, ""), self.output.getvalue())
        released.assert_called_once_with(reply.grant.reservation, 0, reply.grant.cleanup_timing, "result")
        local.assert_called_once()

    def test_promote_only_refuses_a_mac_linux_assignment_before_any_work(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(admission_client, "release") as released,
              mock.patch.object(scratch, "copy_lock") as locked,
              mock.patch.object(gate, "promote") as promoted,
              mock.patch.object(mac_linux, "route") as guest,
              mock.patch.object(gate, "run_suites") as local, redirect_stderr(io.StringIO()) as errors):
            status = gate.main(["--promote-only", "--merge-branch", "x", "--repo", str(self.checkout)])
        self.assertEqual(status, 1)
        refusal = "gate.py --promote-only runs only on natedev and cannot take the mac-linux assignment"
        self.assertEqual(errors.getvalue(), f"REFUSED: {refusal}\n")
        for work in (released, locked, promoted, guest, local, self.cloud_route):
            work.assert_not_called()

    def test_closing_a_handed_off_natedev_gate_ends_its_reservation_and_assignment(self) -> None:
        with placement_fixture.PlacementFixture() as queue, mock.patch.dict(os.environ):
            reply, admitted = self.hand_off_a_natedev_gate(queue, ("natedev", "mac-linux"))
            self.assertIn(reply.assignment.episode, queue.server.reservations)
            with mock.patch.object(admission_client, "cancel_placement",
                                   side_effect=partial(queue_cancel, queue)) as cancelled:
                admitted.close()
            cancelled.assert_called_once()
            self.assertNotIn(reply.assignment.episode, queue.server.reservations)
            self.assertEqual(self.queue_state(reply), placement.ClosedAssignment("cancelled"))

    def test_a_handed_off_natedev_gate_refuses_a_reservation_that_ended(self) -> None:
        argv = ["--tests-only", "--repo", str(self.checkout)]
        with placement_fixture.PlacementFixture() as queue, mock.patch.dict(os.environ):
            reply, _ = self.hand_off_a_natedev_gate(queue, ("natedev", "mac-linux"))
            ended, = queue.connect(admission_wire.ReleaseRequest("fixture-ended", reply.grant.reservation, 0)).replies()
            self.assertIsInstance(ended, admission_wire.Ok)
            owner = build_hold.ProcessIdentity(reply.assignment.owner.pid, reply.assignment.owner.start_time)
            with (mock.patch.object(admission_client, "release") as released,
                  mock.patch.object(build_hold, "caller_processes", return_value=frozenset({owner})),
                  mock.patch.object(gate, "gate_job_from_process",
                                    return_value=gate_job_for_command(["gate.py", *argv])),
                  mock.patch.object(gate, "run_suites") as local, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(gate.main(argv), 1)
        self.assertEqual(errors.getvalue(),
                         "REFUSED: The gate placement attempt is stale: local reservation has ended\n")
        released.assert_not_called()
        local.assert_not_called()
        self.cloud_route.assert_not_called()

    def test_a_module_child_of_a_placed_gate_sees_no_placement(self) -> None:
        path = self.write_gate_assignment(self.gate_assignment("mac-linux"))
        root = self.root / "placement-free-job"
        root.mkdir()
        probe = "import os; print(sorted(set(os.environ) & {'BUILDLOG_PLACEMENT_FILE', 'BUILDLOG_PLACEMENT_EPISODE'}))"
        with mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}):
            # The gate passes its own environment to each job, as run_suites does.
            job = gate.start_gate_job("hooks", Path("test_placement_probe.py"), [sys.executable, "-c", probe],
                                      self.checkout, {**os.environ}, root / "output.log")
            status = job.process.wait(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], "episode")
        result = gate.finish_gate_job(job, status)
        self.assertEqual((status, result.returncode), (0, 0), result.output)
        self.assertIn("[]", result.output.splitlines())

    def test_a_merge_gate_leaves_an_unused_reservation_to_its_caller(self) -> None:
        argv = ["--repo", str(self.checkout), "--merge-branch", "integration", "--unit-branch", "unit-phase",
                "--checkpoint", "checkpoint", "--previous-checkpoint", "previous", "--unit", "fixture",
                "--phase", "phase-2"]
        waits: list[placement.AssignmentState] = []
        with placement_fixture.PlacementFixture() as queue, mock.patch.dict(os.environ):
            def wait_until_free() -> None:
                waits.append(self.queue_state(reply))
                raise GateRefused("the fixture stops at the hold wait")

            with mock.patch.object(admission_client, "release", side_effect=partial(queue_release, queue)) as released:
                reply, admitted = self.hand_off_a_natedev_gate(queue, ("natedev", "mac-linux"))
                owner = build_hold.ProcessIdentity(reply.assignment.owner.pid, reply.assignment.owner.start_time)
                with (mock.patch.object(build_hold, "caller_processes", return_value=frozenset({owner})),
                      mock.patch.object(gate, "gate_job_from_process",
                                        return_value=gate_job_for_command(["gate.py", *argv])),
                      mock.patch.object(gate.BuildHoldWait, "wait_until_free", side_effect=wait_until_free),
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(gate.main(argv), 1)
                with mock.patch.object(admission_client, "cancel_placement", side_effect=partial(queue_cancel, queue)):
                    admitted.close()
                self.assertEqual(self.queue_state(reply), placement.ClosedAssignment("cancelled"))
        self.assertEqual(errors.getvalue(), "REFUSED: the fixture stops at the hold wait\n")
        self.assertEqual(waits, [placement.CurrentAssignment()])
        released.assert_not_called()

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
            run = gate.run_gate_suites(self.checkout, ("scripts",), module_time_limit_s=30, full=True, gate_job=job,
                                       target=checked_target(SuiteMode.GATE, job))
        self.assertIs(run, result)
        guest.assert_called_once_with(self.checkout, mode=SuiteMode.GATE, full=True, module_time_limit_s=30,
                                     selected=gate.ALL_MODULES, gate_job=job, type_dirs=("scripts",),
                                     assignment=assignment)
        self.placement_status.assert_called_once_with("episode", "attempt")
        self.cloud_route.assert_not_called()
        local.assert_not_called()

    def test_mac_linux_prints_a_summary_only_when_the_guest_passes(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        target = gate.MacLinuxAssignment(self.gate_assignment("mac-linux"))
        commit = self.git(self.checkout, "rev-parse", "HEAD")[:10]
        types = gate.GateJobResult("types", "basedpyright", 0, 1, "", kind=gate.GateJobKind.TYPE_CHECK)
        first = gate.GateJobResult("hooks", "test_first.py", 0, 1, "")
        second = gate.GateJobResult("lib", "test_first.py", 0, 1, "")
        for status, dirty in ((0, False), (1, False), (0, True)):
            if dirty:
                self.write(self.checkout / "uncommitted.txt", "Guest snapshot fixture.\n")
            result = gate.GateRun((replace(first, returncode=status), second, types), 1673, 0)
            with (self.subTest(status=status, dirty=dirty), mock.patch.object(mac_linux, "route", return_value=result),
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                run = gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED, full=True, target=target)
                self.assertIs(run, result)
                if status == 0:
                    self.assertEqual(output.getvalue(),
                                     f"Mac Linux guest gate passed: 2 modules in 27 min 53 s (checkout HEAD {commit})\n")
                    self.assertEqual(errors.getvalue(), "")
                else:
                    self.assertEqual(output.getvalue(), "")
                    self.assertIn("Mac Linux guest gate failed\n", errors.getvalue())

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
            run = gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED, gate_job=job,
                                       target=checked_target(SuiteMode.TARGETED, job))
        self.assertIs(run, result)
        guest.assert_called_once_with(self.checkout, mode=SuiteMode.TARGETED, full=False,
                                     module_time_limit_s=gate.DEFAULT_MODULE_TIME_LIMIT_S,
                                     selected=TreeSelection((), tree), gate_job=job, type_dirs=("scripts",),
                                     assignment=assignment)
        self.cloud_route.assert_not_called()
        local.assert_not_called()

    @gate_suites("lib")
    def test_mac_linux_runs_the_affected_modules_that_natedev_selects(self) -> None:
        # The fixture's main branch has no full gate pass. No module declares a scan root.
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "gate.conf",
                                 "landing_branches=fixture-landing\nscan_roots=\n")
        self.assertTrue(gate.run_suites(self.checkout).passed, self.output.getvalue())
        self.write(self.checkout / LIB_SECOND, (self.checkout / LIB_SECOND).read_text() + "\n# Changed test module.\n")
        _ = self.commit(self.checkout, "change one test module")
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        tree, _ = cloud_gate.working_tree(self.checkout)
        result = gate.GateRun((gate.GateJobResult("lib", "test_second.py", 0, 1, ""),), 1, 0)
        job = StepJob("test-affected")
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route", return_value=result) as guest,
              mock.patch.object(gate, "run_suites") as local):
            run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, gate_job=job,
                                       target=checked_target(SuiteMode.TARGETED, job))
        self.assertIs(run, result)
        guest.assert_called_once_with(self.checkout, mode=SuiteMode.TARGETED, full=False,
                                     module_time_limit_s=gate.DEFAULT_MODULE_TIME_LIMIT_S,
                                     selected=TreeSelection((LIB_SECOND,), tree), gate_job=job, type_dirs=(),
                                     assignment=assignment)
        output = self.output.getvalue()
        self.assertEqual(output.count("affected: "), 1, output)
        self.assertIn("affected: 1 of 2 modules (base ", output)
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
            job = StepJob("test-affected")
            run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, gate_job=job,
                                       target=checked_target(SuiteMode.TARGETED, job))
        self.assertTrue(run.passed)
        self.assertTrue(run.tests_reused)
        self.assertEqual(run.jobs, ())
        self.assertIn("tests reused from", self.output.getvalue())
        guest.assert_not_called()
        self.cloud_route.assert_not_called()
        local.assert_not_called()

    def test_mac_linux_enabled_without_a_placement_file_uses_the_cloud(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        result = gate.GateRun((), 1, 0, selection_certified=True)
        self.cloud_route.return_value = result
        with mock.patch.object(mac_linux, "route") as guest:
            self.assertIs(gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT), result)
        guest.assert_not_called()
        self.cloud_route.assert_called_once()

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

    def test_mac_linux_assignment_requires_an_enable_key_in_the_live_registry(self) -> None:
        configuration = Path(self.environment["CONF_DIR"])
        registry = configuration / "build_targets.conf"
        fallback = configuration / "repository" / registry.name
        _ = write_config_fixture(fallback, "enabled.mac-linux=on\n")
        _ = write_config_fixture(registry, "enabled.mac-linux=on\n")
        _ = registry.write_text(registry.read_text().replace("enabled.mac-linux=on\n", ""))
        target = gate.MacLinuxAssignment(self.gate_assignment("mac-linux"))
        with (mock.patch.dict(os.environ), mock.patch.object(conf, "directory", return_value=configuration),
              mock.patch.object(conf, "_repository_path", return_value=fallback),
              mock.patch.object(mac_linux, "route", return_value="unexpected guest route") as guest):
            _ = os.environ.pop("CONF_DIR", None)
            with self.assertRaisesRegex(gate.MacLinuxGateRefused, "enabled.mac-linux=off"):
                _ = gate.run_gate_suites(self.checkout, target=target)
        guest.assert_not_called()

    def test_mac_linux_off_with_a_natedev_target_uses_natedev(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=off\n")
        result = gate.GateRun((), 2, 0, selection_certified=True)
        with (mock.patch.object(mac_linux, "route") as guest,
              mock.patch.object(gate, "run_suites", return_value=result) as local):
            self.assertIs(gate.run_gate_suites(self.checkout, gate_job=StepJob("test-all"),
                                               target="natedev"), result)
        guest.assert_not_called()
        local.assert_called_once()
        self.cloud_route.assert_not_called()

    def test_aws_placement_runs_only_the_cloud_gate(self) -> None:
        assignment = self.gate_assignment("aws", provider="scaler")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        result = gate.GateRun((), 1, 0, selection_certified=True)
        self.cloud_route.return_value = result
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(conf, "read", wraps=conf.read) as read_config,
              mock.patch.object(mac_linux, "route") as guest,
              mock.patch.object(gate, "run_suites") as local):
            job = StepJob("test-all")
            self.assertIs(gate.run_gate_suites(self.checkout, gate_job=job,
                                               target=checked_target(SuiteMode.GATE, job)), result)
        self.assertFalse(any(call.args[0] == "build_targets" for call in read_config.call_args_list))
        self.cloud_route.assert_called_once()
        guest.assert_not_called()
        local.assert_not_called()

    def test_aws_placement_reports_a_cloud_refusal(self) -> None:
        assignment = self.gate_assignment("aws", provider="scaler")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path),
                                          "BUILDLOG_PREPARED_COPY": str(self.checkout)}),
              mock.patch.object(copies, "take_prepared_gate_handoff", return_value=True),
              mock.patch.object(mac_linux, "route") as guest,
              mock.patch.object(gate, "run_suites") as local, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(gate.main(["--tests-only", "--repo", str(self.checkout)]), 1)
        self.assertIn("AWS gate failed: fixture cloud unavailable", errors.getvalue())
        self.cloud_route.assert_called_once()
        guest.assert_not_called()
        local.assert_not_called()

    def test_aws_placement_refuses_type_checks(self) -> None:
        assignment = self.gate_assignment("aws", provider="scaler")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route") as guest, mock.patch.object(gate, "run_suites") as local,
              self.assertRaisesRegex(GateRefused, "AWS gate route does not support type checks")):
            job = StepJob("test-all")
            _ = gate.run_gate_suites(self.checkout, ("scripts",), gate_job=job,
                                     target=checked_target(SuiteMode.GATE, job))
        self.cloud_route.assert_not_called()
        guest.assert_not_called()
        local.assert_not_called()

    def test_gate_refuses_a_stale_placement_attempt(self) -> None:
        old = replace(self.gate_assignment("mac-linux"), attempt="old-attempt")
        path = self.write_gate_assignment(old)
        self.set_placement_status(old, placement.ClosedAssignment("replaced"))
        self.assert_gate_refuses_placement(path, "The gate placement attempt is stale: replaced")
        self.placement_status.assert_called_once_with("episode", "old-attempt")

    def test_gate_refuses_a_placement_for_another_owner_or_episode(self) -> None:
        assignment = self.gate_assignment("mac-linux")
        old_owner = replace(assignment.owner, start_time="previous-process")
        cases = ((replace(assignment, owner=old_owner), "The placement owner is not this process or a live ancestor"),
                 (replace(assignment, episode="other-episode"), "The placement episode does not match"))
        for other, reason in cases:
            with self.subTest(assignment=other):
                self.assert_gate_refuses_placement(self.write_gate_assignment(other), reason)
        self.placement_status.assert_not_called()

    def test_gate_refuses_an_unavailable_placement_status(self) -> None:
        path = self.write_gate_assignment(self.gate_assignment("mac-linux"))
        self.placement_status.side_effect = admission_wire.AdmissionUnavailable("fixture service unavailable")
        self.assert_gate_refuses_placement(path, "Cannot query the gate placement status: fixture service unavailable")
        self.placement_status.assert_called_once_with("episode", "attempt")

    def test_gate_waits_for_a_slow_placement_status(self) -> None:
        # The service answers one placement request at a time, so a status answer can come after the query time limit.
        self.configure_gate("silence_seconds=1\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        status = admission_wire.PlacementStatusReply(admission_wire.KnownPlacementStatus(
            assignment, placement.CurrentAssignment(), placement.NoProviderClaim()))
        clock = FakeClock()
        transport = ScriptedTransport(clock, Exchange(TimeoutError(), reply_bytes(status)))
        self.placement_status.side_effect = LIVE_PLACEMENT_STATUS
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(admission_client, "CLOCK", clock),
              mock.patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket),
              redirect_stderr(io.StringIO()) as errors):
            placed = gate.current_gate_placement(SuiteMode.GATE, StepJob("integrate", unit="fixture"))
        self.assertEqual(gate.placement_target(placed), gate.MacLinuxAssignment(assignment))
        self.assertGreater(clock.monotonic(), admission_records.read_memory_config().query_timeout_seconds)
        self.assertIn("admission service not answering", errors.getvalue())
        self.assertEqual(len(transport.requests), 1)
        self.placement_status.assert_called_once_with("episode", "attempt")

    def test_gate_refuses_a_status_that_does_not_confirm_the_placement(self) -> None:
        assignment = self.gate_assignment("aws", provider="scaler")
        path = self.write_gate_assignment(assignment)
        self.placement_status.side_effect = None
        self.placement_status.return_value = admission_wire.PlacementStatusReply(
            admission_wire.UnknownPlacementStatus(assignment.episode, assignment.attempt))
        self.assert_gate_refuses_placement(path, "The queue does not know the gate placement attempt")
        other = replace(assignment, attempt="other-attempt")
        self.set_placement_status(other, placement.CurrentAssignment())
        self.assert_gate_refuses_placement(path, "The queue assignment does not match the gate placement file")

    def test_gate_refuses_a_missing_or_empty_placement_file_path(self) -> None:
        self.assert_gate_refuses_placement(self.state / "missing-assignment.json", "Cannot read the gate placement file")
        self.assert_gate_refuses_placement("", "BUILDLOG_PLACEMENT_FILE is empty")
        self.placement_status.assert_not_called()

    def test_gate_refuses_an_invalid_placement_file(self) -> None:
        path = self.state / "gate-assignment.json"
        nested = sys.getrecursionlimit() + 1
        for payload in (b"[" * nested + b"0" + b"]" * nested, b" " * (admission_wire.ASSIGNMENT_MAX_BYTES + 1)):
            with self.subTest(size=len(payload)):
                _ = path.write_bytes(payload)
                self.assert_gate_refuses_placement(path, "Cannot read the gate placement file")
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
                    _ = gate.run_gate_suites(self.checkout, mode=mode, full=True, gate_job=StepJob(verb),
                                             target=checked_target(mode, StepJob(verb)))
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
                       "mac-linux: guest unreachable: fixture-guest did not confirm the run ended "
                       + f"within 2 seconds ({target_config.UNREACHABLE_WAIT_KEY})"):
            with (self.subTest(reason=reason), mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
                  mock.patch.object(mac_linux, "route", return_value=reason) as guest,
                  mock.patch.object(gate, "run_suites") as local, redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(gate.main(["--tests-only", "--repo", str(self.checkout)]), 1)
            self.assertIn(f"REFUSED: Mac Linux guest gate failed: {reason}", errors.getvalue())
            guest.assert_called_once()
            self.cloud_route.assert_not_called()
            local.assert_not_called()
            # The guest took the run, so the gate is not placed again.
            self.assertFalse(Path(str(path) + CLAIM_FAILURE_SUFFIX).exists())

    def gate_request(self, *assignments: placement.PlacementAssignment) -> admission_wire.PlacementRequest:
        first = assignments[0]
        profile = admission_records.ProgramProfile("gate", str(self.checkout), str(self.checkout),
            admission_records.HistoryPeakSizing(1, admission_records.FirstProgramPeak(1), 0.0))
        return admission_wire.PlacementRequest(first.episode, first.owner, first.since, profile,
            placement.PlacementConstraints(first.source_key, ("gate.py", "--tests-only"), str(self.checkout), "",
                first.session, tuple(placement.PlacementCandidate(item.destination) for item in assignments)))

    def test_silent_mac_linux_guest_writes_a_claim_failure_and_exits_79(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_targets.conf", "enabled.mac-linux=on\n")
        assignment = self.gate_assignment("mac-linux")
        path = self.write_gate_assignment(assignment)
        self.set_placement_status(assignment, placement.CurrentAssignment())
        reason = "the guest did not arm the run: fixture SSH refused the connection"
        with (mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path)}),
              mock.patch.object(mac_linux, "route", return_value=mac_linux.GuestNotStarted(reason)) as guest,
              mock.patch.object(gate, "run_suites") as local, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(gate.main(["--tests-only", "--repo", str(self.checkout)]), MAC_LINUX_PRELAUNCH_REFUSED_STATUS)
        self.assertEqual(errors.getvalue(), f"Mac Linux guest took no run: {reason}\n")
        guest.assert_called_once()
        local.assert_not_called()
        self.cloud_route.assert_not_called()
        refusal = first_step.read_gate_claim_failure(path, self.gate_request(assignment))
        self.assertEqual((refusal.assignment, refusal.reason), (assignment, MAC_LINUX_NO_ANSWER))
        self.assertIsInstance(refusal.claim, placement.NoProviderClaim)

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
        with (mock.patch.dict(os.environ, {"BUILDLOG_PREPARED_COPY": str(self.checkout)}),
              mock.patch.object(copies, "take_prepared_gate_handoff", return_value=True),
              mock.patch.object(mac_gate_process, "load_mac_gate_route") as load_mac,
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
        with (mock.patch.dict(os.environ, {"BUILDLOG_PREPARED_COPY": str(self.checkout)}),
              mock.patch.object(copies, "take_prepared_gate_handoff", return_value=True),
              mock.patch.object(gate, "run_suites") as local):
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
    def test_natedev_returns_its_verdict_for_the_whole_selection(self) -> None:
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
                run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, target="natedev")
                self.assertEqual(run.passed, not local_failure, self.output.getvalue())
                self.cloud_route.assert_not_called()
                self.assertCountEqual([f"scripts/{job.suite}/{job.module}" for job in run.jobs], paths)

    @gate_suites("hooks", "lib")
    def test_full_targeted_gate_uses_the_cloud_before_natedev(self) -> None:
        self.exclude_modules(HOOKS)
        run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, full=True, target=gate.NO_GATE_ASSIGNMENT)
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
            run = gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED,
                                       target=gate.NO_GATE_ASSIGNMENT)
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
    def test_missing_mac_route_does_not_change_the_natedev_target(self) -> None:
        local = gate.GateRun((gate.GateJobResult("hooks", "test_first.py", 0, 1, ""),), 1, 0)
        with (mock.patch.object(gate, "run_suites", return_value=local) as run_suites,
              mock.patch.dict(sys.modules, {MAC_MODULE: None})):
            run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, target="natedev")
        self.assertIs(run, local)
        self.cloud_route.assert_not_called()
        run_suites.assert_called_once_with(self.checkout, (), gate.DEFAULT_MODULE_TIME_LIMIT_S,
                                           source="tests-only", full=False, mode=SuiteMode.TARGETED)
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
            run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, target=gate.NO_GATE_ASSIGNMENT)
        self.assertEqual(run.jobs, (hooks,))
        self.cloud_route.assert_called_once()
        self.assertFalse((self.state / "mac-call.json").exists())

    @gate_suites("lib")
    def test_natedev_records_and_reuses_its_full_pass(self) -> None:
        first = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, target="natedev")
        self.assertTrue(first.passed, self.output.getvalue())
        self.assertIs(first.coverage, ModuleCoverage.COMPLETE)
        self.assert_all_modules_started()
        self.cloud_route.assert_not_called()
        record, = self.pass_records()
        self.assertEqual(record["kind"], "full")
        self.clear_gate_events()
        self.cloud_route.reset_mock()
        second = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, target="natedev")
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

        run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, target=gate.NO_GATE_ASSIGNMENT)

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
        second = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)
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
        baseline = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)
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
            run = gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED,
                                       target=gate.NO_GATE_ASSIGNMENT)
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
        later = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)
        self.assertTrue(later.passed, self.output.getvalue())
        self.assertFalse(later.tests_reused)
        self.assert_all_modules_started()
        self.assertEqual(self.pass_records()[-1]["tree"], tree)

    @gate_suites("lib")
    def test_empty_exclude_list_sends_all_modules_to_the_cloud(self) -> None:
        jobs = (gate.GateJobResult("lib", "test_first.py", 0, 1, ""),
                gate.GateJobResult("lib", "test_second.py", 0, 1, ""))
        self.cloud_route.return_value = gate.GateRun(jobs, 1, 0)
        run = gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED, target=gate.NO_GATE_ASSIGNMENT)
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
        run = gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED, target=gate.NO_GATE_ASSIGNMENT)
        self.assertIs(run, completed)
        self.assert_targeted_cloud_selection(HOOKS)
        self.assertEqual(self.module_events(), [])
