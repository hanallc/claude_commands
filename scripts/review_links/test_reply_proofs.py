"""Test proof publication in a scratch state folder."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field, replace
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import patch
from uuid import uuid4

from scripts.agents.remote import local_machine
from scripts.lib import atomic_file, conf
from scripts.lib.fixture_test_support import set_up_fixture
from scripts.production import record_store, staff, test_record_store, update_registration
from scripts.production.production_doc import production_backup_checkout
from scripts.production.production_names import BACKUP_DOCUMENT_DIRECTORY
from scripts.review_links import reply_proofs, reply_tokens, review_link, review_page, review_submissions, test_review_submissions


@dataclass(frozen=True)
class ArchivedLegacyQuestion:
    submission: review_submissions.SavedSubmission = field(repr=False)
    path: Path
    clean: bytes = field(repr=False)
    document: Path
    backup: Path


class ReplyProofTests(unittest.TestCase):
    def _archive_legacy_question_in_former_store(self, *, local_checkout: bool = False) -> ArchivedLegacyQuestion:
        case = set_up_fixture(self.addCleanup, test_review_submissions.ReviewSubmissionTests())
        git = test_record_store.NumberedRecordsTests().git
        document_folder = case.doc.parent
        if local_checkout:
            document_folder = case.root / "production-checkout"
            document_folder.mkdir()
            _ = git(document_folder, "init", "-b", "main")
        document = case.doc.rename(document_folder / "production-live.md")
        owner = case.members.by_id(test_review_submissions.OWNER)
        assert owner is not None
        owner_id = staff.StaffId(str(uuid4()))
        owner = replace(owner, id=owner_id, current_session=owner_id, doc=staff.LiveDoc(local_machine(), document))
        members = staff.StaffSnapshot((owner,))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=members))
        self.enterContext(staff.supplied_snapshot(members))
        self.enterContext(patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "STAFF_FILE": str(case.root / "staff.json"),
        }))
        _ = staff.record_path().write_text(members.encode())
        self.assertEqual(staff.local_snapshot(), members)

        saved = review_submissions.submit(test_review_submissions.request(
            review_submissions.SubmissionKind.QUESTION, recipient=owner.id))
        clean = saved.path.read_bytes()
        _ = case.put_proof_in_old_note(saved)
        archive = case.issues / "archive"
        archive.mkdir()
        note = saved.path.rename(archive / saved.path.name)
        shutil.rmtree(record_store.state_root() / "review-folders")

        backup = production_backup_checkout(owner.id)
        backup_doc = backup / BACKUP_DOCUMENT_DIRECTORY / document.name
        backup_doc.parent.mkdir(parents=True)
        _ = git(backup, "init", "-b", "main")
        live_text = document.read_text()
        _ = backup_doc.write_text(live_text)
        _ = git(backup, "add", str(backup_doc))
        _ = git(backup, "commit", "-m", "Set the former store.")
        current = live_text.replace(str(case.issues), str(case.root / "current-issues"))
        _ = document.write_text(current)
        _ = backup_doc.write_text(current)
        _ = git(backup, "add", str(backup_doc))
        _ = git(backup, "commit", "-m", "Set the current store.")

        record, = update_registration.question_records_for_document(
            document, update_registration.QuestionStaff.for_record(members), strict=True)
        self.assertEqual(record.path, note)
        return ArchivedLegacyQuestion(saved, note, clean, document, backup)

    def _assert_scan_refuses_path(self, path: Path) -> None:
        with self.subTest(entry="dry run"):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                self.assertEqual(review_submissions.main(["move-reply-proofs", "--dry-run"]), 1)
            self.assertTrue(str(path) in errors.getvalue())
        with self.subTest(entry="startup"):
            with self.assertRaises(OSError) as refusal:
                cache = review_page.ReviewSnapshotCache(review_link.review_root(), start_worker=False, initialize_on_start=True)
                self.addCleanup(cache.close)
            self.assertTrue(str(path) in str(refusal.exception))

    def test_dry_run_and_startup_name_the_unreadable_staff_record(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        legacy = question.path.read_bytes()
        record = staff.record_path()
        _ = record.write_text("[]")

        self._assert_scan_refuses_path(record)
        self.assertTrue(question.path.read_bytes() == legacy)
        self.assertFalse(reply_proofs.move_journal_path().exists())

    def test_dry_run_and_startup_name_the_unreadable_former_store_archive(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        legacy = question.path.read_bytes()
        archive = question.path.parent
        scandir = os.scandir

        def deny_archive(folder: str | os.PathLike[str]) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
            if folder == archive:
                raise PermissionError("Cannot open the archive.")
            return scandir(folder)

        with patch.object(os, "scandir", side_effect=deny_archive):
            self._assert_scan_refuses_path(archive)
        self.assertTrue(question.path.read_bytes() == legacy)
        self.assertFalse(reply_proofs.move_journal_path().exists())

    def _assert_move_skips_archived_question(self, question: ArchivedLegacyQuestion) -> None:
        legacy = question.path.read_bytes()
        move = review_submissions.LocalProofMove(review_link.review_root())
        self.assertEqual(review_submissions.move_reply_proofs(move, dry_run=True), (0, 0))
        cache = review_page.ReviewSnapshotCache(move.review_root, start_worker=False, initialize_on_start=True)
        self.addCleanup(cache.close)
        self.assertTrue(question.path.read_bytes() == legacy)
        self.assertFalse(reply_proofs.read_move_report())
        saved = question.submission
        self.assertFalse(reply_proofs.path_for(saved.details.proof_identity(saved.id)).exists())

    def test_dry_run_and_startup_skip_a_backup_when_git_finds_no_repository(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        (question.backup / ".git" / "HEAD").unlink()
        self._assert_move_skips_archived_question(question)

    def test_dry_run_and_journal_name_the_skipped_backup(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        journal = reply_proofs.move_journal_path()
        with redirect_stdout(StringIO()) as preview:
            self.assertEqual(review_submissions.main(["move-reply-proofs", "--dry-run"]), 0)
        self.assertEqual(len(preview.getvalue().splitlines()), 3)

        head = question.backup / ".git" / "HEAD"
        saved_head = head.read_bytes()
        head.unlink()
        with redirect_stdout(StringIO()) as preview:
            self.assertEqual(review_submissions.main(["move-reply-proofs", "--dry-run"]), 0)
        self.assertTrue(preview.getvalue().splitlines()[3:] == [str(question.backup)])
        self.assertFalse(journal.parent.exists())

        self._assert_move_skips_archived_question(question)
        entry = json.dumps({"skipped_backup": str(question.backup)})
        self.assertTrue(journal.read_text().splitlines() == [entry])
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        self.assertEqual(tuple(journal.parent.iterdir()), (journal,))
        self._assert_move_skips_archived_question(question)
        self.assertTrue(journal.read_text().splitlines() == [entry])

        _ = head.write_bytes(saved_head)
        with redirect_stdout(StringIO()) as preview:
            self.assertEqual(review_submissions.main(["move-reply-proofs", "--dry-run"]), 0)
        self.assertEqual(len(preview.getvalue().splitlines()), 3)
        self._assert_startup_moves_archived_question(question, question.path.read_bytes())
        self.assertEqual(journal.read_text().splitlines().count(entry), 1)

    def test_startup_syncs_a_skip_line_after_journal_sync_fails(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        (question.backup / ".git" / "HEAD").unlink()
        journal = reply_proofs.move_journal_path()
        root = review_link.review_root()
        sync = os.fsync

        def fail_journal_sync(descriptor: int) -> None:
            if journal.exists() and os.path.samestat(os.fstat(descriptor), journal.stat()):
                raise OSError("Cannot sync the journal.")
            sync(descriptor)

        with (patch.object(os, "fsync", side_effect=fail_journal_sync),
              self.assertRaisesRegex(OSError, "Cannot start Review links")):
            _ = review_page.ReviewSnapshotCache(root, start_worker=False, initialize_on_start=True)

        line = json.dumps({"skipped_backup": str(question.backup)}).encode() + b"\n"
        self.assertTrue(journal.read_bytes() == line)
        synced: list[Path] = []

        def observe_sync(descriptor: int) -> None:
            sync(descriptor)
            status = os.fstat(descriptor)
            for path in (journal, journal.parent):
                if os.path.samestat(status, path.stat()):
                    synced.append(path)

        with patch.object(os, "fsync", side_effect=observe_sync):
            self._assert_move_skips_archived_question(question)
        self.assertEqual(synced, [journal, journal.parent])
        self.assertTrue(journal.read_bytes() == line)

    def test_startup_removes_a_torn_skip_line_after_the_backup_recovers(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        _ = question.path.write_bytes(question.clean)
        head = question.backup / ".git" / "HEAD"
        saved_head = head.read_bytes()
        head.unlink()
        journal = reply_proofs.move_journal_path()
        root = review_link.review_root()
        write = os.write

        def short_journal_write(descriptor: int, data: bytes) -> int:
            if journal.exists() and os.path.samestat(os.fstat(descriptor), journal.stat()):
                return write(descriptor, data[:len(data) // 2])
            return write(descriptor, data)

        with (patch.object(os, "write", side_effect=short_journal_write),
              self.assertRaisesRegex(OSError, "Cannot start Review links")):
            _ = review_page.ReviewSnapshotCache(root, start_worker=False, initialize_on_start=True)
        torn = journal.read_bytes()
        self.assertTrue(bool(torn) and not torn.endswith(b"\n"))

        _ = head.write_bytes(saved_head)
        with redirect_stdout(StringIO()) as preview:
            self.assertEqual(review_submissions.main(["move-reply-proofs", "--dry-run"]), 0)
        self.assertEqual(len(preview.getvalue().splitlines()), 3)
        self.assertTrue(journal.read_bytes() == torn)

        cache = review_page.ReviewSnapshotCache(root, start_worker=False, initialize_on_start=True)
        self.addCleanup(cache.close)
        self.assertEqual(journal.stat().st_size, 0)

    def test_dry_run_and_startup_name_the_backup_when_git_log_fails(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        legacy = question.path.read_bytes()
        git = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

        def fail_backup_log(args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if args[:3] == ["git", "-C", str(question.backup)] and "log" in args:
                raise OSError("Cannot read the backup history.")
            return git(args, **options)

        with patch.object(subprocess, "run", side_effect=fail_backup_log):
            self._assert_scan_refuses_path(question.backup)
        self.assertTrue(question.path.read_bytes() == legacy)
        self.assertFalse(reply_proofs.move_journal_path().exists())

    def test_dry_run_and_startup_name_the_unreadable_production_document(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        _ = question.document.write_bytes(b"\xff")
        self._assert_scan_refuses_path(question.document)

    def test_dry_run_and_startup_reach_backup_when_git_skips_damaged_checkout(self) -> None:
        question = self._archive_legacy_question_in_former_store(local_checkout=True)
        (question.document.parent / ".git" / "HEAD").unlink()
        legacy = question.path.read_bytes()
        move = review_submissions.LocalProofMove(review_link.review_root())
        self.assertEqual(review_submissions.move_reply_proofs(move, dry_run=True), (1, 2))
        self.assertTrue(question.path.read_bytes() == legacy)
        self._assert_startup_moves_archived_question(question, legacy)

    def test_dry_run_and_startup_name_the_invalid_document_rename_record(self) -> None:
        question = self._archive_legacy_question_in_former_store(local_checkout=True)
        owner, = staff.local_snapshot().rows
        history = question.document.parent / ".git" / f"production-document-paths-{owner.id}.json"
        _ = history.write_text("{}")
        self._assert_scan_refuses_path(history)

    def test_dry_run_and_startup_name_the_invalid_folder_registry(self) -> None:
        _ = self._archive_legacy_question_in_former_store()
        registry = record_store.state_root() / "review-folders" / "former" / "question.json"
        registry.parent.mkdir(parents=True)
        _ = registry.write_text("{}")
        self._assert_scan_refuses_path(registry)

    def test_dry_run_and_startup_name_the_unreadable_vault_configuration(self) -> None:
        _ = self._archive_legacy_question_in_former_store()
        config = conf.path("issue_score")
        _ = config.write_bytes(b"\xff")
        self._assert_scan_refuses_path(config)

    def test_dry_run_and_startup_name_the_unreadable_state_folder(self) -> None:
        _ = self._archive_legacy_question_in_former_store()
        state = record_store.state_root()
        folder = state / "review-folders"
        folder.mkdir()
        scandir = os.scandir

        def deny_registry(path: str | os.PathLike[str]) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
            if Path(path) == folder:
                raise PermissionError("Cannot open the folder registry.")
            return scandir(path)

        with patch.object(os, "scandir", side_effect=deny_registry):
            self._assert_scan_refuses_path(folder)

    def test_dry_run_and_startup_name_the_invalid_pending_transfer(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        transfer = review_submissions.image_folder(question.submission).parent / "move.json"
        transfer.parent.mkdir(parents=True, exist_ok=True)
        _ = transfer.write_text("[]")
        self._assert_scan_refuses_path(transfer)

    def test_dry_run_and_startup_skip_history_when_document_folder_is_absent(self) -> None:
        question = self._archive_legacy_question_in_former_store(local_checkout=True)
        shutil.rmtree(question.document.parent)
        self._assert_move_skips_archived_question(question)
        self.assertFalse(reply_proofs.move_journal_path().exists())

    def test_move_uses_the_enclosing_git_checkout_for_production_history(self) -> None:
        question = self._archive_legacy_question_in_former_store(local_checkout=True)
        checkout = question.document.parent
        notes = checkout / "notes"
        notes.mkdir()
        (notes / "objects").mkdir()
        (notes / "refs").mkdir()
        document = question.document.rename(notes / question.document.name)
        tracked = notes / "production.md"
        current = document.read_text()
        former = question.path.parent.parent
        _ = tracked.write_text(f"- **Proposals:** `{former}`\n")
        git = test_record_store.NumberedRecordsTests().git
        _ = git(checkout, "add", str(tracked))
        _ = git(checkout, "commit", "-m", "Set the former store.")
        _ = tracked.write_text(current)
        _ = git(checkout, "add", str(tracked))
        _ = git(checkout, "commit", "-m", "Set the current store.")
        shutil.rmtree(question.backup)

        owner, = staff.local_snapshot().rows
        owner = replace(owner, doc=staff.LiveDoc(local_machine(), document))
        members = staff.StaffSnapshot((owner,))
        _ = staff.record_path().write_text(members.encode())
        record, = update_registration.question_records_for_document(
            document, update_registration.QuestionStaff.for_record(members), strict=True)
        self.assertEqual(record.path, question.path)

        legacy = question.path.read_bytes()
        move = review_submissions.LocalProofMove(review_link.review_root())
        self.assertEqual(review_submissions.move_reply_proofs(move, dry_run=True), (1, 2))
        self.assertTrue(question.path.read_bytes() == legacy)
        cache = review_page.ReviewSnapshotCache(move.review_root, start_worker=False, initialize_on_start=True)
        self.addCleanup(cache.close)
        self.assertTrue(question.path.read_bytes() == question.clean)

    def test_move_matches_reader_when_git_excludes_production_checkout(self) -> None:
        question = self._archive_legacy_question_in_former_store(local_checkout=True)
        ceiling = question.document.parent / "ceiling"
        notes = ceiling / "notes"
        notes.mkdir(parents=True)
        document = question.document.rename(notes / question.document.name)
        owner, = staff.local_snapshot().rows
        owner = replace(owner, doc=staff.LiveDoc(local_machine(), document))
        members = staff.StaffSnapshot((owner,))
        _ = staff.record_path().write_text(members.encode())

        saved, note = question.submission, question.path
        legacy = note.read_bytes()
        proof_path = reply_proofs.path_for(saved.details.proof_identity(saved.id))
        move = review_submissions.LocalProofMove(review_link.review_root())
        kind = record_store.RecordKind.QUESTION

        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(ceiling)}):
            lookup = subprocess.run(["git", "-C", str(notes), "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(lookup.returncode, 0)
            self.assertTrue("not a git repository" in lookup.stderr)
            roots = record_store.known_folders(kind, owner.id, document=document, strict=True)
            self.assertIn(question.path.parent.parent, roots)
            record, = update_registration.question_records_for_document(
                document, update_registration.QuestionStaff.for_record(members), strict=True)
            self.assertEqual(record.path, question.path)
            self.assertEqual(roots, record_store.known_folders(kind, owner.id, document=document, review_scan=True))
            self.assertEqual(review_submissions.move_reply_proofs(move, dry_run=True), (1, 2))
            self.assertTrue(note.read_bytes() == legacy)
            self.assertFalse(proof_path.exists())
            self.assertFalse(reply_proofs.move_journal_path().parent.exists())
            self._assert_startup_moves_archived_question(question, legacy)

    def _assert_startup_moves_archived_question(self, question: ArchivedLegacyQuestion, legacy: bytes) -> None:
        saved, note = question.submission, question.path
        proof = saved.details.reply_proof
        token = saved.reply_token
        self.assertTrue(bool(token))
        identity = saved.details.proof_identity(saved.id)
        proof_path = reply_proofs.path_for(identity)
        move = review_submissions.LocalProofMove(review_link.review_root())
        cache = review_page.ReviewSnapshotCache(move.review_root, start_worker=False, initialize_on_start=True)
        self.addCleanup(cache.close)
        self.assertTrue(note.read_bytes() == question.clean)
        self.assertTrue(reply_proofs.read(identity).reply == proof)
        self.assertEqual(proof_path.stat().st_mode & 0o777, 0o600)
        backup_parent = reply_proofs.move_journal_path().parent
        backup_copy, = backup_parent.glob(f"*/{note.relative_to(note.anchor)}")
        backup_dir = backup_parent / backup_copy.relative_to(backup_parent).parts[0]
        self.assertEqual(backup_dir.stat().st_mode & 0o777, 0o700)
        self.assertTrue(backup_copy.read_bytes() == legacy)
        self.assertEqual(reply_proofs.read_move_report(), {note: ("reply_token_sha256", "reply_token_nonce")})
        self.assertEqual(review_submissions.move_reply_proofs(move, dry_run=False), (0, 0))
        self.assertTrue(review_submissions.answer(saved.key, "Restored reply", token, members=staff.local_snapshot()))

    def test_startup_moves_a_question_from_a_store_in_production_backup_history(self) -> None:
        question = self._archive_legacy_question_in_former_store()
        saved, note = question.submission, question.path
        legacy = note.read_bytes()
        proof_path = reply_proofs.path_for(saved.details.proof_identity(saved.id))
        move = review_submissions.LocalProofMove(review_link.review_root())
        self.assertEqual(review_submissions.move_reply_proofs(move, dry_run=True), (1, 2))
        self.assertTrue(note.read_bytes() == legacy)
        self.assertFalse(proof_path.exists())
        self.assertFalse(reply_proofs.move_journal_path().parent.exists())

        corrupt = legacy.rsplit(b"\n---\n", 1)[0]
        _ = note.write_bytes(corrupt)
        with self.assertRaisesRegex(OSError, re.escape(str(note))):
            _ = review_submissions.move_reply_proofs(move, dry_run=True)
        with self.assertRaisesRegex(OSError, re.escape(str(note))):
            _ = review_page.ReviewSnapshotCache(move.review_root, start_worker=False, initialize_on_start=True)
        self.assertTrue(note.read_bytes() == corrupt)
        _ = note.write_bytes(legacy)

        self._assert_startup_moves_archived_question(question, legacy)

    def test_move_writes_each_of_200_journal_lines_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            notes = root / "notes"
            notes.mkdir()
            with patch.object(reply_proofs, "state_root", return_value=state):
                journal = reply_proofs.move_journal_path()
                run = reply_proofs.ProofMoveRun(False, lambda: (notes,))
                fields = ("request_key",)
                written: list[int] = []
                write = os.write

                def count_journal_bytes(descriptor: int, data: bytes) -> int:
                    if journal.exists() and os.path.samestat(os.fstat(descriptor), journal.stat()):
                        self.assertEqual(data.count(b"\n"), 1)
                        written.append(len(data))
                    return write(descriptor, data)

                for index in range(200):
                    path = notes / f"Q{index:03}.md"
                    run.backup(path, b"original note", fields)
                    with patch.object(os, "write", side_effect=count_journal_bytes):
                        run.record_rewrite((path, fields))
                self.assertEqual(len(written), 200)
                self.assertEqual(sum(written[:100]), sum(written[100:]))
                self.assertEqual(journal.stat().st_size, sum(written))
                self.assertEqual(len(reply_proofs.read_move_report()), 200)

    def test_move_resumes_after_a_partial_journal_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
                reply_proofs, "state_root", return_value=Path(temporary)):
            root = Path(temporary)
            first, second = root / "first.md", root / "second.md"
            fields = ("request_key",)
            run = reply_proofs.ProofMoveRun(False, tuple)
            run.backup(first, b"first note", fields)
            run.record_rewrite((first, fields))
            journal = reply_proofs.move_journal_path()
            complete = journal.read_bytes()
            run.backup(second, b"second note", fields)
            write = os.write

            def short_write(descriptor: int, line: bytes) -> int:
                return write(descriptor, line[:len(line) // 2])

            with (patch.object(os, "write", side_effect=short_write),
                  self.assertRaisesRegex(OSError, "append the complete")):
                run.record_rewrite((second, fields))
            self.assertTrue(journal.read_bytes().startswith(complete))
            self.assertEqual(reply_proofs.read_move_report(), {first: fields})

            retry = reply_proofs.ProofMoveRun(False, tuple)
            retry.backup(second, b"second note", fields)
            retry.record_rewrite((second, fields))
            self.assertTrue(journal.read_bytes().startswith(complete))
            self.assertEqual(len(journal.read_bytes().splitlines()), 2)
            self.assertEqual(reply_proofs.read_move_report(), {first: fields, second: fields})

    def test_removal_preserves_other_json_bytes_and_line_endings(self) -> None:
        body = "\r\n# Note\r\n\r\nKeep this text.\r\n"
        digest = json.dumps("a" * 64)
        clean_submission = r'{"images" : [ ], "evidence": {"text":"\u0061\/😀"}, "other" : "reply_token"}'
        legacy_submission = clean_submission.replace(
            "[ ]", '[ ],  "reply_token_sha256" : ' + digest, 1)
        question = '---\r\nregistration: {  "text":"Question", "review_submission" : '
        tail = ', "log" : [ ]  }\r\n---' + body
        legacy_question = (question.replace("{  ", '{  "request_key":"fixture", ', 1)
                           + legacy_submission + tail)
        layout = reply_proofs.ProofNoteLayout
        question_move = reply_proofs.prepare_note_removal(legacy_question, layout.QUESTION)
        self.assertTrue(question_move.content == question + clean_submission + tail)
        self.assertEqual(question_move.fields, ("reply_token_sha256", "request_key"))

        for quoted in (False, True):
            with self.subTest(quoted=quoted):
                def wrap(text: str) -> str:
                    return (json.dumps(text).replace(r'\"images\"', r'\u0022images\u0022') if quoted else text)

                header = '---\r\nid: "I1"\r\nreview_submission: ' if quoted else "---\r\nreview_submission:\t "
                end = "  \r\n---" + ("\r\n" if quoted else "") + body
                source = header + wrap(legacy_submission) + end
                if quoted:
                    store = record_store.NumberedRecords(staff.StaffId("owner"), "investigations", "I", "report", ValueError)
                    self.assertTrue(store.parse_text("I1", source).fields["review_submission"] == legacy_submission)
                else:
                    self.assertTrue(record_store.record_text_fields(Path("proposal.md"), source)
                                    .value("review_submission") == legacy_submission)
                move = reply_proofs.prepare_note_removal(source, layout.SUBMISSION if quoted else layout.PROPOSAL)
                self.assertEqual(move.fields, ("reply_token_sha256",))
                self.assertTrue(move.content == header + wrap(clean_submission) + end)

    def test_deletes_first_middle_last_and_sole_proof_members(self) -> None:
        prefix = '---\nregistration: '
        suffix = '\n---\n\nA body without a final newline.'
        fixtures = (
            ('{  "request_key": "fixture",  "a": 1, "b": 2 }', '{  "a": 1, "b": 2 }'),
            ('{"a": 1,  "request_key" : "fixture", "b": 2}', '{"a": 1, "b": 2}'),
            ('{"a": 1 , "request_key": "fixture"  }', '{"a": 1   }'),
            ('{  "request_key": "fixture" }', '{   }'),
        )
        for index, (before, expected) in enumerate(fixtures):
            with self.subTest(position=index):
                source = prefix + before + suffix
                issue = record_store.record_text_fields(Path("question.md"), source)
                fields = cast(dict[str, object], json.loads(issue.value("registration")))
                self.assertIn("request_key", fields)
                move = reply_proofs.prepare_note_removal(source, reply_proofs.ProofNoteLayout.QUESTION)
                self.assertEqual(move.fields, ("request_key",))
                self.assertTrue(move.content.encode() == (prefix + expected + suffix).encode())

    def test_failed_note_write_restores_its_previous_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(reply_proofs, "state_root", return_value=Path(temporary)):
            identity = reply_proofs.SubmissionIdentity("Q1", "fixture")
            first = reply_proofs.NoteProof(reply_tokens.LegacyReplyProof(reply_tokens.SavedReplyHash("a" * 64)))
            second = reply_proofs.NoteProof(reply_tokens.LegacyReplyProof(reply_tokens.SavedReplyHash("b" * 64)))
            with reply_proofs.publishing(identity, first):
                pass
            with self.assertRaisesRegex(OSError, "The note write failed"), reply_proofs.publishing(identity, second):
                raise OSError("The note write failed")
            self.assertTrue(reply_proofs.read(identity) == first)
            self.assertEqual(reply_proofs.path_for(identity).stat().st_mode & 0o777, 0o600)

    def test_move_can_retry_after_the_note_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(reply_proofs, "state_root", return_value=Path(temporary)):
            identity = reply_proofs.StaffQuestionIdentity("owner", "Q1")
            proof = reply_proofs.NoteProof(request=reply_proofs.StaffQuestionRequestKey("fixture"))
            with self.assertRaisesRegex(OSError, "The note write failed"), reply_proofs.publishing(identity, proof, moving=True):
                raise OSError("The note write failed")
            self.assertTrue(reply_proofs.read(identity) == proof)
            with reply_proofs.publishing(identity, proof, moving=True):
                pass
            self.assertTrue(reply_proofs.read(identity) == proof)

    def test_move_syncs_the_proof_folder_in_its_parent_before_the_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = reply_proofs.StaffQuestionIdentity("owner", "Q1")
            proof = reply_proofs.NoteProof(request=reply_proofs.StaffQuestionRequestKey("fixture"))
            synced: list[Path] = []
            sync_folder = atomic_file.sync_folder

            def sync(folder: Path) -> None:
                synced.append(folder)
                sync_folder(folder)

            with (patch.object(reply_proofs, "state_root", return_value=root),
                  patch.object(atomic_file, "sync_folder", side_effect=sync)):
                with reply_proofs.publishing(identity, proof, moving=True):
                    self.assertEqual(synced, [root, reply_proofs.folder()])


if __name__ == "__main__":
    _ = unittest.main()
