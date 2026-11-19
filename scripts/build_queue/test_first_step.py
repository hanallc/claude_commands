"""Test cleanup after a remote first-step handoff."""

from dataclasses import replace
import os
import unittest

from scripts.build_queue import execution_identity
from scripts.build_queue import admission_client as client, admission_protocol as wire, first_step, placement
from scripts.build_queue.test_placement import (
    PlacementFixture, adopt_assignment, assignment_for, consume_permission, placement_request, publish_candidate,
)
from scripts.lib.build_slots import SlotQueuePosition
from scripts.message.session_send_test_support import forbid_real_sender


def setUpModule() -> None:
    forbid_real_sender()


class FirstStepTests(unittest.TestCase):
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
