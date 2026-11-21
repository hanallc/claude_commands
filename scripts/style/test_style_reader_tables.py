"""style_reader_tables writes the Unicode tables of the Rust style reader from this Python."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import final

from scripts.style import style_reader_tables


@final
class StyleReaderTablesTest(unittest.TestCase):
    def test_check_requires_a_tables_path(self) -> None:
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as error:
            _ = style_reader_tables.main(["--check"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("required: --tables", errors.getvalue())

    def test_check_detects_drift_and_accepts_the_generated_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tables = Path(directory) / "unicode_tables.rs"
            arguments = ["--tables", str(tables)]
            _ = tables.write_text("// old tables\n")
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(style_reader_tables.main([*arguments, "--check"]), 1)
            self.assertIn(str(tables), errors.getvalue())
            self.assertEqual(tables.read_text(), "// old tables\n")

            self.assertEqual(style_reader_tables.main(arguments), 0)
            self.assertEqual(style_reader_tables.main([*arguments, "--check"]), 0)

    def test_space_table_keeps_python_separators(self) -> None:
        space = style_reader_tables.ranges(str.isspace)
        self.assertIn((0x1C, 0x20), space)
        self.assertIn((0x85, 0x85), space)


if __name__ == "__main__":
    _ = unittest.main()
