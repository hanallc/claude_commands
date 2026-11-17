"""Tests for the records of checkpoint lands that run now."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import patch

from scripts.lib.process_start import Started, process_start
from scripts.production import checkpoint_gate, land_runs, merge_checkpoint, merge_gate


class LandRunTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"MERGE_GATE_STATE_DIR": str(self.root)}))

    def test_a_land_is_running_only_while_its_process_holds_the_record(self) -> None:
        self.assertEqual(merge_gate.gate_state_dir(), self.root)
        self.assertEqual(land_runs.running_lands(), {})
        with land_runs.landing(["a" * 40, "b" * 9], ["shelled images-card"]):
            self.assertEqual(land_runs.running_lands(), {"a" * 40: "shelled images-card", "b" * 9: ""})
        self.assertEqual(list(land_runs.lands_dir().iterdir()), [])
        self.assertEqual(land_runs.running_lands(), {})

    def test_merge_checkpoint_holds_the_record_while_a_batch_lands(self) -> None:
        seen: list[dict[str, str]] = []

        def land(_requests: object) -> int:
            seen.append(land_runs.running_lands())
            return 0

        requests = (SimpleNamespace(commit="a" * 40, production=SimpleNamespace(checkout=self.root, name="sdlc"),
                                    unit=SimpleNamespace(name="shelled"), phase="7"),
                    SimpleNamespace(commit="b" * 40, production=SimpleNamespace(checkout=self.root, name="sdlc"),
                                    unit=SimpleNamespace(name="phases"), phase="30"))
        with (patch.object(sys, "argv", ["merge_checkpoint.py", "--production", "sdlc", "--batch", str(self.root / "batch.txt")]),
              patch.object(merge_checkpoint, "batch_requests", return_value=requests),
              patch.object(merge_checkpoint, "merge_lock", return_value=contextlib.nullcontext()),
              patch.object(merge_checkpoint, "run_batch", side_effect=land)):
            self.assertEqual(merge_checkpoint.main(), 0)
        self.assertEqual(seen, [{"a" * 40: "shelled 7", "b" * 40: "phases 30"}])
        self.assertEqual(land_runs.running_lands(), {})

    def test_an_ended_or_unreadable_record_is_not_running(self) -> None:
        start = process_start(os.getpid())
        assert isinstance(start, Started)
        folder = land_runs.lands_dir()
        folder.mkdir(parents=True)
        _ = (folder / "ended.json").write_text(json.dumps({"pid": os.getpid(), "start": start.start + "0", "commits": ["e" * 40]}))
        _ = (folder / "broken.json").write_text("not json")
        _ = (folder / "partial.json").write_text(json.dumps({"pid": os.getpid()}))
        _ = (folder / "live.json").write_text(json.dumps({"pid": os.getpid(), "start": start.start, "commits": ["f" * 40]}))
        self.assertEqual(land_runs.running_lands(), {"f" * 40: ""})
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["broken.json", "live.json", "partial.json"])

    def test_a_short_hash_holds_its_full_commit(self) -> None:
        full = "0123456789" * 4
        self.assertTrue(land_runs.holds(frozenset({full[:9]}), full))
        self.assertTrue(land_runs.holds(frozenset({full}), full[:9]))
        self.assertFalse(land_runs.holds(frozenset({"9" * 9}), full))
        self.assertFalse(land_runs.holds(frozenset({""}), full))
        self.assertFalse(land_runs.holds(frozenset({full}), ""))


    def test_merge_checkpoint_records_each_land_result(self) -> None:
        request = cast(merge_checkpoint.MergeRequest, cast(object, SimpleNamespace(
            commit="a" * 40, phase="board", unit=SimpleNamespace(name="unit-board"),
            production=SimpleNamespace(name="sdlc", checkout=self.root))))
        stop = checkpoint_gate.Stop("conflicts", "held", "scripts/x.py conflicts\nmore", "resolve it")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(merge_checkpoint.report_checkpoint_stop(request, stop), 1)
        failed = land_runs.result_for(land_runs.last_results(), "a" * 9)
        assert failed is not None
        self.assertEqual((failed["landed"], failed["step"], failed["state"], failed["detail"], failed["unit"]),
                         (False, "conflicts", "held", "scripts/x.py conflicts\nmore", "unit-board"))
        with (patch.object(merge_checkpoint, "record"), patch.object(merge_checkpoint, "end_phase_scratch"),
              patch.object(merge_checkpoint, "checkpoint_step_record", return_value=self.root / "steps" / "promotion"),
              contextlib.redirect_stdout(io.StringIO())):
            merge_checkpoint.finish_checkpoint(request, "b" * 40, "merge tests")
        landed = land_runs.result_for(land_runs.last_results(), "a" * 40)
        assert landed is not None
        self.assertTrue(landed["landed"])
        self.assertEqual(len(land_runs.results_path().read_text().splitlines()), 2)

    def test_the_results_keep_only_the_newest_rows_and_skip_bad_rows(self) -> None:
        with patch.object(land_runs, "KEPT_RESULTS", 3):
            for number in range(5):
                land_runs.record_result(commit=f"{number}" * 40, production="sdlc", unit="u", phase="p", landed=True)
        with land_runs.results_path().open("a") as stream:
            _ = stream.write("not json\n" + json.dumps({"commit": 7}) + "\n" + json.dumps({"unit": "u"}) + "\n")
        self.assertEqual(sorted(land_runs.last_results()), ["2" * 40, "3" * 40, "4" * 40])
        self.assertIsNone(land_runs.result_for(land_runs.last_results(), "0" * 40))
        self.assertIsNone(land_runs.result_for(land_runs.last_results(), ""))


if __name__ == "__main__":
    _ = unittest.main()
