"""Exercise the build filesystem floor through admission and placement."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_records as records
from scripts.build_queue import admission_service, head_stall, memory_feed, queue_snapshot, queue_view
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, only_admitted
from scripts.build_queue.queue_fixture import write_memory_config
from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request, publish_candidate
from scripts.buildlog import store
from scripts.lint import memory_admit as gate


class BuildSpaceTests(unittest.TestCase):
    def configure_floor(self, fixture: AdmissionFixture, *, estimate_lifetime: str = "3600") -> Path:
        output = fixture.root / "cargo-output"
        output.mkdir()
        _ = (fixture.root / "build_folders.conf").write_text(f"roots.slot_cargo={output}\n")
        write_memory_config(fixture.root / "build_admission.conf",
                            free_floor_gib="150", estimate_refresh_seconds=estimate_lifetime)
        _ = fixture.enter_context(mock.patch.object(fixture.readings, "build_space",
                                  new=admission_service.SystemReadings().build_space))
        return output

    def test_low_space_holds_new_local_steps_until_the_next_clear_pass(self) -> None:
        with mock.patch.object(store, "host_name", return_value="natedev"), AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", free_floor_gib=None)
            running = fixture.request(need=GIB)
            with mock.patch.object(fixture.readings, "build_space") as read_space:
                grant = only_admitted(fixture.server.handle_request(running))
            read_space.assert_not_called()
            output = self.configure_floor(fixture)
            fixture.allow_next_start()
            free = 125 * GIB

            def read_build_space(path: Path) -> os.statvfs_result:
                self.assertEqual(path, output)
                return os.statvfs_result((4096, 4096, 100000000, 90000000, free // 4096, 0, 0, 0, 0, 255))

            with mock.patch.object(os, "statvfs", side_effect=read_build_space) as read_space:
                head, follower = fixture.request(need=GIB), fixture.request(need=0)
                head_wait, = fixture.server.handle_request(head)
                follower_wait, = fixture.server.handle_request(follower)
                assert isinstance(head_wait, wire.Wait), head_wait
                self.assertIsInstance(follower_wait, wire.Wait)
                self.assertEqual(wire.parse_reply(wire.encode_reply(head_wait)), head_wait)
                self.assertEqual(set(fixture.server.reservations), {running.episode})

                read_space.reset_mock()
                _ = fixture.run_pass()
                read_space.assert_called_once_with(output)
                snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
                assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
                waiting = gate.queue_read_from(snapshot.inputs, snapshot.policy).view.waiting
                self.assertEqual([row.pid for row in waiting], [head.owner.pid, follower.owner.pid])
                reason = "/build has 125 GiB free, below the 150 GiB floor"
                self.assertEqual(head_wait.text, reason)
                self.assertEqual(queue_view.reason_phrase(waiting[0].reason, waiting), reason)
                self.assertEqual(memory_feed.wait_why(waiting[0].reason), "build space")
                self.assertIsInstance(waiting[1].reason, records.BehindHead)

                fixture.release(grant)
                fixture.clock.advance(1800)
                _ = fixture.run_pass()
                snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
                assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
                stall = head_stall.find_stall(snapshot, 0.0, fixture.clock.now(), 1800)
                assert isinstance(stall, head_stall.HeadStall)
                self.assertIn(reason, head_stall.builds_text(stall))

                free = 151 * GIB
                _ = fixture.run_pass()
                self.assertIn(head.episode, fixture.server.reservations)

    def test_failed_space_read_holds_new_steps_and_keeps_running_work(self) -> None:
        with mock.patch.object(store, "host_name", return_value="natedev"), AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", free_floor_gib=None)
            running = fixture.request(need=GIB)
            _ = only_admitted(fixture.server.handle_request(running))
            output = self.configure_floor(fixture)
            fixture.allow_next_start()
            queued = fixture.request(need=GIB)

            with mock.patch.object(os, "statvfs", side_effect=OSError("fixture disk offline")) as read_space:
                reply, = fixture.server.handle_request(queued)
            read_space.assert_called_once_with(output)
            assert isinstance(reply, wire.Wait), reply
            self.assertEqual(reply.text, "cannot read /build free space: fixture disk offline")
            self.assertEqual(wire.parse_reply(wire.encode_reply(reply)), reply)
            self.assertEqual(set(fixture.server.reservations), {running.episode})

    def test_estimate_refreshes_during_the_hold_and_starts_on_the_first_clear_pass(self) -> None:
        with mock.patch.object(store, "host_name", return_value="natedev"), AdmissionFixture() as fixture:
            output = self.configure_floor(fixture, estimate_lifetime="10")
            queued = fixture.request(need=GIB)
            free = 125 * GIB

            def read_space(path: Path) -> os.statvfs_result:
                self.assertEqual(path, output)
                return os.statvfs_result((4096, 4096, 100000000, 90000000, free // 4096, 0, 0, 0, 0, 255))

            with mock.patch.object(os, "statvfs", side_effect=read_space):
                first, = fixture.server.handle_request(queued)
                self.assertIsInstance(first, wire.Wait)
                fixture.clock.advance(11)
                _ = fixture.run_pass()
                expired = fixture.server.waiters[queued.episode].last_reply
                assert isinstance(expired, wire.Wait), expired
                assert isinstance(expired.reason, records.HeadAdmissionStatus), expired.reason
                self.assertEqual(expired.reason.cause, records.EstimateExpired(queued.profile.estimated_at))

                refreshed = replace(queued, profile=replace(queued.profile, estimated_at=fixture.clock.now()))
                reply, = fixture.server.handle_request(refreshed)
                self.assertIsInstance(reply, wire.Wait)
                self.assertEqual(fixture.server.reservations, {})

                free = 151 * GIB
                _ = fixture.run_pass()
                self.assertIn(queued.episode, fixture.server.reservations)

    def test_disk_hold_allows_remote_placement_when_the_local_route_is_first(self) -> None:
        for unreadable in (False, True):
            with (self.subTest(unreadable=unreadable), mock.patch.object(store, "host_name", return_value="natedev"),
                  PlacementFixture() as fixture):
                output = self.configure_floor(fixture)
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(local_candidate(fixture), *request.constraints.candidates)))

                def read_build_space(path: Path) -> os.statvfs_result:
                    self.assertEqual(path, output)
                    if unreadable:
                        raise OSError("fixture disk offline")
                    return os.statvfs_result((4096, 4096, 100000000, 90000000, 125 * GIB // 4096, 0, 0, 0, 0, 255))

                with mock.patch.object(os, "statvfs", side_effect=read_build_space):
                    reply, = fixture.connect(request).replies()
                assert isinstance(reply, wire.Assignment), reply
                self.assertEqual(reply.assignment.destination.provider, "mac")
                self.assertEqual(fixture.server.reservations, {})
