"""Test the sent item store and reply command with a private state directory."""
from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import inspect
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from queue import Queue
from threading import Event
from typing import TextIO, cast, final, override
from unittest.mock import patch
from urllib.parse import urlencode

from scripts.console_text.test_support import accepting_console_text
from scripts.agents import pane_record
from scripts.agents.remote import local_machine
from scripts.lib import atomic_file, conf, urgency
from scripts.lint.memory_test_support import write_config_fixture
from scripts.notify import review
from scripts.production import change_review, investigation, next_item, production_lifecycle, proposal_issue, proposals_folder, record_store, session_staff, showrunners, staff, tidy_wrapped, update_registration
from scripts.production.issues import Issue, frontmatter
from scripts.production.production_doc import read_production
from scripts.review_links import change_browser, change_page, image_store, new_item_dialog, question_record, reply_tokens, review_actions, review_events, review_items, review_page, review_submissions as submissions

OWNER = staff.StaffId("builds-staff")
PRODUCER = staff.StaffId("producer-staff")
PNG = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAHUlEQVQ4y2O0bH/6n4ECwESJ5lEDRg0YNWAwGQAA9/QCxAu8SdgAAAAASUVORK5CYII="
IMAGE = submissions.PastedImage("image/png", PNG)
EVIDENCE = submissions.BuildStepEvidence(
    captured_at="2026-11-17T16:14:00+00:00", step_id="build-step-50", title="hana nextest",
    owner="sdlc · handler",
    cells=(submissions.BuildCell("Stage", "testing"), submissions.BuildCell("Done", "41.5%"),
           submissions.BuildCell("Dur", "14 min"), submissions.BuildCell("Unit", "handler"),
           submissions.BuildCell("Pid", "50"), submissions.BuildCell("Target", "natedev"),
           submissions.BuildCell("vCPU", "32"), submissions.BuildCell("Slot", "2"),
           submissions.BuildCell("Tokens", "8")),
    memory=submissions.MemoryReading(18.0, 28.0, (
        submissions.MemorySegment("Builds", 10.0, submissions.MemoryTone.BUILD),
        submissions.MemorySegment("Free RAM", 30.0, submissions.MemoryTone.FREE),
        submissions.MemorySegment("Tools", 2.0, submissions.MemoryTone.TOOLS),
        submissions.MemorySegment("System", 8.0, submissions.MemoryTone.SYSTEM),
        submissions.MemorySegment("Everything else", 29.0, submissions.MemoryTone.OTHER))),
    tip_title="handler · hana nextest", tip_status="running 14 min",
    tip=(submissions.TipField("Showrunner", "sdlc"),
         submissions.TipField("Branch", "sdlc-fix-rounds"),
         submissions.TipCaption("Memory at capture time"),
         submissions.TipField("Peak RSS", "9.3 GiB"),
         submissions.TipField("Swap", "0.0 GiB"),
         submissions.TipDetail("Run 18 · Test shard 2 of 4")))


def identify_fixture_image(extension: str, data: bytes) -> subprocess.CompletedProcess[bytes]:
    if data != base64.b64decode(PNG):
        raise AssertionError("Add the image to the test fixtures.")
    return subprocess.CompletedProcess(["identify", extension], 0, stdout=b"PNG\n")


def request(kind: submissions.SubmissionKind = submissions.SubmissionKind.BUG,
            seed: str = "request-1", *, recipient: staff.StaffId = OWNER) -> submissions.NewItemRequest:
    nonce = hashlib.sha256(seed.encode()).hexdigest()[:32]
    request_id = f"r1-{nonce}-{kind}-{recipient}"
    return submissions.NewItemRequest(request_id, kind, recipient, "Check <result>\r\n\n## Answer\n\nWords",
                                      submissions.NoEvidence())


