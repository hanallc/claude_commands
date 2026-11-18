"""Test decoding and file errors for proc text."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.lib.proc_text import read_proc_text


class ReadProcTextTests(unittest.TestCase):
    def test_replaces_invalid_bytes_and_keeps_delimiters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cmdline"
            _ = path.write_bytes(b"verify\xff\0check\0")

            self.assertEqual(read_proc_text(path), "verify\ufffd\0check\0")

    def test_propagates_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileNotFoundError):
                _ = read_proc_text(Path(temporary) / "status")
