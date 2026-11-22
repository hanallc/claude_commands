"""Check durable queue moves with a private admission service."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from typing import cast
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records, execution_identity, placement
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
    return [member.row_id for member in line.members if member.movable]


class QueueMoveTests(unittest.TestCase):
    def test_new_wait_joins_after_the_saved_tail_when_the_bump_clears(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first, second, tail = [fixture.request(since=fixture.clock.now() + index) for index in range(3)]
            first, second = [replace(request, identity=replace(request.identity, unit="urgent"))
                             for request in (first, second)]
            fixture.server.priority = records.QueuePriority(({"unit": "urgent", "why": "fixture",
                "set_at": fixture.clock.now(), "set_by": "fixture"},))
            for request in (first, second, tail):
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            before, after, unmoved = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move", after, line.revision, wire.QueueDrop(before, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest("clear", "fixture")), [wire.Ok()])
            newcomer = fixture.request(since=fixture.clock.now() - 30)
            _ = fixture.server.handle_request(newcomer)
            self.assertEqual(ids(move_line(fixture))[:3], [after, before, unmoved])

    def test_cancelled_remote_only_wait_drops_its_saved_rank(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first, second = [fixture.request(since=fixture.clock.now() + index) for index in range(2)]
            remote = placement_request(fixture, since=fixture.clock.now() + 100)
            _ = publish_candidate(fixture, remote, placement.TemporaryExclusion("busy"))
            remote = replace(remote, constraints=replace(remote.constraints,
                candidates=(*remote.constraints.candidates, local_candidate(fixture))))
            for request in (first, second, remote):
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            before, after, remote_id = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move", remote_id, line.revision, wire.QueueDrop(before, "before")))
            assert isinstance(moved, wire.QueueMoved)
            remote = replace(remote, local=wire.LocalAdmissionUnavailable("remote only"))
            self.assertIsInstance(fixture.server.handle_request(remote)[0], wire.PlacementWait)
            self.assertNotIn(remote.episode, fixture.server.waiters)
            self.assertEqual(move_line(fixture).members[0], records.QueueMoveMember(remote_id, movable=False))
            self.assertEqual(fixture.server.saved_order, (remote_id, before, after))
            self.assertEqual(fixture.server.handle_request(wire.CancelPlacement("cancel", remote.episode,
                remote.owner, "fixture")), [wire.Ok()])
            place = next(place for place in fixture.server.places.values() if place.episode == remote.episode)
            self.assertEqual(place.move_id, "")
            self.assertEqual(fixture.server.saved_order, (before, after))
            newcomer = fixture.request(since=fixture.clock.now() - 30)
            _ = fixture.server.handle_request(newcomer)
            self.assertEqual(ids(move_line(fixture))[:2], [before, after])

    def test_new_wait_stays_below_a_dragged_row_after_the_line_shrinks(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() + index) for index in range(3)]
            for request in requests:
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            first, _, last = ids(line)
            result, = fixture.server.handle_request(wire.QueueMoveRequest(
                "drop-last", first, line.revision, wire.QueueDrop(last, "after")))
            assert isinstance(result, wire.QueueMoved)
            fixture.end_owner(requests[1].owner)
            _ = fixture.run_pass()
            self.assertEqual(ids(move_line(fixture)), [last, first])

            newcomer = fixture.request(since=fixture.clock.now() - 30)
            _ = fixture.server.handle_request(newcomer)
            self.assertEqual(ids(move_line(fixture))[:2], [last, first])

    def test_newcomer_after_restart_joins_after_absent_group_members(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() + index) for index in range(3)]
            for request in requests:
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            first, second, third = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "first-to-third", first, line.revision, wire.QueueDrop(third, "after")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(ids(moved.line), [second, third, first])

            fixture.restart()
            _ = fixture.server.handle_request(requests[0])
            newcomer = fixture.request(since=fixture.clock.now() - 30)
            _ = fixture.server.handle_request(newcomer)
            _, new_id = ids(move_line(fixture))
            self.assertEqual(fixture.server.saved_order, (second, third, first, new_id))
            for request in reversed(requests[1:]):
                _ = fixture.server.handle_request(request)
            self.assertEqual(ids(move_line(fixture)), [second, third, first, new_id])

    def test_durable_rank_survives_restart_and_priority_clear(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() + index) for index in range(5)]
            units = ["natemerge", "urgent", "ordinary", "later-unit", "ordinary"]
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
            assert isinstance(cross, wire.QueueMoved)
            self.assertEqual((cross.old_place, cross.new_place), (5, 2))
            self.assertEqual(ids(cross.line), [merge, third, urgent, first, second])
            _ = fixture.run_pass()
            self.assertEqual(move_line(fixture), cross.line)
            undone, = fixture.server.handle_request(wire.QueueMoveRequest(
                "undo-cross", third, cross.line.revision, wire.QueueUndo("cross")))
            assert isinstance(undone, wire.QueueMoved)
            self.assertEqual((undone.old_place, undone.new_place), (2, 5))
            self.assertEqual(ids(undone.line), ids(line))
            self.assertEqual(fixture.server.dragged_ids, frozenset())

            request = wire.QueueMoveRequest("move", third, undone.line.revision, wire.QueueDrop(merge, "before"))
            moved, = fixture.server.handle_request(request)
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual((moved.old_place, moved.new_place), (5, 1))
            self.assertEqual(ids(moved.line), [third, merge, urgent, first, second])
            _ = fixture.run_pass()
            self.assertEqual(move_line(fixture), moved.line)
            self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                "bump-later", "later-unit", "fixture", "fixture")), [wire.Ok()])
            _ = fixture.run_pass()
            after_bump = [third, merge, urgent, second, first]
            self.assertEqual(ids(move_line(fixture)), after_bump)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([row["pid"] for _, row in gate.ranked_queue_waiters(inputs)],
                             [requests[index].owner.pid for index in (4, 0, 1, 3, 2)])
            self.assertEqual(fixture.server.saved_order, tuple(after_bump))
            fixture.restart()
            self.assertEqual(fixture.server.dragged_ids, frozenset({third}))
            newcomer = fixture.request(since=fixture.clock.now() - 30)
            _ = fixture.server.handle_request(newcomer)
            new_id, = ids(move_line(fixture))
            for waiting in reversed(requests):
                _ = fixture.server.handle_request(waiting)
            current = move_line(fixture)
            self.assertEqual(ids(current), [*after_bump, new_id])
            self.assertEqual(fixture.server.handle_request(request), [moved])
            self.assertIsInstance(fixture.server.handle_request(wire.QueueMoveRequest(
                "stale", first, line.revision, wire.QueueDrop(third, "before")))[0], wire.QueueMoveConflict)
            self.assertEqual(fixture.server.handle_request(
                wire.PriorityClearRequest("clear-later", "fixture", unit="later-unit")), [wire.Ok()])
            after_clear = [third, merge, urgent, first, new_id, second]
            self.assertEqual(ids(move_line(fixture)), after_clear)
            self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                "bump-dragged-unit", "ordinary", "fixture", "fixture")), [wire.Ok()])
            self.assertEqual(ids(move_line(fixture)), after_clear)
            self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest("clear", "fixture")), [wire.Ok()])
            self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
            self.assertEqual(ids(move_line(fixture)), [third, new_id, second, merge, urgent, first])
            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            fixture.allow_next_start()
            _ = fixture.run_pass()
            self.assertIn(requests[4].episode, fixture.server.reservations)
            self.assertEqual(fixture.server.dragged_ids, frozenset())
            remaining = ids(move_line(fixture))
            self.assertNotIn(third, remaining)
            self.assertLess(remaining.index(merge), remaining.index(first))
            fixture.free(0)
            replacement = fixture.request(since=requests[-1].since)
            _ = fixture.server.handle_request(replacement)
            replacement_line = move_line(fixture)
            self.assertIn(merge, ids(replacement_line))
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
            crossed, = fixture.server.handle_request(wire.QueueMoveRequest(
                "cross-pinned", first, moved.revision, wire.QueueDrop(second, "after")))
            assert isinstance(crossed, wire.QueueMoved)
            self.assertEqual(ids(crossed.line), [third, last, second, first])
            _ = fixture.run_pass()
            self.assertEqual(move_line(fixture), crossed.line)
            restored, = fixture.server.handle_request(wire.QueueMoveRequest(
                "undo-crossing", first, crossed.line.revision, wire.QueueUndo("cross-pinned")))
            assert isinstance(restored, wire.QueueMoved)
            self.assertEqual(ids(restored.line), ids(moved))
            undo = wire.QueueMoveRequest("undo", second, restored.line.revision, wire.QueueUndo("move"))
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

    def test_mac_only_join_refuses_a_confirm_with_an_old_revision(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first, second = [fixture.request(since=fixture.clock.now() + index) for index in range(2)]
            for request in (first, second):
                _ = fixture.server.handle_request(request)
            before = move_line(fixture)
            first_id, second_id = ids(before)
            mac = placement_request(fixture, since=fixture.clock.now() - 10)
            mac = replace(mac, constraints=replace(mac.constraints, selection=placement.MacOnlyHost()))
            _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
            self.assertIsInstance(fixture.server.handle_request(mac)[0], wire.PlacementWait)

            reply, = fixture.server.handle_request(wire.QueueMoveRequest(
                "stale", second_id, before.revision, wire.QueueDrop(first_id, "before")))
            assert isinstance(reply, wire.QueueMoveConflict)
            self.assertNotEqual(reply.line.revision, before.revision)
            self.assertFalse(reply.line.members[0].movable)
            self.assertEqual(reply.line.members[1:], before.members)
            self.assertEqual(fixture.server.saved_order, ())

    def test_a_mac_only_waiter_ahead_of_both_movable_waiters_keeps_its_place(self) -> None:
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

    def test_placement_follows_the_visible_order_on_both_hosts(self) -> None:
        for moved in (False, True):
            with self.subTest(moved=moved), PlacementFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                first, mac, last = [placement_request(fixture, since=fixture.clock.now() - 10 + index)
                                    for index in range(3)]
                mac = replace(mac, constraints=replace(mac.constraints, selection=placement.MacOnlyHost()))
                _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
                first, last = [replace(request, prerequisite=placement.TokenNotRequired(),
                    constraints=replace(request.constraints, candidates=(local_candidate(fixture), *request.constraints.candidates)))
                    for request in (first, last)]
                for position, request in enumerate((first, mac, last), 1):
                    reply, = fixture.server.handle_request(request)
                    assert isinstance(reply, wire.PlacementWait)
                    self.assertEqual(reply.position, position)
                self.check_builds_table(fixture, (first, mac, last), mac)
                if moved:
                    line = move_line(fixture)
                    before, after = ids(line)
                    result, = fixture.server.handle_request(wire.QueueMoveRequest(
                        "move", after, line.revision, wire.QueueDrop(before, "before")))
                    assert isinstance(result, wire.QueueMoved)
                    self.assertEqual(ids(result.line), [after, before])
                    for position, request in enumerate((last, first, mac), 1):
                        reply, = fixture.server.handle_request(request)
                        assert isinstance(reply, wire.PlacementWait)
                        self.assertEqual(reply.position, position)
                    self.check_builds_table(fixture, (last, first, mac), mac)

                fixture.free(12 * GIB)
                write_pressure_reading(fixture.root / "pressure")
                _ = publish_candidate(fixture, mac)
                _ = fixture.run_pass()
                local, remote = (last, first) if moved else (first, mac)
                self.assertEqual(set(fixture.caller_reservations()), {local.episode})
                self.assertEqual(set(fixture.server.current_assignments), {local.episode, remote.episode})
                self.assertEqual(assignment_for(fixture, remote).destination.provider, "mac")

    def check_builds_table(self, fixture: PlacementFixture, requests: tuple[wire.PlacementRequest, ...],
                          mac: wire.PlacementRequest) -> None:
        from scripts.build_queue import admission_client, memory_feed
        from scripts.build_queue.test_memory_feed import MemoryFeedTests

        _ = fixture.run_pass()
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        feed = MemoryFeedTests()
        feed.setUp()
        try:
            with patch.object(admission_client, "view", return_value=reply.reading):
                table = memory_feed.reading()["waiters"]
        finally:
            _ = feed.doCleanups()
        self.assertEqual([(row["pid"], row.get("place")) for row in table],
                         [(request.owner.pid, place) for place, request in enumerate(requests, 1)])
        row = next(row for row in table if row["pid"] == mac.owner.pid)
        self.assertEqual(row.get("targets"), ["mac"])
        self.assertEqual(row["reason"], "waiting for mac")
        self.assertIsInstance(member := row.get("move"), records.QueueMoveMember)
        assert isinstance(member, records.QueueMoveMember)
        self.assertFalse(member.movable)

    def test_moved_mac_hold_keeps_its_rank_on_later_passes_and_restart(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first = placement_request(fixture, since=fixture.clock.now() - 10)
            first = replace(first, constraints=replace(first.constraints, candidates=(local_candidate(fixture),)))
            second = placement_request(fixture)
            _ = publish_candidate(fixture, second, placement.TemporaryExclusion("busy"))
            second = replace(second, constraints=replace(second.constraints,
                candidates=(*second.constraints.candidates, local_candidate(fixture))))
            for request in (first, second):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.PlacementWait)
            line = move_line(fixture)
            before, after = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move", after, line.revision, wire.QueueDrop(before, "before")))
            assert isinstance(moved, wire.QueueMoved)
            _ = publish_candidate(fixture, second)
            _ = fixture.run_pass()
            remote = assignment_for(fixture, second)
            self.assertEqual(remote.destination.provider, "mac")
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, first.owner.pid])
            held, next_waiter = move_line(fixture).members
            self.assertEqual((held.row_id, held.movable, next_waiter.row_id), (after, False, before))

            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            _ = fixture.run_pass()
            self.assertFalse(fixture.caller_reservations())
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, first.owner.pid])

            fixture.restart()
            _ = fixture.run_pass()
            self.assertFalse(fixture.caller_reservations())
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, first.owner.pid])
            self.assertEqual(move_line(fixture).members, (held, next_waiter))

    def test_moved_mac_wait_keeps_its_rank_after_the_hold_expires(self) -> None:
        with PlacementFixture() as fixture:
            hold_seconds = 3
            fixture.configure(remote_start_hold_seconds=hold_seconds)
            fixture.free(0)
            fixture.hold_starts()
            first = placement_request(fixture, since=fixture.clock.now() - 10)
            first = replace(first, constraints=replace(first.constraints, candidates=(local_candidate(fixture),)))
            second = placement_request(fixture)
            _ = publish_candidate(fixture, second, placement.TemporaryExclusion("busy"))
            second = replace(second, constraints=replace(second.constraints,
                candidates=(*second.constraints.candidates, local_candidate(fixture))))
            for request in (first, second):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.PlacementWait)
            line = move_line(fixture)
            before, after = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move", after, line.revision, wire.QueueDrop(before, "before")))
            assert isinstance(moved, wire.QueueMoved)
            _ = publish_candidate(fixture, second)
            remote = assignment_for(fixture, second)
            self.assertEqual(remote.destination.provider, "mac")
            fixture.clock.advance(hold_seconds + 1)
            _ = fixture.run_pass()
            self.assertNotIn(second.episode, fixture.server.waiters)
            self.assertIn(second.episode, fixture.server.current_assignments)

            fixture.restart()
            _ = publish_candidate(fixture, second, placement.TemporaryExclusion("busy"))
            refused, = fixture.server.handle_request(wire.ClaimFailure("mac-busy", remote, "busy"))
            assert isinstance(refused, wire.DispatchStatus)
            self.assertEqual(refused.state, placement.ClosedAssignment("busy"))
            _ = fixture.run_pass()
            self.assertEqual(ids(move_line(fixture)), [after, before])
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, first.owner.pid])

    def test_returning_mac_assignment_keeps_its_slot_after_a_move(self) -> None:
        with PlacementFixture() as fixture:
            hold_seconds = 3
            fixture.configure(remote_start_hold_seconds=hold_seconds)
            fixture.free(0)
            fixture.hold_starts()
            first = fixture.request(since=fixture.clock.now() - 10)
            mac = placement_request(fixture)
            _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
            mac = replace(mac, constraints=replace(mac.constraints,
                candidates=(*mac.constraints.candidates, local_candidate(fixture))))
            third = fixture.request(since=fixture.clock.now() + 10)
            for request in (first, mac, third):
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            first_id, mac_id, third_id = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "mac-first", mac_id, line.revision, wire.QueueDrop(first_id, "before")))
            assert isinstance(moved, wire.QueueMoved)
            _ = publish_candidate(fixture, mac)
            assignment = assignment_for(fixture, mac)
            self.assertEqual(assignment.destination.provider, "mac")
            fixture.clock.advance(hold_seconds + 1)
            _ = fixture.run_pass()
            self.assertIn(mac.episode, fixture.server.current_assignments)
            current = move_line(fixture)
            self.assertEqual(ids(current), [first_id, third_id])

            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "third-first", third_id, current.revision, wire.QueueDrop(first_id, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(ids(moved.line), [third_id, first_id])
            self.assertEqual(fixture.server.saved_order, (mac_id, third_id, first_id))
            _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
            refused, = fixture.server.handle_request(wire.ClaimFailure("mac-busy", assignment, "busy"))
            assert isinstance(refused, wire.DispatchStatus)
            self.assertEqual(refused.state, placement.ClosedAssignment("busy"))
            _ = fixture.run_pass()
            self.assertEqual(ids(move_line(fixture)), [mac_id, third_id, first_id])
            self.assertEqual(fixture.queue_pids(), [mac.owner.pid, third.owner.pid, first.owner.pid])

    def test_returned_local_grant_keeps_the_rank_before_launch(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first, second = [replace(request, prerequisite=placement.TokenNotRequired(),
                constraints=replace(request.constraints, candidates=(local_candidate(fixture),)))
                for request in (placement_request(fixture, since=fixture.clock.now() - 10), placement_request(fixture))]
            for request in (first, second):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.PlacementWait)
            line = move_line(fixture)
            before, after = ids(line)
            result, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move", after, line.revision, wire.QueueDrop(before, "before")))
            assert isinstance(result, wire.QueueMoved)
            fixture.free(12 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            fixture.allow_next_start()
            granted, = fixture.server.handle_request(second)
            assert isinstance(granted, wire.LocalPlacementGranted)

            fixture.free(0)
            fixture.hold_starts()
            fixture.release(granted.grant)
            _ = publish_candidate(fixture, second, placement.TemporaryExclusion("blocked"))
            refused, = fixture.server.handle_request(wire.ExecutionReport("hold-changed", granted.assignment,
                placement.NoProviderClaim(), execution_identity.ExecutionNotStarted(),
                placement.PrelaunchRefused("local hold changed before execution"), placement.DispatchPathSettled()))
            assert isinstance(refused, wire.DispatchStatus)
            self.assertEqual(refused.state, placement.ClosedAssignment("local hold changed before execution"))
            _ = fixture.run_pass()
            self.assertNotIn(second.episode, fixture.server.current_assignments)
            self.assertIn(second.episode, fixture.server.waiters)
            self.assertEqual([member.row_id for member in move_line(fixture).members], [after, before])

    def test_builds_table_reports_a_hold_and_the_speculative_copy_rule(self) -> None:
        from scripts.build_queue import admission_client, memory_feed
        from scripts.build_queue.test_memory_feed import MemoryFeedTests

        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            held = placement_request(fixture)
            held = replace(held, constraints=replace(held.constraints, candidates=(local_candidate(fixture),)))
            _ = publish_candidate(fixture, held, placement.TemporaryExclusion("blocked"))
            copy = replace(placement_request(fixture), purpose=placement.SpeculativeCopy("parent", fixture.clock.now()),
                           local=wire.LocalAdmissionUnavailable("remote only"))
            _ = publish_candidate(fixture, copy, placement.TemporaryExclusion("busy"))
            for request in (held, copy):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.PlacementWait)

            reply, = fixture.server.handle_request(wire.ViewRequest())
            reply = wire.parse_reply(wire.encode_reply(reply))
            assert isinstance(reply, wire.ViewReply)
            by_pid = {step.pid: step for step in reply.reading.view.waiting}
            self.assertIsInstance(by_pid[held.owner.pid].reason, records.NatedevHeld)
            self.assertEqual(by_pid[copy.owner.pid].placement_targets, ("mac",))
            feed = MemoryFeedTests()
            feed.setUp()
            try:
                with patch.object(admission_client, "view", return_value=reply.reading):
                    table = {row["pid"]: row for row in memory_feed.reading()["waiters"]}
            finally:
                _ = feed.doCleanups()
            self.assertEqual(table[held.owner.pid].get("why"), "build hold")
            self.assertIs(table[held.owner.pid].get("fits"), False)
            self.assertEqual(table[copy.owner.pid].get("order_note"), "Newest eligible copy starts first")

    def test_hana_refusal_before_launch_keeps_the_local_claim(self) -> None:
        from scripts.build_queue.test_hana_placement import hana_fallback_request

        with PlacementFixture() as fixture:
            run = fixture.run_owner()
            slot = fixture.finish_step(fixture.request(run=run, need=4 * GIB)).reservation
            fixture.hold_starts()
            request = replace(hana_fallback_request(fixture), run=run)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            self.assertEqual(assignment.destination.provider, "ssh")
            claim = fixture.server.slots[slot]
            assert isinstance(claim, records.ContinuationSlot)
            self.assertEqual(claim.owner, records.run_owner(request.owner.pid, request.owner.start_time))
            reference = placement.assigned_claim_reference(assignment)
            refused, = fixture.server.handle_request(wire.ExecutionReport("hana-refused", assignment, reference,
                execution_identity.ExecutionNotStarted(), execution_identity.HanaCommandAdmissionRefused(placement.HANA_IMAGE_STALE),
                placement.DispatchPathSettled()))
            assert isinstance(refused, wire.DispatchStatus)
            self.assertEqual(refused.state, placement.ClosedAssignment(placement.HANA_IMAGE_STALE))
            self.assertEqual(fixture.server.slots[slot], claim)

            fixture.allow_next_start()
            write_pressure_reading(fixture.root / "pressure")
            _ = fixture.run_pass()
            granted, = fixture.server.handle_request(request)
            assert isinstance(granted, wire.LocalPlacementGranted)
            self.assertEqual(granted.assignment.episode, assignment.episode)

    def test_new_mac_hold_blocks_an_unselected_local_waiter_and_keeps_its_claim(self) -> None:
        with PlacementFixture() as fixture:
            run = fixture.run_owner()
            slot_key = fixture.finish_step(fixture.request(run=run, need=4 * GIB)).reservation
            fixture.free(0)
            fixture.hold_starts()
            head = placement_request(fixture, since=fixture.clock.now() - 10)
            _ = publish_candidate(fixture, head, placement.TemporaryExclusion("busy"))
            head = replace(head, run=run, prerequisite=placement.TokenNotRequired(), constraints=replace(
                head.constraints, candidates=(*head.constraints.candidates, local_candidate(fixture))))
            later = placement_request(fixture)
            later = replace(later, prerequisite=placement.TokenNotRequired(), constraints=replace(
                later.constraints, candidates=(local_candidate(fixture),)))
            for request in (head, later):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.PlacementWait)

            fixture.allow_next_start()
            fixture.free(16 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            _ = publish_candidate(fixture, head)
            result = fixture.run_pass()
            self.assertFalse(fixture.caller_reservations())
            self.assertEqual(fixture.queue_pids(), [head.owner.pid, later.owner.pid])
            later_decision = result.rows[1].decision
            self.assertTrue(later_decision.room.passes)
            assert isinstance(later_decision.order_hold, records.HeldBehindHead)
            self.assertEqual(later_decision.order_hold.waiter["pid"], head.owner.pid)
            claim = fixture.server.slots[slot_key]
            assert isinstance(claim, records.ContinuationSlot)
            self.assertEqual(claim.owner, records.run_owner(head.owner.pid, head.owner.start_time))
            remote = assignment_for(fixture, head)
            self.assertEqual(remote.destination.provider, "mac")

            _ = publish_candidate(fixture, head, placement.TemporaryExclusion("busy"))
            refused, = fixture.server.handle_request(wire.ClaimFailure("mac-busy", remote, "busy"))
            assert isinstance(refused, wire.DispatchStatus)
            self.assertEqual(refused.state, placement.ClosedAssignment("busy"))
            self.assertEqual(fixture.server.slots[slot_key], claim)
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.caller_reservations()), {head.episode})
            self.assertIsInstance(fixture.server.handle_request(later)[0], wire.PlacementWait)

    def test_a_move_allows_local_and_mac_starts_in_one_pass(self) -> None:
        for promote_local in (True, False):
            with self.subTest(promote_local=promote_local), PlacementFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                earlier = fixture.clock.now() - 10
                a = placement_request(fixture, since=earlier + int(promote_local))
                b = placement_request(fixture, since=earlier + int(not promote_local))
                _ = publish_candidate(fixture, b, placement.TemporaryExclusion("busy"))
                a = replace(a, prerequisite=placement.TokenNotRequired(), constraints=replace(a.constraints,
                    candidates=(local_candidate(fixture), *a.constraints.candidates)))
                b = replace(b, prerequisite=placement.TokenNotRequired() if promote_local else placement.TokenRequired(),
                    constraints=replace(b.constraints, candidates=(local_candidate(fixture), *b.constraints.candidates)))
                for request in (a, b):
                    self.assertIsInstance(fixture.server.handle_request(request)[0], wire.PlacementWait)
                line = move_line(fixture)
                first, second = ids(line)
                moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                    "move", second, line.revision, wire.QueueDrop(first, "before")))
                assert isinstance(moved, wire.QueueMoved)
                fixture.free(12 * GIB)
                fixture.clock.advance(1)
                mac_probe = replace(b, constraints=replace(b.constraints, candidates=b.constraints.candidates[1:]))
                _ = publish_candidate(fixture, mac_probe)
                _ = fixture.run_pass()
                self.assertEqual(set(fixture.server.reservations), {a.episode})
                self.assertEqual(set(fixture.server.current_assignments), {a.episode, b.episode})
                local = fixture.server.assignments[fixture.server.current_assignments[a.episode]]
                remote = fixture.server.assignments[fixture.server.current_assignments[b.episode]]
                self.assertEqual(local.assignment.destination.provider, "local")
                self.assertIsInstance(local.permission, placement.PermissionConsumed)
                self.assertEqual(remote.assignment.destination.provider, "mac")
                self.assertIsInstance(remote.remote_start_hold, records.RemoteStartHold)

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
