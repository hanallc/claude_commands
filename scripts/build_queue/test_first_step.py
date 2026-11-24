"""Test first-step admission and handoff."""

from contextlib import AbstractContextManager, nullcontext, redirect_stderr
from dataclasses import replace
import io
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_records as records, execution_identity, queue_place
from scripts.build_queue import admission_client as client, admission_protocol as wire, first_step, placement
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.admission_test_support import FakeClock
from scripts.build_queue.test_placement import (
    PlacementFixture, adopt_assignment, assignment_for, consume_guest_permission, consume_permission, guest_gate,
    local_candidate, mac_linux_and_natedev_request, mac_linux_candidate, placement_request, publish_candidate,
)
from scripts.copies import copies
from scripts.lib import build_folders, conf
from scripts.lib.build_slots import SlotQueuePosition
from scripts.lib.build_steps import GATE_RESULT_UNKNOWN_STATUS, command_job
from scripts.mac_test import claim_state
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


GATE_COMMAND = ("python3", "scripts/production/gate.py", "--tests-only")
MODULE_GATE_COMMAND = (*GATE_COMMAND[:2], "--affected", "--module", "lib/test_infrastructure.py")
PROGRAM_GRANT = wire.Admitted("program", 1, "test", "", "", {})


def setUpModule() -> None:
    forbid_real_sender()


def forbid_admission_calls() -> AbstractContextManager[object]:
    """Fail at once when a test with mocked placement reaches an admission socket."""
    return mock.patch.object(client, "_call", side_effect=AssertionError("unmocked admission service call"))


def gate_source(fixture: PlacementFixture, repository: str, *, enabled: str = "on") -> Path:
    """Give the gate a private registry and repository origin."""
    _ = write_config_fixture(fixture.root / "build_targets.conf",
        "targets=mac-linux,natedev,mac,hana-aws\n"
        + f"enabled.mac-linux={enabled}\n")
    _ = write_config_fixture(fixture.root / "mac_linux.conf",
        "ssh.hops=fixture-mac fixture-guest\nguest.user=fixture\n")
    _ = write_config_fixture(fixture.root / "hana_aws.conf", "enabled=on\n")
    source = fixture.root / (".claude" if repository == "claude_commands" else repository)
    source.mkdir()
    for command in (("init", "-q"), ("remote", "add", "origin", f"https://example.invalid/{repository}.git")):
        _ = subprocess.run(("git", "-C", str(source), *command), capture_output=True,
                           check=True, timeout=TEST_HANG_GUARD_SECS)
    return source


