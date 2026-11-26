#!/usr/bin/env python3
"""Tests for process table snapshots and process lookup."""

from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.message import process_table




class ProcessTableTests(unittest.TestCase):
    def test_nearest_named_prefers_the_process_itself_over_named_parents(self) -> None:
        process = process_table.Process(30, 20, "python3", "worker.py")
        table = {30: process, 20: process_table.Process(20, 1, "claude", "claude")}
        self.assertEqual(process_table.nearest_named(process, table, {30: "worker", 20: "director"}), "worker")

    def test_nearest_named_walks_unnamed_parents_and_stops_at_the_nearest_name(self) -> None:
        process = process_table.Process(40, 30, "python3", "worker.py")
        table = {
            40: process,
            30: process_table.Process(30, 20, "sh", "launch worker.py"),
            20: process_table.Process(20, 10, "claude", "claude"),
            10: process_table.Process(10, 1, "claude", "claude"),
        }
        self.assertEqual(process_table.nearest_named(process, table, {20: "director", 10: "showrunner"}), "director")

    def test_nearest_named_returns_none_when_the_unnamed_chain_ends(self) -> None:
        for parent in (0, 999):
            with self.subTest(parent=parent):
                process = process_table.Process(30, 20, "python3", "worker.py")
                table = {30: process, 20: process_table.Process(20, parent, "sh", "launch worker.py")}
                self.assertIsNone(process_table.nearest_named(process, table, {10: "unrelated session"}))

    def test_nearest_named_returns_none_for_an_unnamed_parent_loop(self) -> None:
        process = process_table.Process(30, 20, "python3", "worker.py")
        table = {30: process, 20: process_table.Process(20, 30, "sh", "launch worker.py")}
        self.assertIsNone(process_table.nearest_named(process, table, {10: "unrelated session"}))

    def test_snapshot_reads_the_configured_process_table(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        listing = folder / "ps"
        _ = listing.write_text(
            "10 1 claude claude --resume session\n"
            + "20 10 python3 python3 /tools/worker.py --task one two\n"
            + "30 10 sleep\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"PROCESS_TABLE_PS": f"cat {listing}"}):
            _ = importlib.reload(process_table)
            found: dict[int, process_table.Process] = process_table.snapshot()
        _ = importlib.reload(process_table)

        self.assertEqual(found, {
            10: process_table.Process(10, 1, "claude", "claude --resume session"),
            20: process_table.Process(20, 10, "python3", "python3 /tools/worker.py --task one two"),
            30: process_table.Process(30, 10, "sleep", ""),
        })


if __name__ == "__main__":
    _ = unittest.main()
