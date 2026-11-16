"""Open waits keep their task, resource, holder, and start time."""
from __future__ import annotations

import unittest

from scripts.direct import phase_waits


class PhaseWaitsTests(unittest.TestCase):
    def test_phase_waits_keep_open_waits_and_the_latest_named_holder(self) -> None:
        events: list[dict[str, object]] = [
            {"event_type": "wait_started", "phase_instance_id": "eight", "pass_instance_id": "repair",
             "wait_id": "worker", "wait_reason": "memory admission", "wait_started_epoch": 1_000,
             "wait_kind": "memory", "owner_session": "director-session",
             "wait_attribution": {"actor": "test worker", "command": "release build"},
             "holder": {"state": "HolderNotNamed", "reason": "producer named no holder"}},
            {"event_type": "wait_started", "phase_instance_id": "ten", "wait_id": "unrelated",
             "wait_reason": "build queue", "wait_started_epoch": 900},
            {"event_type": "wait_started", "phase_instance_id": "eight", "wait_id": "director",
             "wait_reason": "build queue", "wait_started_epoch": 1_050,
             "holder": {"state": "NamedHolder", "resource_kind": "slot", "resource": "build",
                 "holder": {"state": "UnitHolder", "unit": "builder"}}},
        ]
        waits = phase_waits.phase_waits(events, "eight")
        self.assertEqual([(wait.reason, wait.started) for wait in waits],
                         [("memory admission", 1_000), ("build queue", 1_050)])
        self.assertEqual((waits[0].wait_kind, waits[0].pass_instance_id, waits[0].owner_session),
                         ("memory", "repair", "director-session"))
        self.assertEqual(waits[0].wait_attribution, {"actor": "test worker", "command": "release build"})
        self.assertEqual((waits[1].resource_kind, waits[1].resource), ("slot", "build"))
        self.assertIsInstance(waits[0].holder, phase_waits.UnnamedWaitHolder)
        self.assertIsInstance(waits[1].holder, phase_waits.NamedWaitHolder)
        assert isinstance(waits[1].holder, phase_waits.NamedWaitHolder)
        self.assertIn("builder", waits[1].holder.label)
        events.append({"event_type": "wait_holder_seen", "phase_instance_id": "eight", "wait_id": "worker",
            "holder": {"state": "NamedHolder", "resource_kind": "slot", "resource": "memory",
                "holder": {"state": "SessionHolder", "session_id": "builder-session"}}})
        waits = phase_waits.phase_waits(events, "eight")
        self.assertIsInstance(waits[0].holder, phase_waits.NamedWaitHolder)
        assert isinstance(waits[0].holder, phase_waits.NamedWaitHolder)
        self.assertIn("builder-session", waits[0].holder.label)
        self.assertEqual((waits[0].started, waits[0].resource_kind, waits[0].resource), (1_000, "slot", "memory"))
        self.assertEqual((waits[0].pass_instance_id, waits[0].owner_session), ("repair", "director-session"))
        self.assertEqual(waits[0].wait_attribution, {"actor": "test worker", "command": "release build"})
        events.extend([
            {"event_type": "wait_finished", "phase_instance_id": "eight", "wait_id": "worker"},
            {"event_type": "wait_unclosed", "phase_instance_id": "eight", "wait_id": "director"},
        ])
        self.assertEqual(phase_waits.phase_waits(events, "eight"), [])
        for kind, fields in (("pass_finished", {"pass_instance_id": "repair"}),
                             ("phase_finished", {"phase_instance_id": "eight"}), ("run_finished", {})):
            with self.subTest(finish=kind):
                self.assertEqual(phase_waits.phase_waits([events[0], {"event_type": kind, **fields}], "eight"), [])


if __name__ == "__main__":
    _ = unittest.main()
