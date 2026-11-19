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

from scripts.build_queue import admission_protocol as wire, head_stall, step_estimator
from scripts.build_queue.admission_records import GIB
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.queue_fixture import write_process
from scripts.lint import memory_admit as gate
from scripts.production import staff


class LandingMergeOrderTests(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: AdmissionFixture
        self.config: Path
        self.merge_state: Path

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
            staff.StaffRow(staff.StaffId("merge"), "producer", "natemerge", staff.StaffRoot(),
                           current_session=staff.StaffId("merge-session")),
            staff.StaffRow(staff.StaffId("gate"), "worker", "gate", staff.ReportsTo(staff.StaffId("merge")),
                           current_session=staff.StaffId("gate-session")),
            staff.StaffRow(staff.StaffId("trial"), "producer", "trial", staff.StaffRoot(),
                           current_session=staff.StaffId("trial-session")),
        ))
        _ = self.enterContext(patch.object(staff, "local_snapshot", return_value=members))
        _ = self.fixture.occupy_host()
        self.fixture.free(0)

    def land_record(self, pid: int, *, start: str = "10") -> None:
        directory = self.merge_state / "lands"
        directory.mkdir(parents=True, exist_ok=True)
        write_process(self.fixture.proc.path, pid, start="10")
        _ = (directory / f"{pid}.json").write_text(json.dumps({"pid": pid, "start": start, "commits": ["abc"]}))

    def submit(self, pid: int, *, step: str = "nextest", parent: int = 1,
               session: str = "gate-session") -> wire.AdmitRequest:
        owner = self.fixture.fixture_owner(pid, str(pid))
        write_process(self.fixture.proc.path, pid, parent=parent)
        request = self.fixture.request(owner=owner, step=step)
        request = replace(request, identity=replace(request.identity, session_id=session, unit="fixture-unit"))
        replies = self.fixture.server.handle_request(request)
        self.assertIsInstance(replies[0], wire.Wait)
        return request

    def test_landing_gate_passes_the_unit_step(self) -> None:
        self.land_record(10)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10)
        self.assertEqual(self.fixture.queue_pids(), [101, 100])
        self.fixture.free(60 * GIB)
        _ = self.fixture.run_pass()
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
        self.assertEqual(self.fixture.queue_pids(), [102, 100, 101])

    def test_land_record_requires_the_process_start_time(self) -> None:
        self.land_record(10, start="9")
        self.land_record(11)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10)
        _ = self.submit(102, step="merge-gate", parent=11)
        self.assertEqual(self.fixture.queue_pids(), [102, 100, 101])

    def test_landing_gate_requires_the_natemerge_staff_chain(self) -> None:
        self.land_record(10)
        self.land_record(11)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10, session="trial-session")
        _ = self.submit(102, step="merge-gate", parent=11)
        self.assertEqual(self.fixture.queue_pids(), [102, 100, 101])

    def test_one_request_per_land_record_gets_first_place(self) -> None:
        self.land_record(10)
        unit = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10)
        self.assertEqual(self.fixture.queue_pids(), [101, 100])
        self.fixture.end_fixture_owner(101)
        self.fixture.restart()
        self.assertIsInstance(self.fixture.server.handle_request(unit)[0], wire.Wait)
        _ = self.submit(102, step="merge-gate", parent=10)
        self.assertEqual(self.fixture.queue_pids(), [100, 102])

    def test_pass_limit_protects_the_waiter_after_two_different_lands(self) -> None:
        _ = self.submit(100)
        for land, gate_pid in ((10, 101), (11, 102)):
            self.land_record(land)
            _ = self.submit(gate_pid, step="merge-gate", parent=land)
            self.assertEqual(self.fixture.queue_pids(), [gate_pid, 100])
            self.fixture.end_fixture_owner(gate_pid)
        self.land_record(12)
        _ = self.submit(103, step="merge-gate", parent=12)
        self.assertEqual(self.fixture.queue_pids(), [100, 103])

    def test_live_switch_returns_to_the_normal_order(self) -> None:
        self.land_record(10)
        _ = self.submit(100)
        _ = self.submit(101, step="merge-gate", parent=10)
        self.assertEqual(self.fixture.queue_pids(), [101, 100])
        _ = self.config.write_text(self.config.read_text().replace("landing_merge_first=on", "landing_merge_first=off"))
        _ = self.fixture.run_pass()
        self.assertEqual(self.fixture.queue_pids(), [100, 101])

    def test_pass_restarts_the_quiet_clock(self) -> None:
        _ = self.submit(100)
        self.fixture.clock.advance(3600)
        _ = self.fixture.run_pass()
        paths = head_stall.StallPaths(self.fixture.state_dir, self.fixture.root / "head-stall.json")
        now = self.fixture.clock.now()
        self.assertIsInstance(head_stall.current_stall(paths, now, 1800, 120), head_stall.HeadStall)
        self.land_record(10)
        _ = self.submit(101, step="merge-gate", parent=10)
        self.fixture.end_fixture_owner(101)
        _ = self.fixture.run_pass()
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
        ])