@final
class ReviewSubmissionTests(unittest.TestCase):
    root: Path = Path()
    doc: Path = Path()
    issues: Path = Path()
    members: staff.StaffSnapshot = staff.StaffSnapshot(())

    @override
    def setUp(self) -> None:
        self.enterContext(accepting_console_text())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.issues = self.root / "issues"
        self.doc = self.root / "production.md"
        _ = self.doc.write_text(f"""# Builds
> **Status: PRODUCTION — running.**

- **Merge branch:** main
- **Showrunner checkout:** `{self.root}`
- **Log:** log.md
- **Proposals:** `{self.issues}`
""")
        members = staff.StaffSnapshot((
            staff.StaffRow(OWNER, "showrunner", "builds", staff.StaffRoot(),
                           current_session=OWNER, doc=staff.LiveDoc(local_machine(), self.doc)),
            staff.StaffRow(PRODUCER, "producer", "producer", staff.StaffRoot(), current_session=PRODUCER)))
        self.members = members
        _ = self.enterContext(patch.object(submissions, "_identify_image", autospec=True,
                                           side_effect=identify_fixture_image))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=members))
        _ = self.enterContext(patch.object(change_review, "lists_directory", side_effect=AssertionError("Use fixture change lists.")))
        _ = self.enterContext(patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(patch.object(reply_tokens, "platform", "linux"))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(self.root / "config"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "key-state"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"),
            "REVIEW_LINKS_ROOT": str(self.root / "review")})))
        conf.path("machines").parent.mkdir(parents=True, exist_ok=True)
        _ = write_config_fixture(conf.path("machines"))

    @property
    def identities(self) -> update_registration.QuestionStaff:
        return update_registration.QuestionStaff.for_record(self.members)

    def questions(self) -> list[update_registration.QuestionRecord]:
        return update_registration.question_records_for_document(self.doc, self.identities)

    def proposals(self) -> list[Issue]:
        return [Issue(path, frontmatter(path)) for path in self.issues.glob("*.md")
                if not path.name.startswith("question-")]

    def test_answered_new_item_projects_one_response_from_the_same_question(self) -> None:
        payload = request(submissions.SubmissionKind.QUESTION)
        saved = submissions.submit(payload)
        waiting = review_items.submission_item(saved, self.members)
        self.assertEqual((waiting.type, waiting.state), (review_items.ReviewType.QUESTION, review_items.ReviewState.WAITING))

        answer = 'Use the desktop layout.'
        self.assertTrue(submissions.answer(saved.key, answer, saved.reply_token))
        self.assertFalse(submissions.answer(saved.key, answer, saved.reply_token))
        replay = submissions.submit(payload)
        self.assertEqual((replay.key, replay.path, replay.owner), (saved.key, saved.path, OWNER))
        native, = self.questions()
        self.assertEqual(native.path, saved.path)
        row = review_items.submission_item(replay, self.members)
        self.assertEqual((row.key, row.type, row.state), (waiting.key, review_items.ReviewType.RESPONSE,
                                                        review_items.ReviewState.OPEN))
        self.assertEqual(row.log[-1].words, 'Answer: '+answer)
        self.assertEqual(row.response, submissions.QuestionResponse(payload.words, answer, row.log[-1].time))

    def test_scoped_review_replies_release_recipient_locks_and_recheck_the_record(self) -> None:
        words = "Use the desktop layout."
        for kind in submissions.SubmissionKind:
            with self.subTest(kind=kind):
                saved = submissions.submit(request(kind))
                token = saved.reply_token

                def read_draft(*_args: object, **_kwargs: object) -> review.QuestionAccepted:
                    lock = investigation.state_root() / "investigation-locks" / (OWNER + ".lock")
                    with lock.open("r") as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if kind == submissions.SubmissionKind.PROPOSAL:
                        digest = hashlib.sha256(str(saved.path.resolve()).encode()).hexdigest()[:32]
                        issue_lock = Path.home() / ".local/state/proposal-issue/locks" / f"proposal-issue-{digest}.lock"
                        issue_lock.parent.mkdir(parents=True, exist_ok=True)
                        with issue_lock.open("a") as handle:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with submissions.scoped_submission(new_item_dialog.read_submission_key(saved.key)) as located:
                        match located.record:
                            case update_registration.QuestionRecord() as record:
                                update_registration.set_waiting_why(record, "Check the second layout.")
                            case Issue() as issue:
                                _ = proposal_issue.record_review(issue.path, "Check the second layout.",
                                                                 review_events.KeepMark(), date.today())
                            case investigation.Investigation() as record:
                                investigation.set_waiting_why(record.owner, record.id, "Check the second layout.")
                    return review.QuestionAccepted(words)

                with patch.object(review, "review_question", side_effect=read_draft) as reader:
                    with self.assertRaisesRegex(submissions.SubmissionError, "selected .* changed"):
                        _ = submissions.answer(saved.key, words, token)
                reader.assert_called_once()
                self.assertNotIn(words, saved.path.read_text())
                self.assertIn("Check the second layout.", saved.path.read_text())

                with patch.object(review, "review_question", return_value=review.QuestionAccepted(words)) as reader:
                    self.assertTrue(submissions.answer(saved.key, words, token))
                    self.assertFalse(submissions.answer(saved.key, words, token))
                reader.assert_called_once()
                self.assertIn(words, saved.path.read_text())

    def test_four_kinds_use_their_native_stores_and_ids(self) -> None:
        expected = ("Q1", "P1", "I1", "I2")
        for number, (kind, identity) in enumerate(zip(submissions.SubmissionKind, expected, strict=True), 1):
            with self.subTest(kind=kind):
                payload = request(kind, f"request-{number}")
                submitted = submissions.submit(payload)
                self.assertEqual(submitted.id, identity)
                self.assertEqual(submitted.key, f"submission/{kind}/{OWNER}/{identity}/{payload.request_id}")
                self.assertEqual(submitted.kind, kind)
                self.assertEqual(submissions.submit(payload), submitted)
                token = submitted.reply_token
                self.assertTrue(token)
                note = submitted.path.read_text(encoding="utf-8")
                self.assertIn(reply_tokens.digest(token), note)
                self.assertIn(submitted.details.reply_token_nonce, note)
                self.assertNotIn(token, note)
        question, = self.questions()
        self.assertEqual((question.item.get("review_id"), question.owner, question.item.get("question")),
                         ("Q1", OWNER, request().words))
        self.assertEqual(question.item["text"], "Check <result>")
        self.assertIn("followup", question.item)
        self.assertEqual(question.item.get("waiting_why"), "builds answers this")
        proposal, = self.proposals()
        self.assertEqual((proposal.value("proposal_id"), proposal.value("proposed_by")), ("P1", OWNER))
        self.assertTrue(proposal_issue.read_issue(proposal.path).endswith(request().words + "\n"))
        self.assertEqual(proposal_issue.review_mark(proposal), review_events.ReviewMark.WAITING)
        reports = investigation.records(OWNER)
        self.assertEqual([(item.id, item.kind, item.question) for item in reports], [
            ("I1", investigation.InvestigationKind.INVESTIGATION, request().words),
            ("I2", investigation.InvestigationKind.BUG, request().words)])
        self.assertTrue(all(item.state == investigation.InvestigationState.OPEN for item in reports))
        self.assertTrue(all(item.title == "Check <result>" for item in reports))
        self.assertFalse((self.root / "state" / OWNER / "sent-review").exists())
        sources: list[Path] = []
        self.assertEqual([item.id for item in investigation.records(OWNER, sources=sources)], ["I1", "I2"])
        self.assertIn(investigation.folder(OWNER), sources)
        self.assertIn(investigation.folder(OWNER) / "I2.md", sources)

    def test_producer_uses_its_questions_and_requires_a_proposal_folder(self) -> None:
        question = submissions.submit(request(submissions.SubmissionKind.QUESTION, recipient=PRODUCER))
        payload = request(submissions.SubmissionKind.PROPOSAL, "proposal", recipient=PRODUCER)
        with self.assertRaisesRegex(submissions.SubmissionError, "native proposal folder"):
            _ = submissions.submit(payload)
        self.assertEqual(question.id, "Q1")
        row = self.identities.record.by_id(PRODUCER)
        assert row is not None
        record, = update_registration.question_records_for_document(row, self.identities)
        self.assertEqual(record.path.parent, self.root / "state" / PRODUCER / "questions")
        self.assertFalse((self.root / "rust" / "hanadocs" / "issues").exists())

        producer_doc = self.root / "producer.md"
        vault = self.root / "producer-issues"
        _ = producer_doc.write_text(f"# Producer\n- **Proposals:** `{vault}`\n")
        producer = replace(row, doc=staff.LiveDoc(local_machine(), producer_doc))
        producer_staff = staff.StaffSnapshot((producer,))
        with patch.object(staff, 'snapshot', return_value=producer_staff):
            proposal = submissions.submit(payload, members=producer_staff)
        self.assertEqual(proposal.id, "P1")
        self.assertIsInstance(proposal_issue.find_review_proposal(vault, PRODUCER, new_item_dialog.ByItemId("P1")), Issue)

    def test_proposal_folder_is_absent_for_a_producer_directory(self) -> None:
        directory = self.root / "brain"
        directory.mkdir()
        producer = replace(self.members.rows[1], doc=staff.LiveDoc(local_machine(), directory))
        self.assertEqual(submissions.proposal_folder(producer, self.members),
                         submissions.NoNativeFolder(submissions.SubmissionKind.PROPOSAL))

    def test_concurrent_retries_reuse_the_id_for_the_same_request(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as executor:
            saved = list(executor.map(submissions.submit, [request()] * 4))
        self.assertEqual([item.id for item in saved], ["I1"] * 4)
        self.assertEqual(len({item.reply_token for item in saved}), 1)
        for different in (replace(request(), words="A different report"),
                          replace(request(), kind=submissions.SubmissionKind.PROPOSAL)):
            with self.assertRaisesRegex(submissions.SubmissionConflict, "different item data"):
                _ = submissions.submit(different)
        self.assertEqual([item.id for item in investigation.records(OWNER)], ["I1"])

    def test_concurrent_draft_sends_reuse_each_recipient_item(self) -> None:
        first = replace(request(), request_id="r2-" + "a" * 32)
        second = replace(first, recipient_id=PRODUCER)
        with ThreadPoolExecutor(max_workers=4) as executor:
            saved, other, replay, other_replay = executor.map(submissions.submit, (first, second, first, second))
        self.assertEqual(saved.key, replay.key)
        self.assertEqual(other.key, other_replay.key)
        self.assertNotEqual(saved.key, other.key)
        self.assertEqual([record.id for record in investigation.records(OWNER)], [saved.id])
        self.assertEqual([record.id for record in investigation.records(PRODUCER)], [other.id])

    def test_f021_page_send_reuses_standard_item_filed_with_parent(self) -> None:
        _ = self.wrap_fixture()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.root / "config")})))
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={PRODUCER}\n")
        standard = replace(request(), request_id="r2-" + "c" * 32)
        saved = submissions.submit(standard)
        self.assertEqual((saved.recipient_id, saved.owner), (OWNER, PRODUCER))

        page = replace(standard, recipient_id=PRODUCER, page="review")
        self.assertEqual(submissions.submit(page).key, saved.key)
        self.assertEqual(submissions.submit(replace(page, page="")).key, saved.key)

        # Remove the index to exercise receipts saved before it existed.
        index = submissions.image_folder(saved).parent.with_name(f"r4-{saved.payload_hash}")
        for marker in (".indexed", f"recipient-{PRODUCER}", "page-review"):
            (index / marker).unlink()
        self.assertEqual(submissions.submit(page).key, saved.key)
        self.assertEqual([record.id for record in investigation.records(PRODUCER)], [saved.id])
        self.assertEqual(investigation.records(OWNER), [])

    def test_f022_page_retry_keeps_reused_standard_item_after_owner_retires(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.root / "config")})))
        _ = write_config_fixture(conf.path("console"), f"page.review.owner={OWNER}\n")
        standard = replace(request(), request_id="r2-" + "d" * 32)
        saved = submissions.submit(standard)
        page = replace(standard, page="review")
        self.assertEqual(submissions.submit(page).key, saved.key)

        _ = self.wrap_fixture()
        retry = replace(page, recipient_id=PRODUCER)
        self.assertEqual(submissions.submit(retry).key, saved.key)
        self.assertEqual([record.id for record in investigation.records(OWNER)], [saved.id])
        self.assertEqual(investigation.records(PRODUCER), [])

    def test_list_producer_uses_native_stores_without_its_staff_document(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.root / "config")})))
        _ = write_config_fixture(conf.path("console"),
                                 f"page.review.owner={OWNER}\npage.list.owner={PRODUCER}\n")
        morty = replace(self.members.rows[1], name="morty")
        members = replace(self.members, rows=(self.members.rows[0], morty))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=members))
        for at, kind in enumerate((submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.INVESTIGATION,
                                   submissions.SubmissionKind.BUG)):
            with self.subTest(kind=kind):
                standard = replace(request(kind, recipient=PRODUCER), request_id=f"r2-{at:032x}")
                selected = replace(standard, page="review", page_recipient=new_item_dialog.PageRecipient.LIST_OWNER)
                saved = submissions.submit(selected)
                folder = (record_store.default_question_folder(PRODUCER) if kind == submissions.SubmissionKind.QUESTION
                          else investigation.folder(PRODUCER))
                self.assertTrue(saved.path.is_relative_to(folder))
                self.assertEqual(saved.owner, morty.id)
                self.assertEqual(submissions.submit(selected).key, saved.key)
                self.assertEqual(submissions.submit(standard).key, saved.key)
                default = replace(selected, recipient_id=OWNER, page_recipient=new_item_dialog.PageRecipient.PAGE_OWNER)
                self.assertNotEqual(submissions.submit(default).key, saved.key)
        with self.assertRaisesRegex(submissions.SubmissionConflict, "List owner is unavailable"):
            invalid = replace(request(recipient=OWNER), page="review",
                              page_recipient=new_item_dialog.PageRecipient.LIST_OWNER)
            _ = submissions.submit(invalid)

    def test_draft_id_files_changed_content_before_and_after_native_write(self) -> None:
        original = replace(request(), request_id="r2-" + "b" * 32, images=(IMAGE,))
        changed_words = replace(original, words="A different report")
        changes = (replace(original, kind=submissions.SubmissionKind.INVESTIGATION),
                   replace(original, recipient_id=PRODUCER),
                   replace(original, images=()))
        with patch.object(record_store.NumberedRecords, "write", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                _ = submissions.submit(original)
        self.assertEqual(investigation.records(OWNER), [])
        changed = submissions.submit(changed_words)
        saved = submissions.submit(original)
        self.assertNotEqual(changed.key, saved.key)
        saved_keys = {changed.key, saved.key}
        for change in changes:
            with self.subTest(change=change):
                item = submissions.submit(change)
                self.assertNotIn(item.key, saved_keys)
                saved_keys.add(item.key)
                self.assertEqual(submissions.submit(change).key, item.key)
        self.assertEqual(submissions.submit(original).key, saved.key)
        self.assertEqual(len(investigation.records(OWNER)), 4)
        self.assertEqual(len(investigation.records(PRODUCER)), 1)

    def test_evidence_and_image_survive_each_native_store_round_trip(self) -> None:
        for kind in submissions.SubmissionKind:
            with self.subTest(kind=kind):
                original = replace(request(kind, kind), evidence=EVIDENCE, images=(IMAGE,))
                payload = asdict(original)
                submitted = submissions.submit(submissions.read_request(payload))
                match kind:
                    case submissions.SubmissionKind.QUESTION:
                        question, = self.questions()
                        saved = new_item_dialog.read_submission(question.item.get("review_submission"))
                    case submissions.SubmissionKind.PROPOSAL:
                        proposal, = self.proposals()
                        saved = proposal_issue.review_submission(proposal)
                    case _:
                        saved = investigation.load(OWNER, submitted.id).submission
                assert isinstance(saved, new_item_dialog.ReviewSubmission)
                self.assertEqual(saved.evidence, EVIDENCE)
                self.assertEqual(saved.request_id, original.request_id)
                self.assertEqual(saved, submitted.details)
                self.assertEqual(len(saved.images), 1)
                image, = saved.images
                folder = submissions.image_folder(submitted) / image.id
                self.assertEqual((folder / image.file).read_bytes(), base64.b64decode(PNG))
                self.assertEqual(image_store.read_record(folder)["subject"], "Review request " + original.request_id)
                self.assertEqual(submissions.submit(submissions.read_request(payload)), submitted)
                self.assertEqual(list(submissions.image_folder(submitted).iterdir()), [folder])

    def test_interrupted_write_keeps_the_images_and_skips_the_reserved_number(self) -> None:
        payload = replace(request(), images=(IMAGE,))
        folder = self.root / "review-attachments" / payload.request_id
        with patch.object(record_store.NumberedRecords, "write", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                _ = submissions.submit(payload)
        self.assertFalse((folder / "receipt.json").exists())
        self.assertFalse(investigation.records(OWNER))
        image, = (folder / "images").glob("*/image.png")
        before = image.stat()
        self.assertEqual(image.read_bytes(), base64.b64decode(PNG))
        with self.assertRaisesRegex(submissions.SubmissionConflict, "different item data"):
            _ = submissions.submit(replace(payload, words="Different words"))
        saved = submissions.submit(payload)
        self.assertEqual(saved.id, "I2")
        self.assertEqual(len(saved.details.images), 1)
        self.assertEqual(image.stat().st_ino, before.st_ino)
        self.assertEqual(image.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(image.read_bytes(), base64.b64decode(PNG))

    def test_retry_uses_the_next_id_after_another_request_filed(self) -> None:
        bug = replace(request(), images=(IMAGE,))
        with patch.object(record_store.NumberedRecords, "write", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                _ = submissions.submit(bug)
        report = submissions.submit(request(submissions.SubmissionKind.INVESTIGATION))
        self.assertEqual(report.id, "I2")
        saved = submissions.submit(bug)
        self.assertEqual(saved.id, "I3")
        self.assertEqual(investigation.load(OWNER, report.id).kind, investigation.InvestigationKind.INVESTIGATION)
        self.assertEqual(investigation.load(OWNER, saved.id).kind, investigation.InvestigationKind.BUG)
        image, = saved.details.images
        self.assertEqual((submissions.image_folder(saved) / image.id / image.file).read_bytes(), base64.b64decode(PNG))

    def test_retry_keeps_the_images_after_rolling_back_a_published_record(self) -> None:
        payload = replace(request(), images=(IMAGE,))
        sync = atomic_file.sync_folder

        def stop_after_link(path: Path) -> None:
            if path == investigation.folder(OWNER) and (path / "I1.md").exists():
                raise OSError("directory sync interrupted")
            sync(path)

        with patch.object(record_store, "sync_folder", side_effect=stop_after_link):
            with self.assertRaisesRegex(OSError, "directory sync interrupted"):
                _ = submissions.submit(payload)
        self.assertEqual(investigation.records(OWNER), [])
        saved = submissions.submit(payload)
        self.assertEqual(saved.id, "I2")
        image, = saved.details.images
        self.assertTrue((submissions.image_folder(saved) / image.id / image.file).is_file())

    def test_interrupted_receipt_recovers_each_native_record_without_filing_again(self) -> None:
        for kind, identity in zip(submissions.SubmissionKind, ("Q1", "P1", "I1", "I2"), strict=True):
            with self.subTest(kind=kind):
                payload = replace(request(kind), images=(IMAGE,))
                errors = StringIO()
                with (patch.object(submissions, "_publish_receipt", side_effect=OSError("Receipt interrupted.")),
                      redirect_stderr(errors)):
                    first = submissions.submit(payload)
                self.assertEqual(errors.getvalue(), f"Review receipt for {identity}: Receipt interrupted.\n")
                with (patch.object(staff, "snapshot", side_effect=AssertionError("Retry read staff.")),
                      patch.object(proposal_issue, "find_review_proposal",
                                   side_effect=AssertionError("Retry scanned proposals.")),
                      patch.object(update_registration, "find_review_question_in_folder",
                                   side_effect=AssertionError("Retry scanned questions.")),
                      patch.object(investigation, "file", side_effect=AssertionError("Retry filed a report."))):
                    saved = submissions.submit(payload)
                self.assertEqual(saved, first)
                self.assertEqual(saved.id, identity)
                self.assertTrue(saved.path.is_file())
                image_dir = submissions.image_folder(saved)
                self.assertFalse((image_dir.parent / "pending.json").exists())
                self.assertTrue((image_dir.parent / "receipt.json").is_file())
                image, = saved.details.images
                self.assertEqual((image_dir / image.id / image.file).read_bytes(), base64.b64decode(PNG))

    def test_reply_command_checks_tokens_and_replays_on_each_native_store(self) -> None:
        status = change_page.DeliveryStatus(self.root / "review")
        second = submissions.submit(request(seed="second"))
        untouched = investigation.load(OWNER, second.id)
        key = reply_tokens.path()
        original_key = key.read_bytes()
        original_token = second.reply_token
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        home = self.root / "fixture-home"
        inside = self.root / "review" / "state"
        alias = self.root / "state-alias"
        alias.symlink_to(inside, target_is_directory=True)
        for environment, overlapping_key in (
            ({"XDG_STATE_HOME": str(alias), "REVIEW_LINKS_ROOT": str(inside.parent)},
             inside / "review-links" / key.name),
            ({"REVIEW_LINKS_ROOT": str(home / ".local")}, home / ".local/state/review-links" / key.name),
        ):
            with (self.subTest(environment=environment),
                  patch.dict(os.environ, environment, clear=True),
                  patch.object(Path, "home", return_value=home),
                  patch.object(reply_tokens, "platform", "darwin"),
                  patch.object(subprocess, "run", side_effect=AssertionError("The server called SSH."))):
                with self.assertRaisesRegex(reply_tokens.UnavailableReplyToken, "outside the served Review Links root"):
                    _ = reply_tokens.mint()
                self.assertFalse(overlapping_key.parent.exists())
                overlapping_key.parent.mkdir(parents=True)
                _ = overlapping_key.write_bytes(original_key)
                overlapping_key.chmod(0o600)
                with self.assertRaisesRegex(reply_tokens.UnavailableReplyToken, "outside the served Review Links root"):
                    _ = reply_tokens.derive(second.details.reply_proof)
        served = self.root / "review"
        spelling = self.root / "review-alias"
        spelling.symlink_to(served, target_is_directory=True)
        state = spelling / "alias-state"
        spelling_key = state / "review-links" / key.name
        resolve = Path.resolve

        def preserve_spelling(path: Path, strict: bool = False) -> Path:
            # Simulate a file system that preserves the supplied case.
            return path if path == spelling_key else resolve(path, strict=strict)

        with (patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}), reply_tokens.serving(served),
              patch.object(Path, "resolve", autospec=True, side_effect=preserve_spelling)):
            with self.assertRaisesRegex(reply_tokens.UnavailableReplyToken, "outside the served Review Links root"):
                _ = reply_tokens.mint()
            self.assertFalse(state.exists())
        mac_key = key.parent / "mac" / key.name
        with (patch.object(reply_tokens, "platform", "darwin"),
              patch.object(reply_tokens, "path", return_value=mac_key),
              patch.object(subprocess, "run", autospec=True) as ssh):
            ssh.return_value = subprocess.CompletedProcess([], 0, b"short", b"")
            with self.assertRaisesRegex(reply_tokens.UnavailableReplyToken, "must contain 32 bytes"):
                _ = reply_tokens.mint()
            self.assertFalse(mac_key.exists())
            ssh.side_effect = subprocess.CalledProcessError(1, "ssh")
            with self.assertRaisesRegex(reply_tokens.UnavailableReplyToken, "Cannot copy the reply key from natedev"):
                _ = reply_tokens.derive(second.details.reply_proof)
            self.assertFalse(mac_key.exists())
            ssh.side_effect = None
            ssh.reset_mock()
            ssh.return_value = subprocess.CompletedProcess([], 0, original_key, b"")
            self.assertEqual(reply_tokens.derive(second.details.reply_proof), original_token)
            proof = reply_tokens.mint()
            token = reply_tokens.derive(proof)
            ssh.assert_called_once()
            self.assertEqual(ssh.call_args.args[0][:4], ["ssh", "-o", "BatchMode=yes", "natedev"])
            self.assertEqual(ssh.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(mac_key.read_bytes(), original_key)
            self.assertEqual(mac_key.stat().st_mode & 0o777, 0o600)

            def copy_with_race(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                _ = mac_key.write_bytes(original_key)
                mac_key.chmod(0o600)
                return subprocess.CompletedProcess([], 0, b"x" * len(original_key), b"")

            mac_key.unlink()
            ssh.side_effect = copy_with_race
            self.assertEqual(reply_tokens.derive(second.details.reply_proof), original_token)
            self.assertEqual(mac_key.read_bytes(), original_key)
        self.assertEqual(reply_tokens.derive(proof), token)

        def accept(_key: review.QuestionKey, draft: str, **_kwargs: object) -> review.QuestionAccepted:
            return review.QuestionAccepted(draft)

        for kind in submissions.SubmissionKind:
            with (self.subTest(kind=kind), patch.object(review, "review_question", side_effect=accept) as reader):
                first = submissions.submit(request(kind, kind))
                reader.reset_mock()
                proof = first.details.reply_proof
                token = reply_tokens.derive(proof)
                self.assertEqual(token, first.reply_token)
                self.assertEqual(key.read_bytes(), original_key)
                for invalid in ("", second.reply_token, "✓"):
                    with self.subTest(token=invalid):
                        with self.assertRaisesRegex(question_record.AnswerRefused, "token does not match"):
                            _ = change_page.answer_item(first.key, "An answer", invalid, status)
                with patch.object(reply_tokens, "path", return_value=key.with_suffix(".absent")):
                    self.assertTrue(change_page.answer_item(first.key, "An answer", token, status))
                    self.assertFalse(change_page.answer_item(first.key, "An answer", token, status))
                    with self.assertRaisesRegex(question_record.AnswerRefused, "no longer accepts"):
                        _ = change_page.answer_item(first.key, "New answer", token, status)
                reader.assert_called_once()
                match kind:
                    case submissions.SubmissionKind.QUESTION:
                        answered, = self.questions()
                        self.assertNotIn("followup", answered.item)
                        self.assertEqual(answered.item.get("followups"), [
                            {"followup": request().words, "reply": "An answer"}])
                    case submissions.SubmissionKind.PROPOSAL:
                        issue, = self.proposals()
                        self.assertEqual(proposal_issue.review_log(issue)[-1].words, "Answer: An answer")
                        self.assertIsInstance(proposal_issue.review_mark(issue), review_events.NoMark)
                    case _:
                        report = investigation.load(OWNER, first.id)
                        self.assertEqual(report.state, investigation.InvestigationState.ANSWER_READY)
                        self.assertEqual([event.words for event in report.log], ["Sent", "Answer: An answer"])
                        read = investigation.mark_read(OWNER, first.id)
                        self.assertEqual(read.state, investigation.InvestigationState.READ)
                        self.assertEqual(investigation.mark_read(OWNER, first.id), read)
                        with self.assertRaisesRegex(question_record.AnswerRefused, "no longer accepts"):
                            _ = change_page.answer_item(first.key, "An answer", first.reply_token, status)
        self.assertEqual(investigation.load(OWNER, second.id), untouched)

    def test_scrub_preserves_notes_and_legacy_replies_without_a_key(self) -> None:
        opened = [submissions.submit(request(kind, "open")) for kind in submissions.SubmissionKind]
        archived = opened[-1]
        archive = archived.path.parent / "archive"
        archive.mkdir(exist_ok=True)
        opened[-1] = replace(archived, path=archived.path.rename(archive / archived.path.name))
        closed = submissions.submit(request(submissions.SubmissionKind.QUESTION, "closed"))
        self.assertTrue(submissions.answer(closed.key, "The owner replied.", closed.reply_token))
        record = update_registration.read_review_question(closed.path)
        update_registration.review_sent_question(record, read_production(OWNER, self.members), "read", "")
        notes = [*opened, closed]
        tokens = {item.key: item.reply_token for item in notes}
        originals: dict[Path, bytes] = {}
        for item in notes:
            blob = item.path.read_bytes()
            for quote in ('"', r'\"'):
                blob = blob.replace(
                    f', {quote}reply_token_nonce{quote}: {quote}{item.details.reply_token_nonce}{quote}'.encode(), b"")
            self.assertNotIn(b"reply_token_nonce", blob)
            originals[item.path] = blob
            legacy = blob.replace(b"reply_token_sha256", b"reply_token", 1)
            _ = item.path.write_bytes(legacy.replace(item.details.reply_token_sha256.encode(),
                                                      tokens[item.key].encode(), 1))

        key = reply_tokens.path()
        previous_key = key.with_suffix(".previous")
        _ = key.rename(previous_key)
        note = update_registration.read_review_question(opened[0].path)
        legacy_details = new_item_dialog.read_submission(note.item.get("review_submission"))
        new_item_dialog.require_reply_token(legacy_details, tokens[opened[0].key])
        with self.assertRaisesRegex(submissions.SubmissionError, "token does not match"):
            new_item_dialog.require_reply_token(legacy_details, tokens[closed.key])

        with redirect_stdout(StringIO()) as output:
            self.assertEqual(submissions.main(["scrub-reply-tokens"]), 0)
        self.assertCountEqual(output.getvalue().splitlines(), [f"Rewrote {item.path}" for item in notes])
        for item in notes:
            self.assertEqual(item.path.read_bytes(), originals[item.path])
        before = {item.path: item.path.stat().st_mtime_ns for item in notes}
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(submissions.main(["scrub-reply-tokens"]), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual({item.path: item.path.stat().st_mtime_ns for item in notes}, before)
        self.assertFalse(key.exists())

        archived = opened[-1]
        self.assertTrue(submissions.answer(archived.key, "Reply while archived.", tokens[archived.key]))
        status = change_page.DeliveryStatus(key.parent)
        for item in opened[:-1]:
            with self.subTest(kind=item.kind):
                saved = submissions.submit(request(item.kind, "open"))
                self.assertEqual(saved.details.reply_token_nonce, "")
                delivery = review_actions.prepare_submission_delivery(saved, saved.words, status)
                assert isinstance(delivery, change_page.MessageDelivery)
                current = submissions.submit(request(item.kind, "open"))
                self.assertTrue(reply_tokens.valid_nonce(current.details.reply_token_nonce))
                token = current.reply_token
                self.assertIn(change_page.reply_command(current.key, token), change_page.outbound_review_message(delivery).text)
                self.assertNotIn(token.encode(), current.path.read_bytes())
                with self.assertRaisesRegex(submissions.SubmissionError, "token does not match"):
                    _ = submissions.answer(current.key, "The old token fails.", tokens[item.key])
                self.assertTrue(submissions.answer(current.key, "The new token works.", token))
                with patch.object(reply_tokens, "path", return_value=key.with_suffix(".absent")):
                    answered = submissions.submit(request(item.kind, "open"))
                    self.assertIsInstance(review_actions.prepare_submission_delivery(answered, answered.words, status),
                                          change_page.NoMessageDelivery)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)

    def test_f001_generated_reply_command_uses_the_derived_token(self) -> None:
        saved = submissions.submit(request())
        self.assertRegex(saved.reply_token, r"^r[A-Za-z0-9_-]{42}$")
        command = shlex.split(change_page.reply_command(saved.key, saved.reply_token))
        command[-1] = "The command works."
        self.assertEqual(change_page.main(command[2:]), 0)
        self.assertEqual(investigation.load(OWNER, saved.id).answer, "The command works.")

    def test_f002_reuse_for_another_recipient_is_refused(self) -> None:
        payload = request()
        saved = submissions.submit(payload)
        with self.assertRaisesRegex(submissions.SubmissionConflict, "different item data") as conflict:
            _ = submissions.submit(replace(payload, recipient_id=PRODUCER))
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual(submissions.submit(payload), saved)
        self.assertEqual(investigation.records(PRODUCER), [])

    def test_f003_proposal_requires_the_recipients_native_folder(self) -> None:
        eligible = {option["id"]: option["proposal"] for option in submissions.recipient_options(self.members)}
        self.assertEqual(eligible, {OWNER: True, PRODUCER: False})
        _ = self.doc.write_text("# Builds\n> **Status: PRODUCTION — running.**\n")
        with self.assertRaisesRegex(submissions.SubmissionError, "native proposal folder"):
            _ = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        self.assertFalse((self.root / "rust" / "hanadocs" / "issues").exists())
        eligible = {option["id"]: option["proposal"] for option in submissions.recipient_options(self.members)}
        self.assertEqual(eligible, {OWNER: False, PRODUCER: False})

    def test_f004_lookup_requires_one_selector(self) -> None:
        for lookup in (update_registration.find_review_question_in_folder, proposal_issue.find_review_proposal):
            with self.subTest(lookup=lookup.__name__):
                parameters = tuple(inspect.signature(lookup).parameters.values())
                selectors = [parameter for parameter in parameters if parameter.name == "lookup"]
                self.assertEqual(len(selectors), 1)
                self.assertEqual(selectors[0].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                self.assertIs(cast(object, selectors[0].default), inspect.Parameter.empty)

    def test_f005_unreadable_proposal_cannot_verify_the_completed_receipt(self) -> None:
        payload = replace(request(submissions.SubmissionKind.PROPOSAL), images=(IMAGE,))
        saved = submissions.submit(payload)
        issue, = self.proposals()
        image, = saved.details.images
        folder = submissions.image_folder(saved)
        receipt = (folder.parent / "receipt.json").read_bytes()
        original = issue.path.read_bytes()
        _ = issue.path.write_bytes(b"---\n\xff\n")
        try:
            with self.assertRaisesRegex(OSError, "Cannot read the native Review record"):
                _ = submissions.submit(payload)
            self.assertEqual((folder.parent / "receipt.json").read_bytes(), receipt)
            self.assertEqual((folder / image.id / image.file).read_bytes(), base64.b64decode(PNG))
        finally:
            _ = issue.path.write_bytes(original)
        self.assertEqual(self.proposals(), [issue])
        self.assertEqual(submissions.submit(payload), saved)
        image, = saved.details.images
        self.assertEqual((submissions.image_folder(saved) / image.id / image.file).read_bytes(), base64.b64decode(PNG))

    def test_f005_unreadable_proposal_cannot_complete_a_pending_receipt(self) -> None:
        payload = replace(request(submissions.SubmissionKind.PROPOSAL), images=(IMAGE,))
        with (patch.object(submissions, "_publish_receipt", side_effect=OSError("Receipt interrupted.")),
              redirect_stderr(StringIO())):
            first = submissions.submit(payload)
        issue, = self.proposals()
        original = issue.path.read_bytes()
        _ = issue.path.write_bytes(b"---\n\xff\n")
        try:
            with self.assertRaises(OSError):
                _ = submissions.submit(payload)
        finally:
            _ = issue.path.write_bytes(original)
        self.assertEqual(self.proposals(), [issue])
        saved = submissions.submit(payload)
        self.assertEqual(saved, first)
        self.assertEqual(saved.id, "P1")
        image, = saved.details.images
        self.assertEqual((submissions.image_folder(saved) / image.id / image.file).read_bytes(), base64.b64decode(PNG))

    def test_f006_active_proposal_refuses_the_waiting_reply(self) -> None:
        saved = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        issue, = self.proposals()
        _ = proposal_issue.edit(issue.path, {"stage": proposal_issue.IN_PROGRESS_STAGE}, date(2026, 11, 17))
        active = issue.path.read_bytes()
        with self.assertRaisesRegex(submissions.SubmissionError, "no longer accepts"):
            _ = submissions.answer(saved.key, "Stale reply", saved.reply_token)
        self.assertEqual(issue.path.read_bytes(), active)
        self.assertIsInstance(proposal_issue.review_mark(self.proposals()[0]), review_events.NoMark)
        text, _ = proposal_issue.set_fields(active.decode(),
                                           {proposal_issue.REVIEW_MARK_FIELD: review_events.ReviewMark.WAITING}, issue.path)
        _ = issue.path.write_text(text)
        with self.assertRaisesRegex(submissions.SubmissionError, "no longer accepts"):
            _ = submissions.answer(saved.key, "Stale reply", saved.reply_token)
        self.assertEqual(issue.path.read_bytes(), text.encode())
        self.assertEqual(review_items.proposal_state(self.proposals()[0]), review_items.ReviewState.DONE)

    def test_f007_wrapping_a_recipient_preserves_retries(self) -> None:
        row = self.members.by_id(OWNER)
        assert row is not None
        wrapped = staff.StaffSnapshot((replace(row, doc=staff.WrappedDoc(local_machine(), self.doc)),))
        for kind in submissions.SubmissionKind:
            with self.subTest(kind=kind):
                payload = request(kind)
                saved = submissions.submit(payload)
                self.assertEqual(submissions.submit(payload, members=wrapped), saved)
                with (patch.object(staff, "snapshot", return_value=wrapped),
                      self.assertRaisesRegex(submissions.SubmissionConflict, "builds has finished; choose another recipient")):
                    _ = submissions.submit(request(kind, "new"), members=wrapped)

    def test_f008_retry_preserves_the_attachment_without_native_request_metadata(self) -> None:
        payload = replace(request(submissions.SubmissionKind.PROPOSAL), images=(IMAGE,))
        saved = submissions.submit(payload)
        issue, = self.proposals()
        image, = saved.details.images
        attachment = submissions.image_folder(saved) / image.id / image.file
        original = proposal_issue.read_issue(issue.path)
        text, _ = proposal_issue.set_fields(original, {"review_submission": ""}, issue.path)
        _ = issue.path.write_text(text)
        with self.assertRaisesRegex(OSError, "Cannot find the native record"):
            _ = submissions.submit(payload)
        self.assertEqual([item.value("proposal_id") for item in self.proposals()], ["P1"])
        self.assertEqual(attachment.read_bytes(), base64.b64decode(PNG))
        _ = issue.path.write_text(original)
        self.assertEqual(submissions.submit(payload), saved)

    def test_f009_retry_keeps_the_original_proposal_folder(self) -> None:
        payload = replace(request(submissions.SubmissionKind.PROPOSAL), images=(IMAGE,))
        saved = submissions.submit(payload)
        image, = saved.details.images
        attachment = submissions.image_folder(saved) / image.id / image.file
        new_folder = self.root / "new-issues"
        new_folder.mkdir()
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(new_folder)))
        with (patch.object(staff, "snapshot", side_effect=AssertionError("Retry read staff.")),
              patch.object(proposal_issue, "find_review_proposal", side_effect=AssertionError("Retry scanned proposals."))):
            self.assertEqual(submissions.submit(payload), saved)
        self.assertEqual(saved.path.parent, self.issues)
        receipt = cast(dict[str, str], json.loads((submissions.image_folder(saved).parent / "receipt.json").read_text()))
        self.assertEqual(receipt, {"destination": "native", "id": "P1", "kind": "proposal", "recipient": OWNER,
                                   "path": str(saved.path), "original_path": str(saved.path),
                                   "payload_hash": saved.payload_hash})
        self.assertEqual(list(new_folder.glob("*.md")), [])
        self.assertEqual([issue.value("proposal_id") for issue in self.proposals()], ["P1"])
        self.assertEqual(attachment.read_bytes(), base64.b64decode(PNG))

    def test_f010_wrap_cleanup_keeps_attachments_and_receipt(self) -> None:
        payload = replace(request(submissions.SubmissionKind.PROPOSAL), images=(IMAGE,))
        saved = submissions.submit(payload)
        image, = saved.details.images
        attachment = submissions.image_folder(saved) / image.id / image.file
        state = self.root / "state"
        owned = state / OWNER
        owned.mkdir(parents=True, exist_ok=True)
        _ = (owned / "example").write_text("Wrap removes this state.")
        row = self.members.by_id(OWNER)
        assert row is not None
        wrapped = staff.StaffSnapshot((replace(row, doc=staff.WrappedDoc(local_machine(), self.doc)),))
        _ = self.doc.write_text(self.doc.read_text().replace("— running.**", "— wrapped.**"))
        with (patch.object(staff, "snapshot", return_value=wrapped),
              patch.object(tidy_wrapped, "STATE_DIR", state),
              patch.object(tidy_wrapped, "NOTIFIER_STATE_DIR", self.root / "notifier"),
              patch("scripts.copies.copies.remove_trees")):
            self.assertEqual(tidy_wrapped.main([str(self.doc)]), 0)
        self.assertFalse(owned.exists())
        self.assertEqual(attachment.read_bytes(), base64.b64decode(PNG))
        self.assertEqual(submissions.submit(payload, members=wrapped), saved)

    def test_f011_plain_markdown_does_not_block_proposal_filing(self) -> None:
        self.issues.mkdir()
        _ = (self.issues / "README.md").write_text("# Vault instructions\n\nproposal_id: P99\n")
        unreadable = self.issues / "unreadable.md"
        _ = unreadable.write_bytes(b"\xff")
        payload = request(submissions.SubmissionKind.PROPOSAL)
        with self.assertRaisesRegex(OSError, "Cannot read record .*unreadable.md"):
            _ = submissions.submit(payload)
        self.assertCountEqual(self.issues.glob("*.md"), (unreadable, self.issues / "README.md"))
        unreadable.unlink()
        saved = submissions.submit(payload)
        self.assertEqual(saved.id, "P1")

    def test_f012_retry_uses_the_native_path_after_wrap_moves_the_document(self) -> None:
        active_doc = self.root / "docs" / "productions" / self.doc.name
        active_doc.parent.mkdir(parents=True)
        _ = active_doc.write_text(self.doc.read_text().replace(str(self.issues), "issues"))
        row = self.members.by_id(OWNER)
        assert row is not None
        row = replace(row, doc=staff.LiveDoc(local_machine(), active_doc))
        active = staff.StaffSnapshot((row,))
        production = read_production(row)
        native_folder = proposals_folder.issues_folder(active_doc)
        assert native_folder is not None
        with patch.object(staff, "snapshot", return_value=active):
            payload = replace(request(submissions.SubmissionKind.PROPOSAL), images=(IMAGE,))
            saved = submissions.submit(payload)
        _ = active_doc.write_text(active_doc.read_text().replace("— running.**", "— wrapped.**"))
        _ = subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        _ = subprocess.run(["git", "-C", str(self.root), "add", "--", str(active_doc)], check=True)
        checked = production_lifecycle.checked

        def wrap_command(checkout: Path, step: str, *arguments: str) -> str:
            if arguments == ("rev-parse", "HEAD"):
                return "fixture-tip"
            return checked(checkout, step, *arguments)

        wrapped_row = replace(row, doc=staff.WrappedDoc(local_machine(),
            self.root / "docs" / "archive" / self.root.name / row.name / active_doc.name))
        assert isinstance(wrapped_row.doc, staff.WrappedDoc)
        with (patch.object(staff, "snapshot", return_value=active),
              patch.object(staff, "set_doc", side_effect=OSError("Stop before the wrap commit.")) as set_doc,
              patch.object(production_lifecycle, "checkout_ready", return_value=production_lifecycle.PlanLanded("fixture-tip")),
              patch.object(production_lifecycle, "merged_checkpoints", return_value=()),
              patch.object(production_lifecycle, "checked", side_effect=wrap_command)):
            with self.assertRaisesRegex(OSError, "Stop before the wrap commit"):
                _ = production_lifecycle.wrap(production, active_doc.read_text().splitlines(), True,
                                              production_lifecycle.VerdictMissing(), production_lifecycle.VerdictMissing(), ())
            set_doc.assert_called_once_with(OWNER, wrapped_row.doc)
        self.assertFalse(active_doc.exists())
        self.assertTrue(wrapped_row.doc.path.is_file())
        self.assertNotEqual(proposals_folder.issues_folder(wrapped_row.doc.path), native_folder)
        self.assertEqual(submissions.submit(payload, members=staff.StaffSnapshot((wrapped_row,))), saved)
        self.assertEqual(saved.path.parent, native_folder.resolve())
        image, = saved.details.images
        self.assertEqual((submissions.image_folder(saved) / image.id / image.file).read_bytes(), base64.b64decode(PNG))

    def replace_review_wait(self, saved: submissions.SavedSubmission, words: str,
                            status: change_page.DeliveryStatus) -> list[str]:
        if saved.kind == submissions.SubmissionKind.QUESTION:
            record, = self.questions()
            item = review_items.question_item(record, self.members)
        elif saved.kind == submissions.SubmissionKind.PROPOSAL:
            issue, = self.proposals()
            item = review_items.proposal_item(f"proposal/{OWNER}/{saved.id}", issue, OWNER, OWNER,
                                              "Proposal", datetime.now(timezone.utc), self.members)
        else:
            item = next(item for item in review_items.investigation_items(self.members, owners=(OWNER,))
                        if item.key.endswith("/" + saved.id))
        if saved.kind == submissions.SubmissionKind.QUESTION and item.state == review_items.ReviewState.OPEN:
            delivery, _ = review_actions.question_action(item, "question", words, status)
        else:
            browser = change_page.ReviewBrowser("fixture-token", "")
            body = urlencode({"row": item.key, "token": change_browser.row_token(browser, item.key),
                              "outcome": "question", "feedback": words})
            with (patch.object(change_browser, "browser_manifest", return_value=(browser.token, browser.initial)),
                  patch.object(change_review, "record_review_log",
                               side_effect=AssertionError("A sent item used a change list.")),
                  patch.object(review_items, "read", return_value=[item])):
                delivery, _ = review_page.submit(status.root, body, status)
        assert isinstance(delivery, change_page.MessageDelivery)
        return shlex.split(change_page.outbound_review_message(delivery).text.splitlines()[-1])[2:]

    def test_f013_replacement_voids_the_first_token(self) -> None:
        status = change_page.DeliveryStatus(self.root / "review")
        for kind in submissions.SubmissionKind:
            with self.subTest(kind=kind):
                saved = submissions.submit(request(kind))
                original = saved.reply_token
                command = self.replace_review_wait(saved, "Check again", status)
                token = command[command.index("--question") + 1]
                current = submissions.submit(request(kind))
                self.assertEqual(current.reply_token, token)
                self.assertNotEqual(token, original)
                self.assertNotEqual(current.details.reply_token_nonce, saved.details.reply_token_nonce)
                before = saved.path.read_bytes()
                with self.assertRaisesRegex(question_record.AnswerRefused, "token does not match"):
                    _ = change_page.answer_item(saved.key, "Stale reply", original, status)
                self.assertEqual(saved.path.read_bytes(), before)

    def test_f013_replacement_uses_its_own_reply_command(self) -> None:
        status = change_page.DeliveryStatus(self.root / "review")
        for kind in submissions.SubmissionKind:
            with self.subTest(kind=kind):
                saved = submissions.submit(request(kind))
                previous = saved.reply_token
                for reply in ("Answer to the replacement", "Answer to the next exchange"):
                    command = self.replace_review_wait(saved, "Check again", status)
                    token = command[command.index("--question") + 1]
                    self.assertNotEqual(token, previous)
                    with self.assertRaisesRegex(question_record.AnswerRefused, "token does not match"):
                        _ = change_page.answer_item(saved.key, reply, previous, status)
                    command[-1] = reply
                    items = review_items.investigation_items(self.members, owners=(OWNER,))
                    with patch.object(review_items, "read", return_value=items):
                        self.assertEqual(change_page.main(command), 0)
                    self.assertFalse(change_page.answer_item(saved.key, reply, token, status))
                    previous = token
                if kind in (submissions.SubmissionKind.INVESTIGATION, submissions.SubmissionKind.BUG):
                    self.assertEqual(investigation.load(OWNER, saved.id).answer, reply)

    def shared_question_title(self, title: str) -> tuple[update_registration.QuestionRecord, submissions.SavedSubmission]:
        scope = read_production(OWNER, self.members)
        parts = update_registration.QuestionParts(ask=title, context='Ordinary context.', items=[], recommend='', choices=[])
        with patch.object(update_registration, 'review_open_question',
                          return_value=update_registration.ReviewedQuestion(title, title)):
            update_registration.ask(scope, title, parts, '', update_registration.Ranking(), identities=self.identities)
        sent = submissions.submit(replace(request(submissions.SubmissionKind.QUESTION, title),
                                          words=title + '\n\nThe sent Question has different context.'))
        ordinary, = (record for record in self.questions() if record.path != sent.path and record.item['text'] == title)
        return ordinary, sent

    def question_files(self) -> dict[Path, bytes]:
        files = {record.path: record.path.read_bytes() for record in self.questions()}
        for path in (self.root / 'state' / 'outstanding').glob('*.json*'):
            files[path] = path.read_bytes()
        return files

    def test_f014_native_title_commands_refuse_ambiguous_questions_without_writes(self) -> None:
        scope = read_production(OWNER, self.members)
        for action in ('answer', 'followup', 'reply', 'mark-important', 'questions', 'withdraw'):
            with self.subTest(action=action):
                title = f'Check the result for {action}'
                ordinary, sent = self.shared_question_title(title)
                if action == 'reply':
                    ordinary.item['followup'] = {'text': 'One ordinary follow-up', 'sent': '2026-11-17T12:00'}
                    update_registration.write_question_record(ordinary)
                before = self.question_files()
                with self.assertRaises(update_registration.RegistrationFailure) as refused:
                    match action:
                        case 'answer':
                            _ = update_registration.answer(scope, title, identities=self.identities)
                        case 'reply':
                            update_registration.reply(scope, title, 'The ordinary reply', identities=self.identities)
                        case 'mark-important':
                            update_registration.mark_important(scope, title)
                        case 'questions':
                            update_registration.replace_outstanding(scope, update_registration.outstanding_path(scope),
                                update_registration.ReplaceOutstanding((update_registration.OpenQuestionWithoutText(title, ''),)),
                                identities=self.identities)
                        case 'withdraw':
                            update_registration.withdraw(scope, title, identities=self.identities)
                        case 'followup':
                            _ = update_registration.followup(scope, title, 'One ordinary follow-up', identities=self.identities)
                self.assertIn(f"question/{OWNER}/{ordinary.item.get('review_id')}", str(refused.exception))
                self.assertIn(sent.key, str(refused.exception))
                self.assertEqual(self.question_files(), before)

    def test_f021_partial_title_lists_the_sent_question_and_the_native_question(self) -> None:
        scope = read_production(OWNER, self.members)
        ordinary, sent = self.shared_question_title('Check a result with a long title')
        before = self.question_files()
        with self.assertRaises(update_registration.RegistrationFailure) as refused:
            _ = update_registration.answer(scope, 'a result', identities=self.identities)
        self.assertIn(f"question/{OWNER}/{ordinary.item.get('review_id')}", str(refused.exception))
        self.assertIn(sent.key, str(refused.exception))
        self.assertEqual(self.question_files(), before)

    def test_f021_title_search_includes_open_sent_questions_after_a_folder_change(self) -> None:
        title = 'Check the earlier folder'
        sent = submissions.submit(replace(request(submissions.SubmissionKind.QUESTION), words=title))
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(self.root / 'new-issues')))
        scope = read_production(OWNER, self.members)
        parts = update_registration.QuestionParts(ask=title, context='', items=[], recommend='', choices=[])
        with patch.object(update_registration, 'review_open_question',
                          return_value=update_registration.ReviewedQuestion(title, title)):
            update_registration.ask(scope, title, parts, '', update_registration.Ranking(), identities=self.identities)
        ordinary, = (record for record in self.questions() if record.path != sent.path)
        before = self.question_files() | {sent.path: sent.path.read_bytes()}
        with self.assertRaises(update_registration.RegistrationFailure) as refused:
            _ = update_registration.answer(scope, title, identities=self.identities)
        self.assertIn(f"question/{OWNER}/{ordinary.item.get('review_id')}", str(refused.exception))
        self.assertIn(sent.key, str(refused.exception))
        self.assertEqual(self.question_files() | {sent.path: sent.path.read_bytes()}, before)

    def test_f021_title_search_includes_sent_questions_in_descendants(self) -> None:
        scope = read_production(OWNER, self.members)
        title = 'Check the shared result'
        ordinary, _ = self.shared_question_title(title)
        local_sent = next(record for record in self.questions() if record.path != ordinary.path)
        update_registration.write_question_record(replace(local_sent, status='closed'))
        child_id = staff.StaffId('child-showrunner')
        child_doc = self.root / 'child.md'
        _ = child_doc.write_text(self.doc.read_text().replace(str(self.issues), str(self.root / 'child-issues')))
        child = staff.StaffRow(child_id, 'showrunner', 'child', staff.ReportsTo(OWNER),
                               current_session=child_id, doc=staff.LiveDoc(local_machine(), child_doc))
        members = staff.StaffSnapshot((*self.members.rows, child))
        identities = update_registration.QuestionStaff.for_record(members)
        with patch.object(staff, 'snapshot', return_value=members):
            sent = submissions.submit(replace(request(submissions.SubmissionKind.QUESTION, recipient=child_id), words=title))
        ordinary_before = ordinary.path.read_bytes()
        sent_before = sent.path.read_bytes()
        with self.assertRaises(update_registration.RegistrationFailure) as refused:
            update_registration.withdraw(scope, title, identities=identities)
        self.assertIn(f"question/{OWNER}/{ordinary.item.get('review_id')}", str(refused.exception))
        self.assertIn(sent.key, str(refused.exception))
        self.assertEqual(ordinary.path.read_bytes(), ordinary_before)
        self.assertEqual(sent.path.read_bytes(), sent_before)

    def test_f021_title_command_refuses_before_adopting_state(self) -> None:
        title = 'Check the command result'
        _, sent = self.shared_question_title(title)
        before = self.question_files()
        output = StringIO()
        with (patch('sys.argv', ['update_registration.py', 'answer', '--production', OWNER, '--item', title]),
              patch.object(update_registration, 'adopt_moved_state', return_value=[]) as adopt,
              redirect_stdout(output)):
            self.assertEqual(update_registration.main(), 2)
        adopt.assert_not_called()
        self.assertIn(sent.key, output.getvalue())
        self.assertEqual(self.question_files(), before)

    def test_f021_answer_does_not_switch_to_a_same_title_record_during_the_lock(self) -> None:
        scope = read_production(OWNER, self.members)
        ordinary, sent = self.shared_question_title('Check the replacement')
        update_registration.write_question_record(replace(
            update_registration.read_review_question(sent.path), status='closed'))
        replacement = replace(ordinary, path=ordinary.path.with_name('question-replacement.md'),
                              item={**ordinary.item, 'review_id': 'Q99'})
        original_lock = update_registration.outstanding_lock

        @contextmanager
        def replace_before_lock(path: Path) -> Generator[None]:
            if path == update_registration.outstanding_path(scope):
                update_registration.write_question_record(replace(ordinary, status='closed'))
                update_registration.write_question_record(replacement)
            with original_lock(path):
                yield

        with (patch.object(update_registration, 'outstanding_lock', side_effect=replace_before_lock),
              self.assertRaisesRegex(update_registration.RegistrationFailure, 'no longer open')):
            _ = update_registration.answer(scope, ordinary.item['text'], identities=self.identities)
        self.assertEqual(update_registration.selected_question_records([replacement.path], (), None), [replacement])
        self.assertFalse(update_registration.outstanding_path(scope).with_suffix('.answered.jsonl').exists())

    def test_f021_question_keys_select_each_native_mutation(self) -> None:
        scope = read_production(OWNER, self.members)
        for action in ('answer', 'withdraw', 'mark-important', 'reply', 'followup'):
            with self.subTest(action=action):
                ordinary, sent = self.shared_question_title(f'Check the key for {action}')
                key = f"question/{OWNER}/{ordinary.item.get('review_id')}"
                sent_before = sent.path.read_bytes()
                if action == 'reply':
                    ordinary.item['followup'] = {'text': 'One ordinary follow-up', 'sent': '2026-11-17T12:00'}
                    update_registration.write_question_record(ordinary)
                if action == 'followup':
                    _ = update_registration.followup(scope, update_registration.ByQuestionKey(key), 'The follow-up',
                                                     identities=self.identities)
                else:
                    args = ['update_registration.py', action, '--production', OWNER, '--key', key]
                    if action == 'reply':
                        args += ['--text', 'The ordinary reply']
                    output = StringIO()
                    with patch('sys.argv', args), redirect_stdout(output):
                        try:
                            result = update_registration.main()
                        except SystemExit as error:
                            result = error.code
                    self.assertEqual(result, 0, output.getvalue())
                self.assertEqual(sent.path.read_bytes(), sent_before)
                current, = update_registration.selected_question_records([ordinary.path], (), None)
                if action in ('answer', 'withdraw'):
                    self.assertEqual(current.status, 'closed')
                elif action == 'mark-important':
                    self.assertEqual(current.item.get('urgency'), 'user')
                elif action == 'reply':
                    self.assertEqual(current.item.get('followups'),
                                     [{'followup': 'One ordinary follow-up', 'reply': 'The ordinary reply'}])
                else:
                    pending = current.item.get('followup')
                    assert pending is not None
                    self.assertEqual(pending['text'], 'The follow-up')

    def test_f036_refiling_in_the_same_minute_does_not_reuse_the_selected_question(self) -> None:
        scope = read_production(OWNER, self.members)
        ordinary, _ = self.shared_question_title('Check the replacement under lock')
        lock = update_registration.outstanding_lock
        replacement: list[update_registration.QuestionRecord] = []

        @contextmanager
        def refile_before_lock(path: Path) -> Generator[None]:
            if path == update_registration.outstanding_path(scope) and not replacement:
                update_registration.write_question_record(replace(ordinary, status='closed'))
                new = ordinary.item.copy()
                _ = new.pop('review_id')
                new['question'] = 'Replacement context in the same minute.'
                update_registration.store_questions(scope, [new], self.identities)
                replacement.extend(record for record in self.questions() if record.status == 'open'
                                   and 'review_submission' not in record.item)
            with lock(path):
                yield

        with (patch.object(update_registration, 'outstanding_lock', side_effect=refile_before_lock),
              self.assertRaisesRegex(update_registration.RegistrationFailure, 'no longer open')):
            _ = update_registration.answer(scope, update_registration.ByQuestionKey(
                update_registration.open_item_key(ordinary)), identities=self.identities)
        current, = replacement
        self.assertNotEqual(current.item.get('review_id'), ordinary.item.get('review_id'))
        self.assertEqual(update_registration.selected_question_records([current.path], (), None), [current])

    def test_f036_native_key_survives_a_rename_and_folder_change(self) -> None:
        scope = read_production(OWNER, self.members)
        ordinary, _ = self.shared_question_title('Check the renamed staff Question')
        key = update_registration.ByQuestionKey(update_registration.open_item_key(ordinary))
        renamed = ordinary.path.rename(ordinary.path.with_name('renamed staff item.md'))
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(self.root / 'new-issues')))
        _ = update_registration.answer(scope, key, identities=self.identities)
        current, = update_registration.selected_question_records([renamed], (), None)
        self.assertEqual(current.status, 'closed')

    def test_f037_old_records_stay_in_the_open_queue_and_keep_folder_history(self) -> None:
        kinds = submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.PROPOSAL
        requests = [request(kind) for kind in kinds]
        items = [submissions.submit(payload) for payload in requests]
        active = self.root / 'second-issues'
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(active)))
        for kind, saved in zip(kinds, items, strict=True):
            with self.subTest(kind=kind):
                self.assertTrue(submissions.answer(saved.key, 'Ready for review', saved.reply_token))
                self.assertTrue(saved.path.exists())
                review_type = (review_items.ReviewType.RESPONSE if kind == submissions.SubmissionKind.QUESTION
                               else review_items.ReviewType(kind.value))
                found = review_items.read(self.root / 'review', members=self.members, kinds=(review_type,))
                self.assertTrue(any(item.source_unit == OWNER and item.id == f'builds/{saved.id}'
                                    and item.state == review_items.ReviewState.OPEN for item in found))
        latest = self.root / 'third-issues'
        _ = self.doc.write_text(self.doc.read_text().replace(str(active), str(latest)))
        for payload, saved in zip(requests, items, strict=True):
            with self.subTest(kind=payload.kind):
                (submissions.image_folder(saved).parent / 'receipt.json').unlink()
                retry = submissions.submit(payload)
                self.assertEqual((retry.key, retry.path.parent), (saved.key, saved.path.parent))
                self.assertFalse(submissions.answer(saved.key, 'Ready for review', saved.reply_token))

    def test_f064_send_returns_the_native_result_when_receipt_rename_fails(self) -> None:
        payload = request(submissions.SubmissionKind.PROPOSAL)
        rename = os.replace

        def refuse_receipt(source: Path, destination: Path) -> None:
            if destination.name == "receipt.json":
                raise PermissionError("Receipt rename denied.")
            rename(source, destination)

        errors = StringIO()
        with (patch.object(os, "replace", side_effect=refuse_receipt), redirect_stderr(errors)):
            first = submissions.submit(payload)
        self.assertEqual(errors.getvalue(), "Review receipt for P1: Receipt rename denied.\n")
        self.assertEqual(first.id, "P1")
        self.assertEqual(submissions.submit(payload), first)
        self.assertEqual([issue.path for issue in self.proposals()], [first.path])
        self.assertTrue((submissions.image_folder(first).parent / "receipt.json").is_file())

    def test_f065_open_includes_an_answered_proposal_sent_to_a_producer(self) -> None:
        producer = self.members.by_id(PRODUCER)
        assert producer is not None
        doc = self.root / "producer.md"
        _ = doc.write_text(f"# Producer\n- **Proposals:** {self.root / 'producer-issues'}\n")
        updated = replace(producer, doc=staff.LiveDoc(local_machine(), doc))
        members = staff.StaffSnapshot(tuple(updated if row.id == PRODUCER else row for row in self.members.rows))
        with patch.object(staff, "snapshot", return_value=members):
            self.assertIn({"id": PRODUCER, "name": "producer", "proposal": True}, submissions.recipient_options(members))
            saved = submissions.submit(request(submissions.SubmissionKind.PROPOSAL, recipient=PRODUCER))
            self.assertTrue(submissions.answer(saved.key, "Ready for review", saved.reply_token))
            items = review_items.read(self.root / "review", members=members, show="open",
                                      kinds=(review_items.ReviewType.PROPOSAL,))
        self.assertEqual([(item.owner, item.id) for item in items], [(PRODUCER, f"producer/{saved.id}")])

    def test_f067_open_keeps_current_proposals_when_git_history_fails(self) -> None:
        saved = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        self.assertTrue(submissions.answer(saved.key, "Ready for review", saved.reply_token))
        with patch.object(record_store, "historical_issue_folders", side_effect=OSError("Folder history timed out.")):
            items = review_items.read(self.root / "review", members=self.members, show="open",
                                      kinds=(review_items.ReviewType.PROPOSAL,))
            self.assertEqual([item.id for item in items], [f"builds/{saved.id}"])
            with self.assertRaisesRegex(OSError, "Folder history timed out"):
                _ = submissions.submit(request(submissions.SubmissionKind.PROPOSAL, "second"))

    def test_f079_send_proposal_retries_a_late_name_collision(self) -> None:
        kind = submissions.SubmissionKind.PROPOSAL
        first = submissions.submit(request(kind, "first"))
        self.assertTrue(submissions.answer(first.key, "The peer answered.", first.reply_token))
        original = first.path.read_bytes()
        file_proposal = proposal_issue.file_review_proposal
        occupied: list[Path] = []

        def file(folder: Path, owner: staff.StaffId, title: str, words: str, reason: str,
                 details: submissions.ReviewSubmission, *, before_write: Callable[[str, Path], None]) -> Issue:
            def sync_answer(identity: str, path: Path) -> None:
                before_write(identity, path)
                if not occupied:
                    occupied.append(path)
                    _ = first.path.rename(path)
            return file_proposal(folder, owner, title, words, reason, details, before_write=sync_answer)

        payload = replace(request(kind, "second"), words="Second proposal")
        with patch.object(proposal_issue, "file_review_proposal", side_effect=file):
            saved = submissions.submit(payload)
        collision, = occupied
        self.assertEqual(collision.read_bytes(), original)
        self.assertNotEqual(saved.path, collision)
        self.assertEqual(submissions.submit(payload), saved)

    def test_f082_archived_send_retries_find_each_request_before_locking(self) -> None:
        cases = ((submissions.SubmissionKind.QUESTION, False), (submissions.SubmissionKind.PROPOSAL, False),
                 (submissions.SubmissionKind.BUG, False), (submissions.SubmissionKind.BUG, True))
        for kind, transfer in cases:
            with self.subTest(kind=kind, transfer=transfer):
                payload = request(kind, "transfer during retry" if transfer else "archive retry")
                saved = submissions.submit(payload)
                archive = saved.path.parent / "archive"
                archive.mkdir(exist_ok=True)
                archived = saved.path.rename(archive / ("moved " + saved.path.name))
                members = self.wrap_fixture() if transfer else self.members
                locks = investigation.recipient_locks
                checked_lookup = False
                with patch.object(submissions, "locate_native_submission",
                                  wraps=submissions.locate_native_submission) as locate:
                    @contextmanager
                    def lock_after_lookup(*owners: staff.StaffId) -> Generator[None]:
                        nonlocal checked_lookup
                        if not checked_lookup:
                            self.assertEqual(locate.call_count, 1)
                            checked_lookup = True
                            if transfer:
                                self.assertEqual(self.tidy_fixture(members), (0, ""))
                        with locks(*owners):
                            yield

                    with patch.object(investigation, "recipient_locks", side_effect=lock_after_lookup):
                        retry = submissions.submit(payload, members=members)
                self.assertTrue(checked_lookup)
                self.assertEqual(retry.reply_token, saved.reply_token)
                if transfer:
                    self.assertFalse(archived.exists())
                else:
                    self.assertEqual(retry, replace(saved, path=archived))
                owner = PRODUCER if transfer else saved.owner
                path = investigation.folder(owner) / saved.path.name if transfer else archived
                self.assertEqual((retry.key, retry.owner, retry.path), (saved.key, owner, path))
                receipt = submissions.image_folder(saved).parent / "receipt.json"
                location = cast(dict[str, str], json.loads(receipt.read_text()))
                self.assertEqual((location.get("owner", location["recipient"]),
                                  location["path"], location["original_path"]),
                                 (owner, str(path), str(saved.path)))

    def test_f083_retry_cannot_commit_a_proposal_during_native_rollback(self) -> None:
        payload = request(submissions.SubmissionKind.PROPOSAL)
        folder = self.root / "review-attachments" / payload.request_id
        published = Event()
        roll_back = Event()
        retry_state: Queue[str] = Queue()
        sync_folder = atomic_file.sync_folder
        locks = investigation.recipient_locks

        def fail_first_sync(path: Path) -> None:
            if path == self.issues and self.proposals() and not published.is_set():
                published.set()
                if not roll_back.wait(5):
                    raise AssertionError("The test did not release the first Send.")
                raise OSError("Native publication failed.")
            sync_folder(path)

        def lock_retry(*owners: staff.StaffId) -> AbstractContextManager[None]:
            if published.is_set() and not roll_back.is_set():
                retry_state.put("request lock")
            return locks(*owners)

        def retry() -> submissions.SavedSubmission:
            saved = submissions.submit(payload)
            retry_state.put("returned")
            return saved

        with (patch.object(atomic_file, "sync_folder", side_effect=fail_first_sync),
              patch.object(record_store, "sync_folder", side_effect=fail_first_sync),
              patch.object(investigation, "recipient_locks", side_effect=lock_retry),
              ThreadPoolExecutor(max_workers=2) as executor):
            first = executor.submit(submissions.submit, payload)
            try:
                self.assertTrue(published.wait(5))
                second = executor.submit(retry)
                self.assertEqual(retry_state.get(timeout=5), "request lock")
                self.assertFalse((folder / "receipt.json").exists())
            finally:
                roll_back.set()
            with self.assertRaisesRegex(OSError, "Native publication failed"):
                _ = first.result(timeout=5)
            saved = second.result(timeout=5)
        self.assertEqual(saved.id, "P2")
        self.assertEqual(self.proposals()[0].path, saved.path)
        self.assertTrue((folder / "receipt.json").is_file())
        self.assertEqual(submissions.submit(payload), saved)

    def test_f083_retry_reports_a_receipt_failure_while_the_request_is_pending(self) -> None:
        payload = request(submissions.SubmissionKind.PROPOSAL)
        with (patch.object(submissions, "_publish_receipt", side_effect=OSError("Receipt interrupted.")),
              redirect_stderr(StringIO())):
            first = submissions.submit(payload)
            with self.assertRaisesRegex(OSError, "Receipt interrupted"):
                _ = submissions.submit(payload)
        self.assertEqual(submissions.submit(payload), first)
        self.assertEqual([issue.path for issue in self.proposals()], [first.path])

    def test_f070_question_retry_refuses_an_unreadable_archive(self) -> None:
        kind = submissions.SubmissionKind.QUESTION
        payload = request(kind)
        with (patch.object(submissions, "_publish_receipt", side_effect=OSError("Receipt interrupted.")),
              redirect_stderr(StringIO())):
            saved = submissions.submit(payload)
        archive = self.issues / "archive"
        archive.mkdir()
        archived = saved.path.rename(archive / saved.path.name)
        current = self.root / "new-issues"
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(current)))
        blocked = PermissionError("Cannot read the Question archive.")
        open_file = Path.open

        def read_file(path: Path, *args: object, **kwargs: object) -> TextIO:
            if path == archived:
                raise blocked
            return cast(Callable[..., TextIO], open_file)(path, *args, **kwargs)

        with patch.object(Path, "open", read_file):
            with self.assertRaisesRegex(OSError, "Cannot read"):
                _ = submissions.submit(payload)
        self.assertEqual(list(current.glob("*.md")), [])
        self.assertEqual(submissions.submit(payload).path, archived)

    def test_f071_completed_retry_needs_no_sequence_or_recipient_lock(self) -> None:
        payload = request(submissions.SubmissionKind.PROPOSAL)
        saved = submissions.submit(payload)
        unavailable = PermissionError("The lock is unavailable.")
        with patch.object(proposal_issue, "proposal_numbers", side_effect=unavailable):
            self.assertEqual(submissions.submit(payload), saved)
        with (patch.object(proposal_issue, "proposal_numbers", side_effect=unavailable),
              patch.object(investigation, "recipient_locks", side_effect=unavailable),
              patch.object(proposal_issue, "find_review_proposal", side_effect=unavailable)):
            self.assertEqual(submissions.submit(payload), saved)
            errors = StringIO()
            with patch.object(atomic_file, "sync_folder", side_effect=OSError("Receipt sync denied.")), redirect_stderr(errors):
                self.assertEqual(submissions.submit(payload), saved)
            self.assertEqual(errors.getvalue(), "Review receipt for P1: Receipt sync denied.\n")

    def test_f073_scoped_reply_uses_the_registry_when_the_document_is_unreadable(self) -> None:
        kind = submissions.SubmissionKind.PROPOSAL
        saved = submissions.submit(request(kind))
        token = saved.reply_token
        archive = self.issues / "archive"
        archive.mkdir()
        archived = saved.path.rename(archive / saved.path.name)
        read_text = Path.read_text

        def read(path: Path, *args: object, **kwargs: object) -> str:
            if path == self.doc:
                raise PermissionError("Cannot read the production document.")
            return read_text(path, *cast(tuple[str], args), **cast(dict[str, str], kwargs))

        with patch.object(Path, "read_text", read):
            self.assertTrue(submissions.answer(saved.key, "Answer from the registry", token))
        self.assertIn("Answer from the registry", archived.read_text())

    def test_f074_open_skips_an_inaccessible_production_directory(self) -> None:
        saved = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        self.assertTrue(submissions.answer(saved.key, "An answer", saved.reply_token))
        doc = self.root / "blocked" / "production.md"
        producer = replace(self.members.rows[1], doc=staff.LiveDoc(local_machine(), doc))
        members = staff.StaffSnapshot((self.members.rows[0], producer))
        is_dir = Path.is_dir

        def directory(path: Path) -> bool:
            if path == doc.parent:
                raise PermissionError("Cannot access the production directory.")
            return is_dir(path)

        with patch.object(Path, "is_dir", directory):
            items = review_items.read(self.root / "review", members=members, show="open",
                                      kinds=(review_items.ReviewType.PROPOSAL,))
        self.assertEqual([item.id for item in items], ["builds/P1"])

    def test_f076_open_includes_a_proposal_without_its_staff_document(self) -> None:
        saved = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        token = saved.reply_token
        owner = replace(self.members.rows[0], doc=staff.DocUnfilled())
        members = staff.StaffSnapshot((owner, self.members.rows[1]))
        self.doc.unlink()
        self.assertTrue(submissions.answer(saved.key, "The production has wrapped", token, members=members))
        items = review_items.read(self.root / "review", members=members, show="open",
                                  kinds=(review_items.ReviewType.PROPOSAL,))
        self.assertEqual([(item.owner, item.id) for item in items], [(OWNER, "builds/P1")])
        self.assertEqual(frontmatter(saved.path)["production_id"], [OWNER])

    def test_f038_filing_reserves_ids_across_folders_and_archives(self) -> None:
        kinds = submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.PROPOSAL
        first = [submissions.submit(request(kind)) for kind in kinds]
        archive = self.issues / 'archive' / '2026'
        archive.mkdir(parents=True)
        for saved in first:
            _ = saved.path.rename(archive / saved.path.name)
        active = self.root / 'new-issues'
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(active)))
        for kind, saved in zip(kinds, first, strict=True):
            with self.subTest(kind=kind):
                newer = submissions.submit(request(kind, 'newer'))
                self.assertEqual(newer.id, saved.id[0] + '2')
                self.assertTrue(submissions.answer(saved.key, 'Reply to the old item', saved.reply_token))
                self.assertEqual(submissions.submit(request(kind)).path, archive / saved.path.name)

    def test_f056_reply_succeeds_with_a_duplicate_id_in_the_active_folder(self) -> None:
        for kind in (submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.PROPOSAL):
            with self.subTest(kind=kind):
                saved = submissions.submit(request(kind))
                archive = self.issues / 'archive'
                archive.mkdir(exist_ok=True)
                archived = saved.path.rename(archive / saved.path.name)
                conflicting = self.issues / f'already in store {kind}.md'
                _ = conflicting.write_bytes(archived.read_bytes().replace(
                    saved.details.request_id.encode(), request(kind, 'conflicting').request_id.encode()))
                before = archived.read_bytes(), conflicting.read_bytes()
                self.assertTrue(submissions.answer(saved.key, 'Reply to the archived item', saved.reply_token))
                self.assertIn(b'Reply to the archived item', archived.read_bytes())
                self.assertEqual(conflicting.read_bytes(), before[1])

    def test_f039_registry_finds_records_without_a_configured_folder(self) -> None:
        unavailable = replace(self.members.rows[0], doc=staff.DocUnfilled())
        members = staff.StaffSnapshot((unavailable, self.members.rows[1]))
        for kind in (submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.PROPOSAL):
            with self.subTest(kind=kind):
                saved = submissions.submit(request(kind))
                token = saved.reply_token
                (submissions.image_folder(saved).parent / "receipt.json").unlink()
                self.assertTrue(submissions.answer(saved.key, "Answer without a document", token, members=members))

    def test_f040_archived_proposal_name_stays_reserved_and_reply_keeps_its_path(self) -> None:
        kind = submissions.SubmissionKind.PROPOSAL
        saved = submissions.submit(request(kind))
        archive = self.issues / 'archive'
        archive.mkdir()
        archived = saved.path.rename(archive / saved.path.name)
        newer = submissions.submit(request(kind, 'same title'))
        self.assertEqual(newer.id, 'P2')
        self.assertNotEqual(newer.path.name, archived.name)
        _ = saved.path.write_text('A different issue now uses this old filename.\n')
        self.assertTrue(submissions.answer(saved.key, 'Reply to the first proposal', saved.reply_token))
        restored = submissions.submit(request(kind))
        self.assertEqual(restored.id, 'P1')
        self.assertEqual(restored.path, archived)
        self.assertEqual(saved.path.read_text(), 'A different issue now uses this old filename.\n')

    def test_f041_renamed_archived_question_can_retry_reply_and_mark_important(self) -> None:
        kind = submissions.SubmissionKind.QUESTION
        saved = submissions.submit(request(kind))
        archive = self.issues / 'archive'
        archive.mkdir()
        renamed = saved.path.rename(archive / (saved.path.stem + '-2026.md'))
        self.assertEqual(submissions.submit(request(kind)).path, renamed)
        self.assertTrue(submissions.answer(saved.key, 'Reply to the renamed item', saved.reply_token))
        update_registration.mark_important(read_production(OWNER, self.members), update_registration.ByQuestionKey(saved.key))
        current, = self.questions()
        self.assertEqual(current.item.get('urgency'), 'user')
        self.assertEqual(current.item.get('review_id'), saved.id)

    def test_renamed_investigation_keeps_its_id_for_filing_and_reply(self) -> None:
        kind = submissions.SubmissionKind.BUG
        saved = submissions.submit(request(kind))
        archive = saved.path.parent / 'archive'
        archive.mkdir()
        renamed = saved.path.rename(archive / 'renamed report.md')
        newer = submissions.submit(request(kind, 'newer'))
        self.assertEqual(newer.id, 'I2')
        self.assertEqual(submissions.submit(request(kind)).path, renamed)
        self.assertTrue(submissions.answer(saved.key, 'Reply to the renamed report', saved.reply_token))
        self.assertEqual(investigation.load(OWNER, saved.id).answer, 'Reply to the renamed report')

    def test_f042_default_question_folder_keeps_its_number_and_key(self) -> None:
        title = 'Question before the Proposals folder'
        default = self.root / 'state' / OWNER / 'questions'
        default.mkdir(parents=True)
        path = default / 'question-legacy.md'
        item = update_registration.OutstandingItem(since='2026-11-17T12:00', text=title, question=title,
                                                  asked_by=OWNER, review_id='Q1')
        update_registration.write_question_record(update_registration.QuestionRecord(path, OWNER, OWNER, item, 'open'))
        sent = submissions.submit(request(submissions.SubmissionKind.QUESTION))
        self.assertEqual(sent.id, 'Q2')
        scope = read_production(OWNER, self.members)
        update_registration.withdraw(scope, update_registration.ByQuestionKey(f'question/{OWNER}/Q1'),
                                     identities=self.identities)
        self.assertIn('status: closed', path.read_text())
        self.assertIn('status: open', sent.path.read_text())

    def test_f051_proposal_number_seeds_from_git_history(self) -> None:
        self.issues.mkdir()
        old = self.issues / 'old.md'
        _ = old.write_text(f'---\ntags: [proposal]\nproposed_by: {OWNER}\nproposal_id: P1\n---\n')
        for arguments in (('init', '-q'), ('add', self.doc.name),
                          ('-c', 'user.name=Review fixture', '-c', 'user.email=review@example.invalid',
                           'commit', '-qm', 'Record the original folder')):
            _ = subprocess.run(('git', '-C', str(self.root), *arguments), check=True)
        active = self.root / 'new-issues'
        active.mkdir()
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(active)))
        plain = active / 'ordinary.md'
        _ = plain.write_text(f'---\ntags: [proposal]\nproposed_by: {OWNER}\n---\n')
        self.assertEqual(proposal_issue.number([plain], date(2026, 11, 17)), ['number ordinary.md: P2'])
        self.assertEqual(submissions.submit(request(submissions.SubmissionKind.PROPOSAL)).id, 'P3')

    def test_f047_shared_folder_uses_one_sequence_after_a_reply(self) -> None:
        other = staff.StaffId('other-staff')
        other_doc = self.root / 'other.md'
        other_folder = self.root / 'other-issues'
        _ = other_doc.write_text(self.doc.read_text().replace(str(self.issues), str(other_folder)))
        member = staff.StaffRow(other, 'showrunner', 'other', staff.StaffRoot(), current_session=other,
                                doc=staff.LiveDoc(local_machine(), other_doc))
        members = replace(self.members, rows=(*self.members.rows, member))
        kind = submissions.SubmissionKind.PROPOSAL
        with patch.object(staff, 'snapshot', return_value=members):
            initial = submissions.submit(request(kind))
            other_folder.mkdir()
            legacy = other_folder / 'legacy.md'
            _ = legacy.write_text(f'---\ntags: [proposal]\nproposed_by: {other}\nproposal_id: P1\n---\n')
            restored = submissions.submit(request(kind, 'restore', recipient=other))
            self.assertEqual((initial.id, restored.id), ('P1', 'P2'))
            _ = other_doc.write_text(other_doc.read_text().replace(str(other_folder), str(self.issues)))
            self.assertTrue(submissions.answer(restored.key, 'Use the shared folder', restored.reply_token))
            newest = submissions.submit(request(kind, 'newest'))
        self.assertEqual(newest.id, 'P3')
        self.assertEqual({issue.value('proposal_id') for issue in self.proposals()}, {'P1', 'P3'})
        self.assertIn(b'Use the shared folder', restored.path.read_bytes())

    def test_f047_question_and_investigation_sequences_cover_other_owners(self) -> None:
        default = self.root / 'state' / PRODUCER / 'questions'
        default.mkdir(parents=True)
        path = default / 'question-legacy.md'
        item = update_registration.OutstandingItem(since='2026-11-17T12:00', text='Legacy', question='Legacy',
                                                  asked_by=PRODUCER, review_id='Q7')
        update_registration.write_question_record(update_registration.QuestionRecord(path, PRODUCER, PRODUCER, item, 'open'))
        report = investigation.file(PRODUCER, 'Other owner', 'Check this.', waiting_why='Await the findings.')
        self.assertEqual(report.id, 'I1')
        self.assertEqual(submissions.submit(request()).id, 'I2')
        self.assertEqual(submissions.submit(request(submissions.SubmissionKind.QUESTION)).id, 'Q8')

    def test_f046_each_later_allocation_checks_unreadable_native_records(self) -> None:
        for kind in (submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.PROPOSAL,
                     submissions.SubmissionKind.BUG):
            with self.subTest(kind=kind):
                saved = submissions.submit(request(kind))
                original = Path.open

                def deny(path: Path, *args: object, **kwargs: object) -> TextIO:
                    if path == saved.path:
                        raise PermissionError('Fixture denies the read')
                    return cast('Callable[..., TextIO]', original)(path, *args, **kwargs)

                with (patch.object(Path, 'open', deny),
                      self.assertRaisesRegex(OSError, re.escape(saved.path.name))):
                    _ = submissions.submit(request(kind, 'unreadable'))
                self.assertEqual(submissions.submit(request(kind, 'unreadable')).id, saved.id[0] + '2')

    def test_f047_allocation_reads_ids_synced_into_the_target_archive(self) -> None:
        for kind in (submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.PROPOSAL,
                     submissions.SubmissionKind.BUG):
            with self.subTest(kind=kind):
                saved = submissions.submit(request(kind))
                archive = saved.path.parent / 'archive'
                archive.mkdir(exist_ok=True)
                synced_id = saved.id[0] + '40'
                _ = (archive / f'{synced_id}.md').write_bytes(saved.path.read_bytes().replace(
                    f'"{saved.id}"'.encode(), f'"{synced_id}"'.encode(), 1))
                self.assertEqual(submissions.submit(request(kind, 'after sync')).id, saved.id[0] + '41')

    def test_f049_refused_legacy_reply_and_followup_keep_the_sequence(self) -> None:
        self.issues.mkdir()
        scope = read_production(OWNER, self.members)
        title = 'Unnumbered Question'
        path = self.issues / 'question-legacy.md'
        item = update_registration.OutstandingItem(since='2026-11-17T12:00', text=title, question=title, asked_by=OWNER)
        record = update_registration.QuestionRecord(path, OWNER, OWNER, item, 'open')
        update_registration.write_question_record(record)
        with self.assertRaisesRegex(update_registration.RegistrationFailure, 'no open follow-up'):
            update_registration.reply(scope, title, 'Too early', identities=self.identities)
        self.assertEqual(record_store.review_folders(OWNER, 'question'), ())
        item['followup'] = update_registration.PendingFollowup(text='First follow-up', sent='2026-11-17T12:01')
        update_registration.write_question_record(record)
        before = path.read_bytes()
        with self.assertRaisesRegex(update_registration.RegistrationFailure, 'already waits'):
            _ = update_registration.followup(scope, title, 'Second follow-up', identities=self.identities)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(record_store.review_folders(OWNER, 'question'), ())
        self.assertEqual(submissions.submit(request(submissions.SubmissionKind.QUESTION)).id, 'Q1')

    def test_f049_missing_proposal_template_keeps_the_sequence(self) -> None:
        self.issues.mkdir()
        path = self.issues / 'decision.md'
        template = self.root / 'template.yaml'
        with self.assertRaises(FileNotFoundError):
            _ = proposal_issue.create(path, 'builds', 'builds', OWNER, [], 'rule', 'crate', 'blob', 1,
                                      'Words', template, date(2026, 11, 17), initial_fields={})
        self.assertEqual(record_store.review_folders(OWNER, 'proposal'), ())
        self.assertEqual(submissions.submit(request(submissions.SubmissionKind.PROPOSAL)).id, 'P1')

    def test_f049_failed_number_publication_keeps_only_the_raised_number(self) -> None:
        kind = submissions.SubmissionKind.QUESTION
        payload = request(kind)
        write = atomic_file.write_text_atomic

        def fail(path: Path, text: str) -> None:
            if path.name == 'question.water':
                write(path, text)
                raise OSError('The sequence sync failed')
            write(path, text)

        with (patch.object(record_store, 'write_text_atomic', fail),
              self.assertRaisesRegex(OSError, 'sequence sync failed')):
            _ = submissions.submit(payload)
        self.assertEqual(self.questions(), [])
        self.assertEqual(record_store.review_folders(OWNER, kind), ())
        self.assertEqual((self.root / 'state' / 'record-ids' / 'question.water').read_text(), '1\n')
        self.assertEqual(submissions.submit(payload).id, 'Q2')

    def test_f042_reseeds_from_recorded_folders_when_the_sequence_is_missing(self) -> None:
        kind = submissions.SubmissionKind.QUESTION
        first = submissions.submit(request(kind))
        (self.root / 'state' / 'record-ids' / 'question.water').unlink()
        active = self.root / 'new-issues'
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(active)))
        second = submissions.submit(request(kind, 'after the folder change'))
        self.assertEqual((first.id, second.id), ('Q1', 'Q2'))
        self.assertEqual(record_store.review_folders(OWNER, kind), (self.issues, active))

    def test_f048_failed_reply_does_not_move_the_waiting_record(self) -> None:
        active = self.root / 'new-issues'
        for kind, module, writer in ((submissions.SubmissionKind.QUESTION, update_registration, 'write_question_record'),
                                     (submissions.SubmissionKind.PROPOSAL, proposal_issue, 'write_issue')):
            with self.subTest(kind=kind):
                _ = self.doc.write_text(self.doc.read_text().replace(str(active), str(self.issues)))
                saved = submissions.submit(request(kind))
                _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(active)))
                before = saved.path.read_bytes()
                folders = record_store.review_folders(OWNER, kind)
                with (patch.object(module, writer, side_effect=OSError('Answer did not publish')),
                      patch.object(Path, 'rename', side_effect=OSError(errno.EXDEV, 'Cross filesystem')),
                      self.assertRaisesRegex(OSError, 'Answer did not publish')):
                    _ = submissions.answer(saved.key, 'Answer at its source', saved.reply_token)
                self.assertTrue(saved.path.is_file())
                self.assertEqual(saved.path.read_bytes(), before)
                self.assertFalse((active / saved.path.name).exists())
                self.assertEqual(record_store.review_folders(OWNER, kind), folders)
                self.assertTrue(submissions.answer(saved.key, 'Answer at its source', saved.reply_token))

    def test_f052_send_retry_preserves_the_proposal_number(self) -> None:
        kind = submissions.SubmissionKind.PROPOSAL
        self.assertEqual(submissions.submit(request(kind, 'first')).id, 'P1')
        payload = request(kind, 'interrupted')
        create = record_store.create_record_text

        def stop(path: Path, text: str) -> None:
            create(path, text)
            raise KeyboardInterrupt('Stopped after native creation')

        with (patch.object(record_store, 'create_record_text', stop),
              self.assertRaisesRegex(KeyboardInterrupt, 'Stopped after native creation')):
            _ = submissions.submit(payload)
        saved = submissions.submit(payload)
        self.assertEqual(saved.id, 'P2')
        number = self.root / 'state' / 'record-ids' / 'proposal.water'
        self.assertEqual(number.read_text(), '2\n')

    def test_f054_updating_a_synced_question_raises_the_sequence(self) -> None:
        scope = read_production(OWNER, self.members)
        ordinary, _ = self.shared_question_title('Synced Question')
        synced = replace(ordinary, path=ordinary.path.with_name('synced.md'),
                         item={**ordinary.item, 'review_id': 'Q9', 'text': 'Imported Question'})
        update_registration.write_question_record(synced)
        number = self.root / 'state' / 'record-ids' / 'question.water'
        _ = number.write_text('2\n')
        _ = update_registration.act_on_question(scope, update_registration.ByQuestionKey(
            update_registration.open_item_key(synced)), 'mark-important', self.identities)
        self.assertEqual(number.read_text(), '9\n')
        self.assertEqual(submissions.submit(request(submissions.SubmissionKind.QUESTION, recipient=PRODUCER)).id, 'Q10')

    def test_f055_number_checks_every_folder_before_writing(self) -> None:
        self.assertEqual(submissions.submit(request(submissions.SubmissionKind.PROPOSAL, 'first')).id, 'P1')
        later = self.root / 'later' / 'issues'
        later.mkdir(parents=True)
        first, second = self.issues / 'first.md', later / 'second.md'
        body = '---\ntags: [proposal]\nstatus: open\n---\n'
        for path in (first, second):
            _ = path.write_text(body)
        _ = (later / 'unreadable.md').write_bytes(b'\xff')
        with self.assertRaisesRegex(OSError, 'unreadable.md'):
            _ = proposal_issue.number([first, second], date(2026, 11, 17))
        self.assertEqual((first.read_text(), second.read_text()), (body, body))

    def test_f057_failed_ask_keeps_legacy_and_new_records_unwritten(self) -> None:
        scope = read_production(OWNER, self.members)
        path = update_registration.outstanding_path(scope)
        path.parent.mkdir(parents=True)
        legacy = '[{"since":"2026-11-17T12:00","text":"Legacy","question":"Legacy words","asked_by":"builds-staff"}]\n'
        _ = path.write_text(legacy)
        write = atomic_file.write_text_atomic

        def stop(destination: Path, text: str) -> None:
            if destination == path:
                raise OSError('The outstanding list write failed')
            write(destination, text)

        parts = update_registration.QuestionParts(ask='New question', context='', items=[], recommend='', choices=[])
        with (patch.object(update_registration, 'review_open_question',
                           return_value=update_registration.ReviewedQuestion('New question', 'New question')),
              patch.object(update_registration, 'write_text_atomic', stop),
              self.assertRaisesRegex(OSError, 'outstanding list write failed')):
            update_registration.ask(scope, 'New question', parts, '', update_registration.Ranking(), identities=self.identities)
        self.assertEqual(path.read_text(), legacy)
        self.assertEqual(self.questions(), [])

    def test_f058_create_recognizes_a_decision_in_git_folder_history(self) -> None:
        template = self.root / 'template.yaml'
        _ = template.write_text('---\nstatus: open\n---\n')
        self.issues.mkdir()
        old = self.issues / 'decision.md'
        _ = old.write_text(f'---\ntags: [proposal]\nproposal_id: P1\nproposed_by: {OWNER}\n' +
                           'style_rule: rule\nstyle_crate: crate\nstyle_rule_blob: blob\n---\n')
        for args in (('init', '-q'), ('add', self.doc.name),
                     ('-c', 'user.name=Review', '-c', 'user.email=review@example.invalid', 'commit', '-qm', 'Record the folder')):
            _ = subprocess.run(('git', '-C', str(self.root), *args), check=True)
        current = self.root / 'new-issues'
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(current)))
        locations: list[Path] = []
        created = proposal_issue.create(current / old.name, 'builds', 'builds', OWNER, [],
                                        'rule', 'crate', 'blob', 1, 'Words', template, date(2026, 11, 17),
                                        initial_fields={}, filed_at=locations.append)
        self.assertFalse(created)
        self.assertEqual(locations, [old])
        self.assertFalse((current / old.name).exists())

    def test_f050_create_uses_a_free_name_next_to_an_active_proposal(self) -> None:
        sent = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        original = sent.path.read_bytes()
        template = self.root / 'template.yaml'
        _ = template.write_text('---\nstatus: open\n---\n')
        locations: list[Path] = []
        self.assertTrue(proposal_issue.create(sent.path, 'builds', 'builds', OWNER, [], 'rule', 'crate', 'blob', 1,
                                             'Decision words', template, date(2026, 11, 17), initial_fields={},
                                             filed_at=locations.append))
        actual, = locations
        self.assertNotEqual(actual, sent.path)
        self.assertEqual(Issue(actual, frontmatter(actual)).value('proposal_id'), 'P2')
        self.assertEqual(sent.path.read_bytes(), original)

    def test_f042_seed_includes_legacy_question_in_git_folder_history(self) -> None:
        scope = read_production(OWNER, self.members)
        title = 'Question in the original folder'
        path = self.issues / 'question-legacy.md'
        item = update_registration.OutstandingItem(since='2026-11-17T12:00', text=title, question=title,
                                                  asked_by=OWNER, review_id='Q1')
        update_registration.write_question_record(update_registration.QuestionRecord(path, OWNER, OWNER, item, 'open'))
        state = self.root / 'state'
        (state / 'review-folders' / OWNER / 'question.json').unlink(missing_ok=True)
        (state / 'record-ids' / 'question.water').unlink(missing_ok=True)
        self.assertEqual(record_store.review_folders(OWNER, 'question'), ())
        legacy, = self.questions()
        key = update_registration.ByQuestionKey(update_registration.open_item_key(legacy))
        for arguments in (('init', '-q'), ('add', self.doc.name),
                          ('-c', 'user.name=Review fixture', '-c', 'user.email=review@example.invalid',
                           'commit', '-qm', 'Record the original folder')):
            _ = subprocess.run(('git', '-C', str(self.root), *arguments), check=True)
        unrelated = self.root / 'unrelated'
        unrelated.mkdir()
        _ = (unrelated / path.name).write_text(path.read_text().replace('Q1', 'Q99'))
        active = self.root / 'new-issues'
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(active)))
        sent = submissions.submit(request(submissions.SubmissionKind.QUESTION))
        self.assertEqual(sent.id, 'Q2')
        _ = update_registration.answer(scope, key, identities=self.identities)
        self.assertIn('status: closed', path.read_text())
        self.assertIn('status: open', sent.path.read_text())

    def test_f042_removed_records_do_not_release_any_native_id(self) -> None:
        for kind in (submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.PROPOSAL,
                     submissions.SubmissionKind.BUG):
            with self.subTest(kind=kind):
                saved = submissions.submit(request(kind))
                saved.path.unlink()
                newer = submissions.submit(request(kind, 'after deletion'))
                self.assertEqual(newer.id, saved.id[0] + '2')

    def test_f043_native_proposal_numbering_and_create_share_send_rules(self) -> None:
        saved = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        archive = self.issues / 'archive'
        archive.mkdir()
        archived = saved.path.rename(archive / saved.path.name)
        active = self.root / 'new-issues'
        active.mkdir()
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(active)))
        plain = active / 'ordinary proposal.md'
        _ = plain.write_text(f'---\ntags: [proposal]\nproposed_by: {OWNER}\nstatus: open\n---\nOrdinary words.\n')
        today = date(2026, 11, 17)
        self.assertEqual(proposal_issue.number([plain], today), ['number ordinary proposal.md: P2'])
        template = self.root / 'template.yaml'
        _ = template.write_text('---\nstatus: open\n---\n')
        path = active / archived.name
        locations: list[Path] = []
        self.assertTrue(proposal_issue.create(path, 'builds', 'builds', OWNER, [], 'test rule', 'crate', 'blob', 1,
                                             'Decision words.', template, today, initial_fields={}, filed_at=locations.append))
        self.assertFalse(path.exists())
        decision, = (candidate for candidate in active.glob('*.md') if candidate != plain)
        self.assertEqual(Issue(decision, frontmatter(decision)).value('proposal_id'), 'P3')
        self.assertFalse(proposal_issue.create(path, 'builds', 'builds', OWNER, [], 'test rule', 'crate', 'blob', 1,
                                              'Decision words.', template, today, initial_fields={}, filed_at=locations.append))
        self.assertEqual(locations, [decision, decision])
        self.assertTrue(submissions.answer(saved.key, 'Reply to the old proposal', saved.reply_token))

    def test_f056_reply_keeps_its_path_when_a_new_proposal_uses_its_filename(self) -> None:
        payload = request(submissions.SubmissionKind.PROPOSAL)
        saved = submissions.submit(payload)
        active = self.root / 'new-issues'
        active.mkdir()
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(active)))
        marker = active / saved.path.name
        _ = marker.write_text('A different Proposal uses this name.\n')
        with patch.object(Path, 'rename', side_effect=OSError('Reply tried to move the record')):
            self.assertTrue(submissions.answer(saved.key, 'Answer at the original path', saved.reply_token))
        self.assertFalse(submissions.answer(saved.key, 'Answer at the original path', saved.reply_token))
        self.assertEqual(submissions.submit(payload).path, saved.path)
        self.assertEqual(marker.read_text(), 'A different Proposal uses this name.\n')
        self.assertIn(b'Answer at the original path', saved.path.read_bytes())

    def test_f087_send_uses_a_peer_record_for_the_same_request_at_the_link(self) -> None:
        for kind in submissions.SubmissionKind:
            with self.subTest(kind=kind):
                payload = request(kind)
                peer = submissions.submit(payload)
                self.assertTrue(submissions.answer(peer.key, "The peer answered.", peer.reply_token))
                original = peer.path.read_bytes()
                peer.path.unlink()
                (submissions.image_folder(peer).parent / "receipt.json").unlink()
                before = set(peer.path.parent.glob("*.md"))
                delivered: list[Path] = []
                link = os.link

                def deliver(source: Path, target: Path) -> None:
                    if target.parent == peer.path.parent and target.suffix == ".md" and not delivered:
                        delivered.append(target)
                        _ = target.write_bytes(original)
                    link(source, target)

                with patch.object(os, "link", side_effect=deliver):
                    saved = submissions.submit(payload)
                collision, = delivered
                self.assertEqual((saved.id, saved.path), (peer.id, collision))
                self.assertEqual(saved.reply_token, "")
                self.assertTrue(new_item_dialog.matches_reply_token(saved.details, peer.reply_token))
                self.assertEqual(collision.read_bytes(), original)
                self.assertEqual(submissions.submit(payload), saved)
                self.assertEqual(set(collision.parent.glob("*.md")) - before, {collision})

    def test_f045_ambiguity_names_a_legacy_question_without_writing_it(self) -> None:
        title = 'Check the legacy Question'
        sent = submissions.submit(replace(request(submissions.SubmissionKind.QUESTION), words=title))
        legacy = self.issues / 'Q2.md'
        item = update_registration.OutstandingItem(since='2026-11-17T12:00', text=title, question=title, asked_by=OWNER)
        update_registration.write_question_record(update_registration.QuestionRecord(legacy, OWNER, OWNER, item, 'open'))
        before = self.question_files()
        with self.assertRaises(update_registration.RegistrationFailure) as refused:
            update_registration.withdraw(read_production(OWNER, self.members), title, identities=self.identities)
        self.assertIn(f"Legacy Question {title!r} at {legacy}", str(refused.exception))
        self.assertIn(sent.key, str(refused.exception))
        self.assertEqual(self.question_files(), before)
        projected = next(item for item in review_items.read(self.root / 'review', members=self.members,
            kinds=(review_items.ReviewType.QUESTION,)) if isinstance(item.source, review_items.QuestionSource)
            and item.source.record.path == legacy)
        assert isinstance(projected.source, review_items.QuestionSource)
        self.assertEqual(projected.key, f'question/{OWNER}/Q2.md')
        self.assertEqual(projected.id, f'{review_items.name(OWNER, self.members)}/Q2')
        self.assertNotIn('review_id', projected.source.record.item)
        self.assertEqual(self.question_files(), before)

    def test_f046_seed_refuses_unreadable_investigation(self) -> None:
        store = investigation.folder(OWNER)
        store.mkdir(parents=True)
        path = store / 'I1.md'
        _ = path.write_text(f'---\nid: "I1"\nowner: "{OWNER}"\n---\n\nLegacy record\n')
        original = Path.open

        def read(path_to_read: Path, *args: object, **kwargs: object) -> TextIO:
            if path_to_read == path:
                raise PermissionError('Test read failure')
            return cast('Callable[..., TextIO]', original)(path_to_read, *args, **kwargs)

        with (patch.object(Path, 'open', read), self.assertRaisesRegex((OSError, investigation.InvestigationError), 'I1.md')):
            _ = investigation.file(OWNER, 'Second report', 'Words', waiting_why='Owner answers this')
        self.assertEqual(list(store.glob('*.md')), [path])
        newer = investigation.file(OWNER, 'Second report', 'Words', waiting_why='Owner answers this')
        self.assertEqual(newer.id, 'I2')

    def test_f021_sent_key_cannot_bypass_the_reply_requirements(self) -> None:
        title = 'Check the sent Question'
        ordinary, sent = self.shared_question_title(title)
        ordinary_before = ordinary.path.read_bytes()
        saved = sent.path.read_bytes()
        for action, extra in (('answer', []), ('reply', ['--text', 'A reply without the token']),
                              ('withdraw', [])):
            with self.subTest(action=action):
                args = ['update_registration.py', action, '--production', OWNER, '--key', sent.key, *extra]
                output = StringIO()
                with patch('sys.argv', args), redirect_stdout(output):
                    try:
                        result = update_registration.main()
                    except SystemExit as error:
                        result = error.code
                self.assertEqual(result, 2)
                self.assertIn('needs to reply' if action == 'answer' else 'sent Question', output.getvalue())
                self.assertEqual(sent.path.read_bytes(), saved)
                self.assertEqual(ordinary.path.read_bytes(), ordinary_before)

    def test_f014_migration_preserves_the_ordinary_question_with_the_same_title(self) -> None:
        scope = read_production(OWNER, self.members)
        title = 'Check the result of migration'
        saved = submissions.submit(replace(request(submissions.SubmissionKind.QUESTION), words=title))
        before = saved.path.read_bytes()
        outstanding = update_registration.outstanding_path(scope)
        outstanding.parent.mkdir(exist_ok=True)
        _ = outstanding.write_text(json.dumps([{'since': '2026-11-17T12:00', 'text': title,
                                                'question': 'Ordinary context.', 'asked_by': OWNER}]))
        update_registration.migrate_questions(scope, identities=self.identities)
        ordinary, = [record for record in self.questions() if record.path != saved.path]
        self.assertEqual(ordinary.item.get('question'), 'Ordinary context.')
        self.assertEqual(saved.path.read_bytes(), before)

    def test_f015_pending_retry_finds_the_proposal_in_its_archive(self) -> None:
        payload = replace(request(submissions.SubmissionKind.PROPOSAL), images=(IMAGE,))
        with (patch.object(submissions, '_publish_receipt', side_effect=OSError('Receipt interrupted.')),
              redirect_stderr(StringIO())):
            first = submissions.submit(payload)
        issue, = self.proposals()
        archive = self.root / proposal_issue.ARCHIVE / '2026'
        archive.mkdir(parents=True)
        archived = issue.path.rename(archive / issue.path.name)
        saved = submissions.submit(payload)
        self.assertEqual(saved.id, first.id)
        self.assertEqual((saved.id, saved.path), ('P1', archived))
        self.assertEqual(self.proposals(), [])
        self.assertEqual(submissions.submit(payload), saved)
        image, = saved.details.images
        self.assertEqual((submissions.image_folder(saved) / image.id / image.file).read_bytes(), base64.b64decode(PNG))
        moved = archived.rename(archive.parent / archived.name)
        self.assertEqual(submissions.submit(payload), replace(saved, path=moved))

    def test_f016_pending_bug_retries_after_an_ordinary_investigation_filed(self) -> None:
        payload = replace(request(), images=(IMAGE,))
        with patch.object(record_store.NumberedRecords, 'write', side_effect=OSError('Native write interrupted.')):
            with self.assertRaisesRegex(OSError, 'Native write interrupted'):
                _ = submissions.submit(payload)
        ordinary = investigation.file(OWNER, 'Ordinary investigation', 'Check the ordinary case.', waiting_why='Wait for a check.')
        self.assertEqual(ordinary.id, 'I2')
        saved = submissions.submit(payload)
        self.assertEqual(saved.id, 'I3')
        self.assertEqual(investigation.load(OWNER, ordinary.id), ordinary)
        self.assertEqual(investigation.load(OWNER, saved.id).kind, investigation.InvestigationKind.BUG)
        self.assertEqual(submissions.submit(payload), saved)
        image, = saved.details.images
        self.assertEqual((submissions.image_folder(saved) / image.id / image.file).read_bytes(), base64.b64decode(PNG))

    def test_f017_scoped_reply_finds_the_original_proposal_after_a_folder_change(self) -> None:
        payload = request(submissions.SubmissionKind.PROPOSAL)
        saved = submissions.submit(payload)
        command = shlex.split(change_page.reply_command(saved.key, saved.reply_token))[2:]
        command[-1] = 'Reply to the original proposal'
        new_folder = self.root / 'new-issues'
        new_folder.mkdir()
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(new_folder)))
        other = submissions.submit(request(submissions.SubmissionKind.PROPOSAL, 'other'))
        self.assertEqual((saved.id, other.id), ('P1', 'P2'))
        unchanged = other.path.read_bytes()
        self.assertEqual(change_page.main(command), 0)
        restored = submissions.submit(payload)
        self.assertEqual((restored.key, restored.path), (saved.key, saved.path))
        self.assertTrue(saved.path.exists())
        issue = Issue(restored.path, frontmatter(restored.path))
        self.assertEqual(proposal_issue.review_log(issue)[-1].words, 'Answer: ' + command[-1])
        self.assertEqual(other.path.read_bytes(), unchanged)

    def wrap_fixture(self) -> staff.StaffSnapshot:
        row = self.members.by_id(OWNER)
        assert row is not None
        parent_doc = self.root / 'active-parent.md'
        _ = parent_doc.write_text('> **Status: PRODUCTION — running.**\n')
        parent = staff.StaffRow(PRODUCER, 'showrunner', 'parent', staff.StaffRoot(), current_session=PRODUCER,
                                doc=staff.LiveDoc(local_machine(), parent_doc))
        _ = self.doc.write_text(self.doc.read_text().replace('— running.**', '— wrapped.**'))
        wrapped = staff.StaffSnapshot((replace(row, doc=staff.WrappedDoc(local_machine(), self.doc),
                                               parent=staff.ReportsTo(parent.id)), parent))
        _ = self.enterContext(patch.object(staff, 'snapshot', return_value=wrapped))
        return wrapped

    def tidy_fixture(self, wrapped: staff.StaffSnapshot, document: Path | None = None) -> tuple[int, str]:
        errors = StringIO()
        with (patch.object(staff, 'snapshot', return_value=wrapped),
              patch.object(tidy_wrapped, 'STATE_DIR', self.root / 'state'),
              patch.object(tidy_wrapped, 'NOTIFIER_STATE_DIR', self.root / 'notifier'),
              patch('scripts.copies.copies.remove_trees'), redirect_stderr(errors)):
            result = tidy_wrapped.main([str(self.doc if document is None else document)])
        return result, errors.getvalue()

    def test_f024_send_with_an_earlier_staff_row_uses_the_active_owner(self) -> None:
        payload = request()
        wrapped = self.wrap_fixture()
        errors = StringIO()
        with (ThreadPoolExecutor(max_workers=1) as executor,
              patch.object(staff, 'snapshot', return_value=wrapped),
              patch.object(tidy_wrapped, 'STATE_DIR', self.root / 'state'),
              patch.object(tidy_wrapped, 'NOTIFIER_STATE_DIR', self.root / 'notifier'),
              redirect_stderr(errors)):
            sends: list[Future[submissions.SavedSubmission]] = []

            def after_scan(owner: str) -> None:
                if owner != OWNER:
                    return
                send = executor.submit(submissions.submit, payload, members=self.members)
                sends.append(send)
                try:
                    _ = send.result(timeout=0.2)
                except FutureTimeout:
                    pass

            with patch('scripts.copies.copies.remove_trees', side_effect=after_scan):
                self.assertEqual(tidy_wrapped.main([str(self.doc)]), 0, errors.getvalue())
            send, = sends
            saved = send.result(timeout=5)
        self.assertEqual(investigation.records(OWNER, include_read=True), [])
        self.assertEqual(saved.path.parent, investigation.folder(PRODUCER))
        record, = investigation.records(PRODUCER, include_read=True)
        self.assertEqual((record.id, record.owner), (saved.id, PRODUCER))

    def test_list_owner_config_write_uses_the_test_directory(self) -> None:
        path = conf.path("console")
        live_path = Path(os.path.expanduser("~/.claude/config/console.conf"))
        self.assertNotEqual(path, live_path)
        self.assertEqual(path, self.root / "config" / "console.conf")

        setting = f"page.list.owner={PRODUCER}"
        _ = write_config_fixture(path, setting + "\n")
        self.assertIn(setting, path.read_text(encoding="utf-8").splitlines())

    def test_f098_send_refuses_a_failed_owner_read_before_filing(self) -> None:
        document = self.doc.read_bytes()
        parent_doc = self.root / "parent.md"
        _ = parent_doc.write_text("> **Status: PRODUCTION — running.**\n")
        parent = replace(self.members.rows[1], kind="showrunner", doc=staff.LiveDoc(local_machine(), parent_doc))
        owner = replace(self.members.rows[0], parent=staff.ReportsTo(parent.id))
        members = staff.StaffSnapshot((owner, parent))
        standard = request(seed="missing owner document")
        draft = replace(request(seed="undecodable owner document"), request_id="r2-" + "a" * 32)

        with patch.object(staff, "snapshot", return_value=members):
            self.doc.unlink()
            with self.assertRaisesRegex(submissions.SubmissionError, rf"^The builds production document {re.escape(str(self.doc))}"
                                        + r" is missing\. producer must fix the builds staff row\.$"):
                _ = submissions.submit(standard)
            _ = self.doc.write_bytes(b"\xff")
            with self.assertRaisesRegex(submissions.SubmissionError, rf"^The builds production document {re.escape(str(self.doc))}"
                                        + r" cannot be read\. producer must fix the builds staff row\.$"):
                _ = submissions.submit(draft)
            for name in ("pending.json", "receipt.json", ".indexed", "investigation.water"):
                self.assertEqual(list(self.root.rglob(name)), [])
            self.assertEqual(investigation.records(OWNER), [])
            self.assertEqual(investigation.records(parent.id), [])

            _ = self.doc.write_bytes(document)
            for number, payload in enumerate((standard, draft), 1):
                saved = submissions.submit(payload)
                retry = submissions.submit(payload)
                self.assertEqual((saved.owner, saved.id), (OWNER, f"I{number}"))
                self.assertEqual((retry.key, retry.path), (saved.key, saved.path))
            self.assertEqual([item.id for item in investigation.records(OWNER)], ["I1", "I2"])
            self.assertEqual(investigation.records(parent.id), [])

        producer_doc = self.root / "producer.md"
        owner = self.members.rows[0]
        producer = replace(self.members.rows[1], doc=staff.LiveDoc(local_machine(), producer_doc),
                           parent=staff.ReportsTo(owner.id))
        members = staff.StaffSnapshot((owner, producer))
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={producer.id}\n")
        standard = replace(request(seed="producer standard", recipient=producer.id), images=(IMAGE,))
        list_page = replace(request(seed="producer list", recipient=producer.id), page="list", images=(IMAGE,))
        review = replace(standard, request_id="r2-" + "b" * 32, page="review",
                         page_recipient=new_item_dialog.PageRecipient.LIST_OWNER)
        before = {path: path.read_bytes() for path in self.root.rglob("receipt.json")}
        with patch.object(staff, "snapshot", return_value=members):
            for content in (b"", b"\xff"):
                if content:
                    _ = producer_doc.write_bytes(content)
                for payload in (standard, list_page, review):
                    with self.subTest(page=payload.page, content=content):
                        state = "cannot be read" if content else "is missing"
                        with self.assertRaisesRegex(submissions.SubmissionError,
                                                    rf"^The producer production document {re.escape(str(producer_doc))}"
                                                    + rf" {state}\. builds must fix the producer staff row\.$"):
                            _ = submissions.submit(payload)
                self.assertEqual({path: path.read_bytes() for path in self.root.rglob("receipt.json")}, before)
                self.assertEqual(list(self.root.rglob("pending.json")), [])
                self.assertEqual(list(self.root.rglob("image.png")), [])
                self.assertEqual(investigation.records(producer.id), [])
                self.assertEqual([item.id for item in investigation.records(owner.id)], ["I1", "I2"])

            _ = producer_doc.write_text("> **Status: PRODUCTION — wrapped.**\n")
            list_draft = replace(list_page, request_id="r2-" + "c" * 32, words="Check this from the List.")
            for number, payload in enumerate((standard, list_page, list_draft, review), 3):
                with self.subTest(page=payload.page, request_id=payload.request_id):
                    saved = submissions.submit(payload)
                    self.assertEqual((saved.owner, saved.id), (owner.id, f"I{number}"))
                    self.assertEqual(submissions.submit(payload).key, saved.key)
            self.assertEqual(investigation.records(producer.id), [])
            self.assertEqual([item.id for item in investigation.records(owner.id)], ["I1", "I2", "I3", "I4", "I5", "I6"])

    def test_f002_allocation_refusal_leaves_the_request_free_for_an_edited_retry(self) -> None:
        other_doc = self.root / "missing-production.md"
        other = replace(self.members.rows[1], kind="showrunner", doc=staff.LiveDoc(local_machine(), other_doc))
        members = staff.StaffSnapshot((self.members.rows[0], other))
        payload = replace(request(submissions.SubmissionKind.PROPOSAL), images=(IMAGE,))
        draft = replace(payload, request_id="r2-" + "1" * 32)
        folder = self.root / "review-attachments" / payload.request_id
        with patch.object(staff, "snapshot", return_value=members):
            for candidate in (payload, draft, draft):
                with self.assertRaisesRegex(submissions.SubmissionError,
                                            r"Cannot check the record folders.*missing-production.md"):
                    _ = submissions.submit(candidate)
                for name in ("pending.json", "receipt.json", "images", ".indexed", "recipient-*"):
                    self.assertEqual(list(self.root.rglob(name)), [])
            self.assertEqual(self.proposals(), [])

            _ = other_doc.write_text(f"- **Proposals:** {self.root / 'other-issues'}\n")
            registry = self.root / "state" / "review-folders" / OWNER / "proposal.json"
            registry.parent.mkdir(parents=True)
            _ = registry.write_text("[]\n")
            selected = False
            next_id = record_store.LockedRecordNumbers.next_id
            read_text = Path.read_text
            read_bytes = Path.read_bytes

            def select_id(numbers: record_store.LockedRecordNumbers, *folders: Path,
                          covered_owner: staff.StaffId | None = None) -> str:
                nonlocal selected
                identity = next_id(numbers, *folders, covered_owner=covered_owner)
                selected = True
                return identity

            def refuse_registry_read(path: Path, encoding: str | None = None, errors: str | None = None,
                                     newline: str | None = None) -> str:
                if path == registry and selected:
                    raise PermissionError("Cannot read the folder registry.")
                return read_text(path, encoding, errors, newline)

            def refuse_registry_backup(path: Path) -> bytes:
                if path == registry:
                    raise PermissionError("Cannot read the folder registry for rollback.")
                return read_bytes(path)

            for operation, refusal in (("read_text", refuse_registry_read), ("read_bytes", refuse_registry_backup)):
                selected = False
                with (self.subTest(operation=operation),
                      patch.object(record_store.LockedRecordNumbers, "next_id", select_id),
                      patch.object(Path, operation, refusal)):
                    with self.assertRaisesRegex(PermissionError, "Cannot read the folder registry"):
                        _ = submissions.submit(payload)
                    self.assertTrue(selected)
                    self.assertFalse(folder.exists())
                    self.assertEqual(self.proposals(), [])
                    self.assertFalse((self.root / "state" / "record-ids" / "proposal.water").exists())
                self.assertEqual(registry.read_text(), "[]\n")

            edited = replace(payload, words="File the edited proposal.")
            saved = submissions.submit(edited)
            self.assertEqual(submissions.submit(edited), saved)
        self.assertEqual(saved.id, "P1")
        self.assertEqual([proposal.path for proposal in self.proposals()], [saved.path])
        self.assertTrue(proposal_issue.read_issue(saved.path).endswith(edited.words + "\n"))
        image, = saved.details.images
        self.assertEqual((folder / "images" / image.id / image.file).read_bytes(), base64.b64decode(PNG))

    def test_f003_stale_review_action_keeps_the_unnumbered_question_wait(self) -> None:
        path = self.issues / "question-legacy.md"
        item = update_registration.OutstandingItem(
            since="2026-11-17T12:00", text="Legacy question", question="What changed?", asked_by=OWNER,
            followup=update_registration.PendingFollowup(text="Check the first result.", sent="2026-11-17T12:30"))
        record = update_registration.QuestionRecord(path, OWNER, OWNER, item, "open")
        update_registration.write_question_record(record)
        stale = review_items.question_item(record, self.members)
        pending = update_registration.PendingFollowup(text="Check the current result.", sent="2026-11-17T12:45")
        current = replace(record, item={**item, "followup": pending})
        update_registration.write_question_record(current)
        before = path.read_bytes()
        root = self.root / "review"
        status = change_page.DeliveryStatus(root)
        browser = change_page.ReviewBrowser("fixture-token", "")
        body = urlencode({"row": stale.key, "token": change_browser.row_token(browser, stale.key),
                          "outcome": "question", "feedback": "Which result is correct?"})
        with patch.object(change_browser, "browser_manifest", return_value=(browser.token, browser.initial)):
            with (patch.object(review_items, "read", return_value=[stale]),
                  patch.object(status, "write_slots", wraps=status.write_slots) as write):
                with self.assertRaisesRegex(review_items.ReviewRecordChanged, "follow-up on this question changed"):
                    _ = review_page.submit(root, body, status)
                write.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.questions()[0].item.get("followup"), pending)
            self.assertNotIn("review_id", self.questions()[0].item)
            with patch.object(review_items, "read", return_value=[review_items.question_item(current, self.members)]):
                delivery, _ = review_page.submit(root, body, status)
        self.assertIsInstance(delivery, change_page.MessageDelivery)
        stored, = self.questions()
        self.assertEqual(stored.item.get("review_id"), "Q1")
        self.assertEqual(stored.item.get("waiting_why"), "Which result is correct?")
        self.assertIn("Superseded followup: Check the current result.", stored.item.get("review_log", ""))

    def test_send_to_a_wrapped_owner_files_with_the_active_owner(self) -> None:
        wrapped = self.wrap_fixture()
        parent = wrapped.rows[1]
        assert isinstance(parent.doc, staff.LiveDoc)
        parent_issues = self.root / "parent-issues"
        _ = parent.doc.path.write_text(self.doc.read_text().replace("— wrapped.**", "— running.**")
                                       .replace(str(self.issues), str(parent_issues)))
        for kind in submissions.SubmissionKind:
            with self.subTest(kind=kind):
                payload = request(kind)
                saved = submissions.submit(payload, members=self.members)
                expected = (parent_issues if kind in (submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.PROPOSAL)
                            else investigation.folder(parent.id))
                self.assertEqual((saved.owner, saved.path.parent), (parent.id, expected))
                self.assertEqual(saved.request_id, payload.request_id)
                self.assertEqual(submissions.submit(payload), saved)
                token = saved.reply_token
                (submissions.image_folder(saved).parent / "receipt.json").unlink()
                former = replace(wrapped.rows[0], parent=staff.StaffRoot())
                with patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((former, parent))):
                    self.assertTrue(submissions.answer(saved.key, "Answer at the filing owner", token))
                    self.assertEqual(submissions.submit(payload), saved)
        self.assertEqual(investigation.records(OWNER, include_read=True), [])
        self.assertEqual(self.questions(), [])
        self.assertEqual(self.proposals(), [])

    def test_f026_send_retry_and_reply_find_the_bug_in_the_parent_showrunners_archive(self) -> None:
        payload = request()
        saved = submissions.submit(payload)
        token = saved.reply_token
        wrapped = self.wrap_fixture()
        self.assertEqual(self.tidy_fixture(wrapped), (0, ''))
        moved = investigation.folder(PRODUCER) / saved.path.name
        receipt = submissions.image_folder(saved).parent / 'receipt.json'
        location = cast(dict[str, str], json.loads(receipt.read_text()))
        self.assertEqual((location['id'], location['owner'], location['path'], location['original_path']),
                         (saved.id, PRODUCER, str(moved), str(saved.path)))
        self.assertFalse(saved.path.exists())
        archive = moved.parent / 'archive' / '2026'
        archive.mkdir(parents=True)
        archived = moved.rename(archive / moved.name)
        with (patch.object(investigation, 'review_record_paths', wraps=investigation.review_record_paths) as search,
              patch.object(review, "review_question", return_value=review.QuestionAccepted("Answer in the archive"))):
            self.assertTrue(submissions.answer(saved.key, 'Answer in the archive', token, members=wrapped))
            self.assertEqual(search.call_args_list, [((PRODUCER,), {"strict": True})] * 2)
            search.reset_mock()
            retry = submissions.submit(payload, members=wrapped)
            search.assert_called_once_with(PRODUCER, strict=True)
        self.assertEqual((retry.id, retry.owner, retry.path), (saved.id, PRODUCER, archived))
        self.assertEqual((retry.key, retry.reply_token), (saved.key, ""))
        self.assertTrue(new_item_dialog.matches_reply_token(retry.details, token))
        self.assertTrue(archived.exists())
        self.assertEqual(investigation.read_review_record(archived).answer, 'Answer in the archive')

    def test_f030_investigation_allocation_skips_archive_ids(self) -> None:
        saved = submissions.submit(request())
        self.assertEqual(saved.id, 'I1')
        native = investigation.file(PRODUCER, 'Archive seed', 'Keep this record.', waiting_why='Check it.')
        self.assertEqual(native.id, 'I2')
        active = investigation.folder(PRODUCER) / 'I2.md'
        archive = active.parent / 'archive'
        archive.mkdir()
        _ = active.rename(archive / active.name)
        second = investigation.file(PRODUCER, 'Archive', 'Keep this record too.', waiting_why='Check it.')
        self.assertEqual(second.id, 'I3')

    def test_f031_archived_bug_stays_in_place_and_returns_to_the_open_queue(self) -> None:
        saved = submissions.submit(request())
        token = saved.reply_token
        archive = saved.path.parent / 'archive'
        archive.mkdir()
        archived = saved.path.rename(archive / saved.path.name)
        self.assertTrue(submissions.answer(saved.key, 'Answer in Open', token))
        self.assertTrue(archived.exists())
        record, = investigation.records(OWNER)
        self.assertEqual(record.answer, 'Answer in Open')

    def test_f031_archived_question_and_proposal_keep_their_paths(self) -> None:
        for kind in (submissions.SubmissionKind.QUESTION, submissions.SubmissionKind.PROPOSAL):
            with self.subTest(kind=kind):
                payload = request(kind)
                saved = submissions.submit(payload)
                token = saved.reply_token
                archive = saved.path.parent / 'archive'
                archive.mkdir(exist_ok=True)
                archived = saved.path.rename(archive / saved.path.name)
                self.assertTrue(submissions.answer(saved.key, 'Answer in Open', token))
                self.assertTrue(archived.exists())
                self.assertEqual(submissions.submit(payload), replace(saved, path=archived))
                if kind == submissions.SubmissionKind.QUESTION:
                    record = update_registration.read_review_question(archived)
                    self.assertEqual(record.item.get('followups', [])[-1]['reply'], 'Answer in Open')
                else:
                    issue = Issue(archived, frontmatter(archived))
                    self.assertEqual(proposal_issue.review_log(issue)[-1].words, 'Answer: Answer in Open')

    def test_f033_doc_can_finish_before_the_staff_row_changes(self) -> None:
        wrapped = self.wrap_fixture()
        live = replace(wrapped.rows[0], doc=staff.LiveDoc(local_machine(), self.doc))
        stale = replace(wrapped, rows=(live, wrapped.rows[1]))
        self.assertEqual(self.tidy_fixture(stale), (0, ''))
        with patch.object(staff, 'snapshot', return_value=stale):
            saved = submissions.submit(request())
        self.assertEqual(investigation.records(OWNER), [])
        self.assertEqual(saved.path.parent, investigation.folder(PRODUCER))
        self.assertEqual(investigation.load(PRODUCER, saved.id).owner, PRODUCER)

    def test_f035_missing_receipt_finds_the_bug_in_the_parent_showrunners_store(self) -> None:
        payload = request()
        saved = submissions.submit(payload)
        token = saved.reply_token
        wrapped = self.wrap_fixture()
        self.assertEqual(self.tidy_fixture(wrapped), (0, ''))
        receipt = submissions.image_folder(saved).parent / 'receipt.json'
        receipt.unlink()
        self.assertTrue(submissions.answer(saved.key, 'Reply without a receipt', token, members=wrapped))
        retry = submissions.submit(payload, members=wrapped)
        self.assertEqual((retry.id, retry.key, retry.reply_token), (saved.id, saved.key, ""))
        self.assertTrue(new_item_dialog.matches_reply_token(retry.details, token))
        self.assertEqual((retry.owner, retry.path), (PRODUCER, investigation.folder(PRODUCER) / saved.path.name))
        self.assertEqual(investigation.load(PRODUCER, saved.id).answer, 'Reply without a receipt')
        self.assertEqual(investigation.records(OWNER, include_read=True), [])
        location = cast(dict[str, str], json.loads(receipt.read_text()))
        self.assertEqual((location['id'], location['owner'], location['path']),
                         (saved.id, PRODUCER, str(retry.path)))

    def test_f021_and_f035_answer_key_survives_a_folder_change(self) -> None:
        payload = request(submissions.SubmissionKind.QUESTION)
        sent = submissions.submit(payload)
        self.assertTrue(submissions.answer(sent.key, 'Reply in the original folder', sent.reply_token))
        command = shlex.split(next_item.show('question', urgency.OLDEST, self.doc).splitlines()[-1])[2:]
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(self.root / 'new-issues')))
        self.assertEqual(submissions.submit(payload), sent)
        with patch('sys.argv', ['update_registration.py', *command]):
            self.assertEqual(update_registration.main(), 0)
        self.assertEqual(update_registration.read_review_question(sent.path).status, 'closed')

    def test_f035_native_folder_history_survives_a_folder_change_without_receipts(self) -> None:
        question = submissions.submit(request(submissions.SubmissionKind.QUESTION))
        proposal = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        self.assertTrue(submissions.answer(question.key, 'Reply before the folder change', question.reply_token))
        command = shlex.split(next_item.show('question', urgency.OLDEST, self.doc).splitlines()[-1])[2:]
        (submissions.image_folder(question).parent / 'receipt.json').unlink()
        token = proposal.reply_token
        receipt = submissions.image_folder(proposal).parent / 'receipt.json'
        _ = receipt.write_text('An unreadable receipt.')
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(self.root / 'new-issues')))
        with patch('sys.argv', ['update_registration.py', *command]):
            self.assertEqual(update_registration.main(), 0)
        self.assertEqual(update_registration.read_review_question(question.path).status, 'closed')
        self.assertTrue(submissions.answer(proposal.key, 'Reply after the folder change', token))
        restored = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        self.assertEqual((restored.key, restored.path), (proposal.key, proposal.path))
        self.assertTrue(proposal.path.exists())

    def test_f032_mark_important_uses_the_sent_question_key(self) -> None:
        title = 'Check the shared title'
        sent = submissions.submit(replace(request(submissions.SubmissionKind.QUESTION), words=title))
        self.assertTrue(submissions.answer(sent.key, 'The recipient reply', sent.reply_token))
        scope = read_production(OWNER, self.members)
        parts = update_registration.QuestionParts(ask=title, context='', items=[], recommend='', choices=[])
        with patch.object(update_registration, 'review_open_question',
                          return_value=update_registration.ReviewedQuestion(title, title)):
            update_registration.ask(scope, title, parts, '', update_registration.Ranking(), identities=self.identities)
        procedure = (Path(__file__).resolve().parents[2] / 'commands' / 'next.md').read_text()
        with patch('sys.argv', ['update_registration.py', 'mark-important', '--production', OWNER, '--key', sent.key]):
            try:
                result = update_registration.main()
            except SystemExit as error:
                result = error.code
        self.assertEqual(result, 0)
        self.assertIn('mark-important --key', procedure)
        self.assertEqual(update_registration.read_review_question(sent.path).item.get('urgency'), 'user')
        ordinary, = (record for record in self.questions() if record.path != sent.path)
        self.assertNotEqual(ordinary.item.get('urgency'), 'user')

    def test_f027_proposal_followup_preserves_pasted_frontmatter_text_in_the_reply(self) -> None:
        saved = submissions.submit(request(submissions.SubmissionKind.PROPOSAL))
        words = 'First\u2028review_state: pasted\u2029Last'
        self.assertTrue(submissions.answer(saved.key, words, saved.reply_token))
        self.assertEqual(proposal_issue.review_log(self.proposals()[0])[-1].words, 'Answer: ' + words)
        self.assertTrue(proposal_issue.record_review(saved.path, 'question: Please clarify', review_events.ReviewMark.WAITING,
                                                    date.today(), waiting_why='The recipient clarifies the answer.'))
        issue, = self.proposals()
        self.assertEqual(proposal_issue.review_log(issue)[-2].words, 'Answer: ' + words)
        self.assertEqual(proposal_issue.review_mark(issue), review_events.ReviewMark.WAITING)

    def test_f019_unicode_line_separators_round_trip_in_a_reply(self) -> None:
        saved = submissions.submit(request())
        words = 'Alpha\u2028Beta\u2029Gamma'
        self.assertTrue(submissions.answer(saved.key, words, saved.reply_token))
        item = investigation.load(OWNER, saved.id)
        self.assertEqual(item.answer, words)
        self.assertEqual(item.log[-1].words, 'Answer: ' + words)
        self.assertEqual(submissions.submit(request(submissions.SubmissionKind.INVESTIGATION, 'after-reply')).id, 'I2')

    def test_f020_retry_finds_the_proposal_restored_to_its_first_folder(self) -> None:
        payload = request(submissions.SubmissionKind.PROPOSAL)
        saved = submissions.submit(payload)
        archive = self.root / proposal_issue.ARCHIVE
        archive.mkdir()
        archived = saved.path.rename(archive / saved.path.name)
        self.assertEqual(submissions.submit(payload).path, archived)
        receipt = cast(dict[str, str], json.loads((submissions.image_folder(saved).parent / "receipt.json").read_text()))
        self.assertEqual((receipt["original_path"], receipt["path"]), (str(saved.path), str(archived)))
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.issues), str(self.root / 'new-issues')))
        _ = archived.rename(saved.path)
        with patch.object(proposal_issue, 'find_review_proposal', side_effect=AssertionError('Retry scanned the store.')):
            self.assertEqual(submissions.submit(payload), saved)

    def test_f021_next_displays_the_command_that_closes_only_the_sent_question(self) -> None:
        title = 'Check the shared title'
        payload = replace(request(submissions.SubmissionKind.QUESTION), words=title)
        sent = submissions.submit(payload)
        self.assertTrue(submissions.answer(sent.key, 'The recipient reply', sent.reply_token))
        sent_record = update_registration.read_review_question(sent.path)
        sent_record.item['since'] = '2025-01-01T00:00'
        update_registration.write_question_record(sent_record)
        scope = read_production(OWNER, self.members)
        parts = update_registration.QuestionParts(ask=title, context='', items=[], recommend='', choices=[])
        with patch.object(update_registration, 'review_open_question',
                          return_value=update_registration.ReviewedQuestion(title, title)):
            update_registration.ask(scope, title, parts, '', update_registration.Ranking(), identities=self.identities)
        text = next_item.show('question', urgency.OLDEST, self.doc)
        self.assertIn('The recipient reply', text)
        command = shlex.split(text.splitlines()[-1])[2:]
        self.assertIn('--key', command)
        self.assertIn(sent.key, command)
        with patch('scripts.production.merge_checkpoint.merged_checkpoints', return_value=()):
            state = update_registration.ShowrunnerState((), (), update_registration.KeepOutstanding())
            state_words = update_registration.state_block(scope, state, '2026-11-17 13:00')
        self.assertIn(f'{title} ({sent.key})', state_words)
        self.assertEqual(state_words.count(title), 2)
        procedure = (Path(__file__).resolve().parents[2] / 'commands' / 'next.md').read_text()
        sent_instructions = procedure.split('## question\n', 1)[1].split('## investigation\n', 1)[0]
        self.assertIn('answer --key', sent_instructions)
        with patch('sys.argv', ['update_registration.py', *command]):
            self.assertEqual(update_registration.main(), 0)
        self.assertEqual(update_registration.read_review_question(sent.path).status, 'closed')
        ordinary, = (record for record in self.questions() if record.path != sent.path)
        self.assertEqual(ordinary.status, 'open')
        self.assertNotIn(sent.key, next_item.show('question', urgency.OLDEST, self.doc))

    def test_f022_pasted_images_use_the_registration_helper(self) -> None:
        with patch.object(image_store, 'register_file', wraps=image_store.register_file) as register:
            saved = submissions.submit(replace(request(), images=(IMAGE,)))
        register.assert_called_once()
        image, = saved.details.images
        folder = submissions.image_folder(saved) / image.id
        self.assertEqual(image_store.read_record(folder)['file'], image.file)
        self.assertEqual((folder / image.file).read_bytes(), base64.b64decode(PNG))

    def test_f023_staff_sources_only_read_the_fixture_pane_store(self) -> None:
        fixture = self.root / 'panes'
        self.assertEqual(pane_record.record_directory(), fixture)
        fixture.mkdir()
        paths: list[Path] = []
        _ = session_staff.member_sources(dependencies=paths.append)
        self.assertIn(fixture, [path.parent for path in paths])

    def test_bad_request_and_invalid_image_leave_no_record(self) -> None:
        for invalid in (
                replace(request(), words="   "), replace(request(), recipient_id=staff.StaffId("../other")),
                replace(request(), request_id="bad id"),
                replace(request(), images=(submissions.PastedImage("image/svg+xml", PNG),)),
                replace(request(), images=(submissions.PastedImage("image/png", "invalid base64"),)),
                replace(request(), images=(submissions.PastedImage("image/jpeg", PNG),)),
                replace(request(), images=(IMAGE,)*5)):
            with self.subTest(invalid=invalid):
                with self.assertRaises(submissions.SubmissionError):
                    _ = submissions.submit(invalid)
        self.assertEqual(investigation.records(OWNER), [])
        self.assertEqual(self.questions(), [])
        self.assertEqual(self.proposals(), [])


