"""Check queue order after a Mac Linux probe refusal before launch."""

from dataclasses import replace
import unittest

from scripts.build_queue import admission_protocol as wire, placement
from scripts.build_queue.test_placement import (
    PlacementFixture,
    assignment_for,
    local_candidate,
    mac_linux_and_natedev_request,
    placement_request,
    publish_candidate,
)


def queue_order(fixture: PlacementFixture) -> list[tuple[int, float, str]]:
    reply, = fixture.server.handle_request(wire.ViewRequest())
    assert isinstance(reply, wire.ViewReply), reply
    return [(row["pid"], row["since"], row.get("move_id", "")) for _, row in reply.reading.ordered]


class AdmissionPrelaunchOrderTests(unittest.TestCase):
    def test_guest_probe_refusal_keeps_the_original_queue_place(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            now = fixture.clock.now()
            older = placement_request(fixture, since=now - 60)
            middle = mac_linux_and_natedev_request(fixture)
            younger = placement_request(fixture, since=now + 60)
            local = local_candidate(fixture)
            older = replace(older, constraints=replace(older.constraints, candidates=(local,)))
            younger = replace(younger, constraints=replace(younger.constraints, candidates=(local,)))
            fixture.clock.advance(60)

            no_answer = placement.MAC_LINUX_NO_ANSWER
            unavailable = placement.TemporaryExclusion(no_answer)
            _ = publish_candidate(fixture, middle, unavailable)
            for request in (older, middle, younger):
                reply, = fixture.server.handle_request(request)
                self.assertIsInstance(reply, wire.PlacementWait)

            before = queue_order(fixture)
            self.assertEqual([(pid, since) for pid, since, _ in before],
                             [(req.owner.pid, req.since) for req in (older, middle, younger)])
            self.assertTrue(before[1][2], "the middle waiter must have a move ID to preserve")

            _ = publish_candidate(fixture, middle)
            offered = assignment_for(fixture, middle)
            self.assertEqual(offered.destination.target, "mac-linux")
            self.assertEqual((offered.episode, offered.since), (middle.episode, middle.since))

            fixture.clock.advance(1)
            _ = publish_candidate(fixture, middle, unavailable)
            refused, = fixture.server.handle_request(wire.ClaimFailure("probe-refused", offered, no_answer))
            assert isinstance(refused, wire.DispatchStatus), refused
            self.assertEqual(refused.state, placement.ClosedAssignment(no_answer))

            waiting, = fixture.server.handle_request(middle)
            assert isinstance(waiting, wire.PlacementWait), waiting
            self.assertEqual((waiting.episode, waiting.since), (offered.episode, offered.since))
            self.assertEqual(queue_order(fixture), before)
