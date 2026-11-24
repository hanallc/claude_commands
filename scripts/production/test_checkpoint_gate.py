"""Test how checkpoint gates enter placement and retry a silent Mac guest."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import io
from dataclasses import replace
from functools import partial
import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast, final
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client, admission_records, admission_wire, execution_identity, first_step, placement
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.admission_test_support import only_admitted
from scripts.build_queue.queue_fixture import write_process
from scripts.build_queue.test_placement import PlacementFixture
from scripts.copies import copies
from scripts.lib import build_folders, process_stat
from scripts.lib.build_steps import GATE_UNSETTLED_LAUNCH_STATUS, gate_job_from_process
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test.claim_state import RepositoryScope
from scripts.production import checkpoint_gate
from scripts.production.production_doc import ProductionDocument


GATE = ("python3", "scripts/production/gate.py", "--tests-only")


class CheckpointGateTests(unittest.TestCase):
    def test_named_gate_uses_a_supervisor_with_the_gate_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            token = copies.COMMAND_LOCKS.set((9,))
            self.addCleanup(copies.COMMAND_LOCKS.reset, token)
            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "natedev,mac-linux"}),
                  mock.patch.object(subprocess, "run",
                      return_value=subprocess.CompletedProcess(GATE, 0, "", "")) as run):
                result = checkpoint_gate.run_test_command(list(GATE), folder)
                self.assertEqual(result.returncode, 0)
                run.assert_called_once()
                argv = cast(list[str], run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs["pass_fds"], (9,))
                self.assertEqual(argv[2:], ["gate-step", "--repo", str(folder), "--targets", "natedev,mac-linux",
                    "--lease-fd", "9", "--", *GATE])

            process = folder / "42"
            write_process(folder, 42, start="123")
            _ = (process / "cmdline").write_text("\0".join(("python3", *argv[1:])) + "\0")
            self.assertEqual(gate_job_from_process("gate", process, start_time="123"), StepJob("test-all"))

            with (mock.patch.object(first_step, "admitted_run") as place,
                  mock.patch.object(subprocess, "run",
                      return_value=subprocess.CompletedProcess(GATE, 0)) as child):
                handoff = mock.Mock()

                def run_gate(run: Callable[[], subprocess.CompletedProcess[str]]) -> subprocess.CompletedProcess[str]:
                    return run()

                admitted = mock.Mock(spec=first_step.FirstStepAdmission, handoff=handoff,
                                     run_gate=mock.Mock(side_effect=run_gate))
                place.return_value = nullcontext(admitted)
                self.assertEqual(copies.main(argv[2:]), 0)
                place.assert_called_once_with(list(GATE), folder, targets=("natedev", "mac-linux"))
                handoff.assert_called_once_with(folder)
                child.assert_called_once_with(list(GATE), cwd=folder, pass_fds=[9], check=False, text=True)

    def test_gate_without_targets_starts_directly(self) -> None:
        folder = Path("/fixture")
        with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
              mock.patch.object(build_folders, "repo_word", return_value="hana"),
              mock.patch.object(subprocess, "run",
                  return_value=subprocess.CompletedProcess(GATE, 0, "", "")) as run):
            _ = checkpoint_gate.run_test_command(list(GATE), folder)
        run.assert_called_once_with(list(GATE), cwd=folder, capture_output=True, text=True, check=False, pass_fds=())

    def test_claude_gate_without_target_override_launches_with_default_targets(self) -> None:
        with PlacementFixture() as fixture:
            _ = write_config_fixture(fixture.root / "build_targets.conf", "targets=natedev\nenabled.natedev=on\n")
            with (mock.patch.dict(os.environ),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.object(subprocess, "run",
                      return_value=subprocess.CompletedProcess(GATE, 0, "", "")) as run):
                _ = os.environ.pop("BUILDLOG_GATE_TARGETS", None)
                result = checkpoint_gate.run_test_command(list(GATE), fixture.root)
            self.assertEqual(result.returncode, 0)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][2:],
                             ["gate-step", "--repo", str(fixture.root), "--targets", "natedev", "--", *GATE])

    def test_named_module_gate_keeps_the_parent_reservation(self) -> None:
        with PlacementFixture() as fixture:
            outer = fixture.request(owner=fixture.fixture_owner(700001, "123"))
            with fixture.loop_turn():
                grant = only_admitted(fixture.server.handle_request(outer))
            fixture.hold_starts()
            reservation = f"{grant.reservation}\t{outer.owner.pid}\t{fixture.state_dir}\t{fixture.socket_path}"
            command = [*GATE, "--module", "lib/test_infrastructure.py"]
            _ = write_config_fixture(fixture.root / "build_targets.conf", "targets=natedev\n")

            def read_private_stat(path: Path) -> str:
                return (fixture.proc.path / path.relative_to("/proc")).read_text()

            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "", "BUILDLOG_RESERVED_STEP": reservation}),
                  mock.patch.object(os, "getppid", return_value=outer.owner.pid),
                  mock.patch.object(admission_records, "read_proc_text", side_effect=read_private_stat),
                  mock.patch.object(process_stat, "process_fields",
                      side_effect=partial(process_stat.process_fields, proc_root=fixture.proc.path)),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.object(subprocess, "run",
                      return_value=subprocess.CompletedProcess(command, 0, "", "")) as run):
                self.assertEqual(admission_client.outer_reservation(),
                                 admission_client.OuterReservation(grant.reservation, outer.owner.pid))
                result = checkpoint_gate.run_test_command(command, fixture.root)
            self.assertEqual(result.returncode, 0)
            run.assert_called_once_with(command, cwd=fixture.root, capture_output=True, text=True, check=False,
                                        pass_fds=())

    def test_claude_gate_without_enabled_targets_refuses_before_start(self) -> None:
        with PlacementFixture() as fixture:
            _ = write_config_fixture(fixture.root / "build_targets.conf",
                "targets=mac-linux,natedev\nenabled.mac-linux=off\nenabled.natedev=off\n")
            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.object(subprocess, "run",
                      return_value=subprocess.CompletedProcess(GATE, 0, "", "")) as run):
                with self.assertRaises(checkpoint_gate.Stop) as refused:
                    _ = checkpoint_gate.run_test_command(list(GATE), fixture.root)
            run.assert_not_called()
            self.assertEqual(refused.exception.state, "blocked")
            self.assertEqual(refused.exception.detail,
                             "No enabled build target can run this Claude gate. Check enabled.* in build_targets.conf.")


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
            return first_step.run_gate_with_placement(
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
        with (mock.patch.object(checkpoint_gate, "report", wraps=checkpoint_gate.report) as report,
              redirect_stdout(io.StringIO()) as output):
            detail = fixture.run_step(invoked)
        self.assertEqual(detail, fixture.step.label)
        self.assertEqual(invoked.call_count, 2)
        fixture.report_failure.assert_called_once_with(fixture.refusal)
        fixture.place_again.assert_called_once_with(fixture.request)
        # gate-step prints the retry line into the gate output.
        self.assertIn("placement: retry — fixture-mac (mac-linux) did not answer.", output.getvalue())
        self.assertIn("0 red", str(report.call_args_list))

    def test_silent_guest_after_its_consume_requests_placement_again(self) -> None:
        # The guest gate consumed its launch permission, then took no run.
        fixture = GatePlacementFixture(self)
        fixture.report_failure.return_value = admission_wire.DispatchStatus(fixture.guest,
            placement.CurrentAssignment(), placement.PermissionConsumed(12.0), retry=placement.RetryAt(13.0))
        run = mock.Mock(side_effect=[fixture.refuse_guest(), subprocess.CompletedProcess(COMMAND, 0, "", "")])
        with redirect_stdout(io.StringIO()):
            detail = fixture.run_step(run)
        self.assertEqual(detail, fixture.step.label)
        self.assertEqual(run.call_count, 2)
        fixture.place_again.assert_called_once_with(fixture.request)

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

    def test_exit_80_blocks_checkpoint_without_rerun(self) -> None:
        fixture = GatePlacementFixture(self)
        result = subprocess.CompletedProcess(COMMAND, GATE_UNSETTLED_LAUNCH_STATUS, "", "settlement pending")
        with (mock.patch.object(checkpoint_gate, "run_test_command", return_value=result) as run,
              self.assertRaises(checkpoint_gate.Stop) as stopped):
            _ = checkpoint_gate.run_test_steps(fixture.production, [fixture.step], frozenset(),
                                               frozenset(), fixture.log)
        self.assertEqual(stopped.exception.state, "blocked")
        self.assertIn("settlement", stopped.exception.detail)
        run.assert_called_once_with(COMMAND, fixture.step.folder)

    def test_queue_loss_after_guest_exit_80_preserves_checkpoint_settlement(self) -> None:
        fixture = GatePlacementFixture(self)

        @contextmanager
        def tool_folder(_tool: str) -> Generator[Path]:
            try:
                yield fixture.step.folder
            finally:
                raise OSError("fixture folder cleanup failed")

        def run(command: list[str], folder: Path) -> subprocess.CompletedProcess[str]:
            with redirect_stderr(io.StringIO()) as errors:
                code = copies.main(["gate-step", "--repo", str(folder), "--", *command])
            return subprocess.CompletedProcess(command, code, "", errors.getvalue())

        child = subprocess.CompletedProcess(COMMAND, GATE_UNSETTLED_LAUNCH_STATUS, "", "guest end unproven\n")
        with (PlacementFixture(),
              mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": ""}, clear=False),
              mock.patch.object(first_step, "gate_targets", return_value=("mac-linux",)),
              mock.patch.object(first_step, "_gate_request", return_value=fixture.request),
              mock.patch.object(first_step, "_placement_grant", return_value=admission_wire.Assignment(fixture.guest)),
              mock.patch.object(admission_records, "process_start_time", return_value="456"),
              mock.patch.object(build_folders, "tool_folder", side_effect=tool_folder),
              mock.patch.object(admission_client, "_connect", side_effect=FileNotFoundError("fixture socket is gone")) as connect,
              mock.patch.object(subprocess, "run", return_value=child) as invoked,
              mock.patch.object(checkpoint_gate, "run_test_command", side_effect=run) as runner):
            del os.environ["BUILDLOG_PLACEMENT_FILE"]
            with self.assertRaises(checkpoint_gate.Stop) as stopped:
                _ = checkpoint_gate.run_test_steps(fixture.production, [fixture.step], frozenset(),
                                                   frozenset(), fixture.log)
        self.assertEqual(stopped.exception.state, "blocked")
        self.assertIn("launch settlement", stopped.exception.detail)
        self.assertEqual(connect.call_count, 2)
        invoked.assert_called_once()
        runner.assert_called_once_with(COMMAND, fixture.step.folder)
        output = fixture.log.read_text()
        self.assertIn("guest end unproven", output)
        self.assertIn("Gate cleanup failed. no socket", output)
        self.assertIn("Gate cleanup failed. fixture folder cleanup failed", output)
        self.assertIn("merge_test_1_EXIT=80", output)
        self.assertNotIn("RERUN", output)

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
