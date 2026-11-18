"""Test numbered records in a private state folder."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.production import record_store, staff


class NumberedRecordsTests(unittest.TestCase):
    def test_unicode_separators_stay_inside_a_header_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": directory}):
            store = record_store.NumberedRecords(staff.StaffId("fixture"), "records", "R", "record", ValueError)
            text = "First\u2028Second\u2029Third"
            fields = {"id": "R1", "description": text, "state": "open"}
            body = "Body text\n"
            with store.locked():
                store.write("R1", fields, body)

            self.assertIn(text.encode("utf-8"), store.path("R1").read_bytes())
            self.assertEqual(store.read("R1"), record_store.RecordText(fields, body))
            self.assertEqual(store.field("R1", "description"), text)
