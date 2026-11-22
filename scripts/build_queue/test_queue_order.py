"""Check durable queue moves with a private admission service."""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import write_pressure_reading


def move_line(fixture: AdmissionFixture) -> records.QueueMoveLine:
    reply, = fixture.server.handle_request(wire.ViewRequest())
    assert isinstance(reply, wire.ViewReply)
    line = reply.reading.view.move_line
    assert isinstance(line, records.QueueMoveLine)
    return line


def ids(line: records.QueueMoveLine) -> list[str]:
    return [member.row_id for member in line.members]


class QueueMoveTests(unittest.TestCase):
    def test_priority_order_new_waits_restart_and_grant(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() + index) for index in range(5)]
            units = ["natemerge", "urgent", "ordinary", "ordinary", "ordinary"]
            requests = [replace(request, identity=replace(request.identity, unit=unit))
                        for request, unit in zip(requests, units, strict=True)]
            priority = records.QueuePriority(tuple({"unit": unit, "why": "fixture",
                "set_at": fixture.clock.now(), "set_by": "fixture"} for unit in units[:2]))
            fixture.server.priority = priority
            for request in requests:
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            merge, urgent, first, second, third = ids(line)
            cross, = fixture.server.handle_request(
                wire.QueueMoveRequest("cross", third, line.revision, wire.QueueDrop(urgent, "before")))
            self.assertEqual(cross, wire.QueueMoveConflict(line, "Use /build:bump to move a unit ahead"))
            request = wire.QueueMoveRequest("move", third, line.revision, wire.QueueDrop(first, "before"))
            moved, = fixture.server.handle_request(request)
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual((moved.old_place, moved.new_place), (5, 3))
            self.assertEqual(ids(moved.line), [merge, urgent, third, first, second])
            _ = fixture.run_pass()
            refreshed = move_line(fixture)
            self.assertEqual(refreshed, moved.line)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([row["pid"] for _, row in gate.ranked_queue_waiters(inputs)],
                             [requests[index].owner.pid for index in (0, 1, 4, 2, 3)])
            fixture.restart()
            newcomer = fixture.request(since=fixture.clock.now() - 30)
            for waiting in (newcomer, *reversed(requests)):
                _ = fixture.server.handle_request(waiting)
            current = move_line(fixture)
            self.assertEqual(ids(current)[:-1], ids(moved.line))
            self.assertEqual(fixture.server.handle_request(request), [moved])
            self.assertIsInstance(fixture.server.handle_request(wire.QueueMoveRequest(
                "stale", first, line.revision, wire.QueueDrop(third, "before")))[0], wire.QueueMoveConflict)
            self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest("clear", "fixture")), [wire.Ok()])
            self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            fixture.allow_next_start()
            _ = fixture.run_pass()
            self.assertIn(requests[-1].episode, fixture.server.reservations)
            self.assertNotIn(third, ids(move_line(fixture)))
            fixture.free(0)
            replacement = fixture.request(since=requests[-1].since)
            _ = fixture.server.handle_request(replacement)
            replacement_line = move_line(fixture)
            self.assertNotIn(third, ids(replacement_line))
            self.assertNotIn(ids(replacement_line)[-1], ids(current))

    def test_undo_uses_only_surviving_neighbors(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() + index) for index in range(4)]
            for request in requests:
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            first, second, third, last = ids(line)
            _ = fixture.server.handle_request(wire.QueueMoveRequest(
                "move", second, line.revision, wire.QueueDrop(last, "after")))
            _ = fixture.run_pass()
            moved = move_line(fixture)
            self.assertEqual(ids(moved), [first, third, last, second])
            undo = wire.QueueMoveRequest("undo", second, moved.revision, wire.QueueUndo("move"))
            reply, = fixture.server.handle_request(undo)
            assert isinstance(reply, wire.QueueMoved)
            self.assertEqual(ids(reply.line), ids(line))
            self.assertNotEqual(reply.line.revision, line.revision)
            _ = fixture.server.handle_request(wire.QueueMoveRequest(
                "move-again", second, reply.line.revision, wire.QueueDrop(last, "after")))
            fixture.proc.kill(requests[2].owner)
            fixture.restart()
            for waiting in (requests[0], requests[1], requests[3]):
                _ = fixture.server.handle_request(waiting)
            current = move_line(fixture)
            reply, = fixture.server.handle_request(wire.QueueMoveRequest(
                "missing-neighbor", second, current.revision, wire.QueueUndo("move-again")))
            self.assertEqual(reply, wire.QueueMoveConflict(current, "Undo not available"))

    def test_move_does_not_bypass_a_hold(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.service_request(job="test")
            second = fixture.service_request(job="test")
            reasons = {request.episode: records.NatedevHeld("fixture", "Wait for landing")
                       for request in (first, second)}
            with patch.object(fixture.server, "_ci_hold_reasons", return_value=reasons):
                for request in (first, second):
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.Wait)
                line = move_line(fixture)
                before, after = ids(line)
                moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                    "held-test", after, line.revision, wire.QueueDrop(before, "before")))
                assert isinstance(moved, wire.QueueMoved)
                _ = fixture.run_pass()
                self.assertEqual(ids(move_line(fixture)), [after, before])
                self.assertFalse(fixture.server.reservations)
