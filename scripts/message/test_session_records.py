"""Test registry folder evidence through the shared reader."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import final
from unittest.mock import patch

from scripts.message import session_records


@final
class SessionRegistryFolderTests(unittest.TestCase):
    def test_missing_registry_preserves_unknown_evidence(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory())) / "sessions"
        with patch.object(session_records, "sessions_dir", return_value=directory):
            registry = session_records.read_registry()
            self.assertEqual(registry, session_records.RegistryRead((), (directory,)))
            self.assertEqual(session_records.session_liveness("absent", registry),
                             session_records.SessionLivenessUnknown(f"session registry unreadable: {directory}"))

    def test_unreadable_registry_preserves_unknown_evidence(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        with (patch.object(session_records, "sessions_dir", return_value=directory),
              patch.object(Path, "iterdir", side_effect=PermissionError("registry denied"))):
            registry = session_records.read_registry()
            self.assertEqual(registry, session_records.RegistryRead((), (directory,)))
            self.assertEqual(session_records.session_liveness("absent", registry),
                             session_records.SessionLivenessUnknown(f"session registry unreadable: {directory}"))
