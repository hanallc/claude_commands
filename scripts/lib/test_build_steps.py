"""Name gate tests from a private process tree and land record."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.lib import build_steps
from scripts.lint.memory_admit import NATEMERGE_STAFF_ID
from scripts.production import staff


class GateJobTests(unittest.TestCase):
    def test_only_a_live_natemerge_land_names_gate_tests_integrate(self) -> None:
        caller_pid = 101
        land_pid = 100
        natemerge = staff.StaffRow(NATEMERGE_STAFF_ID, "worker", "natemerge", staff.StaffRoot(),
                                 current_session=staff.StaffId("landing-session"))
        unit = staff.StaffRow(staff.StaffId("ordinary"), "worker", "unit", staff.StaffRoot(),
                              current_session=staff.StaffId("ordinary-session"))
        cases = (
            ("first full run", (), "1000", "landing-session", "integrate"),
            ("base check", ("--module", "lib/example"), "1000", "landing-session", "integrate"),
            ("batch retry", (), "1000", "landing-session", "integrate"),
            ("unit run", (), "", "ordinary-session", "test-all"),
            ("dead land", (), "900", "landing-session", "test-all"),
            ("non-natemerge land", (), "1000", "ordinary-session", "test-all"),
        )
        with (tempfile.TemporaryDirectory() as temporary,
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((natemerge, unit)))):
            root = Path(temporary)
            proc = root / "proc"
            state = root / "state"
            records = state / "lands"
            records.mkdir(parents=True)
            land_record = records / f"{land_pid}.json"
            for pid, parent, start in ((caller_pid, land_pid, "1001"), (land_pid, 0, "1000")):
                process = proc / str(pid)
                process.mkdir(parents=True)
                fields = ["S", str(parent), *["0"] * 17, start]
                _ = (process / "stat").write_text(f"{pid} (fixture) " + " ".join(fields))
            caller = proc / str(caller_pid)
            with patch.dict(os.environ, {"MERGE_GATE_STATE_DIR": str(state)}):
                for name, args, recorded_start, session, expected in cases:
                    with self.subTest(name=name):
                        if recorded_start:
                            _ = land_record.write_text(json.dumps(
                                {"pid": land_pid, "start": recorded_start, "commits": ["abc123"]}))
                        else:
                            land_record.unlink(missing_ok=True)
                        _ = (proc / str(land_pid) / "environ").write_bytes(
                            f"CLAUDE_CODE_SESSION_ID={session}\0".encode())
                        command = ("python3", "merge_gate.py", "--checkout", ".", "--tests-only", *args)
                        if name == "batch retry":
                            command = ("python3", "copies.py", "run", "--repo", str(root), "--", *command)
                        _ = (caller / "cmdline").write_bytes(("\0".join(command) + "\0").encode())
                        self.assertEqual(build_steps.gate_job_from_process(
                            "merge-gate", caller, start_time="1001"), build_steps.StepJob(expected))

    def test_retired_merge_arguments_have_no_job(self) -> None:
        self.assertIs(build_steps.command_job(("merge_gate.py", "--unit", "queue", "--checkpoint", "abc123")),
                      build_steps.OTHER_COMMAND)
