"""Check landing priority, the stall clock, and the wait report with private state."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.build_queue import admission_protocol as wire, admission_service as service, head_stall, step_estimator
from scripts.build_queue.admission_records import GIB
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.queue_fixture import write_process
from scripts.lint import memory_admit as gate
from scripts.production import staff


MERGE_STAFF_ID = staff.StaffId("506b6872-03cb-4a1a-9465-fbd9cf88975e")


class LandingMergeOrderTests(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: AdmissionFixture
        self.config: Path
        self.merge_state: Path
        self.host_grant: wire.Admitted

    @override
    def setUp(self) -> None:
        self.fixture = self.enterContext(AdmissionFixture())
        self.config = self.fixture.root / "build_admission.conf"
        _ = self.config.write_text(self.config.read_text() + "\n[landing]\n"
            + "# one of: on, off\nlanding_merge_first=on\n# whole: 0..\nlanding_merge_max_passes=2\n")
        self.merge_state = self.fixture.root / "merge-state"
        self.enterContext(patch.dict(os.environ, {
            "MERGE_GATE_STATE_DIR": str(self.merge_state), "VERIFY_PROC_ROOT": str(self.fixture.proc.path),
        }))
        members = staff.StaffSnapshot((
            staff.StaffRow(MERGE_STAFF_ID, "producer", "natemerge", staff.StaffRoot(),
                           current_session=staff.StaffId("merge-session")),
            staff.StaffRow(staff.StaffId("gate"), "worker", "gate", staff.ReportsTo(MERGE_STAFF_ID),
                           current_session=staff.StaffId("gate-session")),
            staff.StaffRow(staff.StaffId("trial"), "producer", "natemerge", staff.StaffRoot(),
                           current_session=staff.StaffId("trial-session")),
        ))
        _ = self.enterContext(patch.object(staff, "local_snapshot", return_value=members))
        self.host_grant = self.fixture.occupy_host()
        self.fixture.free(0)

    def land_record(self, pid: int, *, start: str = "10") -> None:
        directory = self.merge_state / "lands"
        directory.mkdir(parents=True, exist_ok=True)
        write_process(self.fixture.proc.path, pid, start="10")
        _ = (directory / f"{pid}.json").write_text(json.dumps({"pid": pid, "start": start, "commits": ["abc"]}))

    def request(self, pid: int, *, step: str = "nextest", parent: int = 1,
                session: str = "gate-session", need: int = 8 * GIB) -> wire.AdmitRequest:
        owner = self.fixture.fixture_owner(pid, str(pid))
        write_process(self.fixture.proc.path, pid, start=str(pid), parent=parent)
        request = self.fixture.request(owner=owner, step=step, need=need)
        return replace(request, identity=replace(request.identity, session_id=session, unit="fixture-unit"))

    def submit(self, pid: int, *, step: str = "nextest", parent: int = 1,
               session: str = "gate-session", need: int = 8 * GIB) -> wire.AdmitRequest:
        request = self.request(pid, step=step, parent=parent, session=session, need=need)
        if step == "merge-gate":
            write_process(self.fixture.proc.path, os.getpid(), start="500", parent=pid)
            replies = self.fixture.connect(request).replies()
        else:
            replies = self.fixture.server.handle_request(request)
        self.assertIsInstance(replies[0], wire.Wait)
        return request

    def test_landing_requires_the_socket_peer_to_descend_from_the_owner(self) -> None:
        self.land_record(10)
        request = self.request(101, step="merge-gate", parent=10)
        write_process(self.fixture.proc.path, os.getpid(), start="500", parent=10)
        replies = self.fixture.connect(request).replies()
        self.assertIsInstance(replies[0], wire.Wait)
        self.assertIsInstance(self.fixture.server.waiters[request.episode].landing, service.OrdinaryTest)

    def test_waiting_gate_loses_priority_when_the_land_record_disappears(self) -> None:
        self.land_record(10)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10)
        (self.merge_state / "lands" / "10.json").unlink()
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertIn("episode-100", self.fixture.caller_reservations())
        self.assertNotIn("episode-101", self.fixture.caller_reservations())

    def test_oversized_landing_spends_no_pass_and_does_not_start_alone(self) -> None:
        self.land_record(10)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10, need=61 * GIB)
        scenery, = self.fixture.server.reservations.values()
        assert isinstance(scenery.run, wire.RunIdentity)
        self.fixture.end_run(scenery.run)
        self.fixture.free(60 * GIB)
        self.fixture.release(self.host_grant)
        _ = self.fixture.run_pass()
        self.assertIn("episode-100", self.fixture.caller_reservations())
        self.assertNotIn("episode-101", self.fixture.caller_reservations())
        self.assertEqual(self.fixture.server.landing_first_places, {})
        self.assertEqual(self.fixture.server.landing_pass_counts, {})
        events = step_estimator.anonymous_history_rows(self.fixture.state_dir / "queue_times.jsonl")
        self.assertFalse(any(event["kind"] == "landing_pass" for event in events))

    def test_reconnect_preserves_the_pass_limit_for_the_same_arrival(self) -> None:
        unit = self.request(100)
        peer = self.fixture.connect(unit)
        self.assertIsInstance(peer.replies()[0], wire.Wait)
        for land, pid in ((10, 101), (11, 102)):
            self.land_record(land)
            _ = self.submit(pid, step="merge-gate", parent=land)
            self.fixture.free(60 * GIB)
            _ = self.fixture.run_pass()
            gate_reservation = self.fixture.caller_reservations()[f"episode-{pid}"]
            self.assertNotIn(unit.episode, self.fixture.caller_reservations())
            self.fixture.free(0)
            _ = self.fixture.server.handle_request(wire.ReleaseRequest(
                f"release-{pid}", gate_reservation.reservation_id, 0))
            self.fixture.allow_next_start()
        peer.close()
        self.fixture.server.run_once(0)
        self.fixture.restart()
        reconnected = self.fixture.connect(replace(unit, episode="reconnected"))
        self.assertIsInstance(reconnected.replies()[0], wire.Wait)
        self.land_record(12)
        _ = self.submit(103, step="merge-gate", parent=12)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertIn("reconnected", self.fixture.caller_reservations())
        self.assertNotIn("episode-103", self.fixture.caller_reservations())
        self.fixture.free(0)
        self.fixture.end_fixture_owner(100)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.server.landing_pass_counts, {})

    def test_report_discloses_older_sessions_are_not_counted(self) -> None:
        self.assertIn("The report excludes older sessions for unmarked landing tests.", gate.landing_wait_report(
            self.fixture.state_dir, self.fixture.clock.now()).splitlines())

    def test_landing_gate_passes_the_unit_step(self) -> None:
        self.land_record(10)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [101, 100])
        grant = self.fixture.server.reservations["episode-101"]
        self.assertNotIn("episode-100", self.fixture.server.reservations)
        _ = self.fixture.server.handle_request(wire.ReleaseRequest("gate-finished", grant.reservation_id, 0))
        events = step_estimator.anonymous_history_rows(self.fixture.state_dir / "queue_times.jsonl")
        self.assertEqual([(event["kind"], event["landing_merge"]) for event in events if event.get("pid") == 101],
                         [("admitted", True), ("ended", True)])

    def test_trial_requires_a_land_ancestor(self) -> None:
        self.land_record(10)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate")
        _ = self.submit(102, step="merge-gate", parent=10)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [102, 100, 101])

    def test_land_record_requires_the_process_start_time(self) -> None:
        self.land_record(10, start="9")
        self.land_record(11)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10)
        _ = self.submit(102, step="merge-gate", parent=11)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [102, 100, 101])

    def test_landing_gate_requires_the_natemerge_staff_chain(self) -> None:
        self.land_record(10)
        self.land_record(11)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10, session="trial-session")
        _ = self.submit(102, step="merge-gate", parent=11)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [102, 100, 101])

    def test_one_request_per_land_record_gets_first_place(self) -> None:
        self.land_record(10)
        unit = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [101, 100])
        self.fixture.free(0)
        self.fixture.end_fixture_owner(101)
        self.fixture.restart()
        self.fixture.allow_next_start()
        self.assertIsInstance(self.fixture.server.handle_request(unit)[0], wire.Wait)
        _ = self.submit(102, step="merge-gate", parent=10)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [100, 102])
        self.assertIn(unit.episode, self.fixture.caller_reservations())

    def test_pass_limit_protects_the_waiter_after_two_different_lands(self) -> None:
        _ = self.submit(100)
        for land, gate_pid in ((10, 101), (11, 102)):
            self.land_record(land)
            _ = self.submit(gate_pid, step="merge-gate", parent=land)
            self.fixture.free(60 * GIB)
            _ = self.fixture.run_pass()
            self.assertEqual(self.fixture.queue_pids(), [gate_pid, 100])
            self.fixture.free(0)
            self.fixture.end_fixture_owner(gate_pid)
            self.fixture.allow_next_start()
        self.land_record(12)
        _ = self.submit(103, step="merge-gate", parent=12)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [100, 103])
        self.assertIn("episode-100", self.fixture.caller_reservations())

    def test_live_switch_returns_to_the_normal_order(self) -> None:
        self.land_record(10)
        _ = self.submit(90)
        _ = self.submit(91, step="merge-gate", parent=10)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [91, 90])
        self.fixture.free(0)
        self.fixture.end_fixture_owner(91)
        self.fixture.end_fixture_owner(90)
        self.fixture.allow_next_start()
        self.land_record(11)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=11)
        _ = self.config.write_text(self.config.read_text().replace("landing_merge_first=on", "landing_merge_first=off"))
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [100, 101])
        self.assertIn("episode-100", self.fixture.caller_reservations())

    def test_pass_restarts_the_quiet_clock(self) -> None:
        _ = self.submit(100)
        self.fixture.clock.advance(3600)
        _ = self.fixture.run_pass()
        paths = head_stall.StallPaths(self.fixture.state_dir, self.fixture.root / "head-stall.json")
        now = self.fixture.clock.now()
        self.assertIsInstance(head_stall.current_stall(paths, now, 1800, 120), head_stall.HeadStall)
        self.land_record(10)
        _ = self.submit(101, step="merge-gate", parent=10)
        self.assertIsInstance(head_stall.current_stall(paths, now, 1800, 120), head_stall.HeadStall)
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
        self.fixture.free(0)
        self.fixture.end_fixture_owner(101)
        _ = self.fixture.run_pass()
        events = step_estimator.anonymous_history_rows(self.fixture.state_dir / "queue_times.jsonl")
        self.assertEqual([(event.get("pid"), event.get("passed_at")) for event in events
                          if event["kind"] == "landing_pass"], [(100, now)])
        self.assertEqual(self.fixture.queue_pids(), [100])
        self.assertEqual(head_stall.current_stall(paths, now, 1800, 120), head_stall.NoStall())
        self.fixture.clock.advance(1800)
        _ = self.fixture.run_pass()
        self.assertIsInstance(head_stall.current_stall(paths, now + 1800, 1800, 120), head_stall.HeadStall)

    def test_wait_report_splits_marked_landing_history_and_unit_steps(self) -> None:
        now = self.fixture.clock.now()
        for pid, step, wait, mark in (
                (100, "merge-gate", 12, {"landing_merge": True}),
                (101, "merge-gate", 16, {"landing_merge": True}),
                (102, "merge-gate", 30, {}),
                (103, "merge-gate", 10, {"landing_merge": False}),
                (104, "nextest", 20, {"landing_merge": False}),
                (105, "nextest", 4000, {"landing_merge": False})):
            gate.append_queue_time(self.fixture.state_dir, {
                "kind": "admitted", "event_id": f"admitted:{pid}", "pid": pid, "start_time": str(pid),
                "step": step, "repo": "fixture", "folder": str(self.fixture.root), "unit": "fixture-unit",
                "session_id": "gate-session", "session_name": "", "since": now - wait, "admitted_at": now,
                "reservation_id": str(pid), "need": GIB, "need_source": "measured",
                "is_head": True, "wait_seconds": wait, **mark,
            }, now)
        output = io.StringIO()
        with (patch("sys.stdout", output),
              patch.object(sys, "argv", ["memory_admit.py", "landing-waits", "--since", str(now - 60)])):
            gate.main()
        self.assertEqual(output.getvalue().splitlines(), [
            "Group\tCount\tMedian (s)\tMax (s)",
            "Landing tests\t2\t14.0\t16.0",
            "Landing tests before (by session)\t1\t30.0\t30.0",
            "Unit steps\t2\t15.0\t20.0",
            "The report excludes older sessions for unmarked landing tests.",
        ])
