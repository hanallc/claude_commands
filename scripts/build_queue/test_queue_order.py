"""Check durable queue moves with a private admission service."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from typing import cast
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records, placement
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB
from scripts.build_queue.test_placement import PlacementFixture, assignment_for, local_candidate, placement_request, publish_candidate
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import write_pressure_reading
from scripts.production import land_runs


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

    def test_the_local_order_leaves_mac_competition_in_submission_order(self) -> None:
        for open_local in (False, True):
            with self.subTest(open_local=open_local), PlacementFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                mac, first, second = [placement_request(fixture, since=fixture.clock.now() + index)
                                     for index in range(3)]
                mac = replace(mac, constraints=replace(mac.constraints, selection=placement.MacOnlyHost()))
                first, second = [replace(request, prerequisite=placement.TokenNotRequired(),
                    constraints=replace(request.constraints, candidates=(*request.constraints.candidates,
                                                                         local_candidate(fixture))))
                    for request in (first, second)]
                _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
                for request in (mac, first, second):
                    self.assertIsInstance(fixture.server.handle_request(request)[0], wire.PlacementWait)
                line = move_line(fixture)
                before, after = ids(line)
                moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                    "move", after, line.revision, wire.QueueDrop(before, "before")))
                assert isinstance(moved, wire.QueueMoved)
                _ = fixture.run_pass()
                self.assertEqual(move_line(fixture), moved.line)
                inputs = fixture.server.last_inputs
                assert isinstance(inputs, records.QueueInputs)
                self.assertEqual([row["pid"] for _, row in gate.ranked_queue_waiters(inputs)],
                                 [second.owner.pid, first.owner.pid])
                mac_reply, = fixture.server.handle_request(mac)
                assert isinstance(mac_reply, wire.PlacementWait)
                self.assertEqual(mac_reply.position, 1)
                moved_reply, = fixture.server.handle_request(second)
                assert isinstance(moved_reply, wire.PlacementWait)
                self.assertEqual(moved_reply.position, 2)
                if open_local:
                    fixture.free(60 * GIB)
                    write_pressure_reading(fixture.root / "pressure")
                    fixture.allow_next_start()
                    _ = fixture.run_pass()
                    self.assertEqual(set(fixture.server.reservations), {second.episode})
                    self.assertIsInstance(fixture.server.handle_request(second)[0], wire.LocalPlacementGranted)
                else:
                    _ = publish_candidate(fixture, mac)
                    self.assertEqual(assignment_for(fixture, mac).destination.provider, "mac")
                    self.assertNotIn(first.episode, fixture.server.current_assignments)
                    self.assertNotIn(second.episode, fixture.server.current_assignments)

    @unittest.skipUnless(hasattr(wire, "LandingRepositoryKey"), "requires the 44eb landing protection")
    def test_move_preserves_the_landing_test_hold(self) -> None:
        with (AdmissionFixture(skip_idle_spacing=True) as fixture,
              patch.dict(os.environ, {"GATE_STATE_DIR": str(fixture.root / "gate-state")}),
              land_runs.landing(["a" * 40])):
            for _index in range(2):
                request = fixture.request()
                identity = cast(dict[str, object], json.loads(wire.encode_record(request.identity)))
                identity["landing_repository"] = {
                    "kind": "LandingRepositoryKey", "key": "https://github.com/natepiano/claude_commands",
                }
                request = replace(request, identity=wire.parse_record(json.dumps(identity), wire.Identity))
                reply, = fixture.server.handle_request(request)
                assert isinstance(reply, wire.Wait)
                self.assertEqual(reply.text, "waits for the landing queue")
            line = move_line(fixture)
            first, second = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move-held-test", second, line.revision, wire.QueueDrop(first, "before")))
            assert isinstance(moved, wire.QueueMoved)
            _ = fixture.run_pass()
            self.assertEqual(ids(move_line(fixture)), [second, first])
            view, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(view, wire.ViewReply)
            self.assertEqual([records.reason_phrase(wait.reason, ()) for wait in view.reading.view.waiting],
                             ["waits for the landing queue"] * 2)
            self.assertFalse(fixture.server.reservations)
