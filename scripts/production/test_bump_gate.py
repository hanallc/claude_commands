"""Test admission at the bump gate caller."""

from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.lib import build_folders
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import bump_gate, checkpoint_gate, staff


GATE = ["python3", "scripts/production/gate.py", "--tests-only"]


def bump_record(root: Path) -> checkpoint_gate.BumpGateRecord:
    return {
        "tip": "tip", "checkpoint": "checkpoint", "production_doc": str(root / "plan.md"),
        "owner_id": "owner", "unit_id": "unit", "name": "production", "merge_branch": "main",
        "checkout": str(root), "production_log": "production.log", "zone": "UTC", "unit": "unit",
        "delivered_to": [], "paths": [], "merge_tests": [GATE], "flakes": [], "flaky_tests": [],
        "started": "", "pid": 0, "proc_start": "", "result": "pending", "detail": "", "log": str(root / "gate.log"),
    }


class BumpGateTests(unittest.TestCase):
    def test_named_gate_leaves_placement_to_the_checkpoint_caller(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_path = root / "bump-gate.json"
            record = bump_record(root)
            for names, preparation in (("natedev,mac-linux", []), ("", GATE)):
                with (self.subTest(names=names),
                      mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": names}),
                      mock.patch.object(copies, "requested_source", return_value=nullcontext(root)),
                      mock.patch.object(copies, "command_tree", return_value=nullcontext(root)) as tree,
                      mock.patch.object(bump_gate, "run_checkpoint_test_steps", return_value="passed") as test_steps,
                      mock.patch.object(bump_gate, "complete_gate") as complete):
                    checkpoint_gate.save_record(record_path, record)
                    self.assertEqual(bump_gate.run_gate(record_path), 0)
                    tree.assert_called_once_with("owner", root, copies.CommittedSource("HEAD"),
                        preparation, step="integrate")
                    test_steps.assert_called_once()
                    self.assertEqual(test_steps.call_args.args[2], (tuple(GATE),))
                    complete.assert_called_once()

    def test_bump_gate_with_all_targets_off_records_and_sends_blocked(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_path = root / "bump-gate.json"
            checkpoint_gate.save_record(record_path, bump_record(root))
            _ = write_config_fixture(root / "build_targets.conf",
                "targets=mac-linux,natedev\nenabled.mac-linux=off\nenabled.natedev=off\n")
            members = {
                "owner": SimpleNamespace(name="showrunner", current_session="owner-session"),
                "unit": SimpleNamespace(name="unit", current_session="unit-session"),
            }
            with (mock.patch.dict(os.environ, {"CONF_DIR": str(root), "BUILDLOG_GATE_TARGETS": ""}),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.object(copies, "requested_source", return_value=nullcontext(root)),
                  mock.patch.object(copies, "command_tree") as tree,
                  mock.patch.object(bump_gate, "run_checkpoint_test_steps") as tests,
                  mock.patch.object(staff, "snapshot", return_value=SimpleNamespace(rows_by_id=members)),
                  mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as send):
                self.assertEqual(bump_gate.run_gate(record_path), 1)
            tree.assert_not_called()
            tests.assert_not_called()
            record = checkpoint_gate.read_record(record_path)
            self.assertEqual(record["result"], "blocked")
            self.assertEqual(record["delivered_to"], ["unit", "showrunner"])
            notices = [cast(list[str], call.args[0]) for call in send.call_args_list]
            self.assertEqual([argv[argv.index("--to") + 1] for argv in notices],
                             ["session:unit-session", "session:owner-session"])
            for argv in notices:
                self.assertIn("Bump gate blocked: unit; tip tip; test: No enabled build target can run this Claude gate.",
                              argv[argv.index("--text") + 1])
