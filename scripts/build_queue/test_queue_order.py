"""Check durable queue moves with a private admission service."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from typing import cast
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records, execution_identity, head_stall, placement, queue_snapshot
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB
from scripts.build_queue.test_placement import PlacementFixture, assignment_for, local_candidate, placement_request, publish_candidate
from scripts.build_hold import build_hold
from scripts.build_targets import build_targets
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import admission_policy_fixture, write_pressure_reading
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
    def test_remote_only_head_keeps_the_moved_continuation_first_for_the_slot(self) -> None:
        with PlacementFixture() as fixture:
            run = fixture.run_owner()
            running = fixture.request(run=run, need=4 * GIB)
            grant, = fixture.server.handle_request(running)
            assert isinstance(grant, wire.Admitted)
            fixture.server.spacing = records.NextStartAt(fixture.clock.now() + 30)
            fixture.free(0)
            fixture.hold_starts()
            remote = placement_request(fixture, since=running.since - 10)
            remote = replace(remote, local=wire.LocalAdmissionUnavailable("remote only"),
                             constraints=replace(remote.constraints, selection=placement.MacOnlyHost()))
            _ = publish_candidate(fixture, remote, placement.TemporaryExclusion("busy"))
            self.assertIsInstance(fixture.server.handle_request(remote)[0], wire.PlacementWait)
            first, second = [fixture.request(run=run, need=4 * GIB, since=running.since + index,
                             owner=fixture.fixture_owner(700000 + index, "4000")) for index in (1, 2)]
            for request in (first, second):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            line = move_line(fixture)
            remote_row, first_row, second_row = line.members
            self.assertFalse(remote_row.movable)
            self.assertNotIn(remote.episode, fixture.server.waiters)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move-before-first", second_row.row_id, line.revision, wire.QueueDrop(first_row.row_id, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(moved.line.members, (remote_row, second_row, first_row))
            fixture.release(grant)
            claim = fixture.server.slots[grant.reservation]
            assert isinstance(claim, records.ContinuationSlot)
            self.assertEqual(claim.owner, records.waiter_owner(fixture.server.waiters[second.episode].row))
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {second.episode})

    def test_move_after_release_reassigns_the_slot_on_the_next_pass(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            running = fixture.request(run=run, need=4 * GIB)
            grant, = fixture.server.handle_request(running)
            assert isinstance(grant, wire.Admitted)
            fixture.server.spacing = records.NextStartAt(fixture.clock.now() + 30)
            fixture.free(0)
            fixture.hold_starts()
            first, second = [fixture.request(run=run, need=4 * GIB, since=running.since + index)
                             for index in (1, 2)]
            for request in (first, second):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            line = move_line(fixture)
            first_id, second_id = ids(line)
            fixture.release(grant)
            claim = fixture.server.slots[grant.reservation]
            assert isinstance(claim, records.ContinuationSlot)
            self.assertEqual(claim.owner, records.run_owner(first.owner.pid, first.owner.start_time))

            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move-after-release", second_id, line.revision, wire.QueueDrop(first_id, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(fixture.server.slots[grant.reservation], claim.slot)
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {second.episode})
            self.assertEqual(ids(move_line(fixture)), [first_id])
            self.assertIn(first.episode, fixture.server.waiters)

    def test_move_reassigns_both_slots_when_the_earlier_wait_needs_the_larger_slot(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            large, = fixture.server.handle_request(fixture.request(run=run, need=8 * GIB))
            assert isinstance(large, wire.Admitted)
            fixture.allow_next_start()
            small, = fixture.server.handle_request(fixture.request(run=run, need=4 * GIB))
            assert isinstance(small, wire.Admitted)
            fixture.free(0)
            fixture.hold_starts()
            first, second = [fixture.request(run=run, need=8 * GIB, since=fixture.clock.now() + index)
                             for index in (1, 2)]
            for request in (first, second):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            line = move_line(fixture)
            first_id, second_id = ids(line)
            fixture.release(large)
            fixture.clock.advance(0.1)
            fixture.release(small)
            large_claim = fixture.server.slots[large.reservation]
            small_claim = fixture.server.slots[small.reservation]
            assert isinstance(large_claim, records.ContinuationSlot)
            assert isinstance(small_claim, records.ContinuationSlot)
            self.assertEqual(large_claim.owner, records.run_owner(first.owner.pid, first.owner.start_time))
            self.assertEqual(small_claim.owner, records.run_owner(second.owner.pid, second.owner.start_time))

            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move-two-slots", second_id, line.revision, wire.QueueDrop(first_id, "before")))
            assert isinstance(moved, wire.QueueMoved)
            result = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {second.episode})
            self.assertEqual(result.rows[0].decision.room.basis, records.WithinSlot())
            self.assertEqual(ids(move_line(fixture)), [first_id])
            claim = fixture.server.slots[small.reservation]
            assert isinstance(claim, records.ContinuationSlot)
            self.assertEqual(claim.owner, large_claim.owner)

    def test_undo_returns_a_slot_claimed_after_the_move(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            grant, = fixture.server.handle_request(fixture.request(run=run, need=4 * GIB))
            assert isinstance(grant, wire.Admitted)
            fixture.free(0)
            fixture.hold_starts()
            first, second = [fixture.request(run=run, need=4 * GIB, since=fixture.clock.now() + index)
                             for index in (1, 2)]
            for request in (first, second):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            line = move_line(fixture)
            first_id, second_id = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move-before-release", second_id, line.revision, wire.QueueDrop(first_id, "before")))
            assert isinstance(moved, wire.QueueMoved)
            fixture.release(grant)
            claim = fixture.server.slots[grant.reservation]
            assert isinstance(claim, records.ContinuationSlot)
            self.assertEqual(claim.owner, records.run_owner(second.owner.pid, second.owner.start_time))

            undone, = fixture.server.handle_request(wire.QueueMoveRequest(
                "undo-before-pass", second_id, moved.line.revision, wire.QueueUndo("move-before-release")))
            assert isinstance(undone, wire.QueueMoved)
            self.assertEqual(fixture.server.slots[grant.reservation], claim.slot)
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {first.episode})
            self.assertEqual(ids(move_line(fixture)), [second_id])

    def test_move_ahead_of_a_mac_start_keeps_its_local_fallback_slot(self) -> None:
        with PlacementFixture() as fixture:
            run = fixture.run_owner()
            slot_key = fixture.finish_step(fixture.request(run=run, need=8 * GIB)).reservation
            fixture.free(0)
            fixture.hold_starts()
            folder = placement.CargoFolder(fixture.root / "target")
            mac = placement_request(fixture, since=fixture.clock.now() - 10)
            mac = replace(mac, run=run, prerequisite=placement.TokenNotRequired(), constraints=replace(
                mac.constraints, candidates=(*mac.constraints.candidates, local_candidate(fixture)), cargo_folder=folder))
            _ = publish_candidate(fixture, mac)
            assignment = assignment_for(fixture, mac)
            self.assertEqual(assignment.destination.provider, "mac")
            claim = fixture.server.slots[slot_key]
            assert isinstance(claim, records.ContinuationSlot)
            self.assertEqual(claim.owner, records.run_owner(mac.owner.pid, mac.owner.start_time))

            later = placement_request(fixture)
            later = replace(later, prerequisite=placement.TokenNotRequired(), constraints=replace(
                later.constraints, candidates=(local_candidate(fixture),), cargo_folder=folder))
            self.assertIsInstance(fixture.server.handle_request(later)[0], wire.PlacementWait)
            line = move_line(fixture)
            mac_row, later_row = line.members
            self.assertFalse(mac_row.movable)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move-ahead-of-mac", later_row.row_id, line.revision, wire.QueueDrop(mac_row.row_id, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(moved.line.members, (later_row, mac_row))
            self.assertEqual(fixture.server.slots[slot_key], claim)
            result = fixture.run_pass()
            self.assertEqual(result.rows[1].owner, claim.owner)
            self.assertIsInstance(result.rows[1].decision.order_hold, records.HeldBehindHead)
            self.assertEqual(fixture.server.slots[slot_key], claim)
            room = result.policy.room
            assert isinstance(room, records.MemoryRoom)
            self.assertEqual(result.rows[0].decision.room.room, room.signed_room)

            fixture.free(32 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            fixture.allow_next_start()
            _ = fixture.run_pass()
            local, = fixture.server.handle_request(later)
            assert isinstance(local, wire.LocalPlacementGranted)
            self.assertEqual(fixture.server.slots[slot_key], claim)
            _ = fixture.run_pass()
            self.assertEqual(fixture.server.slots[slot_key], claim)
            fixture.release(local.grant)

            _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
            refused, = fixture.server.handle_request(wire.ClaimFailure("mac-busy", assignment, "busy"))
            assert isinstance(refused, wire.DispatchStatus)
            self.assertEqual(refused.state, placement.ClosedAssignment("busy"))
            self.assertEqual(fixture.server.slots[slot_key], claim)
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.run_pass()
            self.assertIsInstance(fixture.server.handle_request(mac)[0], wire.LocalPlacementGranted)

    def test_tail_drag_keeps_held_ci_at_the_head_of_the_saved_snapshot(self) -> None:
        with AdmissionFixture() as fixture:
            running = fixture.request()
            grant, = fixture.server.handle_request(running)
            assert isinstance(grant, wire.Admitted)
            fixture.free(0)
            fixture.hold_starts()
            ci = fixture.service_request()
            holder = build_hold.Holder("fixture", datetime.fromtimestamp(fixture.clock.now(), UTC),
                                       "fixture hold", build_hold.UnknownReleaseEta(), "fixture")
            ordinary = [fixture.request(since=fixture.clock.now() + index) for index in (1, 2, 3)]
            with patch.object(build_targets, "hold_waited_on", return_value=holder):
                for request in (ci, *ordinary):
                    self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
                line = move_line(fixture)
                ci_row, first, second, third = line.members
                moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                    "move-tail", third.row_id, line.revision, wire.QueueDrop(second.row_id, "before")))
                assert isinstance(moved, wire.QueueMoved)
                self.assertEqual(moved.line.members, (ci_row, first, third, second))
                _ = fixture.run_pass()
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                expected = (ci, ordinary[0], ordinary[2], ordinary[1])
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                                 [request.owner.pid for request in expected])
                for position, request in enumerate(expected, 1):
                    wait = fixture.server.waiters[request.episode].last_reply
                    assert isinstance(wait, wire.Wait)
                    self.assertEqual(wait.position, position)
                    if position > 2:
                        self.assertEqual(wait.text, "behind #2")
                snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
                assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
                self.assertEqual([row["pid"] for _, row in snapshot.inputs.ordered],
                                 [request.owner.pid for request in expected])
                stall = head_stall.find_stall(snapshot, snapshot.written_at, snapshot.written_at + 60, 30)
                assert isinstance(stall, head_stall.HeadStall)
                self.assertEqual(stall.pid, ci.owner.pid)

    def test_unpinned_readers_keep_main_order_for_remote_local_and_held_ci_waits(self) -> None:
        with PlacementFixture() as fixture:
            run = fixture.run_owner()
            running = fixture.request(run=run, need=4 * GIB)
            grant, = fixture.server.handle_request(running)
            assert isinstance(grant, wire.Admitted)
            fixture.free(0)
            fixture.hold_starts()
            ci = fixture.service_request()
            holder = build_hold.Holder("fixture", datetime.fromtimestamp(fixture.clock.now(), UTC),
                                       "fixture hold", build_hold.UnknownReleaseEta(), "fixture")
            fixture.server.priority = records.QueuePriority(({"unit": ci.runner, "why": "fixture",
                "set_at": fixture.clock.now(), "set_by": "fixture"},))
            remote = placement_request(fixture, since=running.since - 10)
            remote = replace(remote, local=wire.LocalAdmissionUnavailable("remote only"),
                             constraints=replace(remote.constraints, selection=placement.MacOnlyHost()))
            _ = publish_candidate(fixture, remote, placement.TemporaryExclusion("busy"))
            first, second = [fixture.request(run=run, since=running.since + index,
                             owner=fixture.fixture_owner(700000 + index, "4000")) for index in (1, 2)]
            with patch.object(build_targets, "hold_waited_on", return_value=holder):
                for request in (second, ci, first, remote):
                    _ = fixture.server.handle_request(request)
                result = fixture.run_pass()
                full = [request.owner.pid for request in (ci, remote, first, second)]
                self.assertEqual(fixture.queue_pids(), full)
                self.assertEqual([entry.waiter["pid"] for entry in result.policy.walk], full[2:])
                snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
                assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
                self.assertEqual([row["pid"] for _, row in snapshot.inputs.ordered], [full[0], *full[2:]])
                stall = head_stall.find_stall(snapshot, snapshot.written_at, snapshot.written_at + 60, 30)
                assert isinstance(stall, head_stall.HeadStall)
                self.assertEqual(stall.pid, ci.owner.pid)
                line = move_line(fixture)
                undo, = fixture.server.handle_request(wire.QueueMoveRequest(
                    "undo-missing", line.members[-1].row_id, line.revision, wire.QueueUndo("missing")))
                self.assertEqual(undo, wire.QueueMoveConflict(line, "Undo not available"))
                self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                    "cancel-remote", remote.episode, remote.owner, "fixture")), [wire.Ok()])
                _ = fixture.run_pass()
                self.assertEqual(fixture.queue_pids(), [full[0], *full[2:]])
                fixture.release(grant)
                claim = fixture.server.slots[grant.reservation]
                assert isinstance(claim, records.ContinuationSlot)
                self.assertEqual(claim.owner, records.waiter_owner(fixture.server.waiters[first.episode].row))
                self.assertEqual(fixture.server.queue_pins, ())

    def test_absent_continuation_keeps_its_run_order_after_restart(self) -> None:
        with PlacementFixture() as fixture:
            now = fixture.clock.now()
            run = fixture.run_owner()
            _ = fixture.finish_step(fixture.request(run=run, since=now - 30, need=GIB))
            fixture.free(0)
            fixture.hold_starts()
            continuation = fixture.request(run=run, since=now - 5)
            pinned = fixture.request(since=now - 4)
            departing = placement_request(fixture, since=now - 10)
            departing = replace(departing, constraints=replace(departing.constraints,
                candidates=(*departing.constraints.candidates, local_candidate(fixture))))
            _ = publish_candidate(fixture, departing, placement.TemporaryExclusion("busy"))
            for request in (departing, continuation, pinned):
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            self.assertEqual(fixture.queue_pids(), [continuation.owner.pid, departing.owner.pid, pinned.owner.pid])
            continuing_id, departing_id, pinned_id = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "pin-before-departure", pinned_id, line.revision, wire.QueueDrop(departing_id, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(ids(moved.line), [continuing_id, pinned_id, departing_id])

            fixture.restart()
            _ = publish_candidate(fixture, departing, placement.TemporaryExclusion("busy"))
            self.assertIsInstance(fixture.server.handle_request(departing)[0], wire.PlacementWait)
            self.assertEqual(ids(move_line(fixture)), [departing_id])
            self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                "cancel", departing.episode, departing.owner, "fixture")), [wire.Ok()])
            self.assertEqual(fixture.server.queue_pins, ((pinned_id, 1),))
            for request in (pinned, continuation):
                _ = fixture.server.handle_request(request)
            self.assertEqual(ids(move_line(fixture)), [continuing_id, pinned_id])

    def test_undo_after_a_departure_keeps_the_other_pin_at_its_current_place(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            leaving = placement_request(fixture, since=fixture.clock.now() - 10)
            leaving = replace(leaving, constraints=replace(leaving.constraints,
                candidates=(*leaving.constraints.candidates, local_candidate(fixture))))
            _ = publish_candidate(fixture, leaving, placement.TemporaryExclusion("busy"))
            self.assertIsInstance(fixture.server.handle_request(leaving)[0], wire.PlacementWait)
            for index in range(6):
                request = fixture.request(since=fixture.clock.now() + index)
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            departing, first, third, pinned, fourth, fifth, last = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "pin-third", pinned, line.revision, wire.QueueDrop(third, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(ids(moved.line), [departing, first, pinned, third, fourth, fifth, last])
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "drag-last", last, moved.line.revision, wire.QueueDrop(fifth, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                "cancel-first", leaving.episode, leaving.owner, "fixture")), [wire.Ok()])
            _ = fixture.run_pass()
            current = move_line(fixture)
            self.assertEqual(ids(current), [first, pinned, third, fourth, last, fifth])
            self.assertEqual(fixture.server.queue_pins, ((pinned, 1), (last, 4)))
            undone, = fixture.server.handle_request(wire.QueueMoveRequest(
                "undo-last", last, current.revision, wire.QueueUndo("drag-last")))
            assert isinstance(undone, wire.QueueMoved)
            self.assertEqual(ids(undone.line), [first, pinned, third, fourth, fifth, last])
            self.assertEqual(fixture.server.queue_pins, ((pinned, 1), (last, 5)))

    def test_tail_drag_keeps_continuations_in_step_arrival_order(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            running = fixture.request(run=run, need=4 * GIB)
            fixture.allow_next_start()
            grant, = fixture.server.handle_request(running)
            assert isinstance(grant, wire.Admitted)
            fixture.free(0)
            fixture.hold_starts()
            earlier = fixture.request(run=run, since=running.since + 1, owner=fixture.fixture_owner(700002, "4000"))
            later = fixture.request(run=run, since=running.since + 2, owner=fixture.fixture_owner(700001, "4000"))
            tail = [fixture.request(since=running.since + index) for index in (3, 4)]
            for request in (earlier, later, *tail):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            line = move_line(fixture)
            first, second, third, fourth = ids(line)
            self.assertEqual(fixture.queue_pids()[:2], [earlier.owner.pid, later.owner.pid])
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "drag-tail", fourth, line.revision, wire.QueueDrop(third, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(ids(moved.line), [first, second, fourth, third])
            _ = fixture.run_pass()
            fixture.release(grant)
            claim = fixture.server.slots[grant.reservation]
            assert isinstance(claim, records.ContinuationSlot)
            self.assertEqual(claim.owner, records.run_owner(earlier.owner.pid, earlier.owner.start_time))

    def test_move_places_a_repository_limit_hold_before_a_memory_hold(self) -> None:
        with AdmissionFixture() as fixture:
            running = fixture.request(step="gate", need=2 * GIB)
            self.assertIsInstance(fixture.server.handle_request(running)[0], wire.Admitted)
            fixture.free(0)
            fixture.hold_starts()
            oversized = fixture.request(need=70 * GIB, since=running.since + 1)
            capped = fixture.request(step="gate", since=running.since + 2)
            follower = fixture.request(step="gate", since=running.since + 3)
            for request in (capped, oversized, follower):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            line = move_line(fixture)
            oversized_row, capped_row, follower_row = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move-cap-first", capped_row, line.revision, wire.QueueDrop(oversized_row, "before")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(ids(moved.line), [capped_row, oversized_row, follower_row])
            _ = fixture.run_pass()
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered],
                             [request.owner.pid for request in (capped, oversized, follower)])
            inputs = replace(inputs, host=records.HostMemory(60 * GIB, 60 * GIB),
                             spacing=records.ReadyToStart())
            policy = replace(admission_policy_fixture(),
                repository_runs=records.RepositoryRunLimits({f"{inputs.local_machine}.fixture": 1}))
            capped_wait, memory_wait, follower_wait = gate.admission_pass(inputs, inputs.now, policy).rows
            self.assertFalse(memory_wait.decision.room.passes)
            self.assertEqual(capped_wait.decision.run_limit,
                             records.RunLimitWait(1, 1, "fixture", inputs.local_machine))
            hold = follower_wait.decision.order_hold
            assert isinstance(hold, records.HeldBehindHead)
            self.assertEqual(hold.waiter["pid"], capped.owner.pid)

    def test_second_move_sets_all_pins_to_the_confirmed_places(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() + index) for index in range(5)]
            for request in requests:
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            first, second, third, fourth, pinned = ids(line)
            move, = fixture.server.handle_request(wire.QueueMoveRequest(
                "pin-fourth", pinned, line.revision, wire.QueueDrop(fourth, "before")))
            assert isinstance(move, wire.QueueMoved)
            self.assertEqual(ids(move.line), [first, second, third, pinned, fourth])

            move, = fixture.server.handle_request(wire.QueueMoveRequest(
                "pin-after", first, move.line.revision, wire.QueueDrop(pinned, "after")))
            assert isinstance(move, wire.QueueMoved)
            self.assertEqual(ids(move.line), [second, third, pinned, first, fourth])
            self.assertEqual(fixture.server.queue_pins, ((pinned, 2), (first, 3)))
            _ = fixture.run_pass()
            self.assertEqual(move_line(fixture), move.line)

            undo, = fixture.server.handle_request(wire.QueueMoveRequest(
                "undo-second", first, move.line.revision, wire.QueueUndo("pin-after")))
            assert isinstance(undo, wire.QueueMoved)
            self.assertEqual(ids(undo.line), [first, second, third, pinned, fourth])
            self.assertEqual(fixture.server.queue_pins, ((first, 0), (pinned, 3)))

    def test_departure_behind_an_absent_pin_keeps_its_place_after_restart(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            pinned = fixture.request(since=fixture.clock.now() - 20)
            first = fixture.request(since=fixture.clock.now() - 10)
            last = placement_request(fixture)
            last = replace(last, constraints=replace(last.constraints,
                candidates=(*last.constraints.candidates, local_candidate(fixture))))
            _ = publish_candidate(fixture, last, placement.TemporaryExclusion("busy"))
            for request in (pinned, first, last):
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            pinned_id, first_id, last_id = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "pin-second", pinned_id, line.revision, wire.QueueDrop(first_id, "after")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(ids(moved.line), [first_id, pinned_id, last_id])

            fixture.restart()
            _ = publish_candidate(fixture, last, placement.TemporaryExclusion("busy"))
            self.assertIsInstance(fixture.server.handle_request(last)[0], wire.PlacementWait)
            self.assertEqual(ids(move_line(fixture)), [last_id])
            self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                "cancel-last", last.episode, last.owner, "fixture")), [wire.Ok()])
            self.assertEqual(fixture.server.queue_pins, ((pinned_id, 1),))

            _ = fixture.server.handle_request(pinned)
            _ = fixture.server.handle_request(first)
            self.assertEqual(ids(move_line(fixture)), [first_id, pinned_id])

    def test_new_wait_joins_its_bumped_group_before_an_ordinary_wait(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first, dragged, ordinary = [fixture.request(since=fixture.clock.now() + index) for index in range(3)]
            first, dragged = [replace(request, identity=replace(request.identity, unit="urgent"))
                              for request in (first, dragged)]
            fixture.server.priority = records.QueuePriority(({"unit": first.identity.unit, "why": "fixture",
                "set_at": fixture.clock.now(), "set_by": "fixture"},))
            for request in (first, dragged, ordinary):
                _ = fixture.server.handle_request(request)
            line = move_line(fixture)
            first_id, dragged_id, ordinary_id = ids(line)
            moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                "drop-below-group", dragged_id, line.revision, wire.QueueDrop(ordinary_id, "after")))
            assert isinstance(moved, wire.QueueMoved)
            self.assertEqual(ids(moved.line), [first_id, ordinary_id, dragged_id])

            newcomer = fixture.request(since=fixture.clock.now() + 3)
            newcomer = replace(newcomer, identity=replace(newcomer.identity, unit=first.identity.unit))
            _ = fixture.server.handle_request(newcomer)
            self.assertEqual(fixture.server.queue_pins, ((dragged_id, 2),))
            self.assertEqual(fixture.queue_pids(), [request.owner.pid for request in (first, newcomer, dragged, ordinary)])

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
            self.assertEqual(fixture.server.queue_pins, ((remote_id, 0),))
            self.assertEqual(fixture.server.handle_request(wire.CancelPlacement("cancel", remote.episode,
                remote.owner, "fixture")), [wire.Ok()])
            place = next(place for place in fixture.server.places.values() if place.episode == remote.episode)
            self.assertEqual(place.move_id, "")
            self.assertEqual(fixture.server.queue_pins, ())
            newcomer = fixture.request(since=fixture.clock.now() + 3)
            _ = fixture.server.handle_request(newcomer)
            self.assertEqual(ids(move_line(fixture))[:2], [before, after])

    def test_pin_moves_up_when_a_wait_ahead_starts_or_ends(self) -> None:
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
            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            fixture.allow_next_start()
            _ = fixture.run_pass()
            self.assertIn(requests[1].episode, fixture.server.reservations)
            self.assertEqual(ids(move_line(fixture)), [last, first])
            self.assertEqual(fixture.server.queue_pins, ((first, 1),))
            fixture.free(0)

            newcomer = fixture.request(since=fixture.clock.now() + 3)
            _ = fixture.server.handle_request(newcomer)
            self.assertEqual(ids(move_line(fixture))[:2], [last, first])
            fixture.end_owner(requests[2].owner)
            _ = fixture.run_pass()
            self.assertEqual(ids(move_line(fixture))[0], first)
            self.assertEqual(fixture.server.queue_pins, ((first, 0),))

    def test_pin_survives_restart_with_a_newcomer_before_reconnect(self) -> None:
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
            new_id, clamped = ids(move_line(fixture))
            self.assertEqual(clamped, first)
            self.assertEqual(fixture.server.queue_pins, ((first, 2),))
            for request in reversed(requests[1:]):
                _ = fixture.server.handle_request(request)
            self.assertEqual(ids(move_line(fixture)), [new_id, second, first, third])

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
            self.assertEqual(fixture.server.queue_pins, ((third, 4),))

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
            self.assertEqual(fixture.server.queue_pins, ((third, 0),))
            fixture.restart()
            self.assertEqual(fixture.server.queue_pins, ((third, 0),))
            newcomer = fixture.request(since=fixture.clock.now() - 30)
            _ = fixture.server.handle_request(newcomer)
            new_id, = ids(move_line(fixture))
            for waiting in reversed(requests):
                _ = fixture.server.handle_request(waiting)
            current = move_line(fixture)
            self.assertEqual(ids(current), [third, merge, urgent, second, new_id, first])
            self.assertEqual(fixture.server.handle_request(request), [moved])
            self.assertIsInstance(fixture.server.handle_request(wire.QueueMoveRequest(
                "stale", first, line.revision, wire.QueueDrop(third, "before")))[0], wire.QueueMoveConflict)
            self.assertEqual(fixture.server.handle_request(
                wire.PriorityClearRequest("clear-later", "fixture", unit="later-unit")), [wire.Ok()])
            after_clear = [third, merge, urgent, new_id, first, second]
            self.assertEqual(ids(move_line(fixture)), after_clear)
            self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                "bump-dragged-unit", "ordinary", "fixture", "fixture")), [wire.Ok()])
            current = move_line(fixture)
            self.assertEqual(ids(current), [third, merge, urgent, first, new_id, second])
            undone, = fixture.server.handle_request(wire.QueueMoveRequest(
                "undo-after-bump", third, current.revision, wire.QueueUndo("move")))
            assert isinstance(undone, wire.QueueMoved)
            self.assertEqual(ids(undone.line), [merge, urgent, first, new_id, second, third])
            self.assertEqual(fixture.server.queue_pins, ((third, 5),))
            restored, = fixture.server.handle_request(wire.QueueMoveRequest(
                "move-after-undo", third, undone.line.revision, wire.QueueDrop(merge, "before")))
            assert isinstance(restored, wire.QueueMoved)
            self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest("clear", "fixture")), [wire.Ok()])
            self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
            self.assertEqual(ids(move_line(fixture)), [third, new_id, merge, urgent, first, second])
            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            fixture.allow_next_start()
            _ = fixture.run_pass()
            self.assertIn(requests[4].episode, fixture.server.reservations)
            self.assertEqual(fixture.server.queue_pins, ())
            remaining = ids(move_line(fixture))
            self.assertNotIn(third, remaining)
            self.assertLess(remaining.index(merge), remaining.index(first))
            fixture.free(0)
            replacement = fixture.request(since=requests[-1].since)
            _ = fixture.server.handle_request(replacement)
            replacement_line = move_line(fixture)
            self.assertIn(merge, ids(replacement_line))
            self.assertNotIn(ids(replacement_line)[-1], ids(current))

    def test_reads_undo_destinations_from_earlier_replies(self) -> None:
        line = records.QueueMoveLine("revision", ())
        for before, after, destination in (("first", "last", wire.QueueDrop("last", "before")),
                                           ("first", "", wire.QueueDrop("first", "after"))):
            with self.subTest(destination=destination):
                moved = wire.QueueMoved("moving", line, 2, 1, destination)
                record = cast(dict[str, object], json.loads(wire.encode_record(moved)))
                del record["undo_destination"]
                record.update(old_before=before, old_after=after)
                self.assertEqual(wire.parse_record(json.dumps(record), wire.QueueMoved), moved)

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
            pinned_again, = fixture.server.handle_request(wire.QueueMoveRequest(
                "pin-first", second, moved.revision, wire.QueueDrop(first, "before")))
            assert isinstance(pinned_again, wire.QueueMoved)
            undone_pin, = fixture.server.handle_request(wire.QueueMoveRequest(
                "restore-pin", second, pinned_again.line.revision, wire.QueueUndo("pin-first")))
            assert isinstance(undone_pin, wire.QueueMoved)
            self.assertEqual(ids(undone_pin.line), ids(moved))
            self.assertEqual(fixture.server.queue_pins, ((second, 3),))
            moved = undone_pin.line
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
            self.assertEqual(fixture.server.queue_pins, ((first, 0), (second, 2)))
            for waiting in (requests[0], requests[1], requests[3]):
                _ = fixture.server.handle_request(waiting)
            current = move_line(fixture)
            reply, = fixture.server.handle_request(wire.QueueMoveRequest(
                "missing-neighbor", second, current.revision, wire.QueueUndo("move-again")))
            self.assertEqual(reply, wire.QueueMoveConflict(current, "The old neighbor left the queue."))

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
            self.assertEqual(fixture.server.queue_pins, ())

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
            self.assertEqual(fixture.server.queue_pins, ((third_id, 0), (mac_id, 1)))
            _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
            refused, = fixture.server.handle_request(wire.ClaimFailure("mac-busy", assignment, "busy"))
            assert isinstance(refused, wire.DispatchStatus)
            self.assertEqual(refused.state, placement.ClosedAssignment("busy"))
            _ = fixture.run_pass()
            self.assertEqual(ids(move_line(fixture)), [third_id, mac_id, first_id])
            self.assertEqual(fixture.queue_pids(), [third.owner.pid, mac.owner.pid, first.owner.pid])

    def test_bump_while_a_mac_assignment_is_absent(self) -> None:
        for pin_mac in (False, True):
            with self.subTest(pin_mac=pin_mac), PlacementFixture() as fixture:
                hold_seconds = 3
                fixture.configure(remote_start_hold_seconds=hold_seconds)
                fixture.free(0)
                fixture.hold_starts()
                bumped = fixture.request(since=fixture.clock.now() - 10 if pin_mac else fixture.clock.now() + 5)
                bumped = replace(bumped, identity=replace(bumped.identity, unit="urgent"))
                mac = placement_request(fixture)
                mac = replace(mac, constraints=replace(mac.constraints,
                    candidates=(*mac.constraints.candidates, local_candidate(fixture))))
                _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
                dragged = fixture.request(since=fixture.clock.now() + 10)
                for request in (bumped, mac, dragged):
                    _ = fixture.server.handle_request(request)
                line = move_line(fixture)
                if pin_mac:
                    bumped_id, mac_id, dragged_id = ids(line)
                    moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                        "mac-first", mac_id, line.revision, wire.QueueDrop(bumped_id, "before")))
                    assert isinstance(moved, wire.QueueMoved)
                    line = moved.line
                else:
                    mac_id, bumped_id, dragged_id = ids(line)
                moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                    "dragged-second", dragged_id, line.revision, wire.QueueDrop(bumped_id, "before")))
                assert isinstance(moved, wire.QueueMoved)
                self.assertEqual(ids(moved.line), [mac_id, dragged_id, bumped_id])

                _ = publish_candidate(fixture, mac)
                assignment = assignment_for(fixture, mac)
                fixture.clock.advance(hold_seconds + 1)
                _ = fixture.run_pass()
                self.assertNotIn(mac.episode, fixture.server.waiters)
                self.assertEqual(ids(move_line(fixture)), [bumped_id, dragged_id])
                self.assertEqual(fixture.completed_queue_change(
                    wire.PrioritySetRequest("bump-third", "urgent", "fixture", "fixture")), [wire.Ok()])
                pins = ((mac_id, 0), (dragged_id, 1)) if pin_mac else ((dragged_id, 1),)
                self.assertEqual(fixture.server.queue_pins, pins)
                _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
                refused, = fixture.server.handle_request(wire.ClaimFailure("mac-busy", assignment, "busy"))
                assert isinstance(refused, wire.DispatchStatus)
                self.assertEqual(refused.state, placement.ClosedAssignment("busy"))
                _ = fixture.run_pass()
                expected = [mac_id, dragged_id, bumped_id] if pin_mac else [bumped_id, dragged_id, mac_id]
                self.assertEqual(ids(move_line(fixture)), expected)

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

    def test_mac_startup_keeps_its_hold_without_a_move_across_it(self) -> None:
        for unrelated_move in (False, True):
            with self.subTest(unrelated_move=unrelated_move), PlacementFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                mac = placement_request(fixture, since=fixture.clock.now() - 10)
                local = placement_request(fixture, since=fixture.clock.now() - 9)
                mac = replace(mac, prerequisite=placement.TokenRequired(), constraints=replace(mac.constraints,
                    candidates=(local_candidate(fixture), *mac.constraints.candidates)))
                local = replace(local, prerequisite=placement.TokenNotRequired(), constraints=replace(local.constraints,
                    candidates=(local_candidate(fixture), *local.constraints.candidates)))
                _ = publish_candidate(fixture, mac, placement.TemporaryExclusion("busy"))
                for request in (mac, local):
                    self.assertIsInstance(fixture.server.handle_request(request)[0], wire.PlacementWait)
                if unrelated_move:
                    for index in range(2):
                        _ = fixture.server.handle_request(fixture.request(since=fixture.clock.now() + index))
                    line = move_line(fixture)
                    self.assertEqual(len(line.members), 4)
                    third, fourth = (member.row_id for member in line.members[-2:])
                    moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                        "move-tail", fourth, line.revision, wire.QueueDrop(third, "before")))
                    assert isinstance(moved, wire.QueueMoved)
                fixture.free(12 * GIB)
                fixture.clock.advance(1)
                mac_probe = replace(mac, constraints=replace(mac.constraints, candidates=mac.constraints.candidates[1:]))
                _ = publish_candidate(fixture, mac_probe)
                _ = fixture.run_pass()
                self.assertFalse(fixture.server.reservations)
                self.assertEqual(set(fixture.server.current_assignments), {mac.episode})
                remote = fixture.server.assignments[fixture.server.current_assignments[mac.episode]]
                self.assertEqual(remote.assignment.destination.provider, "mac")
                self.assertIsInstance(remote.remote_start_hold, records.RemoteStartHold)

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

    def test_move_preserves_the_landing_test_hold(self) -> None:
        with (AdmissionFixture() as fixture,
              patch.dict(os.environ, {"GATE_STATE_DIR": str(fixture.root / "gate-state")})):
            commit = "a" * 40
            directory = build_hold.holder_directory()
            self.assertEqual(directory, fixture.root / "holders")
            holder = build_hold.HolderIdentity("landing", "landing")
            reason = records.NatedevHeld("landing", "the landing queue")
            first, second = [replace(fixture.service_request(), argv=("cargo", "nextest", "run"))
                             for _ in range(2)]
            with land_runs.landing([commit]):
                self.assertIn(commit, land_runs.running_lands())
                _ = build_hold.write_hold(directory, holder, reason.purpose, build_hold.NoReleaseEta(),
                                          datetime.fromtimestamp(fixture.clock.now(), UTC))
                for request in (first, second):
                    reply, = fixture.server.handle_request(request)
                    assert isinstance(reply, wire.Wait)
                    self.assertEqual(reply.reason, reason)
                line = move_line(fixture)
                first_id, second_id = ids(line)
                moved, = fixture.server.handle_request(wire.QueueMoveRequest(
                    "move-held-test", second_id, line.revision, wire.QueueDrop(first_id, "before")))
                assert isinstance(moved, wire.QueueMoved)
                _ = fixture.run_pass()
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([(step.pid, step.reason) for step in view.reading.view.waiting],
                                 [(second.owner.pid, reason), (first.owner.pid, reason)])
                self.assertFalse(fixture.server.reservations)
            build_hold.holder_path(directory, holder.key).unlink()
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {second.episode})
