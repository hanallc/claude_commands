"""Test proof publication in a scratch state folder."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib import atomic_file
from scripts.review_links import reply_proofs, reply_tokens


class ReplyProofTests(unittest.TestCase):
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
