"""Keep nightly results when slot cleanup fails."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.lib import build_slots, scratch
from scripts.lib.build_slots_test_support import private_slot_config
from scripts.production import merge_gate
from scripts.production.gate_modules import GateRefused


class NightlySlotSweepTests(unittest.TestCase):
    def test_local_sweep_failure_reports_once_and_keeps_the_nightly_result(self) -> None:
        for status in (0, 1, 75):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                config = private_slot_config(Path(temporary), repository="fixture")
                events: list[str] = []
                notices: list[str] = []

                def sweep() -> None:
                    events.append("local")
                    raise ValueError("local cleanup failed")

                def run(argv: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    self.assertTrue(options["check"])
                    if argv[1].endswith("nix_env.py"):
                        self.assertEqual(list(argv[2:4]), ["exec", sys.executable])
                        self.assertTrue(argv[4].endswith("mac_queue_run.py"))
                        events.append("Mac")
                        self.assertEqual(list(argv[5:13]),
                                         ["--kind", "run", "--what", "Sweep Mac build slots", "--package", "claude", "--", "/bin/zsh"])
                    else:
                        self.assertTrue(argv[1].endswith("send.py"))
                        self.assertEqual(list(argv[2:6]), ["--to", "builds", "--from", "nightly"])
                        notices.append(argv[-1])
                    return subprocess.CompletedProcess(argv, 0, "", "")

                with (mock.patch.dict(os.environ, config.environment),
                      mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                      mock.patch.object(merge_gate, "nightly", return_value=status),
                      mock.patch.object(build_slots, "sweep_slots", side_effect=sweep),
                      mock.patch.object(subprocess, "run", side_effect=run),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(merge_gate.main(["--nightly", "--merge-branch", "fixture"]), status)
                self.assertEqual(events, ["local", "Mac"])
                self.assertEqual(len(notices), 1)
                self.assertIn("natedev failed: local cleanup failed", notices[0])
                self.assertEqual(output.getvalue().strip(), notices[0])

    def test_mac_sweep_failure_reports_once_and_keeps_the_nightly_result(self) -> None:
        for status in (0, 1, 75):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                config = private_slot_config(Path(temporary), repository="fixture")
                notices: list[str] = []

                def run(argv: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    self.assertTrue(options["check"])
                    if argv[1].endswith("nix_env.py"):
                        self.assertEqual(list(argv[2:4]), ["exec", sys.executable])
                        self.assertTrue(argv[4].endswith("mac_queue_run.py"))
                        raise subprocess.CalledProcessError(3, argv, stderr="Mac is unreachable\n")
                    self.assertTrue(argv[1].endswith("send.py"))
                    notices.append(argv[-1])
                    return subprocess.CompletedProcess(argv, 0, "", "")

                with (mock.patch.dict(os.environ, config.environment),
                      mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                      mock.patch.object(merge_gate, "nightly", return_value=status),
                      mock.patch.object(build_slots, "sweep_slots") as local,
                      mock.patch.object(subprocess, "run", side_effect=run),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(merge_gate.main(["--nightly", "--merge-branch", "fixture"]), status)
                local.assert_called_once_with()
                self.assertEqual(len(notices), 1)
                self.assertIn("on Mac failed:", notices[0])
                self.assertIn("Mac is unreachable", notices[0])
                self.assertEqual(output.getvalue().strip(), notices[0])

    def test_gate_failure_still_sweeps_both_pools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = private_slot_config(Path(temporary), repository="fixture")
            events: list[str] = []

            def nightly(args: merge_gate.GateArguments) -> int:
                events.append("nightly:" + args.merge_branch)
                raise GateRefused("gate failed")

            def run(argv: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                self.assertTrue(options["check"])
                events.append("Mac")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with (mock.patch.dict(os.environ, config.environment),
                  mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                  mock.patch.object(merge_gate, "nightly", side_effect=nightly),
                  mock.patch.object(build_slots, "sweep_slots", side_effect=lambda: events.append("local")),
                  mock.patch.object(subprocess, "run", side_effect=run),
                  redirect_stderr(io.StringIO()) as output):
                self.assertEqual(merge_gate.main(["--nightly", "--merge-branch", "fixture"]), 1)
            self.assertEqual(events, ["nightly:fixture", "local", "Mac"])
            self.assertIn("gate failed", output.getvalue())

    def test_inactive_pools_start_no_mac_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = private_slot_config(Path(temporary), repository="fixture")
            _ = config.settings.write_text("\n".join(line for line in config.settings.read_text().splitlines()
                                                   if not line.startswith("roots.slots")) + "\n")
            with (mock.patch.dict(os.environ, config.environment),
                  mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                  mock.patch.object(merge_gate, "nightly", return_value=0),
                  mock.patch.object(subprocess, "run") as run,
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(merge_gate.main(["--nightly", "--merge-branch", "fixture"]), 0)
            run.assert_not_called()
            self.assertEqual(output.getvalue(), "")
