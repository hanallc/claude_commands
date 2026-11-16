"""style_reader_tables writes the Unicode tables of the Rust style reader from this Python."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import final
from unittest import mock

from scripts.style import style_reader_tables


@final
class StyleReaderTablesTest(unittest.TestCase):
    def test_checked_in_tables_are_current(self) -> None:
        self.assertEqual(style_reader_tables.TABLES_PATH.read_text(), style_reader_tables.rust_source())

    def test_check_reports_a_file_that_is_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tables = Path(directory) / "unicode_tables.rs"
            _ = tables.write_text("// old tables\n")
            with (
                mock.patch.object(style_reader_tables, "TABLES_PATH", tables),
                mock.patch.object(sys, "argv", ["style_reader_tables.py", "--check"]),
                mock.patch.object(sys, "stderr"),
            ):
                self.assertEqual(style_reader_tables.main(), 1)
                with mock.patch.object(sys, "argv", ["style_reader_tables.py"]):
                    self.assertEqual(style_reader_tables.main(), 0)
                self.assertEqual(style_reader_tables.main(), 0)

    def test_space_table_keeps_python_separators(self) -> None:
        space = style_reader_tables.ranges(str.isspace)
        self.assertIn((0x1C, 0x20), space)
        self.assertIn((0x85, 0x85), space)


if __name__ == "__main__":
    _ = unittest.main()
