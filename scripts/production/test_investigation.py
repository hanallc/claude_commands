"""Test investigation commands with private staff records and state folders."""
from __future__ import annotations

import io
import fcntl
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import TextIO, cast, override
from unittest.mock import patch

from scripts.console_text.test_support import accepting_console_text, real_review_question
from scripts.notify import review
from scripts.production import investigation, staff, the_list
from scripts.review_links import change_page, new_item_dialog, reply_tokens, review_page, test_review_page

RUNNER = staff.StaffRow(staff.StaffId("32102748-cf44-498e-9a53-b710c2a70916"), "showrunner", "show", staff.StaffRoot(),
                        current_session=staff.StaffId("2b7d4419-b8d4-4c43-b9b2-48bed9337f27"))
OWNER = staff.StaffRow(staff.StaffId("62ec2f46-df7a-40fd-b919-789581e63d53"), "unit", "example", staff.ReportsTo(RUNNER.id),
                       current_session=staff.StaffId("07990246-10cf-440e-a32a-13e62f15b78b"), unit_type=staff.UnitDetails("direct"))
OTHER = staff.StaffRow(staff.StaffId("4fc94517-b49f-4979-9308-37bb5cc90236"), "producer", "dwight", staff.StaffRoot(),
                       current_session=staff.StaffId("e289fc59-9b1f-44b2-bb22-beb7359f97bb"))
LIST_KEEPER = staff.StaffRow(staff.StaffId("fdab6975-69b4-44ec-b688-22f66d92aa67"), "producer",
                           "fixture-list-keeper", staff.StaffRoot(),
                           current_session=staff.StaffId("e6183e17-a8ce-437f-93be-e2b3004a95cf"))


class InvestigationTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.enterContext(accepting_console_text())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staff_file = self.root / "staff.json"
        _ = staff_file.write_text(json.dumps({
            "schema": 1, "rows": [row.to_json() for row in (RUNNER, OWNER, OTHER, LIST_KEEPER)],
        }))
        configuration = self.root / "config"
        configuration.mkdir()
        _ = (configuration / "console.conf").write_text(f"page.list.owner={LIST_KEEPER.id}\n")
        _ = (configuration / "producer_map.conf").write_text("")
        _ = self.enterContext(patch.object(reply_tokens, "platform", "linux"))
        _ = self.enterContext(patch.object(review, "reviewer_environment", side_effect=OSError("Fixture reviewer.")))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "STAFF_FILE": str(staff_file), "SHOWRUNNER_STATE_DIR": str(self.root / "state"),
            "REVIEW_LINKS_ROOT": str(self.root / 'reviews'), "NOTIFY_STATE_DIR": str(self.root / "notify"),
            "XDG_STATE_HOME": str(self.root / "key-state"), "CONF_DIR": str(configuration),
            "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": OWNER.current_session})))

    def command(self, *args: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            arguments = list(args)
            if arguments and arguments[0] == "file":
                arguments.extend(["--waiting-why", "The owner must complete the investigation."])
            result = investigation.main(arguments)
        return result, output.getvalue(), errors.getvalue()

    def test_answer_refuses_a_draft_and_records_a_model_outage(self) -> None:
        self.assertEqual(self.command("file", "--title", "Find the cause", "--question", "Why did it stop?")[0], 0)
        before = investigation.load(OWNER.id, "I1")
        directory = self.root / "notify"
        with (patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(directory)}),
              patch.object(review, "review_question", new=real_review_question),
              patch.object(review, "_review", return_value=review.Unreviewed("reviewer unavailable")) as reader):
            code, _, errors = self.command("answer", "--id", "I1", "--text", "Read /tmp/result.txt.")
            self.assertEqual(code, 1)
            self.assertIn("Revise the investigation answer draft. omit-file-path: /tmp/result.txt", errors)
            self.assertEqual(investigation.load(OWNER.id, "I1"), before)
            reader.assert_not_called()

            answer = "The download stopped when the disk filled."
            code, _, errors = self.command("answer", "--id", "I1", "--text", answer)
            self.assertEqual(code, 0, errors)
            self.assertEqual(investigation.load(OWNER.id, "I1").answer, answer)
            self.assertIn("unreviewed: reviewer unavailable", (directory / "review.jsonl").read_text())
            reader.assert_called_once()

    def test_answer_reviews_without_the_owner_lock_and_refuses_a_changed_record(self) -> None:
        report = investigation.file(OWNER.id, "Find the cause", "Which check failed?",
                                    waiting_why="The owner answers.")
        answer = "The first check failed."
        lock = investigation.state_root() / "investigation-locks" / (OWNER.id + ".lock")

        def read_draft(*_args: object, **_kwargs: object) -> review.QuestionAccepted:
            with lock.open("r") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            investigation.set_waiting_why(OWNER.id, report.id, "The owner checks a new result.")
            return review.QuestionAccepted(answer)

        with patch.object(review, "review_question", side_effect=read_draft) as reader:
            with self.assertRaisesRegex(investigation.InvestigationError, "The selected investigation changed"):
                _ = investigation.answer(OWNER.id, report.id, answer)
        reader.assert_called_once()
        current = investigation.load(OWNER.id, report.id)
        self.assertEqual((current.waiting_why, current.answer), ("The owner checks a new result.", ""))

    def test_file_answer_read_and_list(self) -> None:
        question = "Find the cause.\r\n\n## Answer\n\nThis heading is part of the question.\n"
        title = 'Find the cause: "example"'
        _ = (self.root / "config" / "producer_map.conf").write_text(f"{RUNNER.id}={OTHER.id}\n")
        self.assertEqual(self.command("file", "--title", title, "--question", question), (0, "I1\n", ""))
        item = investigation.load(OWNER.id, "I1")
        row, = the_list.records(LIST_KEEPER.id)
        self.assertEqual(item.list_destination, investigation.ListDestination(LIST_KEEPER.id, row.number))
        self.assertEqual((row.owner, row.producer), (OWNER.id, OTHER.id))
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
        self.assert_review_checks_under_the_owner_lock(ready)
        self.assertEqual(self.command("read", "--id", "I1"),
                         (1, "", "investigation: Open the report on the List.\n"))
        self.assertEqual(investigation.load(OWNER.id, "I1"), ready)

    def test_f079_new_investigation_keeps_the_answer_at_a_late_collision(self) -> None:
        first = investigation.file(OWNER.id, "First report", "Check the result.", waiting_why="The owner answers.")
        _ = investigation.answer(OWNER.id, first.id, "The peer answered.")
        original = investigation.folder(OWNER.id) / "I1.md"
        content = original.read_bytes()
        collision = investigation.folder(OWNER.id) / "I2.md"

        def sync_answer(_identity: str, path: Path) -> None:
            if path == collision:
                _ = original.rename(path)

        saved = investigation.file(OWNER.id, "Second report", "Check the other result.",
                                   waiting_why="The owner answers.", before_write=sync_answer)
        self.assertEqual(collision.read_bytes(), content)
        self.assertEqual(investigation.load(OWNER.id, first.id).answer, "The peer answered.")
        self.assertEqual(investigation.load(OWNER.id, saved.id), saved)
        self.assertEqual((collision.with_stem("I2-2")).read_text().count("Second report"), 1)

    def test_file_without_staff_uses_existing_store_ids(self) -> None:
        staff_file = self.root / "staff.json"
        staff_file.unlink()
        first = investigation.file(OTHER.id, "First report", "Why?", waiting_why="The owner answers.")
        second = investigation.file(OWNER.id, "Second report", "Why?", waiting_why="The owner answers.")
        self.assertEqual((first.id, second.id), ("I1", "I2"))
        self.assertEqual(investigation.load(OTHER.id, first.id), first)
        self.assertEqual(investigation.load(OWNER.id, second.id), second)

    def test_file_refuses_an_unreadable_staff_record(self) -> None:
        _ = (self.root / "staff.json").write_text("invalid staff record")
        with self.assertRaises(staff.StaffUnreadable):
            _ = investigation.file(OWNER.id, "Check the result", "Why?", waiting_why="The owner answers.")
        self.assertEqual(investigation.records(OWNER.id), [])

    def test_legacy_record_without_kind_is_an_investigation(self) -> None:
        directory = investigation.folder(OWNER.id)
        directory.mkdir(parents=True)
        fields = {
            "id": "I1", "owner": OWNER.id, "title": "Find the cause", "state": "open",
            "filed": "2026-11-17T16:00:00+00:00", "answered": "", "read": "", "question_length": "4",
        }
        header = "\n".join(f"{key}: {json.dumps(value)}" for key, value in fields.items())
        _ = (directory / "I1.md").write_text(f"---\n{header}\n---\n\nWhy?\n\n## Answer\n\n")
        self.assertEqual(investigation.load(OWNER.id, "I1").kind, investigation.InvestigationKind.INVESTIGATION)
        self.assertEqual(investigation.answer(OWNER.id, "I1", "Fixed").kind,
                         investigation.InvestigationKind.INVESTIGATION)

    def test_moves_refuse_invalid_states_and_preserve_exact_repeats(self) -> None:
        item = investigation.file(OWNER.id, "Find the cause", "What caused this?", waiting_why='The owner must complete the investigation.')
        path = investigation.folder(OWNER.id) / f"{item.id}.md"
        for args in (("read", "--id", item.id), ("answer", "--id", item.id, "--text", " ")):
            with self.subTest(args=args):
                before = path.read_bytes()
                result, _, error = self.command(*args)
                self.assertEqual(result, 1)
                self.assertEqual(len(error.splitlines()), 1)
                self.assertEqual(path.read_bytes(), before)
        ready = investigation.answer(OWNER.id, item.id, "The cause is known.")
        before, timestamp = path.read_bytes(), path.stat().st_mtime_ns
        _ = investigation.answer(OWNER.id, item.id, "The cause is known.")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (before, timestamp))
        self.assertEqual(self.command("answer", "--id", item.id, "--text", "Different answer")[0], 1)
        for args in (("read", "--id", item.id), ("list",)):
            with self.subTest(args=args):
                self.assertEqual(self.command(*args), (0, "", ""))
        read = investigation.load(OWNER.id, item.id)
        self.assertTrue(read.read)
        self.assertEqual(read.answered, ready.answered)
        self.assertIn(f"{item.id} | read | {item.title}", self.command("list", "--all")[1])
        self.assertEqual(investigation.records(OWNER.id, include_read=True), [read])
        before, timestamp = path.read_bytes(), path.stat().st_mtime_ns
        _ = investigation.mark_read(OWNER.id, item.id)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (before, timestamp))
        self.assertEqual(self.command("answer", "--id", item.id, "--text", "The cause is known.")[0], 1)

    def assert_review_checks_under_the_owner_lock(self, item: investigation.Investigation) -> None:
        """Use the answered record to check refusal under the owner lock."""
        folder = investigation.folder(OWNER.id)
        path = folder / f"{item.id}.md"
        before = path.read_bytes()

        def refuse() -> None:
            lock = investigation.state_root() / "investigation-locks" / (OWNER.id + ".lock")
            with lock.open("r") as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise investigation.InvestigationError("The record changed.")

        with self.assertRaisesRegex(investigation.InvestigationError, "The record changed"):
            _ = investigation.mark_read(OWNER.id, item.id, check=refuse)
        self.assertEqual(path.read_bytes(), before)

    def test_native_answer_refuses_a_superseded_review_exchange(self) -> None:
        for wait in ('native', 'question'):
            with self.subTest(wait=wait):
                case = test_review_page.ReviewPageTests('runTest')
                case.setUp()
                try:
                    report = case.investigation(wait == 'question')
                    key = f'investigation/{report.owner}/{report.id}'
                    if wait == 'question':
                        old, _ = review_page.submit(case.root, case.body(key, 'question', 'Check the old case.'), case.status)
                        change_page.deliver(old, case.status)
                    delivery, _ = review_page.submit(case.root, case.body(key, 'question', 'Check the replacement case.'), case.status)
                    assert isinstance(delivery, change_page.MessageDelivery)
                    change_page.deliver(delivery, case.status)
                    path = investigation.folder(report.owner)/(report.id+'.md')
                    delivery_path = case.status.path(delivery.page, delivery.row)
                    before = path.read_bytes(), delivery_path.read_bytes()
                    with patch.dict(os.environ, {'CODEX_THREAD_ID': case.fixture.unit.current_session,
                                                 'CLAUDE_CODE_SESSION_ID': ''}):
                        self.assertEqual(self.command('answer', '--id', report.id, '--owner', report.owner,
                                                      '--text', 'The old answer.'),
                                         (1, '', 'investigation: this question id does not match the current question\n'))
                    with self.assertRaisesRegex(investigation.InvestigationError,
                                                'this question id does not match the current question'):
                        _ = investigation.answer(report.owner, report.id, 'The old answer.')
                    self.assertEqual((path.read_bytes(), delivery_path.read_bytes()), before)
                    request = case.status.slots(delivery.page, delivery.row).question
                    assert isinstance(request, change_page.OpenQuestion)
                    self.assertTrue(change_page.answer_item(key, 'The replacement case passes.', request.question_id, case.status))
                    self.assertEqual(investigation.load(report.owner, report.id).state,
                                     investigation.InvestigationState.ANSWER_READY)
                    self.assertIsInstance(case.status.slots(delivery.page, delivery.row).question, change_page.AnsweredQuestion)
                finally:
                    case.doCleanups()

    def test_native_question_can_follow_a_completed_review_exchange(self) -> None:
        case = test_review_page.ReviewPageTests('runTest')
        case.setUp()
        try:
            report = case.investigation()
            key = f'investigation/{report.owner}/{report.id}'
            delivery, _ = review_page.submit(case.root, case.body(key, 'question', 'Check the Review case.'), case.status)
            assert isinstance(delivery, change_page.MessageDelivery)
            change_page.deliver(delivery, case.status)
            request = case.status.slots(delivery.page, delivery.row).question
            assert isinstance(request, change_page.OpenQuestion)
            self.assertTrue(change_page.answer_item(key, 'The Review case passes.', request.question_id, case.status))
            _ = investigation.review_action(report.owner, report.id, 'question', 'Check the native case.')
            answered = investigation.answer(report.owner, report.id, 'The native case passes.')
            self.assertEqual(answered.state, investigation.InvestigationState.ANSWER_READY)
            self.assertEqual(answered.answer, 'The native case passes.')
            self.assertIsInstance(case.status.slots(delivery.page, delivery.row).question, change_page.AnsweredQuestion)
        finally:
            case.doCleanups()

    def test_answering_a_sent_investigation_uses_the_private_hash(self) -> None:
        kind = new_item_dialog.SubmissionKind.INVESTIGATION
        request = f"r1-{'0' * 32}-{kind}-{OWNER.id}"
        proof = reply_tokens.mint()
        token = reply_tokens.derive(proof)
        details = new_item_dialog.ReviewSubmission(request, "b" * 64, new_item_dialog.NoEvidence(), (),
                                                   reply_proof=proof)
        report = investigation.file(OWNER.id, "Find the cause", "Which check failed?",
                                    waiting_why="The owner answers.", submission=details)
        path = investigation.folder(OWNER.id) / f"{report.id}.md"
        key = new_item_dialog.submission_key(kind, OWNER.id, report.id, request)
        note = path.read_text(encoding="utf-8")
        self.assertFalse(token in note)
        self.assertFalse("reply_token_nonce" in note)
        reply_tokens.path().unlink()
        answer = "The first check failed."

        def read_draft(*_args: object, **_kwargs: object) -> review.QuestionAccepted:
            lock = investigation.state_root() / "investigation-locks" / (OWNER.id + ".lock")
            with lock.open("r") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return review.QuestionAccepted(answer)

        with patch.object(review, "review_question", side_effect=read_draft) as reader:
            self.assertTrue(investigation.answer_submission(path, new_item_dialog.read_submission_key(key),
                                                            answer, token, owner=OWNER.id))
        reader.assert_called_once()
        self.assertFalse(reply_tokens.path().exists())

    def test_all_owners_number_records_under_one_lock(self) -> None:
        def file_one(number: int) -> str:
            return investigation.file(OWNER.id, f"Find cause {number}", "What caused this?", waiting_why='The owner must complete the investigation.').id

        with ThreadPoolExecutor(max_workers=4) as writers:
            identities = list(writers.map(file_one, range(12)))
        self.assertEqual(set(identities), {f"I{number}" for number in range(1, 13)})
        _ = investigation.answer(OWNER.id, "I12", "Found it.")
        _ = investigation.mark_read(OWNER.id, "I12")
        self.assertEqual(investigation.file(OWNER.id, "Another cause", "What caused this?", waiting_why='The owner must complete the investigation.').id, "I13")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER.current_session}):
            self.assertEqual(self.command("file", "--title", "Other cause", "--question", "Why?"), (0, "I14\n", ""))
            self.assertEqual(len(self.command("list")[1].splitlines()), 1)
            self.assertEqual(self.command("answer", "--id", "I1", "--owner", OWNER.id, "--text", "Found it.")[0], 0)
            self.assertEqual(self.command("read", "--id", "I1", "--owner", OWNER.id)[0], 0)
        self.assertEqual(investigation.load(OWNER.id, "I1").state, investigation.InvestigationState.READ)
        self.assertEqual(investigation.load(OTHER.id, "I14").state, investigation.InvestigationState.OPEN)
        row, = the_list.records(LIST_KEEPER.id)
        self.assertEqual((row.owner, row.producer), (OTHER.id, OTHER.id))

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
        folder = investigation.folder(OWNER.id)
        watched = [folder, folder / "archive", folder.parent / "archive"]
        self.assertEqual(sources, watched)
        item = investigation.file(OWNER.id, "Cause", "Why?", waiting_why='The owner must complete the investigation.')
        sources.clear()
        self.assertEqual(investigation.records(OWNER.id, sources=sources), [item])
        path = investigation.folder(OWNER.id) / "I1.md"
        self.assertEqual(sources, [*watched, path])
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
        item = investigation.file(OWNER.id, "Cause", "Why?", waiting_why='The owner must complete the investigation.')
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

    def test_f078_records_skip_an_unreadable_renamed_record(self) -> None:
        first = investigation.file(OWNER.id, "First", "Why?", waiting_why="The unit answers.")
        second = investigation.file(OWNER.id, "Second", "Why?", waiting_why="The unit answers.")
        folder = investigation.folder(OWNER.id)
        blocked = (folder / f"{first.id}.md").rename(folder / "renamed.md")
        _ = investigation.answer(OWNER.id, second.id, "The answer")
        open_file = Path.open

        def read_file(path: Path, *args: object, **kwargs: object) -> TextIO:
            if path == blocked:
                raise PermissionError("Cannot read the renamed record.")
            return cast(Callable[..., TextIO], open_file)(path, *args, **kwargs)

        sources: list[Path] = []
        with patch.object(Path, "open", read_file), redirect_stderr(io.StringIO()):
            records = investigation.records(OWNER.id, sources=sources)
        self.assertEqual([item.id for item in records], [second.id])
        self.assertIn(blocked, sources)

    def test_a_showrunner_owns_records_without_a_production_document(self) -> None:
        owner = replace(OWNER, kind="showrunner", parent=staff.StaffRoot(), unit_type=staff.NotUnit())
        _ = (self.root / "staff.json").write_text(json.dumps({
            "schema": 1, "rows": [owner.to_json(), LIST_KEEPER.to_json()],
        }))
        self.assertEqual(self.command("file", "--title", "Cause", "--question", "Why?"), (0, "I1\n", ""))
        row, = the_list.records(LIST_KEEPER.id)
        self.assertEqual((row.owner, row.producer), (owner.id, LIST_KEEPER.id))

    def test_corrupt_records_keep_their_files_and_allocated_numbers(self) -> None:
        item = investigation.file(OWNER.id, "Cause", "Why?", waiting_why='The owner must complete the investigation.')
        path = investigation.folder(OWNER.id) / "I2.md"
        bad_time = (path.parent / "I1.md").read_text().replace('id: "I1"', 'id: "I2"')
        bad_time = bad_time.replace(f'filed: {json.dumps(item.filed)}', 'filed: "bad"')
        for text, identity, filename in ((b"bad", "I2", "I2-2.md"), (b"\xff", "I3", "I3.md"),
                                         (bad_time.encode(), "I4", "I4.md")):
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
                if text == b"\xff":
                    with self.assertRaisesRegex(OSError, "I2.md"):
                        _ = investigation.file(OWNER.id, "Next cause", "Why?", waiting_why='Await the findings.')
                    self.assertEqual(path.read_bytes(), text)
                    path.unlink()
                next_item = investigation.file(OWNER.id, "Next cause", "Why?", waiting_why='The owner must complete the investigation.')
                self.assertEqual(next_item.id, identity)
                self.assertEqual(investigation.load(OWNER.id, identity), next_item)
                if text != b"\xff":
                    self.assertEqual(path.read_bytes(), text)
                (path.parent / filename).unlink()

    def test_load_checks_required_times_and_empty_times_for_each_state(self) -> None:
        item = investigation.file(OWNER.id, "Cause", "Why?", waiting_why='The owner must complete the investigation.')
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
