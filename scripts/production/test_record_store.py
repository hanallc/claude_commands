"""Test numbered records in a private state folder."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.agents.remote import local_machine
from scripts.production import record_store, staff


class NumberedRecordsTests(unittest.TestCase):
    def test_producer_question_folders_do_not_read_the_producer_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": directory}):
            root = Path(directory)
            owner = staff.StaffId("producer")
            document = root / "producer"
            document.mkdir()
            producer = staff.StaffRow(owner, "producer", "producer", staff.StaffRoot(),
                                      current_session=staff.StaffId(""), doc=staff.LiveDoc(local_machine(), document))
            members = staff.StaffSnapshot((producer,))
            registered = root / "former-questions"
            registered.mkdir()
            registry = root / "review-folders" / owner / "question.json"
            registry.parent.mkdir(parents=True)
            _ = registry.write_text(json.dumps([str(registered)]))

            self.assertCountEqual(
                record_store.staff_store_folders(record_store.RecordKind.QUESTION, members=members),
                [registered, root / owner / "questions"],
            )

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
