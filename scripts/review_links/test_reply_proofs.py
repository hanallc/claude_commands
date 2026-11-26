"""Test proof publication in a scratch state folder."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.lib import atomic_file
from scripts.production import record_store, staff
from scripts.review_links import reply_proofs, reply_tokens


class ReplyProofTests(unittest.TestCase):
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
