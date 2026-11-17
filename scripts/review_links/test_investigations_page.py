"""Test investigation cards with private staff records and state folders."""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr
from pathlib import Path
import shutil
import tempfile
import unittest
from typing import cast, override
from unittest.mock import patch
from urllib.parse import urlencode

from scripts.review_links.test_page_frame import assert_status_states
from scripts.production import investigation, staff
from scripts.review_links import console, investigations_page

RUNNER = staff.StaffRow(staff.StaffId("a0882376-677b-4da5-adbb-f11db9146021"), "showrunner", "Alpha", staff.StaffRoot(),
                        current_session=staff.StaffId("a0882376-677b-4da5-adbb-f11db9146021"))
UNIT = staff.StaffRow(staff.StaffId("bf6915e1-6391-45a2-9cc0-8151d4157270"), "unit", "Builder", staff.ReportsTo(RUNNER.id),
                      current_session=staff.StaffId("bf6915e1-6391-45a2-9cc0-8151d4157270"), unit_type=staff.UnitDetails("direct"))
ORPHAN = staff.StaffId("f5636030-6745-4ff4-aeda-4c2773d18da8")


class InvestigationsPageTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="investigations-page-test-")))
        config = self.root / "config"
        config.mkdir()
        for name in ("review_links", "console", "change_review"):
            _ = shutil.copyfile(Path(__file__).resolve().parents[2] / f"config/{name}.conf", config / f"{name}.conf")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(config), "SHOWRUNNER_STATE_DIR": str(self.root / "state"),
            "STAFF_FILE": str(self.root / "staff.json"), "USER_ZONE": "America/New_York",
            "REVIEW_LINKS_ROOT": str(self.root / "reviews")})))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [RUNNER.to_json(), UNIT.to_json()]}))

    def file(self, owner: staff.StaffId, title: str, moment: str) -> investigation.Investigation:
        with patch.object(investigation, "_now", return_value=moment):
            return investigation.file(owner, title, "Read the report.\n\n1. Check the first result.\n2. Check the second result.")

    def test_cards_show_the_state_text_footer_and_owner_filters(self) -> None:
        ready = self.file(UNIT.id, "Find the cause <now>", "2026-11-16T13:05:00+00:00")
        _ = investigation.answer(UNIT.id, ready.id, "The cause is known.\nThe repair is ready.")
        _ = self.file(RUNNER.id, "Find the next cause", "2026-11-16T14:00:00+00:00")
        read = self.file(ORPHAN, "Read answer", "2026-11-16T12:00:00+00:00")
        _ = investigation.answer(ORPHAN, read.id, "An answer.")
        _ = investigation.mark_read(ORPHAN, read.id)
        body = investigations_page.render(self.root / "reviews").decode()
        self.assertEqual(body.count("<article "), 3)
        self.assertEqual(body.count(">Mark read</button>"), 1)
        for part in ('Find the cause &lt;now&gt;', '<h3>Answer</h3>', '<ol>', 'Waiting for an answer',
                     '<dt>source:</dt><dd>Alpha · Builder</dd>', '<dt>date:</dt><dd>2026-11-16</dd>',
                     '<dt>time:</dt><dd>08:05</dd>', f'<dt>source:</dt><dd>{ORPHAN}</dd>'):
            with self.subTest(part=part):
                self.assertIn(part, body)
        for value, label in (("not-read", "Not read"), ("answer ready", "Answer ready"), ("open", "Waiting"),
                             ("read", "Read"), ("all", "All")):
            self.assertIn(f'<option value="{value}">{label}</option>', body)
        self.assertLess(body.index(f'<option value="{RUNNER.id}"'), body.index(f'<option value="{UNIT.id}"'))
        self.assertIn(f'<option value="{UNIT.id}">Builder</option>', body)
        self.assertIn(f'<option value="{ORPHAN}">{ORPHAN}</option>', body)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_uses_the_real_investigations_loader(self) -> None:
        ready = self.file(UNIT.id, "Find the cause <now>", "2026-11-16T13:05:00+00:00")
        _ = investigation.answer(UNIT.id, ready.id, "The cause is known.\nThe repair is ready.")
        _ = self.file(RUNNER.id, "Find the next cause", "2026-11-16T14:00:00+00:00")
        read = self.file(ORPHAN, "Read answer", "2026-11-16T12:00:00+00:00")
        _ = investigation.answer(ORPHAN, read.id, "An answer.")
        _ = investigation.mark_read(ORPHAN, read.id)
        body = investigations_page.render(self.root / "reviews").decode()
        assert_status_states(self, body, 15)

    def test_order_uses_answer_time_then_filed_time_then_read_time(self) -> None:
        items: list[investigation.Investigation] = []
        for title, filed, answered, read in (
                ("Open new", "2026-11-16T13:00:00+00:00", "", ""),
                ("Open old", "2026-11-16T12:00:00+00:00", "", ""),
                ("Ready old", "2026-11-16T11:00:00+00:00", "2026-11-16T14:00:00+00:00", ""),
                ("Ready new", "2026-11-16T10:00:00+00:00", "2026-11-16T15:00:00+00:00", ""),
                ("Read old", "2026-11-16T09:00:00+00:00", "2026-11-16T14:00:00+00:00", "2026-11-16T16:00:00+00:00"),
                ("Read new", "2026-11-16T08:00:00+00:00", "2026-11-16T14:00:00+00:00", "2026-11-16T17:00:00+00:00")):
            item = self.file(RUNNER.id, title, filed)
            if answered:
                with patch.object(investigation, "_now", return_value=answered):
                    item = investigation.answer(RUNNER.id, item.id, "Found it.")
            if read:
                with patch.object(investigation, "_now", return_value=read):
                    item = investigation.mark_read(RUNNER.id, item.id)
            items.append(item)
        expected = ["Ready new", "Ready old", "Open old", "Open new", "Read new", "Read old"]
        self.assertEqual([item.title for item in investigations_page.records()], expected)
        body = investigations_page.render(self.root / "reviews").decode()
        positions = [body.index(f'<h2>{title}</h2>') for title in expected]
        self.assertEqual(positions, sorted(positions))

    def test_signed_mark_read_refuses_bad_forms_and_accepts_a_repeat(self) -> None:
        root = self.root / "reviews"
        item = self.file(ORPHAN, "Orphan answer", "2026-11-16T12:00:00+00:00")
        fields = {"owner": ORPHAN, "id": item.id, "token": investigations_page.request_token(root, ORPHAN, item.id)}
        for body in (urlencode(fields), urlencode({**fields, "token": "bad"}), urlencode({**fields, "id": "I2"}),
                     urlencode({**fields, "extra": "bad"}), urlencode(fields) + '&id=I2'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                investigations_page.submit(root, body)
        _ = investigation.answer(ORPHAN, item.id, "Found it.")
        investigations_page.submit(root, urlencode(fields))
        investigations_page.submit(root, urlencode(fields))
        self.assertEqual(investigation.load(ORPHAN, item.id).state, investigation.InvestigationState.READ)

    def test_a_bad_time_does_not_stop_valid_cards_or_news(self) -> None:
        ready = self.file(UNIT.id, "Valid answer", "2026-11-16T12:00:00+00:00")
        ready = investigation.answer(UNIT.id, ready.id, "Found it.")
        bad = self.file(UNIT.id, "Invalid time", "2026-11-16T13:00:00+00:00")
        path = investigation.folder(UNIT.id) / f"{bad.id}.md"
        _ = path.write_text(path.read_text().replace(f'filed: {json.dumps(bad.filed)}', 'filed: "bad"'))
        root = self.root / "reviews"
        errors = io.StringIO()
        with redirect_stderr(errors):
            body = investigations_page.render(root).decode()
            news = investigations_page.waiting_investigations(console.NewsRequest(root))
        self.assertEqual(body.count("<article "), 1)
        self.assertIn("<h2>Valid answer</h2>", body)
        self.assertNotIn("<h2>Invalid time</h2>", body)
        self.assertEqual(news, frozenset({f"{UNIT.id}/{ready.id}/{ready.answered}"}))
        self.assertEqual(len(errors.getvalue().splitlines()), 2)
        for line in errors.getvalue().splitlines():
            self.assertTrue(line.startswith(f"investigation: {path}: {bad.id}: invalid investigation:"))


if __name__ == "__main__":
    _ = unittest.main()
