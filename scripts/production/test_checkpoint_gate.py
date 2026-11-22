"""Check placement retries through the checkpoint test runner."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
from typing import cast, final
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client, admission_records, admission_wire, execution_identity, placement
from scripts.mac_test.claim_state import RepositoryScope
from scripts.production import checkpoint_gate
from scripts.production.production_doc import ProductionDocument


COMMAND = ["python3", "gate.py", "--tests-only"]
REASON = "mac_linux_no_answer"
RED_OUTPUT = "  FAIL [  0.1s] (1/1) check known_flake\n--- lib/test_infrastructure.py exit=1 ---\n"


@final
class GatePlacementFixture:
    def __init__(self, case: unittest.TestCase) -> None:
        self.case = case
        root = Path(case.enterContext(tempfile.TemporaryDirectory()))
        self.production = ProductionDocument(root / "production.md", "fixture", "merge", root,
                                             root / "production.log", ZoneInfo("UTC"))
        self.log = root / "test.log"
        self.assignment_file = root / "assignment.json"
        self.report_file = self.assignment_file.with_suffix(".json.claim-failure")
        self.step = checkpoint_gate.CheckpointTestStep("merge test 1", COMMAND, root)
        scope = RepositoryScope("fixture")
        guest = placement.PlacementDestination("ssh", "mac-linux", "fixture-mac", scope)
        local = placement.PlacementDestination("local", "natedev", "fixture-dev", scope)
        owner = placement.SubmittingOwner("fixture-dev", 123, "456")
        source = execution_identity.SourceKeyKnown("fixture-source")
        session = NoOwningSession()
        self.guest = placement.PlacementAssignment("episode", "guest-attempt", guest, source, owner, session, 12.5)
        local_assignment = replace(self.guest, destination=local, attempt="local-attempt")
        self.local = admission_wire.LocalPlacementGranted(local_assignment,
            admission_wire.Admitted("reservation", 1, "fixture", "", "", {}))
        self.request = admission_wire.PlacementRequest(self.guest.episode, owner, self.guest.since,
            admission_records.ProgramProfile("gate", str(root), str(root),
                admission_records.HistoryPeakSizing(1, admission_records.FirstProgramPeak(1), 0.0)),
            placement.PlacementConstraints(source, tuple(COMMAND), str(root), "", session,
                (placement.PlacementCandidate(guest), placement.PlacementCandidate(local))))
        self.refusal = admission_wire.ClaimFailure("failure-id", self.guest, REASON)
        admission_wire.write_assignment(self.assignment_file, admission_wire.Assignment(self.guest))
        self.report_failure = case.enterContext(mock.patch.object(admission_client, "claim_failure",
            return_value=admission_wire.DispatchStatus(self.guest, placement.ClosedAssignment(REASON),
                placement.PermissionAvailable(), retry=placement.RetryAt(13.0))))
        self.place_again = mock.Mock(side_effect=self.install_local_assignment)

    def install_local_assignment(self, request: admission_wire.PlacementRequest) -> None:
        self.case.assertEqual(request, self.request)
        self.report_failure.assert_called_once_with(self.refusal)
        admission_wire.write_assignment(self.assignment_file, self.local)

    def run_step(self, child: mock.Mock, *, retry_failed_steps: bool = False) -> str:
        def run(command: list[str], folder: Path) -> subprocess.CompletedProcess[str]:
            return checkpoint_gate.run_gate_with_placement(
                lambda: cast("subprocess.CompletedProcess[str]", child(command, folder)),
                self.assignment_file, self.request, self.place_again)

        with mock.patch.object(checkpoint_gate, "run_test_command", side_effect=run):
            return checkpoint_gate.run_test_steps(self.production, [self.step], frozenset(),
                frozenset({"known_flake"}), self.log, retry_failed_steps=retry_failed_steps)

    def refuse_guest(self) -> subprocess.CompletedProcess[str]:
        _ = self.report_file.write_text(admission_wire.encode_request(self.refusal))
        return subprocess.CompletedProcess(COMMAND, 79, RED_OUTPUT, "")


@final
class CheckpointGatePlacementTests(unittest.TestCase):
    def test_silent_guest_requests_placement_again_at_original_queue_time(self) -> None:
        fixture = GatePlacementFixture(self)

        def run(command: list[str], folder: Path) -> subprocess.CompletedProcess[str]:
            self.assertEqual((command, folder), (COMMAND, fixture.step.folder))
            if admission_wire.read_assignment(fixture.assignment_file).assignment == fixture.guest:
                return fixture.refuse_guest()
            self.assertEqual(admission_wire.read_assignment(fixture.assignment_file), fixture.local)
            return subprocess.CompletedProcess(command, 0, "", "")

        invoked = mock.Mock(side_effect=run)
        with mock.patch.object(checkpoint_gate, "report", wraps=checkpoint_gate.report) as report:
            detail = fixture.run_step(invoked)
        self.assertEqual(detail, fixture.step.label)
        self.assertEqual(invoked.call_count, 2)
        fixture.report_failure.assert_called_once_with(fixture.refusal)
        fixture.place_again.assert_called_once_with(fixture.request)
        self.assertIn("fixture-mac", str(report.call_args_list))
        self.assertIn("mac-linux", str(report.call_args_list))
        self.assertIn("0 red", str(report.call_args_list))

    def test_silent_guest_output_does_not_select_a_flaky_test(self) -> None:
        fixture = GatePlacementFixture(self)
        run = mock.Mock(side_effect=[fixture.refuse_guest(), subprocess.CompletedProcess(COMMAND, 0, "", "")])
        detail = fixture.run_step(run, retry_failed_steps=True)
        self.assertEqual(detail, fixture.step.label)
        self.assertEqual(run.call_args_list, [mock.call(COMMAND, fixture.step.folder)] * 2)
        self.assertFalse(fixture.production.log.exists())
        self.assertNotIn("RERUN", fixture.log.read_text())

    def test_red_after_launch_keeps_only_the_real_red_module(self) -> None:
        fixture = GatePlacementFixture(self)
        red = "lib/test_gate.py"
        run = mock.Mock(side_effect=[fixture.refuse_guest(), subprocess.CompletedProcess(COMMAND, 1, f"--- {red} exit=1 ---\n", "")])
        with self.assertRaises(checkpoint_gate.RedCheckpointTests) as stopped:
            _ = fixture.run_step(run)
        self.assertEqual(stopped.exception.red, (checkpoint_gate.RedStep(fixture.step, frozenset({red})),))
        self.assertEqual(run.call_count, 2)
        fixture.report_failure.assert_called_once_with(fixture.refusal)

    def test_unsettled_attempt_blocks_another_launch(self) -> None:
        fixture = GatePlacementFixture(self)
        fixture.report_failure.return_value = admission_wire.DispatchStatus(fixture.guest, placement.CurrentAssignment(),
                                                                        placement.PermissionAvailable())
        run = mock.Mock(return_value=fixture.refuse_guest())
        with self.assertRaises(checkpoint_gate.Stop) as stopped:
            _ = fixture.run_step(run, retry_failed_steps=True)
        self.assertEqual(stopped.exception.state, "blocked")
        fixture.place_again.assert_not_called()
        run.assert_called_once()

    def test_red_gate_ignores_a_stale_refusal_file(self) -> None:
        fixture = GatePlacementFixture(self)
        _ = fixture.refuse_guest()
        run = mock.Mock(return_value=subprocess.CompletedProcess(COMMAND, 1, RED_OUTPUT, ""))
        with self.assertRaises(checkpoint_gate.RedCheckpointTests) as stopped:
            _ = fixture.run_step(run)
        self.assertEqual(stopped.exception.red[0].names, frozenset({"lib/test_infrastructure.py"}))
        fixture.report_failure.assert_not_called()
        fixture.place_again.assert_not_called()
