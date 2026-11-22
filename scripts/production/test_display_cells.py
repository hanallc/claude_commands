"""Tests for complete cells in tables that use native Markdown wrapping."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.production import claude_table_layout, display_cells
from scripts.production.config_test_support import UNIT_TABLE_SETTINGS


def setUpModule() -> None:
    configuration = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = (configuration / "production.conf").write_text(UNIT_TABLE_SETTINGS)
    _ = cast(object, unittest.enterModuleContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))



class WrappedTableTests(unittest.TestCase):
    def test_wrapped_rows_preserve_text_and_remove_only_empty_columns(self) -> None:
        headers = ("Unit", "Notes", "Workers", "Queued")
        for text in (
                "Review the complete production plan before delivery " * 8,
                "Review 界面 and cafe\u0301 before delivery " * 8,
                "界" * 180):
            with self.subTest(text=text):
                titles, rows = display_cells.wrapped(headers, [
                    ("one", text, "—", "—"),
                    ("two", "Check the result", "—", text),
                ])
                self.assertEqual(titles, ("Unit", "Notes", "Queued"))
                for name, originals in (("one", (text, "—")), ("two", ("Check the result", text))):
                    selected = [row for row in rows if row[0] == name]
                    for column, original in enumerate(originals, 1):
                        restored = " ".join(row[column] for row in selected if row[column])
                        self.assertEqual(restored.replace(" ", ""), original.replace(" ", ""))
                width = 120 - claude_table_layout.REPLY_INDENT
                fitted = claude_table_layout.fit(titles, rows, width)
                self.assertTrue(fitted.keeps_table(titles, rows, width, 4))

    def test_a_kept_column_stays_when_every_cell_is_empty(self) -> None:
        titles, rows = display_cells.wrapped(("Unit", "Notes", "Workers"), [("one", "—", "—")], keep=(2,))
        self.assertEqual((titles, rows), (("Unit", "Workers"), [("one", "—")]))

    def test_ids_break_only_when_the_terminal_width_needs_it(self) -> None:
        headers = ("Unit", "Status", "Progress", "Phase", "ETA Next", "ETA Plan", "Workers")
        rows = [("admin-console", "busy", "🟩" * 20, "restart-record-replace", "no ETA stated",
                 "17:05 · phase gate-time-limit: Thu 19:00 · late 20h 32m", "item137-bf9-final-callers working")]
        for columns, phase in ((200, "restart-record-replace"), (120, "restart- record- replace"),
                               (None, "restart- record- replace")):
            with self.subTest(columns=columns):
                _, shaped = display_cells.wrapped(headers, rows, columns)
                self.assertEqual(shaped[0][3], phase)

    def test_wrapped_empty_table_keeps_its_headers(self) -> None:
        headers = ("Unit", "Notes", "Queued")
        self.assertEqual(display_cells.wrapped(headers, []), (headers, []))
