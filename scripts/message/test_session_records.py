"""Test registry evidence through the shared reader."""

import json
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
            self.assertEqual(session_records.enumerate_running_sessions(registry=registry),
                             session_records.RunningSessionEnumeration((), (directory,)))
            unknown = session_records.SessionLivenessUnknown(f"session registry unreadable: {directory}")
            self.assertEqual(session_records.session_liveness("absent", registry), unknown)
            self.assertEqual(session_records.session_processes("absent"), unknown)

    def test_running_enumeration_returns_a_retry_policy_read_error(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        with (patch.object(session_records, "sessions_dir", return_value=directory),
              patch.object(session_records, "retry_policy", side_effect=PermissionError("retry policy denied"))):
            self.assertEqual(session_records.enumerate_running_sessions(),
                             session_records.RunningSessionDiscoveryFailed(
                                 "session registry discovery failed: retry policy denied"))

    def test_unreadable_record_prevents_proof_of_absence_or_a_complete_pid_list(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        unreadable = directory / "unfinished.json"
        _ = unreadable.write_text("{")
        _ = (directory / "good.json").write_text(json.dumps({
            "pid": 123, "sessionId": "known", "name": "fixture", "messagingSocketPath": "", "updatedAt": 1,
        }))
        with (patch.object(session_records, "sessions_dir", return_value=directory),
              patch.object(session_records, "running_session", return_value=True)):
            registry = session_records.read_registry()
            unknown = session_records.SessionLivenessUnknown(f"session registry unreadable: {unreadable}")
            self.assertEqual(session_records.session_liveness("absent", registry), unknown)
            self.assertEqual(session_records.session_liveness("known", registry), session_records.LiveSessionProcess(123))
            for identity in ("absent", "known"):
                with self.subTest(identity=identity):
                    self.assertEqual(session_records.session_processes(identity), unknown)

    def test_unreadable_registry_preserves_unknown_evidence(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        with (patch.object(session_records, "sessions_dir", return_value=directory),
              patch.object(Path, "iterdir", side_effect=PermissionError("registry denied"))):
            registry = session_records.read_registry()
            self.assertEqual(registry, session_records.RegistryRead((), (directory,)))
            self.assertEqual(session_records.session_liveness("absent", registry),
                             session_records.SessionLivenessUnknown(f"session registry unreadable: {directory}"))
