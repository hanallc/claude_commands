"""Tests for the records of checkpoint lands that run now."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import override
from unittest.mock import patch

from scripts.lib.process_start import Started, process_start
from scripts.production import land_runs, merge_checkpoint, merge_gate


class LandRunTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"MERGE_GATE_STATE_DIR": str(self.root)}))

    def test_a_land_is_running_only_while_its_process_holds_the_record(self) -> None:
        self.assertEqual(merge_gate.gate_state_dir(), self.root)
        self.assertEqual(land_runs.running_commits(), frozenset())
        with land_runs.landing(["a" * 40, "b" * 9]):
            self.assertEqual(land_runs.running_commits(), frozenset({"a" * 40, "b" * 9}))
        self.assertEqual(list(land_runs.lands_dir().iterdir()), [])
        self.assertEqual(land_runs.running_commits(), frozenset())

    def test_merge_checkpoint_holds_the_record_while_a_batch_lands(self) -> None:
        seen: list[frozenset[str]] = []

        def land(_requests: object) -> int:
            seen.append(land_runs.running_commits())
            return 0

        requests = (SimpleNamespace(commit="a" * 40, production=SimpleNamespace(checkout=self.root, name="sdlc")),
                    SimpleNamespace(commit="b" * 40, production=SimpleNamespace(checkout=self.root, name="sdlc")))
        with (patch.object(sys, "argv", ["merge_checkpoint.py", "--production", "sdlc", "--batch", str(self.root / "batch.txt")]),
              patch.object(merge_checkpoint, "batch_requests", return_value=requests),
              patch.object(merge_checkpoint, "merge_lock", return_value=contextlib.nullcontext()),
              patch.object(merge_checkpoint, "run_batch", side_effect=land)):
            self.assertEqual(merge_checkpoint.main(), 0)
        self.assertEqual(seen, [frozenset({"a" * 40, "b" * 40})])
        self.assertEqual(land_runs.running_commits(), frozenset())

    def test_an_ended_or_unreadable_record_is_not_running(self) -> None:
        start = process_start(os.getpid())
        assert isinstance(start, Started)
        folder = land_runs.lands_dir()
        folder.mkdir(parents=True)
        _ = (folder / "ended.json").write_text(json.dumps({"pid": os.getpid(), "start": start.start + "0", "commits": ["e" * 40]}))
        _ = (folder / "broken.json").write_text("not json")
        _ = (folder / "partial.json").write_text(json.dumps({"pid": os.getpid()}))
        _ = (folder / "live.json").write_text(json.dumps({"pid": os.getpid(), "start": start.start, "commits": ["f" * 40]}))
        self.assertEqual(land_runs.running_commits(), frozenset({"f" * 40}))
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["broken.json", "live.json", "partial.json"])

    def test_a_short_hash_holds_its_full_commit(self) -> None:
        full = "0123456789" * 4
        self.assertTrue(land_runs.holds(frozenset({full[:9]}), full))
        self.assertTrue(land_runs.holds(frozenset({full}), full[:9]))
        self.assertFalse(land_runs.holds(frozenset({"9" * 9}), full))
        self.assertFalse(land_runs.holds(frozenset({""}), full))
        self.assertFalse(land_runs.holds(frozenset({full}), ""))


if __name__ == "__main__":
    _ = unittest.main()
