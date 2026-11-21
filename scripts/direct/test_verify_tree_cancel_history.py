"""Real verify entry point cancellation with private Cargo and Git fixtures."""
from __future__ import annotations

import unittest

import json
from pathlib import Path
from typing import cast

from scripts.direct import recorder_state
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.verify_tree_cancel_test_support import VerifyTreeCancelFixture


class VerifyTreeCancelTests(VerifyTreeCancelFixture):
    def test_tree_cancellation_closes_verification_progress_activity(self) -> None:
        environment = {**self.environment, "UNIT_HISTORY_DIR": str(self.root / "history"),
                       "UNIT_SYNC_WRITES": "0", "USER_ZONE": "UTC"}
        for args in (["start-run", "--working-dir", str(self.workspace),
                      "--main-family", "codex", "--main-model", "fixture-model"],
                     ["start-phase", "--phase-id", "2", "--phase-title", "Verify"],
                     ["start-phase", "--phase-id", "3", "--phase-title", "Concurrent"],
                     ["start-activity", "--phase-id", "3", "--label", "Review", "--activity", "Review phase 3"]):
            result = invoke_recorder([*args, "--session-dir", str(self.board)], environment,
                                     self.workspace)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        process = self.start_verify("test", UNIT_HISTORY_DIR=environment["UNIT_HISTORY_DIR"],
                                    UNIT_SYNC_WRITES="0", USER_ZONE="UTC", UNIT_PHASE_ID="2")
        self.wait_for_running(self.control / "running", process)
        state_file = self.board / "progress_history_state.json"
        active = cast(dict[str, object], json.loads(state_file.read_text()))
        phase, = [phase for phase in recorder_state.open_phases(active) if phase["id"] == "2"]
        activity = cast(dict[str, object], phase["activity"])
        self.assertEqual(activity["status"], "active", "verify never opened an activity")
        self.assertIn("verify.sh test", str(activity["activity"]))
        self.edit_source()
        _ = self.assert_cancelled(process, "test")
        closed = cast(dict[str, object], json.loads(state_file.read_text()))
        phases = {phase["id"]: phase for phase in recorder_state.open_phases(closed)}
        self.assertEqual(cast(dict[str, object], phases["2"]["activity"])["status"], "canceled")
        self.assertEqual(cast(dict[str, object], phases["3"]["activity"])["status"], "active")
        events = [cast(dict[str, object], json.loads(line)) for line in
                  Path(str(closed["history_file"])).read_text().splitlines()]
        finished = [event for event in events if event.get("event_type") == "activity_finished"]
        self.assertEqual(len(finished), 1, events)
        self.assertEqual(finished[0]["status"], "canceled")
        self.assertEqual(finished[0]["phase_instance_id"], phase["instance_id"])


if __name__ == "__main__":
    _ = unittest.main()
