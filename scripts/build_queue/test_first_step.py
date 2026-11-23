"""Test first-step admission and handoff."""

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
import os
from pathlib import Path
import unittest
from unittest import mock

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_records as records, execution_identity, queue_place
from scripts.build_queue import admission_client as client, admission_protocol as wire, first_step, placement
from scripts.build_queue.test_placement import (
    PlacementFixture, adopt_assignment, assignment_for, consume_permission, local_candidate, placement_request,
    publish_candidate,
)
from scripts.build_targets import build_targets
from scripts.lib import build_folders
from scripts.lib.build_slots import SlotQueuePosition
from scripts.lib.build_steps import command_job
from scripts.mac_test import claim_state
from scripts.message.session_send_test_support import forbid_real_sender


GATE_COMMAND = ("python3", "scripts/production/gate.py", "--tests-only", "--type-dirs", "scripts")
PROGRAM_GRANT = wire.Admitted("program", 1, "test", "", "", {})


def setUpModule() -> None:
    forbid_real_sender()


def forbid_admission_calls() -> AbstractContextManager[object]:
    """Fail at once when a test with mocked placement reaches an admission socket."""
    return mock.patch.object(client, "_call", side_effect=AssertionError("unmocked admission service call"))


class FirstStepTests(unittest.TestCase):
    def test_local_gate_releases_preparation_and_keeps_its_assignment(self) -> None:
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
            release.assert_called_once_with(PROGRAM_GRANT.reservation, 0, PROGRAM_GRANT.cleanup_timing, "handed_off")
            cancel.assert_not_called()
            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
            self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], assignment.episode)
            self.assertEqual(admitted.grant, grant)
            admitted.close()
            cancel.assert_called_once()
            self.assertIsInstance(admitted.grant, first_step.HandedOff)

    def test_named_gate_places_one_request_and_exports_the_assignment(self) -> None:
        with PlacementFixture() as fixture:
            _ = (fixture.root / "build_targets.conf").write_text("\n".join((
                "targets=mac-linux,natedev,mac",
                "kind.mac-linux=ssh", "platform.mac-linux=linux", "settings.mac-linux=guest.conf",
                "kind.natedev=local", "platform.natedev=linux",
                "kind.mac=ssh", "platform.mac=macos", "settings.mac=mac.conf", "",
            )))
            remote = placement.PlacementCandidate(
                placement.PlacementDestination("ssh", "mac-linux", "fixture-guest", claim_state.WHOLE_MAC, 1))
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
                  mock.patch.object(build_targets, "fixed_remote_candidates", return_value=(remote,)) as candidates,
                  mock.patch.object(client, "acquire_placement", side_effect=grant),
                  mock.patch.object(queue_place, "request_place", return_value=PROGRAM_GRANT) as program,
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()),
                  mock.patch.object(client, "cancel_placement", return_value=wire.Ok()) as cancel,
                  forbid_admission_calls()):
                with first_step.admitted_run(GATE_COMMAND, fixture.root, targets=("natedev", "mac-linux")) as admitted:
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
                candidates.assert_called_once()
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
