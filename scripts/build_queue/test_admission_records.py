"""Test process identity and memory configuration reads for admission clients."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_records as records
from scripts.build_queue.queue_fixture import write_memory_config
from scripts.lib import conf


class ProcessStartTimeTests(unittest.TestCase):
    def test_process_stat_with_invalid_name_keeps_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            directory = proc / "123"
            directory.mkdir()
            _ = (directory / "stat").write_bytes(b"123 (verify\xff) S " + b"0 " * 18 + b"4200\n")

            with patch.object(records, "Path", return_value=proc):
                self.assertEqual(records.process_start_time(123), "4200")


class MemoryConfigTests(unittest.TestCase):
    def test_missing_key_names_the_key_without_a_recovery_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "build_admission.conf"
            write_memory_config(config, safety_gib=None)

            with patch.dict(os.environ, {"CONF_DIR": temporary}), self.assertRaises(conf.Unusable) as refused:
                _ = records.read_memory_config(report=False)

        self.assertEqual(str(refused.exception), f"{config}: safety_gib is missing; add it to {config}")


class QueueCallerTests(unittest.TestCase):
    def test_an_unknown_worktree_does_not_get_a_terminal_prefix(self) -> None:
        caller = records.queue_caller("", "", "", "", "/private/unregistered session")
        self.assertEqual(caller.name, "unregistered session")
