"""Test investigation commands with private staff records and state folders."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.production import investigation, staff

RUNNER = staff.StaffRow(staff.StaffId("32102748-cf44-498e-9a53-b710c2a70916"), "showrunner", "show", staff.StaffRoot(),
                        current_session=staff.StaffId("2b7d4419-b8d4-4c43-b9b2-48bed9337f27"))
OWNER = staff.StaffRow(staff.StaffId("62ec2f46-df7a-40fd-b919-789581e63d53"), "unit", "example", staff.ReportsTo(RUNNER.id),
                       current_session=staff.StaffId("07990246-10cf-440e-a32a-13e62f15b78b"), unit_type=staff.UnitDetails("direct"))
OTHER = staff.StaffRow(staff.StaffId("4fc94517-b49f-4979-9308-37bb5cc90236"), "producer", "dwight", staff.StaffRoot(),
                       current_session=staff.StaffId("e289fc59-9b1f-44b2-bb22-beb7359f97bb"))


class InvestigationTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staff_file = self.root / "staff.json"
        _ = staff_file.write_text(json.dumps({"schema": 1, "rows": [RUNNER.to_json(), OWNER.to_json(), OTHER.to_json()]}))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "STAFF_FILE": str(staff_file), "SHOWRUNNER_STATE_DIR": str(self.root / "state"),
            "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": OWNER.current_session})))

    def command(self, *args: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = investigation.main(list(args))
        return result, output.getvalue(), errors.getvalue()

    def test_file_answer_read_and_list(self) -> None:
        question = "Find the cause.\r\n\n## Answer\n\nThis heading is part of the question.\n"
        title = 'Find the cause: "example"'
        self.assertEqual(self.command("file", "--title", title, "--question", question), (0, "I1\n", ""))
        item = investigation.load(OWNER.id, "I1")
        self.assertEqual((item.owner, item.title, item.question, item.answer), (OWNER.id, title, question, ""))
        self.assertEqual(item.state, investigation.InvestigationState.OPEN)
        self.assertTrue(item.filed)
        self.assertEqual((item.answered, item.read), ("", ""))
        self.assertIn(f"I1 | open | {title}", self.command("list")[1])
        answer = "The cause is known.\n\n## Answer\n\nKeep this heading too.\n"
        self.assertEqual(self.command("answer", "--id", "I1", "--text", answer), (0, "", ""))
        ready = investigation.load(OWNER.id, "I1")
        self.assertEqual((ready.question, ready.answer, ready.state),
                         (question, answer, investigation.InvestigationState.ANSWER_READY))
        self.assertEqual(ready.filed, item.filed)
        self.assertTrue(ready.answered)
        self.assertEqual(self.command("read", "--id", "I1"), (0, "", ""))
        read = investigation.load(OWNER.id, "I1")
        self.assertTrue(read.read)
        self.assertEqual(read.answered, ready.answered)
        self.assertEqual(self.command("list"), (0, "", ""))
        self.assertIn(f"I1 | read | {title}", self.command("list", "--all")[1])
        self.assertEqual(investigation.records(OWNER.id, include_read=True), [read])

    def test_moves_refuse_invalid_states_and_preserve_exact_repeats(self) -> None:
        item = investigation.file(OWNER.id, "Find the cause", "What caused this?")
        path = investigation.folder(OWNER.id) / f"{item.id}.md"
        for args in (("read", "--id", item.id), ("answer", "--id", item.id, "--text", " ")):
            with self.subTest(args=args):
                before = path.read_bytes()
                result, _, error = self.command(*args)
                self.assertEqual(result, 1)
                self.assertEqual(len(error.splitlines()), 1)
                self.assertEqual(path.read_bytes(), before)
        _ = investigation.answer(OWNER.id, item.id, "The cause is known.")
        before, timestamp = path.read_bytes(), path.stat().st_mtime_ns
        _ = investigation.answer(OWNER.id, item.id, "The cause is known.")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (before, timestamp))
        self.assertEqual(self.command("answer", "--id", item.id, "--text", "Different answer")[0], 1)
        _ = investigation.mark_read(OWNER.id, item.id)
        before, timestamp = path.read_bytes(), path.stat().st_mtime_ns
        _ = investigation.mark_read(OWNER.id, item.id)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (before, timestamp))
        self.assertEqual(self.command("answer", "--id", item.id, "--text", "The cause is known.")[0], 1)

    def test_each_owner_numbers_records_under_a_lock(self) -> None:
        def file_one(number: int) -> str:
            return investigation.file(OWNER.id, f"Find cause {number}", "What caused this?").id

        with ThreadPoolExecutor(max_workers=4) as writers:
            identities = list(writers.map(file_one, range(12)))
        self.assertEqual(set(identities), {f"I{number}" for number in range(1, 13)})
        _ = investigation.answer(OWNER.id, "I12", "Found it.")
        _ = investigation.mark_read(OWNER.id, "I12")
        self.assertEqual(investigation.file(OWNER.id, "Another cause", "What caused this?").id, "I13")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("file", "--title", "Other cause", "--question", "Why?"), (0, "I1\n", ""))
            self.assertEqual(len(self.command("list")[1].splitlines()), 1)
            self.assertEqual(self.command("answer", "--id", "I1", "--owner", OWNER.id, "--text", "Found it.")[0], 0)
            self.assertEqual(self.command("read", "--id", "I1", "--owner", OWNER.id)[0], 0)
        self.assertEqual(investigation.load(OWNER.id, "I1").state, investigation.InvestigationState.READ)
        self.assertEqual(investigation.load(OTHER.id, "I1").state, investigation.InvestigationState.OPEN)

    def test_missing_callers_and_invalid_inputs_get_one_refusal_line(self) -> None:
        for args in (("file", "--title", "Two\nlines", "--question", "Why?"),
                     ("file", "--title", "Title\n", "--question", "Why?"),
                     ("file", "--title", "Title", "--question", ""),
                     ("answer", "--id", "../I1", "--text", "Answer"),
                     ("read", "--id", "I0"), ("read", "--id", "I1"),
                     ("read", "--id", "I1", "--owner", "missing")):
            with self.subTest(args=args):
                result, output, error = self.command(*args)
                self.assertEqual((result, output), (1, ""))
                self.assertEqual(len(error.splitlines()), 1)
        for command in ("file", "list", "answer", "read"):
            args = {"file": ["--title", "Cause", "--question", "Why?"], "list": [],
                    "answer": ["--id", "I1", "--text", "Answer"], "read": ["--id", "I1"]}[command]
            with patch.dict(os.environ, {"CODEX_THREAD_ID": "no-staff-row"}):
                self.assertEqual(self.command(command, *args),
                                 (1, "", "investigation: the current session has no staff row\n"))

    def test_reads_watch_sources_and_refuse_corrupt_records(self) -> None:
        sources: list[Path] = []
        self.assertEqual(investigation.records(OWNER.id, sources=sources), [])
        self.assertEqual(sources, [investigation.folder(OWNER.id)])
        item = investigation.file(OWNER.id, "Cause", "Why?")
        sources.clear()
        self.assertEqual(investigation.records(OWNER.id, sources=sources), [item])
        path = investigation.folder(OWNER.id) / "I1.md"
        self.assertEqual(sources, [path.parent, path])
        before = path.read_text()
        for text in ("bad", before.replace('state: "open"', 'state: "closed"'),
                     before.replace(OWNER.id, OTHER.id), before.replace('id: "I1"', 'id: "I2"')):
            with self.subTest(text=text):
                _ = path.write_text(text)
                with self.assertRaises(investigation.InvestigationError):
                    _ = investigation.load(OWNER.id, item.id)
        _ = path.write_text(before)
        with patch.dict(os.environ, {"SHOWRUNNER_STATE_DIR": ""}):
            self.assertEqual(investigation.folder(OWNER.id), self.root / ".local/state/showrunner" / OWNER.id / "investigations")

    def test_records_and_list_skip_a_bad_record_and_watch_its_path(self) -> None:
        item = investigation.file(OWNER.id, "Cause", "Why?")
        path = investigation.folder(OWNER.id) / "I2.md"
        _ = path.write_text("bad")
        sources: list[Path] = []
        errors = io.StringIO()
        with redirect_stderr(errors):
            items = investigation.records(OWNER.id, sources=sources)
        self.assertEqual(len(items), 1)
        self.assertEqual(items, [item])
        self.assertIn(path, sources)
        expected = f"investigation: {path}: I2: invalid investigation: missing frontmatter\n"
        self.assertEqual(errors.getvalue(), expected)
        self.assertEqual(self.command("list"), (0, "I1 | open | Cause\n", expected))
        for args in (("answer", "--id", "I2", "--text", "Found it."), ("read", "--id", "I2")):
            with self.subTest(args=args):
                self.assertEqual(self.command(*args)[0], 1)
                self.assertEqual(path.read_text(), "bad")

    def test_a_showrunner_owns_records_without_a_production_document(self) -> None:
        owner = replace(OWNER, kind="showrunner", parent=staff.StaffRoot(), unit_type=staff.NotUnit())
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [owner.to_json()]}))
        self.assertEqual(self.command("file", "--title", "Cause", "--question", "Why?"), (0, "I1\n", ""))

    def test_invalid_records_keep_their_numbers_and_valid_records_still_count(self) -> None:
        item = investigation.file(OWNER.id, "Cause", "Why?")
        path = investigation.folder(OWNER.id) / "I2.md"
        bad_time = (path.parent / "I1.md").read_text().replace('id: "I1"', 'id: "I2"')
        bad_time = bad_time.replace(f'filed: {json.dumps(item.filed)}', 'filed: "bad"')
        for text in (b"bad", b"\xff", bad_time.encode()):
            with self.subTest(text=text):
                _ = path.write_bytes(text)
                errors = io.StringIO()
                sources: list[Path] = []
                with redirect_stderr(errors):
                    items = investigation.records(OWNER.id, sources=sources)
                self.assertEqual(items, [item])
                self.assertEqual(len(items), 1)
                self.assertIn(path, sources)
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertTrue(errors.getvalue().startswith(f"investigation: {path}: I2: invalid investigation:"))
                result, output, error = self.command("list")
                self.assertEqual((result, output), (0, "I1 | open | Cause\n"))
                self.assertEqual(error, errors.getvalue())
                with self.assertRaises(investigation.InvestigationError):
                    _ = investigation.load(OWNER.id, "I2")
                next_item = investigation.file(OWNER.id, "Next cause", "Why?")
                self.assertEqual(next_item.id, "I3")
                self.assertEqual(path.read_bytes(), text)
                (path.parent / "I3.md").unlink()

    def test_load_checks_required_times_and_empty_times_for_each_state(self) -> None:
        item = investigation.file(OWNER.id, "Cause", "Why?")
        path = investigation.folder(OWNER.id) / f"{item.id}.md"
        for state in investigation.InvestigationState:
            if state == investigation.InvestigationState.ANSWER_READY:
                item = investigation.answer(OWNER.id, item.id, "Found it.")
            elif state == investigation.InvestigationState.READ:
                item = investigation.mark_read(OWNER.id, item.id)
            before = path.read_text()
            for field, value, required in (
                    ("filed", item.filed, True),
                    ("answered", item.answered, state != investigation.InvestigationState.OPEN),
                    ("read", item.read, state == investigation.InvestigationState.READ)):
                invalid = ("", "bad", "2026-11-16T13:00:00") if required else ("2026-11-16T13:00:00+00:00",)
                for time in invalid:
                    with self.subTest(state=state, field=field, time=time):
                        _ = path.write_text(before.replace(f'{field}: {json.dumps(value)}',
                                                           f'{field}: {json.dumps(time)}'))
                        with self.assertRaises(investigation.InvestigationError):
                            _ = investigation.load(OWNER.id, item.id)
                _ = path.write_text(before)


if __name__ == "__main__":
    _ = unittest.main()
