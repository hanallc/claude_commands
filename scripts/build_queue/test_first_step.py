"""Test first-step admission and handoff."""

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_records as records, execution_identity, queue_place
from scripts.build_queue import admission_client as client, admission_protocol as wire, first_step, placement
from scripts.build_queue.test_placement import (
    PlacementFixture, adopt_assignment, assignment_for, consume_permission, local_candidate, mac_linux_candidate,
    placement_request, publish_candidate,
)
from scripts.lib import build_folders, conf
from scripts.lib.build_slots import SlotQueuePosition
from scripts.lib.build_steps import command_job
from scripts.mac_test import claim_state
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


GATE_COMMAND = ("python3", "scripts/production/gate.py", "--tests-only", "--type-dirs", "scripts")
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
    def test_explicit_gate_targets_override_the_claude_default(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "mac-linux"}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source, requested="mac"), ("mac",))
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("mac-linux",))

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
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ())

    def test_disabling_only_mac_linux_keeps_natedev_in_the_claude_gate_default(self) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands", enabled="off")
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                self.assertEqual(first_step.gate_targets(GATE_COMMAND, source=source), ("natedev",))

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
            # The gate ends the reservation after it confirms the assignment.
            release.assert_not_called()
            cancel.assert_not_called()
            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], assignment.episode)
            self.assertEqual(admitted.grant, grant)
            admitted.close()
            cancel.assert_called_once()
            self.assertIsInstance(admitted.grant, first_step.HandedOff)

    def test_named_gate_places_one_request_and_exports_the_assignment(self) -> None:
        self.check_gate_placement(use_default=False)

    def test_default_claude_gate_places_one_request_and_exports_the_assignment(self) -> None:
        self.check_gate_placement(use_default=True)

    def check_gate_placement(self, *, use_default: bool) -> None:
        with PlacementFixture() as fixture:
            source = gate_source(fixture, "claude_commands")
            configured = conf.read("build_targets").text("targets").split(",")
            expected = tuple(name for name in configured if name not in ("mac", "hana-aws"))
            with mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}):
                targets = first_step.gate_targets(GATE_COMMAND, source=source) if use_default else expected[::-1]
                if use_default:
                    self.assertEqual(targets, expected)
                    local_only = (*GATE_COMMAND, "--module=fixture/test_gate.py")
                    self.assertEqual(first_step.gate_targets(local_only, source=source), ("natedev",))
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
                      {"BUILDLOG_PLACEMENT_FILE": "outer-file", "BUILDLOG_PLACEMENT_EPISODE": "outer-episode"}),
                  mock.patch.object(records, "process_start_time", return_value="123"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.root)),
                  mock.patch.object(client, "acquire_placement", side_effect=grant),
                  mock.patch.object(queue_place, "request_place", return_value=PROGRAM_GRANT) as program,
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()),
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok()) as cancel,
                  forbid_admission_calls()):
                with first_step.admitted_run(GATE_COMMAND, source, targets=targets) as admitted:
                    self.assertEqual(len(requests), 1)
                    request = requests[0]
                    self.assertEqual(request.constraints.candidates, (remote, local))
                    self.assertEqual(request.constraints.command, GATE_COMMAND)
                    self.assertEqual(request.run, wire.RunIdentity(os.getpid(), "123"))
                    self.assertEqual(request.identity.job, command_job(GATE_COMMAND))
                    self.assertIsInstance(request.profile, records.ProgramProfile)
                    assert isinstance(request.local, wire.LocalAdmissionContext)
                    self.assertEqual(request.local.argv, ("gate", "--type-dirs", "scripts"))

                    saved = client.read_assignment(admitted.assignment_file).assignment
                    self.assertEqual(saved.owner, request.owner)
                    self.assertEqual(saved.destination, remote.destination)
                    admitted.handoff(fixture.root)
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
                    self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], saved.episode)
                    self.assertNotIn("BUILDLOG_FIRST_STEP_ASSIGNMENT", os.environ)
                    cancel.assert_not_called()
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], "outer-file")
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], "outer-episode")
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
                    self.assertEqual(program_requests[0].argv, ("gate", "--type-dirs", "scripts"))
                    self.assertFalse(admitted.assignment_file.exists())
                    admitted.handoff(fixture.root)
                    self.assertNotIn("BUILDLOG_PLACEMENT_FILE", os.environ)
                release.assert_called_once_with(PROGRAM_GRANT.reservation, 0, PROGRAM_GRANT.cleanup_timing)
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