_NATEDEV = staff.StaffRow(staff.StaffId("natedev-staff"), "showrunner", "natedev", staff.StaffRoot(),
                         current_session=staff.StaffId("natedev-session"),
                         doc=staff.LiveDoc(local_machine(), Path("/natedev-production.md")))
_SDLC = replace(_NATEDEV, id=staff.StaffId("sdlc-staff"), name="sdlc", parent=staff.ReportsTo(_NATEDEV.id))
_BUILDS = replace(_NATEDEV, id=staff.StaffId("builds-staff"), name="builds", parent=staff.ReportsTo(_NATEDEV.id))
_AWS = staff.StaffRow(staff.StaffId("aws-staff"), "unit", "aws", staff.ReportsTo(_BUILDS.id),
                      current_session=staff.StaffId("aws-session"), unit_type=staff.UnitDetails("direct"))
_MORTY = staff.StaffRow(staff.StaffId("morty-staff"), "producer", "morty", staff.StaffRoot(),
                        current_session=staff.StaffId("morty-session"))


@final
class PageOwnerReasonTests(unittest.TestCase):
    """Each route to a page owner returns the sentence that says why."""

    root: Path = Path()
    natedev: staff.StaffRow = _NATEDEV
    sdlc: staff.StaffRow = _SDLC
    builds: staff.StaffRow = _BUILDS
    aws: staff.StaffRow = _AWS
    morty: staff.StaffRow = _MORTY
    members: staff.StaffSnapshot = staff.StaffSnapshot((_NATEDEV, _SDLC, _BUILDS, _AWS, _MORTY))

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def route(self, page: str, owner: str, members: staff.StaffSnapshot) -> submissions.PageOwnerRoute:
        config = self.root / "console.conf"
        owner_line = f"page.{page}.owner={owner}\n" if owner else ""
        _ = config.write_text(f"page.{page}.label={page.replace('_', ' ').title()}\n{owner_line}"
                              + f"owner.default={self.sdlc.id}\n", encoding="utf-8")
        route = submissions.page_owners(page, conf.read(config), members).route
        assert isinstance(route, submissions.PageOwnerRoute)
        return route

    def test_reason_names_the_owner_that_takes_the_item(self) -> None:
        for page, owner, expected in (("list", self.morty, "(why morty? because morty owns the List page!)"),
                                      ("builds", self.builds, "(why builds? because builds owns the Builds page!)")):
            with self.subTest(page=page):
                route = self.route(page, owner.id, self.members)
                self.assertEqual((route.row, route.reason), (owner, expected))
        config = self.root / "console.conf"
        _ = config.write_text(f"page.rebuilds.owner={self.natedev.id}\n", encoding="utf-8")
        route = submissions.page_owners("rebuilds", conf.read(config), self.members).route
        assert isinstance(route, submissions.PageOwnerRoute)
        self.assertEqual(route.reason, "(why natedev? because natedev owns the Rebuilds page!)")

    def test_reason_names_the_showrunner_of_a_unit_owner(self) -> None:
        route = self.route("aws_machines", self.aws.id, self.members)
        self.assertEqual((route.row, route.reason), (self.builds, "(why builds? because aws owns the Aws Machines page"
                                                     + " and builds is its showrunner!)"))
        worker = staff.StaffRow(staff.StaffId("worker-staff"), "worker", "fixer", staff.ReportsTo(self.aws.id),
                                current_session=staff.StaffId("worker-session"))
        route = self.route("aws_machines", worker.id, replace(self.members, rows=(*self.members.rows, worker)))
        self.assertEqual((route.row, route.reason), (self.builds, "(why builds? because fixer owns the Aws Machines page"
                                                     + " and builds is the nearest showrunner above it!)"))

    def test_reason_names_the_owner_that_is_not_available_and_the_parent_that_takes_the_item(self) -> None:
        retired = replace(self.members, pending_retire=(self.builds.id,))
        expected = "(why natedev? because builds is not available and natedev is the nearest available showrunner above it!)"
        for page, owner in (("builds", self.builds), ("aws_machines", self.aws)):
            with self.subTest(page=page):
                route = self.route(page, owner.id, retired)
                self.assertEqual((route.row, route.reason), (self.natedev, expected))

    def test_reason_names_the_default_owner_when_no_owner_in_the_chain_is_available(self) -> None:
        retired = replace(self.members, pending_retire=(self.morty.id,))
        route = self.route("list", self.morty.id, retired)
        self.assertEqual((route.row, route.reason),
                         (self.sdlc, "(why sdlc? because morty is not available and sdlc is the default owner!)"))

    def test_reason_names_the_default_owner_when_the_page_has_no_owner(self) -> None:
        route = self.route("disks", "", self.members)
        self.assertEqual((route.row, route.reason),
                         (self.sdlc, "(why sdlc? because the Disks page has no owner and sdlc is the default owner!)"))

    def test_reason_names_the_default_owner_when_the_owner_is_not_on_the_staff_list(self) -> None:
        route = self.route("disks", "missing-staff", self.members)
        self.assertEqual((route.row, route.reason), (
            self.sdlc, "(why sdlc? because the Disks page owner is not on the staff list and sdlc is the default owner!)"))

    def test_reason_names_the_default_owner_when_a_unit_owner_has_no_showrunner(self) -> None:
        orphan = replace(self.aws, parent=staff.StaffRoot())
        members = staff.StaffSnapshot((self.natedev, self.sdlc, self.builds, orphan, self.morty))
        route = self.route("aws_machines", orphan.id, members)
        self.assertEqual((route.row, route.reason),
                         (self.sdlc, "(why sdlc? because aws has no showrunner and sdlc is the default owner!)"))

    def test_reason_names_the_parent_of_a_default_owner_that_is_not_available(self) -> None:
        retired = replace(self.members, pending_retire=(self.sdlc.id,))
        route = self.route("disks", "", retired)
        self.assertEqual((route.row, route.reason), (self.natedev, "(why natedev? because the Disks page has no owner,"
                                                     + " the default owner sdlc is not available, and natedev"
                                                     + " is the nearest available showrunner above it!)"))

    def test_no_available_owner_has_no_route_and_no_reason(self) -> None:
        config = self.root / "console.conf"
        _ = config.write_text("page.disks.label=Disks\nowner.default=missing-staff\n", encoding="utf-8")
        owners = submissions.page_owners("disks", conf.read(config), self.members)
        self.assertEqual(owners, submissions.PageOwners(submissions.PageOwnerUnavailable(),
                                                        submissions.PageOwnerUnavailable()))
