"""Test admission at the bump gate caller."""

from contextlib import nullcontext
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.production import bump_gate, checkpoint_gate


class BumpGateTests(unittest.TestCase):
    def test_named_gate_leaves_placement_to_the_checkpoint_caller(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            gate = ["python3", "scripts/production/gate.py", "--tests-only"]
            record_path = root / "bump-gate.json"
            record: checkpoint_gate.BumpGateRecord = {
                "tip": "tip", "checkpoint": "checkpoint", "production_doc": str(root / "plan.md"),
                "owner_id": "owner", "unit_id": "unit", "name": "production", "merge_branch": "main",
                "checkout": str(root), "production_log": "production.log", "zone": "UTC", "unit": "unit",
                "delivered_to": [], "paths": [], "merge_tests": [gate], "flakes": [], "flaky_tests": [],
                "started": "", "pid": 0, "proc_start": "", "result": "pending", "detail": "", "log": str(root / "gate.log"),
            }
            for names, preparation in (("natedev,mac-linux", []), ("", gate)):
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
                    self.assertEqual(test_steps.call_args.args[2], (tuple(gate),))
                    complete.assert_called_once()
