"""Test registry evidence through the shared reader."""

from collections.abc import Iterator
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from typing import final
from unittest.mock import patch

from scripts.message import session_records


@final
class TranscriptScanTests(unittest.TestCase):
    def test_failed_projects_scan_does_not_hide_transcripts_in_other_roots(self) -> None:
        home = Path(self.enterContext(TemporaryDirectory())).resolve()
        unreadable, readable = home / ".claude", home / ".claude2"
        project = readable / "projects" / "fixture"
        project.mkdir(parents=True)
        (project / "found.jsonl").touch()
        glob = Path.glob

        def scan(root: Path, pattern: str) -> Iterator[Path]:
            if root == unreadable / "projects":
                raise PermissionError("fixture cannot scan projects")
            yield from glob(root, pattern)

        with (patch.object(session_records, "config_dirs", return_value=[unreadable, readable]),
              patch.object(Path, "glob", autospec=True, side_effect=scan)):
            self.assertEqual(session_records.transcript_session_ids({"found", "missing"}), {"found"})


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

    def test_liveness_keeps_the_target_record_when_a_later_retry_cannot_start(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        target, unreadable = directory / "123.json", directory / "other.json"
        _ = target.write_text(json.dumps({
            "pid": 123, "sessionId": "known", "name": "fixture", "messagingSocketPath": "", "updatedAt": 1,
        }))
        _ = unreadable.write_text("{")
        with (patch.object(session_records, "sessions_dir", return_value=directory),
              patch.object(Path, "iterdir", return_value=iter((target, unreadable))),
              patch.object(session_records, "retry_policy", return_value=session_records.SessionRetryPolicy(2, 0.5)),
              patch.object(session_records, "running_session", return_value=True),
              patch.object(time, "monotonic", return_value=0.5),
              patch.object(time, "sleep") as sleep):
            self.assertEqual(session_records.session_liveness("known", deadline=0.75),
                             session_records.LiveSessionProcess(123))
            sleep.assert_not_called()

    def test_liveness_reports_the_unreadable_path_when_the_last_read_finishes_late(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        unreadable = directory / "unfinished.json"
        now = 0.25

        def read() -> str:
            nonlocal now
            now = 0.5
            return "{"

        with (patch.object(session_records, "sessions_dir", return_value=directory),
              patch.object(Path, "iterdir", return_value=iter((unreadable,))),
              patch.object(Path, "read_text", side_effect=read) as read_text,
              patch.object(session_records, "retry_policy", return_value=session_records.SessionRetryPolicy(1, 0.125)),
              patch.object(time, "monotonic", lambda: now)):
            result = session_records.session_liveness("known", deadline=0.375)
        self.assertIsInstance(result, session_records.SessionLivenessUnknown)
        assert isinstance(result, session_records.SessionLivenessUnknown)
        self.assertEqual(result.reason, f"session registry unreadable: {unreadable}")
        self.assertEqual(result.unreadable_registry, (unreadable,))
        read_text.assert_called_once_with()