class FirstStepTests(unittest.TestCase):
    def consumed_guest_gate(self, fixture: PlacementFixture) -> first_step.FirstStepAdmission:
        request = mac_linux_and_natedev_request(fixture)
        owner = fixture.fixture_owner(os.getpid(), "owner-start")
        request = replace(request, owner=replace(request.owner, pid=owner.pid, start_time=owner.start_time),
                          run=wire.RunIdentity(owner.pid, owner.start_time))
        _ = publish_candidate(fixture, request)
        assignment = assignment_for(fixture, request)
        granted, = consume_guest_permission(fixture, assignment, guest_gate(fixture, assignment))
        self.assertIsInstance(granted, wire.FirstAuthorization)
        grant = wire.Assignment(assignment)
        path = fixture.root / "assignment.json"
        client.write_assignment(path, grant)
        return first_step.FirstStepAdmission(SlotQueuePosition(request.episode, request.since),
                                            first_step.GatePlacement(grant, request), path)

    def test_copies_gate_step_closes_an_old_branch_without_a_completion_report(self) -> None:
        with PlacementFixture() as fixture:
            admitted = self.consumed_guest_gate(fixture)
            assignment = client.read_assignment(admitted.assignment_file).assignment
            args = copies.CopyArguments(repo=str(fixture.root), targets="mac-linux")
            child = mock.Mock(return_value=subprocess.CompletedProcess(GATE_COMMAND, 0))
            with (fixture.serving(), mock.patch.dict(os.environ),
                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)),
                  mock.patch.object(copies, "subprocess", SimpleNamespace(run=child))):
                self.assertEqual(copies.run_gate_step(args, GATE_COMMAND), 0)
            child.assert_called_once()
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            saved = fixture.server.assignments[key]
            self.assertEqual(saved.state, placement.ClosedAssignment("completed"))
            status = saved.terminal_report
            assert isinstance(status, placement.TerminalAttemptReport), status
            self.assertEqual(status.outcome, placement.AttemptCompleted(0))
            self.assertEqual(status.path, placement.DispatchPathSettled())

    def test_guest_with_unknown_result_keeps_its_assignment_for_cleanup(self) -> None:
        with PlacementFixture() as fixture:
            admitted = self.consumed_guest_gate(fixture)
            assignment = client.read_assignment(admitted.assignment_file).assignment
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            saved = fixture.server.assignments[key]
            result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(GATE_COMMAND, GATE_RESULT_UNKNOWN_STATUS)
            with (fixture.serving(), mock.patch.object(client, "report_owner_end", wraps=client.report_owner_end) as report):
                self.assertIs(admitted.run_gate(lambda: result), result)
                report.assert_not_called()
            self.assertEqual(fixture.server.assignments[key], saved)
            self.assertIsInstance(admitted.grant, first_step.GatePlacement)

    def test_guest_owner_end_retries_until_the_queue_answers(self) -> None:
        with PlacementFixture() as fixture:
            admitted = self.consumed_guest_gate(fixture)
            assignment = client.read_assignment(admitted.assignment_file).assignment
            clock = FakeClock()
            timeouts: list[float] = []
            config = records.read_memory_config()

            def connect_after_refusal(timeout: float) -> socket.socket:
                timeouts.append(timeout)
                if len(timeouts) == 1:
                    clock.advance(timeout)
                    raise TimeoutError("fixture queue is down")
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(timeout)
                connection.connect(str(fixture.socket_path))
                return connection

            result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(GATE_COMMAND, 5)
            with (fixture.serving(), mock.patch.object(client, "CLOCK", clock),
                  mock.patch.object(client, "_connect", side_effect=connect_after_refusal)):
                self.assertIs(admitted.run_gate(lambda: result), result)
                self.assertEqual(timeouts, [config.query_timeout_seconds] * 2)
                self.assertEqual(clock.sleeps, [config.reconnect_seconds])
                self.assertEqual(clock.monotonic(), config.query_timeout_seconds + config.reconnect_seconds)
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            saved = fixture.server.assignments[key]
            self.assertEqual(saved.state, placement.ClosedAssignment("completed"))
            assert isinstance(saved.terminal_report, placement.TerminalAttemptReport), saved
            self.assertEqual(saved.terminal_report.outcome, placement.AttemptCompleted(5))

    def test_guest_owner_end_deadline_warns_once_and_preserves_the_child_status(self) -> None:
        with PlacementFixture() as fixture:
            admitted = self.consumed_guest_gate(fixture)
            assignment = client.read_assignment(admitted.assignment_file).assignment
            clock = FakeClock()
            timeout = records.read_memory_config().unreachable_limit_seconds
            result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(GATE_COMMAND, 7)
            with (mock.patch.object(client, "CLOCK", clock),
                  mock.patch.object(client, "_connect", side_effect=FileNotFoundError("fixture queue is down")) as connect,
                  mock.patch.object(client, "cancel_placement") as cancel,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertIs(admitted.run_gate(lambda: result), result)
                admitted.close()
            cancel.assert_not_called()
            self.assertGreater(connect.call_count, 1)
            self.assertEqual(clock.monotonic(), timeout)
            self.assertEqual(errors.getvalue().splitlines(),
                             [f"Mac Linux gate owner end report failed: no answer in {timeout:g} s"])
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)

    def test_explicit_gate_targets_override_the_claude_default(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "mac-linux"}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source, requested="mac"), ("mac",))
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("mac-linux",))

    def test_direct_nightly_default_excludes_aws_when_cloud_gates_are_enabled(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            command = (*GATE_COMMAND[:2], "--nightly")
            _ = write_config_fixture(fixture.root / "cloud_gate.conf", "enabled=on\n")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(command, source), ("mac-linux", "natedev"))

    def test_hana_gate_has_no_default_targets(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "hana")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ())

    def test_disabled_targets_leave_the_claude_gate_default(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands", enabled="off")
            _ = write_config_fixture(fixture.root / "build_targets.conf", "enabled.natedev=off\n")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                with self.assertRaisesRegex(first_step.FirstStepRefused, "No enabled build target"):
                    _ = first_step.gate_targets(GATE_COMMAND, source=source)

    def test_disabling_only_mac_linux_keeps_natedev_in_the_claude_gate_default(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands", enabled="off")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("natedev",))

    def test_default_gate_omits_an_unsupported_linux_target(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            _ = write_config_fixture(fixture.root / "build_targets.conf",
                "targets=linux-extra,natedev\nkind.linux-extra=ssh\nplatform.linux-extra=linux\n"
                + "settings.linux-extra=linux-extra.conf\n")
            _ = (fixture.root / "linux-extra.conf").write_text("host=fixture-linux\nkinds=check,test\n")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("natedev",))

    def test_default_gate_omits_mac_linux_when_its_enable_key_is_missing(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            registry = fixture.root / "build_targets.conf"
            _ = registry.write_text(registry.read_text().replace("enabled.mac-linux=on\n", ""))
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("natedev",))

    def test_named_module_gate_keeps_both_explicit_targets_eligible(self) -> None:
        self.check_named_gate_candidates(MODULE_GATE_COMMAND)

    def test_named_module_gate_accepts_a_guest_only_target(self) -> None:
        self.check_named_gate_candidates(MODULE_GATE_COMMAND, targets=("mac-linux",))

    def test_named_module_gate_offers_the_guest_by_default(self) -> None:
        self.check_named_gate_candidates(MODULE_GATE_COMMAND, targets=None)

    def test_named_plain_gate_keeps_both_targets_eligible(self) -> None:
        self.check_named_gate_candidates(GATE_COMMAND)

    def check_named_gate_candidates(self, command: tuple[str, ...], *,
                                    targets: tuple[str, ...] | None = ("natedev", "mac-linux")) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            owner = fixture.fixture_owner(os.getpid(), "123")
            guest = mac_linux_candidate()
            if targets is None:
                with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "", "BUILDLOG_RESERVED_STEP": ""}):
                    targets = first_step.gate_targets(command, source=source)
                self.assertEqual(targets, ("mac-linux", "natedev"))
            candidates = (guest, local_candidate(fixture)) if "natedev" in targets else (guest,)

            def grant(request: wire.PlacementRequest) -> wire.LocalPlacementGranted | wire.Assignment:
                _ = publish_candidate(fixture, request)
                reply, = fixture.connect(request).replies()
                assert isinstance(reply, (wire.Assignment, wire.LocalPlacementGranted)), reply
                return reply

            with (mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(fixture.root / "target-state")}),
                  mock.patch.object(records, "process_start_time", return_value=owner.start_time),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(client, "acquire_placement", side_effect=grant) as acquire,
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok())):
                with first_step.admitted_run(command, source, targets=targets) as admitted:
                    placed = admitted.grant
                    assert isinstance(placed, first_step.GatePlacement)
                    self.assertEqual(placed.grant.assignment.destination.target, "mac-linux")
                    self.assertEqual(placed.request.constraints.candidates, candidates)
                    acquire.assert_called_once_with(placed.request)

    def test_local_gate_keeps_its_reservation_and_assignment_through_the_handoff(self) -> None:
        position = SlotQueuePosition("episode", 3)
        assignment = placement.PlacementAssignment(position.episode, "attempt",
            placement.PlacementDestination("local", "natedev", "fixture-host", claim_state.WHOLE_MAC),
            execution_identity.SourceKeyKnown("source"), placement.SubmittingOwner("fixture-host", 1, "2"),
            NoOwningSession(), position.since)
        profile = records.ProgramProfile("gate", "/fixture", "/fixture",
            records.HistoryPeakSizing(1, records.FirstProgramPeak(1), 0.0))
        request = wire.PlacementRequest(position.episode, assignment.owner, position.since, profile,
            placement.PlacementConstraints(assignment.source_key, ("gate.py",), "/fixture", "", assignment.session,
                (placement.PlacementCandidate(assignment.destination),)))
        grant = first_step.GatePlacement(wire.LocalPlacementGranted(assignment, PROGRAM_GRANT), request)
        admitted = first_step.FirstStepAdmission(position, grant, Path("/fixture/assignment.json"))

        with (mock.patch.dict(os.environ),
              mock.patch.object(queue_place, "release_place", return_value=wire.Ok()) as release,
              mock.patch.object(client, "cancel_placement", return_value=wire.Ok()) as cancel):
            admitted.handoff(Path("/fixture"))
            # The gate keeps the reservation through the suites.
            release.assert_not_called()
            cancel.assert_not_called()
            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], assignment.episode)
            self.assertEqual(admitted.grant, grant)
            admitted.close()
            cancel.assert_called_once()
            self.assertIsInstance(admitted.grant, first_step.HandedOff)

    def test_named_gate_places_one_request_and_exports_the_assignment(self) -> None:
        self.check_gate_placement((*GATE_COMMAND[:2], "--affected"),
                                  ("test-affected", "test-affected", "test affected"), use_default=False)

    def test_default_claude_gate_places_one_request_and_exports_the_assignment(self) -> None:
        self.check_gate_placement(GATE_COMMAND, ("gate", "test-all", "test all"), use_default=True)

    def check_gate_placement(self, command: tuple[str, ...], expected_identity: tuple[str, str, str], *,
                             use_default: bool) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            configured = conf.read("build_targets").text("targets").split(",")
            expected = tuple(name for name in configured if name not in ("mac", "hana-aws"))
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                targets = first_step.gate_targets(command, source=source) if use_default else expected[::-1]
                if use_default:
                    self.assertEqual(targets, expected)
                    nightly = (*GATE_COMMAND[:2], "--nightly", "--merge-branch", "main", "--unit", "fixture")
                    self.assertEqual(first_step.gate_targets(nightly, source=source), expected)
                    modules = (*GATE_COMMAND, "--module=fixture/test_gate.py")
                    promote = (*GATE_COMMAND[:2], "--promote-only", "--merge-branch", "main")
                    with mock.patch.dict(os.environ, {"BUILDLOG_RESERVED_STEP": ""}):
                        self.assertEqual(first_step.gate_targets(modules, source=source), expected)
                    self.assertEqual(first_step.gate_targets(promote, source=source), ())
                    for explicit in (modules, promote):
                        self.assertEqual(first_step.gate_targets(explicit, source=source, requested="natedev"),
                                         ("natedev",))
                    _ = write_config_fixture(fixture.root / "build_targets.conf", "targets=natedev,mac,hana-aws\n")
                    self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("natedev",))
                    _ = write_config_fixture(fixture.root / "build_targets.conf", "targets=" + ",".join(configured) + "\n")
            remote = mac_linux_candidate()
            local = local_candidate(fixture)
            requests: list[wire.PlacementRequest] = []

            def grant(request: wire.PlacementRequest) -> wire.Assignment:
                requests.append(request)
                return wire.Assignment(placement.PlacementAssignment(request.episode, "attempt", remote.destination,
                    request.constraints.source_key, request.owner, request.constraints.session, request.since))

            with (mock.patch.dict(os.environ,
                      {"BUILDLOG_PLACEMENT_FILE": "outer-file", "BUILDLOG_PLACEMENT_EPISODE": "outer-episode",
                       "BUILDLOG_PLACEMENT_SIDECAR": "outer-sidecar"}),
                  mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(client, "acquire_placement", side_effect=grant),
                  mock.patch.object(queue_place, "request_place", return_value=PROGRAM_GRANT) as program,
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()),
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok()) as cancel,
                  forbid_admission_calls()):
                with first_step.admitted_run(command, source, targets=targets) as admitted:
                    self.assertEqual(len(requests), 1)
                    request = requests[0]
                    self.assertEqual(request.constraints.candidates, (remote, local))
                    self.assertEqual(request.constraints.command, command)
                    self.assertEqual(request.run, wire.RunIdentity(os.getpid(), "123"))
                    job = request.identity.job
                    self.assertEqual(job, command_job(command))
                    assert isinstance(job, StepJob)
                    self.assertEqual((request.identity.step, job.step, request.identity.what), expected_identity)
                    self.assertIsInstance(request.profile, records.ProgramProfile)
                    assert isinstance(request.local, wire.LocalAdmissionContext)
                    self.assertEqual(request.local.argv, (expected_identity[0],))
                    self.assertEqual(request.local.sidecar, str(fixture.root / "placement.cgroup"))

                    saved = client.read_assignment(admitted.assignment_file).assignment
                    self.assertEqual(saved.owner, request.owner)
                    self.assertEqual(saved.destination, remote.destination)
                    admitted.handoff(fixture.root)
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], saved.episode)
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_SIDECAR"], request.local.sidecar)
                    self.assertNotIn("BUILDLOG_FIRST_STEP_ASSIGNMENT", os.environ)
                    cancel.assert_not_called()
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], "outer-file")
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], "outer-episode")
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_SIDECAR"], "outer-sidecar")
                cancel.assert_called_once()
                program.assert_not_called()

    def test_unnamed_gate_uses_program_memory_admission(self) -> None:
        with PlacementFixture() as fixture:
            program_requests: list[wire.AdmitRequest] = []

            def grant(request: queue_place.RefreshedRequest, _output: client.WaitOutput) -> wire.Admitted:
                program_requests.append(request.refresh())
                return PROGRAM_GRANT

            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
                  mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(first_step, "_placement_grant") as placement_grant,
                  mock.patch.object(queue_place, "request_place", side_effect=grant),
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()) as release,
                  forbid_admission_calls()):
                with first_step.admitted_run(GATE_COMMAND, fixture.root) as admitted:
                    self.assertEqual(len(program_requests), 1)
                    profile = program_requests[0].profile
                    assert isinstance(profile, records.ProgramProfile)
                    self.assertEqual(profile.program, "gate")
                    self.assertGreater(profile.sizing.first_peak_bytes, 0)
                    self.assertEqual(program_requests[0].argv, ("gate",))
                    self.assertFalse(admitted.assignment_file.exists())
                    admitted.handoff(fixture.root)
                    self.assertNotIn("BUILDLOG_PLACEMENT_FILE", os.environ)
                release.assert_called_once_with(PROGRAM_GRANT.reservation, 0, PROGRAM_GRANT.cleanup_timing, "handed_off")
                placement_grant.assert_not_called()

    def test_close_after_verify_cancels_the_completed_remote_placement(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            grant = wire.Assignment(assignment)
            path = fixture.root / "assignment.json"
            client.write_assignment(path, grant)
            admitted = first_step.FirstStepAdmission(SlotQueuePosition(request.episode, request.since), grant, path)

            admitted.handoff(fixture.root)
            self.assertEqual(os.environ["BUILDLOG_FIRST_STEP_ASSIGNMENT"], str(path))
            self.assertIsInstance(fixture.connect(consume_permission("launch", assignment, claim)).replies()[-1],
                                  wire.FirstAuthorization)
            _ = publish_candidate(fixture, request)
            completed, = fixture.connect(wire.ExecutionReport("done", assignment, claim,
                execution_identity.ExecutionNotStarted(), placement.AttemptCompleted(0), placement.DispatchPathSettled())).replies()
            assert isinstance(completed, wire.DispatchStatus)
            self.assertEqual(completed.state, placement.ClosedAssignment("completed"))

            with fixture.serving():
                self.assertEqual(client.cancel_placement(wire.CancelPlacement(
                    "verify-exit", request.episode, request.owner, "verify ended")), wire.Ok())
                with fixture.loop_turn():
                    self.assertNotIn(request.episode, fixture.server.placement_requests)
                admitted.close()
                with self.assertRaises(wire.AdmissionUnavailable) as refusal:
                    _ = client.cancel_placement(wire.CancelPlacement(
                        "after-close", request.episode, request.owner, "verify ended"))
                self.assertEqual(refusal.exception.reason, wire.PLACEMENT_REQUEST_MISSING_REASON)
            self.assertIsInstance(admitted.grant, first_step.HandedOff)

    def test_close_propagates_a_coordinator_mismatch(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            other_owner = replace(assignment.owner, start_time=assignment.owner.start_time + "0")
            grant = wire.Assignment(replace(assignment, owner=other_owner))
            admitted = first_step.FirstStepAdmission(SlotQueuePosition(request.episode, request.since),
                                                     grant, fixture.root / "assignment.json")

            with fixture.serving(), self.assertRaisesRegex(wire.AdmissionUnavailable,
                                                          "^placement coordinator does not match$"):
                admitted.close()
            self.assertEqual(admitted.grant, grant)
