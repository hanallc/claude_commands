"""Test how checkpoint gates enter placement."""

from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue import first_step
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.queue_fixture import write_process
from scripts.copies import copies
from scripts.lib.build_steps import gate_job_from_process
from scripts.production import checkpoint_gate


GATE = ("python3", "scripts/production/gate.py", "--tests-only")


class CheckpointGateTests(unittest.TestCase):
    def test_named_gate_uses_a_supervisor_with_the_gate_command(self) -> None:
        with TemporaryDirectory() as temporary:
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
                child.assert_called_once_with(list(GATE), cwd=folder, pass_fds=[9], check=False)

    def test_gate_without_targets_starts_directly(self) -> None:
        folder = Path("/fixture")
        with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": ""}),
              mock.patch.object(subprocess, "run",
                  return_value=subprocess.CompletedProcess(GATE, 0, "", "")) as run):
            _ = checkpoint_gate.run_test_command(list(GATE), folder)
        run.assert_called_once_with(list(GATE), cwd=folder, capture_output=True, text=True, check=False, pass_fds=())
