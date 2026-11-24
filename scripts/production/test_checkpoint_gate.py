"""Test how checkpoint gates enter placement and report the result."""
from __future__ import annotations

from contextlib import nullcontext
from functools import partial
import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast, final
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.build_queue import admission_client, admission_records, first_step
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.admission_test_support import only_admitted
from scripts.build_queue.queue_fixture import write_process
from scripts.build_queue.test_placement import PlacementFixture
from scripts.copies import copies
from scripts.lib import build_folders, process_stat
from scripts.lib.build_steps import gate_job_from_process
from scripts.lint.memory_test_support import write_config_fixture
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

                admitted = mock.Mock(spec=first_step.FirstStepAdmission, handoff=handoff)
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
            _ = write_config_fixture(fixture.root / "cloud_gate.conf", "enabled=off\n")
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
            _ = write_config_fixture(fixture.root / "cloud_gate.conf", "enabled=off\n")
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
RED_OUTPUT = "  FAIL [  0.1s] (1/1) check known_flake\n--- lib/test_infrastructure.py exit=1 ---\n"


@final
class CheckpointGatePlacementTests(unittest.TestCase):
    def run_step(self, status: int, *, retry_failed_steps: bool) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        production = ProductionDocument(root / "production.md", "fixture", "merge", root,
                                        root / "production.log", ZoneInfo("UTC"))
        log = root / "test.log"
        step = checkpoint_gate.CheckpointTestStep("merge test 1", COMMAND, root)
        admitted = mock.Mock(spec=first_step.FirstStepAdmission)

        def run(command: list[str], folder: Path) -> subprocess.CompletedProcess[str]:
            args = copies.CopyArguments(repo=str(folder), targets="mac-linux,natedev")
            result = copies.run_gate_step(args, command)
            return subprocess.CompletedProcess(command, result, RED_OUTPUT, "")

        with (mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)) as place,
              mock.patch.object(subprocess, "run",
                                return_value=subprocess.CompletedProcess(COMMAND, status)) as child,
              mock.patch.object(checkpoint_gate, "run_test_command", side_effect=run),
              self.assertRaises(checkpoint_gate.Stop) as stopped):
            _ = checkpoint_gate.run_test_steps(production, [step], frozenset(), frozenset({"known_flake"}), log,
                                               retry_failed_steps=retry_failed_steps)
        place.assert_called_once_with(COMMAND, root, targets=("mac-linux", "natedev"))
        child.assert_called_once()
        if status == 1:
            assert isinstance(stopped.exception, checkpoint_gate.RedCheckpointTests)
            self.assertEqual(stopped.exception.red, (checkpoint_gate.RedStep(step, frozenset({"lib/test_infrastructure.py"})),))
        else:
            self.assertEqual(stopped.exception.state, "blocked")
            self.assertIn(f"gate status {status}", stopped.exception.detail)
            self.assertNotIn("RERUN", log.read_text())
        self.assertFalse(production.log.exists())

    def test_silent_guest_blocks_the_checkpoint_without_a_retry(self) -> None:
        self.run_step(79, retry_failed_steps=True)

    def test_unknown_guest_result_blocks_the_checkpoint_without_a_retry(self) -> None:
        self.run_step(80, retry_failed_steps=True)

    def test_red_gate_remains_a_test_failure(self) -> None:
        self.run_step(1, retry_failed_steps=False)
