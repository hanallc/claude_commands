"""Keep nightly results when the build folder sweep fails."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
from itertools import product
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.copies import copies
from scripts.lib import scratch
from scripts.lint import sweep as folder_sweep
from scripts.message import send_budget
from scripts.production import gate, staff
from scripts.production.gate_modules import GateRefused


class NightlySlotSweepTests(unittest.TestCase):
    def test_nightly_requires_a_unit_or_production_before_work_starts(self) -> None:
        with (mock.patch.object(gate, "nightly_git") as git,
              mock.patch.object(gate, "_sweep_nightly_build_folders") as sweep,
              redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as status):
            _ = gate.main(["--nightly", "--merge-branch", "fixture"])
        self.assertEqual(status.exception.code, 2)
        self.assertIn("--nightly needs --unit or --production", output.getvalue())
        git.assert_not_called()
        sweep.assert_not_called()

    def test_timer_selects_a_unit_from_its_production(self) -> None:
        with tempfile.TemporaryDirectory() as checkout:
            production = staff.StaffId("20b0427d-945b-4c40-bb24-a6bec83dd648")
            merge = staff.StaffId("11111111-0000-4000-8000-111111111111")
            other_merge = staff.StaffId("22222222-0000-4000-8000-222222222222")
            owner = staff.StaffRow(production, "showrunner", "sdlc", staff.StaffRoot(),
                                   current_session=production)
            direct = staff.StaffRow(staff.StaffId("direct"), "unit", "fixture-direct", staff.ReportsTo(production),
                                    current_session=staff.StaffId("direct"), unit_type=staff.UnitDetails("direct"))
            unit = staff.StaffRow(merge, "unit", "fixture-merge", staff.ReportsTo(production),
                                  current_session=merge, unit_type=staff.UnitDetails("merge"))
            other = staff.StaffRow(other_merge, "unit", "other-merge", staff.ReportsTo(production),
                                   current_session=other_merge, unit_type=staff.UnitDetails("merge"))
            arguments = ["--nightly", "--merge-branch", "main", "--checkout", checkout,
                         "--production", str(production)]
            cases = (
                ((owner, direct, unit), 0, str(merge)),
                ((owner, direct), 0, "nightly"),
                ((owner, direct, unit, other), 1, ""),
            )
            for rows, expected_status, expected_unit in cases:
                with (self.subTest(unit=expected_unit), staff.supplied_snapshot(staff.StaffSnapshot(rows)),
                      mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                      mock.patch.object(gate, "nightly_git") as git,
                      mock.patch.object(gate, "checked_git", return_value="fixture-commit"),
                      mock.patch.object(copies, "standing", return_value=nullcontext(Path(checkout))) as take,
                      mock.patch.object(gate, "run_suites", return_value=gate.GateRun((gate.GateJobResult("test", "fixture", 0, 0, ""),), 0, 0)),
                      mock.patch.object(gate, "gate_state_dir", return_value=Path(checkout)),
                      mock.patch.object(gate, "push_nightly_result"),
                      mock.patch.object(gate, "_sweep_nightly_build_folders") as sweep,
                      redirect_stderr(io.StringIO()) as output):
                    self.assertEqual(gate.main(arguments), expected_status)
                    if expected_status == 0:
                        take.assert_called_once_with(expected_unit, Path(checkout).resolve(),
                            copies.CommittedSource("fixture-commit"), step="test-nightly")
                    else:
                        git.assert_not_called()
                        take.assert_not_called()
                        sweep.assert_not_called()
                        self.assertIn("sdlc: the production record has multiple merge units", output.getvalue())

    def test_local_sweep_failure_reports_once_and_keeps_the_nightly_result(self) -> None:
        for status in (0, 1, 75):
            with self.subTest(status=status):
                events: list[str] = []
                notices: list[str] = []

                def sweep(arguments: list[str]) -> int:
                    self.assertEqual(arguments, ["--unused-folders"])
                    events.append("local")
                    raise ValueError("local cleanup failed")

                def run(argv: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    self.assertTrue(options["check"])
                    if argv[1].endswith("nix_env.py"):
                        self.assertEqual(list(argv[2:4]), ["exec", sys.executable])
                        self.assertTrue(argv[4].endswith("mac_queue_run.py"))
                        self.assertNotIn("--manual", argv)
                        events.append("Mac")
                        self.assertEqual(list(argv[5:13]),
                                         ["--kind", "run", "--what", "Sweep unused Mac build folders", "--package", "claude",
                                          "--worktree", str(Path(gate.__file__).resolve().parents[2])])
                        # The nightly runs from $HOME; the Mac queue refuses a worktree that is not a repository.
                        self.assertTrue((Path(argv[12]) / ".git").exists())
                        self.assertEqual(argv[13], "--caller-fd")
                        caller = int(argv[14])
                        self.assertEqual(list(argv[15:17]), ["--", "/bin/zsh"])
                        self.assertEqual(options["pass_fds"], (caller,))
                        self.assertIs(options["start_new_session"], True)
                        self.assertEqual(select.select([caller], [], [], 0)[0], [])
                    else:
                        self.assertTrue(argv[1].endswith("send.py"))
                        self.assertEqual(list(argv[2:6]), ["--to", "builds", "--from", "nightly"])
                        notices.append(argv[-1])
                    return subprocess.CompletedProcess(argv, 0, "", "")

                with (mock.patch.object(send_budget, "seconds", return_value=1),
                      mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                      mock.patch.object(gate, "nightly", return_value=status),
                      mock.patch.object(folder_sweep, "main", side_effect=sweep),
                      mock.patch.object(subprocess, "run", side_effect=run),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(gate.main(["--nightly", "--merge-branch", "fixture", "--unit", "fixture-merge"]), status)
                self.assertEqual(events, ["local", "Mac"])
                self.assertEqual(len(notices), 1)
                self.assertIn("natedev failed: local cleanup failed", notices[0])
                self.assertEqual(output.getvalue().strip(), notices[0])

    def test_mac_sweep_failure_reports_once_and_keeps_the_nightly_result(self) -> None:
        refusals = ("refused\trepo", "enabled.mac is off")
        for status, refusal in product((0, 1, 75), refusals):
            with self.subTest(status=status, refusal=refusal):
                notices: list[str] = []

                def run(argv: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    self.assertTrue(options["check"])
                    if argv[1].endswith("nix_env.py"):
                        self.assertEqual(list(argv[2:4]), ["exec", sys.executable])
                        self.assertTrue(argv[4].endswith("mac_queue_run.py"))
                        self.assertNotIn("--manual", argv)
                        raise subprocess.CalledProcessError(2, argv, output=refusal + "\n",
                                                            stderr="Mac queue: Sweep unused Mac build folders\n")
                    self.assertTrue(argv[1].endswith("send.py"))
                    notices.append(argv[-1])
                    return subprocess.CompletedProcess(argv, 0, "", "")

                with (mock.patch.object(send_budget, "seconds", return_value=1),
                      mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                      mock.patch.object(gate, "nightly", return_value=status),
                      mock.patch.object(folder_sweep, "main", return_value=0) as local,
                      mock.patch.object(subprocess, "run", side_effect=run),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(gate.main(["--nightly", "--merge-branch", "fixture", "--unit", "fixture-merge"]), status)
                local.assert_called_once_with(["--unused-folders"])
                self.assertEqual(len(notices), 1)
                self.assertIn("on Mac failed:", notices[0])
                self.assertIn(f"Mac queue: Sweep unused Mac build folders {refusal}", notices[0])
                self.assertEqual(output.getvalue().strip(), notices[0])

    def test_gate_failure_still_sweeps_both_pools(self) -> None:
        events: list[str] = []

        def nightly(args: gate.GateArguments) -> int:
            events.append("nightly:" + args.merge_branch)
            raise GateRefused("gate failed")

        def run(argv: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            self.assertTrue(options["check"])
            events.append("Mac")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def local(arguments: list[str]) -> int:
            self.assertEqual(arguments, ["--unused-folders"])
            events.append("local")
            return 0

        with (mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
              mock.patch.object(gate, "nightly", side_effect=nightly),
              mock.patch.object(folder_sweep, "main", side_effect=local),
              mock.patch.object(subprocess, "run", side_effect=run),
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(gate.main(["--nightly", "--merge-branch", "fixture", "--unit", "fixture-merge"]), 1)
        self.assertEqual(events, ["nightly:fixture", "local", "Mac"])
        self.assertIn("gate failed", output.getvalue())
