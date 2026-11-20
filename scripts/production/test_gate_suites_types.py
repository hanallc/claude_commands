"""Merge gate suites contracts."""

from __future__ import annotations
from scripts.lint.memory_test_support import copy_config_fixture, write_config_fixture

import unittest
import fcntl
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import io
import json
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar, TextIO, cast, final, override
from unittest import mock
from datetime import UTC, datetime, timedelta, tzinfo
from scripts.agents import remote
from scripts.copies import copies
from scripts.direct import progress_history
from scripts.lib import build_folders
from scripts.message import send_budget
from scripts.production import change_review, gate, staff, showrunners
from scripts.production.staff_test_support import isolated_staff_file, fixture_member
from scripts.production.gate_modules import GateRefused
from scripts.production.cli_fixture import invoke_cli
from scripts.production.gate_test_support import gate_suites, wait_for_file
from scripts.production.gate_suites_test_support import GateSuitesFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class MergeGateTypesTests(GateSuitesFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.object(gate, "push_nightly_result")))

    def run_nightly(self, *, production: str = "") -> subprocess.CompletedProcess[str]:
        arguments = ["--nightly", "--repo", str(self.checkout), "--merge-branch", "integration"]
        if production:
            arguments.extend(["--production", production])
        owner = nullcontext() if production else mock.patch.object(gate, "nightly_owner", return_value="fixture-showrunner")
        with owner:
            return invoke_cli(gate, lambda: gate.main(arguments), arguments,
                              self.environment, self.root)

    def test_nightly_checks_all_scripts_with_the_fake_type_checker(self) -> None:
        result = self.run_nightly()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        type_call, = [call for call in self.calls() if call["tool"] == "basedpyright"]
        python = self.state / "runner/typecheck-venv/bin/python"
        self.assertEqual(type_call["args"], ["--pythonpath", str(python), "scripts"])
        self.assertNotEqual(type_call["cwd"], str(self.checkout))
        self.assertTrue(Path(type_call["cwd"]).exists())
        self.assertEqual(build_folders.build_folder_state(Path(type_call["cwd"])), "free")
        records = list((self.root / "slots").glob("*-slot*.lease.json"))
        self.assertEqual(len(records), 1)
        self.assertTrue((Path(type_call["cwd"]) / "target").is_dir())
        self.assertEqual(json.loads(records[0].read_text())["commit"], self.base)
        self.assert_all_modules_started()
        self.assertIn("basedpyright: OK", result.stdout)
        history, = self.gate_history()
        self.assertEqual(history["shape"], "types")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_nightly_type_error_names_the_file_without_requiring_full_test_runs(self) -> None:
        failing_file = "scripts/fixture.py"
        self.write(self.state / "types-red", "red")
        self.write(self.state / "runner/full-required", "older marker\n")
        with mock.patch.object(gate, "tell_nightly_owner", return_value="told fixture-showrunner") as notice:
            result = self.run_nightly()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(failing_file, result.stdout)
        self.assert_all_modules_started()
        notice.assert_called_once()
        arguments = notice.call_args
        assert arguments is not None
        tree = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
        self.assertEqual(arguments.args[:2], ("fixture-showrunner", f"merge-gate-nightly:{tree}"))
        message = cast(str, arguments.args[2])
        self.assertIn(failing_file, message)
        self.assertNotIn("requires the full suite", message)
        self.assertFalse((self.state / "runner/full-required").exists())
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_nightly_sends_each_type_diagnostic_to_its_author_unit(self) -> None:
        history = self.root / "history"
        source = "scripts/receipt.py"
        hidden_source = "scripts/receipt detail.py"
        wide_source = "scripts/" + ("long" * 45 + "/") * 3 + "wide.py"
        plan = "docs/plans/receipt.md"
        self.write(self.main / "scripts/unknown.py", "VALUE = 3\n")
        main_change = self.commit(self.main, "Change without an author unit")
        _ = self.git(self.checkout, "merge", "--no-ff", "-m", "Merge origin/main into integration", main_change)
        main_merge = self.git(self.checkout, "rev-parse", "HEAD")
        self.write(self.main / "scripts/fast.py", "VALUE = 2\n")
        fast_change = self.commit(self.main, "fast-track(fast): change the value")
        _ = self.git(self.main, "push", "origin", "main")
        _ = self.git(self.unit, "merge", "--no-ff", "-m", "Merge origin/main into unit-phase", fast_change)
        self.write(self.unit / plan, "# Receipt plan\n")
        self.write(self.unit / source, "VALUE = 1\nOTHER = 2\n")
        self.write(self.unit / hidden_source, "DETAIL = 1\n")
        self.write(self.unit / wide_source, "DETAIL = 2\n")
        checkpoint = self.commit(self.unit, "Checkpoint change")
        _ = self.git(self.checkout, "merge", "--no-ff", "-m", "Merge receipt fixture", checkpoint)
        receipt_merge = self.git(self.checkout, "rev-parse", "HEAD")
        _ = self.git(self.checkout, "push", "origin", "HEAD:integration")
        self.write(self.unit / ".unit.toml", f'plan = "{plan}"\nresponsibilities = []\n')

        self.environment["UNIT_HISTORY_DIR"] = str(history)
        with (mock.patch.dict(os.environ, self.environment),
              isolated_staff_file(self.root / "nightly-staff.json", self.environment)):
            owner = fixture_member("showrunner", name="fixture-showrunner",
                                   doc=staff.LiveDoc(remote.local_machine(), self.quiet_production))
            receipt_author = fixture_member("unit", name="receipt", parent=owner.id,
                                            worktree=staff.PresentWorktree(remote.local_machine(), self.unit))
            fast_author = fixture_member("unit", name="fast", parent=owner.id)
            recorded_author = fixture_member("unit", name="original", parent=owner.id)
            _ = progress_history.write_merge_receipt(project="origin", phase_id="2", merge_commit=main_merge,
                                                     source_checkpoint=main_change, target_branch="integration",
                                                     merged_at=1, issue=plan)
            _ = progress_history.write_merge_receipt(project="origin", phase_id="2", merge_commit=receipt_merge,
                                                     source_checkpoint=checkpoint, target_branch="integration",
                                                     merged_at=2, issue=plan)
            self.assertIsInstance(change_review.merge_producer(receipt_merge), change_review.UnknownProducer)
            with (history / "merge_receipts.jsonl").open("a", encoding="utf-8") as receipts:
                _ = receipts.write("{incomplete\n")

            receipt_lines = (f"{source}:1:1 - error: first", f"{source}:2:1 - error: second")
            fast_line = "scripts/fast.py:1:1 - error: third"
            unknown_line = "scripts/unknown.py:1:1 - error: fourth"
            limit = gate.NIGHTLY_TYPE_DIAGNOSTIC_LINES
            byte_limit = gate.NIGHTLY_TYPE_LINE_BYTES
            long_line = f"{source}:1:1 - error: " + "漢" * byte_limit
            for case in ("assigned", "unknown", "test red", "long report", "wide files", "notice failure", "recorded producer"):
                with self.subTest(case=case):
                    self.environment["GATE_STATE_DIR"] = str(self.root / case)
                    expected_author = receipt_author
                    if case == "recorded producer":
                        change_review.record_merge_producer(
                            change_review.KnownMergeProducer(receipt_merge, recorded_author.id))
                        expected_author = recorded_author
                    routed_lines: list[str] = list(receipt_lines)
                    if case == "long report":
                        routed_lines.append(long_line)
                        routed_lines.extend(f"{source}:1:1 - error: finding {i}." for i in range(limit - 2))
                        routed_lines.append(f"{hidden_source}:1:1 - error: omitted file")
                    if case == "wide files":
                        routed_lines.append(f"{wide_source}:1:1 - error: wide path")
                    lines = [*routed_lines, fast_line]
                    if case == "unknown":
                        lines.append(unknown_line)
                    output = "\n".join([*lines, f"{len(lines)} errors, 0 warnings, 0 notes"])
                    type_check = gate.GateJobResult("types", "basedpyright", 1, 0, output,
                                                         kind=gate.GateJobKind.TYPE_CHECK)
                    test = gate.GateJobResult("hooks", "test_first.py", int(case == "test red"), 0, "")
                    run = gate.GateRun((type_check, test), 0, 0)
                    notice_result = "could not tell fixture-showrunner" if case == "notice failure" else "told fixture-showrunner"
                    with (mock.patch.object(gate, "run_suites", return_value=run),
                          mock.patch.object(gate, "send_nightly_notice",
                                            side_effect=[f"told {expected_author.name}", "told fast"]) as deliver,
                          mock.patch.object(gate, "tell_nightly_owner", return_value=notice_result) as notice,
                          mock.patch.object(gate, "send_nightly_red", return_value=True) as push):
                        result = self.run_nightly(production=owner.name)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    first, second = deliver.call_args_list
                    self.assertEqual(first.args[0], f"session:{expected_author.current_session}")
                    self.assertEqual(second.args[0], f"session:{fast_author.current_session}")
                    first_text = cast(str, first.args[2])
                    second_text = cast(str, second.args[2])
                    shown = first_text.splitlines()[1:-1]
                    recheck = first_text.splitlines()[-1]
                    expected = routed_lines[:limit]
                    if case in ("long report", "wide files"):
                        self.assertLessEqual(len(shown[2].encode()), byte_limit)
                        self.assertTrue(shown[2].endswith("..."))
                        self.assertTrue(routed_lines[2].startswith(shown[2][:-3]))
                        expected[2] = shown[2]
                    if case == "long report":
                        expected.append("2 other diagnostic lines omitted.")
                        self.assertEqual(recheck, "Run `basedpyright 'scripts/receipt detail.py' scripts/receipt.py` "
                                         + "to see every diagnostic in these files.")
                    elif case == "wide files":
                        self.assertLessEqual(len(recheck.encode()), byte_limit + 80)
                        self.assertTrue(recheck.startswith("Files include scripts/long"))
                        self.assertIn("Run `basedpyright scripts` to see every diagnostic.", recheck)
                    else:
                        self.assertEqual(recheck, f"Run `basedpyright {source}` to see every diagnostic in these files.")
                    self.assertEqual(shown, expected)
                    self.assertEqual(second_text.splitlines()[1:], [
                        fast_line, "Run `basedpyright scripts/fast.py` to see every diagnostic in these files."])
                    self.assertIn("Fix it in your next phase.", first_text)
                    notice.assert_called_once()
                    notice_text = cast(str, notice.call_args.args[2])
                    heading = f"Sent to {expected_author.name}:"
                    self.assertIn(heading + "\n" + "\n".join(receipt_lines), notice_text)
                    if case == "long report":
                        author_lines = notice_text[notice_text.index(heading):].splitlines()
                        self.assertEqual(author_lines, [heading, *shown[:limit - 1], "5 other diagnostic lines omitted."])
                        self.assertNotIn(long_line, notice_text)
                    elif case == "wide files":
                        self.assertNotIn(wide_source, notice_text)
                        self.assertIn("Sent to fast:\n" + fast_line, notice_text)
                    else:
                        self.assertIn("Sent to fast:\n" + fast_line, notice_text)
                    path, = (Path(self.environment["GATE_STATE_DIR"]) / "nightly-red").glob("*.json")
                    saved = gate.read_nightly_red(path)
                    if case not in ("unknown", "test red"):
                        self.assertIsInstance(saved.assignment, gate.NightlyFixAssigned)
                        assert isinstance(saved.assignment, gate.NightlyFixAssigned)
                        self.assertEqual(saved.assignment.unit, expected_author.id)
                        push.assert_called_once()
                        self.assertNotIn("nightly-assigned", notice_text)
                    else:
                        self.assertIsInstance(saved.assignment, gate.NightlyFixUnassigned)
                        push.assert_not_called()
                        self.assertIn(f"nightly-assigned --result {saved.id} --unit UNIT", notice_text)
                    if case == "unknown":
                        self.assertIn(unknown_line, notice_text)
                    if case == "notice failure":
                        self.assertIsInstance(saved.received, gate.NightlyNoticeUnreceived)

    def test_nightly_notice_limits_a_large_type_report_and_keeps_its_summary(self) -> None:
        diagnostics = [f"scripts/broken_{index}.py:1: error: " + "incompatible type " * 8
                       for index in range(2000)]
        summary = "2000 errors, 0 warnings, 0 notes"
        job = gate.GateJobResult("types", "basedpyright", 1, 0,
            "\n".join([*diagnostics, summary]), kind=gate.GateJobKind.TYPE_CHECK)
        with (mock.patch.object(gate, "nightly_owner", return_value="fixture-showrunner"),
              mock.patch.object(gate, "tell_nightly_owner", return_value="told fixture-showrunner") as notice):
            _ = gate.tell_nightly_red(gate.GateArguments(merge_branch="integration"),
                                        "fixture-tree", [], type_failures=(job,))

        notice.assert_called_once()
        arguments = notice.call_args
        assert arguments is not None
        message = cast(str, arguments.args[2])
        self.assertLess(len(message.encode()), 8000)
        for line in diagnostics[:20]:
            self.assertIn(line, message)
        self.assertNotIn(diagnostics[20], message)
        self.assertIn("1980 other diagnostic lines omitted.", message)
        self.assertIn(summary, message)

    def test_nightly_notice_without_a_failed_job_says_the_full_suite_is_required(self) -> None:
        with (mock.patch.object(gate, "nightly_owner", return_value="fixture-showrunner"),
              mock.patch.object(gate, "tell_nightly_owner", return_value="told fixture-showrunner") as notice):
            _ = gate.tell_nightly_red(gate.GateArguments(merge_branch="integration"), "fixture-tree", [])

        arguments = notice.call_args
        assert arguments is not None
        message = cast(str, arguments.args[2])
        self.assertIn("(runner failure)", message)
        self.assertIn("requires the full suite", message)
        self.assertNotIn("Tests passed", message)

    def test_type_check_requires_exit_zero_and_a_clean_summary(self) -> None:
        for status, summary, expected in (
            (0, "0 errors, 0 warnings, 0 notes", 0),
            (3, "0 errors, 0 warnings, 0 notes", 1),
            (1, "1 error, 0 warnings, 0 notes", 1),
            (0, "1 error, 0 warnings, 0 notes", 1),
        ):
            with self.subTest(status=status, summary=summary):
                self.write(self.bin / "basedpyright",
                           f"#!{sys.executable}\nprint({summary!r})\nraise SystemExit({status})\n")
                result = self.run_nightly()
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn("basedpyright: " + ("OK" if expected == 0 else "RED"), result.stdout)

    @gate_suites("hooks")
    def test_types_owns_a_shared_slot_until_it_finishes(self) -> None:
        self.supply_jobserver(b"T")
        start = gate.start_gate_job
        types_pids: list[int] = []

        def observed(suite: str, path: Path, argv: list[str], repo: Path,
                     environment: dict[str, str], log: Path, *,
                kind: gate.GateJobKind = gate.GateJobKind.TEST_MODULE) -> gate.RunningGateJob:
            if suite == "types":
                self.assertEqual(self.jobserver_tokens(), b"")
            job = start(suite, path, argv, repo, environment, log, kind=kind)
            if suite == "types":
                types_pids.append(job.process.pid)
                wait_for_file(self.state / "types-ran")
            return job

        with mock.patch.object(gate, "start_gate_job", side_effect=observed):
            result = self.run_nightly()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(types_pids), 1)
        self.assertEqual(self.jobserver_tokens(), b"T")

    def test_the_type_check_venv_is_built_once_with_the_pinned_stubs(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            first, second = gate.typecheck_python(), gate.typecheck_python()
        venv = self.state / "runner" / "typecheck-venv"
        self.assertEqual((first, second), (venv / "bin" / "python", venv / "bin" / "python"))
        self.assertEqual([call["args"] for call in self.calls() if call["tool"] == "uv"],
                         [["venv", "--clear", str(venv)],
                          ["pip", "install", "--python", str(venv / "bin" / "python"),
                           "fake-bpy-module==20260822"]])

    def test_tests_only_refuses_type_check_options(self) -> None:
        for option in ("--type-dirs", "--type-dir"):
            with self.subTest(option=option):
                result = self.run_gate("--tests-only", option, "scripts")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f"unrecognized arguments: {option} scripts", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

    def test_tests_only_runs_modules_without_a_default_type_check(self) -> None:
        result = subprocess.run(
            [*self.gate_command(), "--tests-only", "--repo", str(self.checkout)],
            cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        self.assert_all_modules_started()
        self.assert_not_pushed()
        history, = self.gate_history()
        self.assertEqual(history["shape"], "tests")

    def test_merge_pushes_modules_without_a_default_type_check(self) -> None:
        result = self.run_gate()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        self.assert_all_modules_started()
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"),
                         self.git(self.checkout, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    @gate_suites("lib")
    def test_a_type_check_venv_that_cannot_be_built_is_red_and_names_the_command(self) -> None:
        self.write(self.state / "uv-fails", "fail")
        result = self.run_nightly()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("basedpyright: RED\ntype-check venv missing: stderr: fixture: no network; build it with: ", result.stdout)
        self.assertIn(" pip install --python ", result.stdout)
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        self.assertRegex(result.stdout, r"(?m)^lib: OK \d+\.\d+s$")
        self.assert_not_pushed()

    def test_a_red_type_stub_does_not_prevent_a_merge_push(self) -> None:
        self.write(self.state / "types-red", "red")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "integration"))
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")


class NightlyClock(datetime):
    current: ClassVar[datetime] = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)

    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return cls.current.astimezone(tz) if tz is not None else cls.current.replace(tzinfo=None)


@final
class NightlyPushTests(unittest.TestCase):
    root: Path = Path()

    @property
    def fixer(self) -> staff.StaffRow:
        row = staff.snapshot().by_path("sdlc/fixes")
        assert row is not None
        return row

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / "config"
        _ = copy_config_fixture(Path(__file__).resolve().parents[2] / "config", config)
        _ = write_config_fixture(config / "gate.conf",
            "nightly_result_push=on\nnightly_result_modules=8\nnightly_assign_minutes=30\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "CONF_DIR": str(config),
            "GATE_STATE_DIR": str(self.root / "state"), "USER_ZONE": "America/New_York"})))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        owner = fixture_member("showrunner", name="sdlc")
        _ = fixture_member("unit", name="fixes", parent=owner.id)

    @contextmanager
    def execution(self, run: gate.GateRun | Exception, *, mock_owner_notice: bool = True
                  ) -> Generator[mock.MagicMock, None, None]:
        owner_notice = (mock.patch.object(gate, "tell_nightly_owner", return_value="told sdlc")
                        if mock_owner_notice else nullcontext())
        with (mock.patch.object(gate, "nightly_git", return_value=""),
              mock.patch.object(gate, "checked_git", return_value="fixture-tree"),
              mock.patch.object(gate, "nightly_owner", return_value="sdlc"),
              mock.patch.object(copies, "standing", return_value=nullcontext(self.root)),
              mock.patch.object(gate, "run_suites", return_value=run,
                                side_effect=run if isinstance(run, Exception) else None),
              owner_notice,
              mock.patch.object(showrunners, "tell", return_value="told sdlc"),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                  [], 0, "SENT: fixture\n", "")) as sent):
            yield sent

    def nightly(self) -> int:
        return gate.nightly(gate.GateArguments(repo=str(self.root), merge_branch="main"))

    def test_stalled_author_notice_releases_nightly_result_lock(self) -> None:
        job = gate.GateJobResult("types", "basedpyright", 1, 0,
            "scripts/fixture.py:1:1 - error: fixture", kind=gate.GateJobKind.TYPE_CHECK)
        run = gate.GateRun((job,), 0, 0)
        budget = 2.0
        run_sender = subprocess.run

        def stalled_sender(command: list[str], *, timeout: float | None = None,
                           **_options: object) -> subprocess.CompletedProcess[str]:
            assert timeout is not None, "The notice must have a deadline"
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, budget)
            with self.assertRaises(GateRefused), gate.nightly_red_lock():
                self.fail("The nightly result lock must be held during delivery")
            if command[command.index("--from") + 1] == "showrunners":
                return subprocess.CompletedProcess(command, 0, "SENT: fixture\n", "")
            return run_sender([sys.executable, "-c", "from threading import Event\nEvent().wait()"],
                              timeout=timeout, capture_output=True, text=True, check=False)

        output = io.StringIO()
        with (self.execution(run, mock_owner_notice=False) as sent,
              mock.patch.object(gate, "_nightly_author", return_value=self.fixer),
              mock.patch.object(send_budget, "seconds", return_value=budget),
              mock.patch.object(showrunners, "current_address", return_value="uds:/fixture"),
              redirect_stdout(output)):
            sent.side_effect = stalled_sender
            self.assertEqual(self.nightly(), 1)

        author, owner = sent.call_args_list
        self.assertIn("gate", cast(list[str], author.args[0]))
        failure = "could not tell fixes: the notice deadline expired"
        self.assertIn(failure, output.getvalue())
        owner_command = cast(list[str], owner.args[0])
        self.assertIn(failure, owner_command[owner_command.index("--text") + 1])
        with gate.nightly_red_lock() as directory:
            path, = directory.glob("*.json")
            result = gate.read_nightly_red(path)
            self.assertIsInstance(result.assignment, gate.NightlyFixUnassigned)
            self.assertIsInstance(result.received, gate.NightlyNoticeReceived)

    def test_nightly_push_describes_the_run_and_limits_failed_modules(self) -> None:
        good = gate.GateJobResult("message", "test_send.py", 0, 0, "")
        types = gate.GateJobResult("types", "basedpyright", 0, 0, "",
                                        kind=gate.GateJobKind.TYPE_CHECK)
        red_types = gate.GateJobResult("types", "basedpyright", 1, 0,
            "5 errors, 2 warnings, 0 notes\ndiagnostic\n3 errors, 1 warning, 0 notes",
            kind=gate.GateJobKind.TYPE_CHECK)
        red_jobs = tuple(gate.GateJobResult("message", f"test_{i}.py", 1, 0, "") for i in range(10))
        cases: tuple[tuple[str, gate.GateRun | Exception, str, str, str], ...] = (
            ("green", gate.GateRun((good, types), 0, 0), "passed", "passed", "passed"),
            ("test red", gate.GateRun((red_jobs[0], types), 0, 0), "failed", "passed",
             "failed in message/test_0.py"),
            ("many red", gate.GateRun((*red_jobs, types), 0, 0), "failed", "passed",
             "failed in " + ", ".join(f"message/test_{i}.py" for i in range(8)) + " and 2 more"),
            ("types red", gate.GateRun((good, red_types), 0, 0), "failed",
             "failed with 3 errors, 1 warning, 0 notes", "passed"),
            ("runner", GateRefused("runner failed"), "failed", "did not run", "did not finish"),
            ("empty", gate.GateRun((), 0, 0), "failed", "did not run", "did not finish"),
        )
        for name, run, outcome, expected_types, tests in cases:
            with self.subTest(name=name), self.execution(run) as sent, redirect_stdout(io.StringIO()):
                self.assertEqual(self.nightly(), 0 if outcome == "passed" else 1)
                expected_key = "nightly-result"
                if outcome == "failed":
                    sent.assert_not_called()
                    pending, = (gate.read_nightly_red(path) for path in (self.root / "state/nightly-red").glob("*.json")
                                if isinstance(gate.read_nightly_red(path).delivery, gate.NightlyPushPending))
                    expected_key += ":" + pending.id
                    self.assertEqual(gate.main(["nightly-assigned", "--unit", str(self.fixer.id)]), 0)
                sent.assert_called_once()
                command = cast(list[str], sent.call_args.args[0])
                for flag, value in (("--text-id", "nightly.result" if outcome == "passed" else "nightly.assigned"), ("--need", "note"),
                                    ("--key", expected_key)):
                    self.assertEqual(command[command.index(flag) + 1], value)
                values = dict(command[i + 1].split("=", 1) for i, word in enumerate(command) if word == "--value")
                self.assertEqual((values["types"], values["tests"], values["outcome"]),
                                 (expected_types, tests, outcome))
                self.assertEqual(values["branch"], "main")
                self.assertRegex(values["time"], r"^\d{2}:\d{2} E[SD]T$")
                self.assertNotIn("--priority", command)

    def test_assignment_sends_each_red_result_once(self) -> None:
        from scripts.notify import push_texts
        from scripts.maintenance import herdr_upkeep
        from scripts.herdr import herdr, detection_rules
        from scripts.accounts import accounts

        run = gate.GateRun((gate.GateJobResult("production", "test_staff.py", 1, 0,
                                 "FAIL: saved branch differs"),), 0, 0)
        for route in ("assignment", "maintenance", "notice failure", "send failure", "assignment race",
                      "busy lock", "save failure", "long assignment"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                case_run = (gate.GateRun(tuple(gate.GateJobResult("production",
                            f"test_staff.py_{index}_" + "x" * 100, 1, 0, "FAIL: saved branch differs")
                            for index in range(8)), 0, 0) if route.startswith("long ") else run)
                with (mock.patch.dict(os.environ, {"GATE_STATE_DIR": str(state)}),
                      self.execution(case_run) as sent, mock.patch.object(gate, "datetime", NightlyClock),
                      mock.patch.object(gate, "tell_nightly_owner", return_value="told sdlc") as notice,
                      redirect_stdout(io.StringIO())):
                    finished = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
                    NightlyClock.current = finished
                    if route == "notice failure":
                        notice.return_value = "could not tell sdlc: offline"
                    if route == "busy lock":
                        result_directory = state / "nightly-red"
                        result_directory.mkdir()
                        attempted = Event()
                        flock = fcntl.flock
                        with (result_directory / "lock").open("a") as held:
                            flock(held, fcntl.LOCK_EX)
                            def observed_lock(fd: TextIO, operation: int) -> None:
                                if operation & fcntl.LOCK_NB:
                                    try:
                                        flock(fd, operation)
                                    finally:
                                        attempted.set()
                                else:
                                    attempted.set()
                                    flock(fd, operation)
                            with ThreadPoolExecutor(max_workers=1) as executor:
                                with mock.patch.object(fcntl, "flock", side_effect=observed_lock):
                                    future = executor.submit(self.nightly)
                                    try:
                                        self.assertTrue(attempted.wait(TEST_HANG_GUARD_SECS))
                                    finally:
                                        flock(held, fcntl.LOCK_UN)
                                    self.assertEqual(future.result(timeout=TEST_HANG_GUARD_SECS), 1)
                    else:
                        self.assertEqual(self.nightly(), 1)
                    sent.assert_not_called()
                    notice.assert_called_once()
                    self.assertIn("production/test_staff.py", cast(str, notice.call_args.args[2]))
                    record_path, = (state / "nightly-red").glob("*.json")
                    saved = cast(dict[str, object], json.loads(record_path.read_text()))
                    self.assertEqual(saved["tree"], "fixture-tree")
                    self.assertIn("FAIL: saved branch differs", cast(list[str], saved["failures"]))
                    self.assertEqual(saved["received"], {} if route == "notice failure" else {"at": finished.isoformat()})
                    self.assertEqual(saved["finished"], finished.isoformat())
                    assignment = ["nightly-assigned", "--result", record_path.stem, "--unit", str(self.fixer.id)]
                    if route == "save failure":
                        from scripts.message import send, send_result
                        NightlyClock.current = finished + timedelta(minutes=5)
                        sessions = state / "sessions"
                        sessions.mkdir()
                        def invoke_sender(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                            del options
                            result = send.send(send.parse(command[2:] + ["--here"]))
                            return subprocess.CompletedProcess(command, send_result.EXIT[result.outcome], result.detail, "")
                        save = gate.save_nightly_red
                        def fail_acceptance_save(directory: Path, result: gate.NightlyRedResult) -> None:
                            if isinstance(result.delivery, gate.NightlyPushAccepted):
                                raise OSError("fixture: accepted result save failed")
                            save(directory, result)
                        with (mock.patch.object(send, "STATE", state / "message"),
                              mock.patch.object(send, "SESSIONS", sessions),
                              mock.patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(sessions)}),
                              mock.patch.object(send, "user", return_value=send.Result("sent", "fixture phone")) as phone,
                              redirect_stderr(io.StringIO())):
                            sent.side_effect = invoke_sender
                            with mock.patch.object(gate, "save_nightly_red", side_effect=fail_acceptance_save):
                                self.assertEqual(gate.main(assignment), 1)
                            self.assertEqual(gate.main(assignment), 0)
                            self.assertEqual(gate.main(["nightly-check"]), 0)
                            phone.assert_called_once()
                        continue
                    assigned_minutes = 30 if route in ("assignment race", "maintenance") else 5
                    NightlyClock.current = finished + timedelta(minutes=assigned_minutes)
                    if route == "maintenance":
                        with (mock.patch.object(herdr, "restore_server"),
                              mock.patch.object(herdr, "live_sessions", return_value=[]),
                              mock.patch.object(herdr, "save_shared_layout"),
                              mock.patch.object(detection_rules, "refresh"),
                              mock.patch.object(accounts, "producer_upkeep_members", return_value=[]),
                              mock.patch.object(showrunners, "tell", new=notice)):
                            herdr_upkeep.upkeep()
                        sent.assert_not_called()
                        self.assertEqual(notice.call_count, 2)
                        self.assertEqual(notice.call_args.args[1], f"merge-gate-nightly-unassigned:{record_path.stem}")
                    if route == "assignment race":
                        def racing_check(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                            del options
                            self.assertEqual(gate.main(["nightly-check"]), 1)
                            return subprocess.CompletedProcess(command, 0, "SENT: fixture\n", "")
                        sent.side_effect = racing_check
                    if route == "send failure":
                        sent.return_value = subprocess.CompletedProcess([], 3, "", "sender unavailable")
                        with redirect_stderr(io.StringIO()):
                            self.assertEqual(gate.main(assignment), 1)
                        pending = cast(dict[str, object], json.loads(record_path.read_text()))
                        self.assertEqual(pending["delivery"], {})
                        sent.return_value = subprocess.CompletedProcess([], 0, "SENT: fixture\n", "")
                        sent.reset_mock()
                    self.assertEqual(gate.main(assignment), 0)
                    sent.assert_called_once()
                    command = cast(list[str], sent.call_args.args[0])
                    text_id = command[command.index("--text-id") + 1]
                    values = dict(command[i + 1].split("=", 1) for i, word in enumerate(command) if word == "--value")
                    message = push_texts.render(text_id, **values).message
                    self.assertIn("production/test_staff.py", message)
                    if route.startswith("long "):
                        from scripts.notify import pushover
                        message = pushover.fields({"token": "fixture", "user": "fixture"}, "Nightly", message, "0")["message"]
                    self.assertIn("sdlc received it at " + ("03:05 EST" if route == "notice failure" else "03:00 EST"), message)
                    self.assertIn(f"Assigned to fixes at 03:{assigned_minutes:02d} EST", message)
                    with redirect_stdout(io.StringIO()) as repeated:
                        self.assertEqual(gate.main(assignment), 0)
                        self.assertEqual(gate.main(assignment), 0)
                        self.assertEqual(gate.main(["nightly-check"]), 0)
                    self.assertIn("unchanged", repeated.getvalue())
                    sent.assert_called_once()
                    saved = cast(dict[str, object], json.loads(record_path.read_text()))
                    self.assertEqual(cast(dict[str, str], saved["assignment"])["unit"], str(self.fixer.id))

    def test_nightly_check_retries_an_assigned_push_without_claiming_receipt(self) -> None:
        directory = self.root / "state/nightly-red"
        directory.mkdir(parents=True)
        finished = "2026-11-11T08:00:00+00:00"
        assignment = gate.NightlyFixAssigned(self.fixer.id, "2026-11-11T08:05:00+00:00")
        saved = gate.NightlyRedResult("automatic", "main", "fixture-tree", finished, (),
                                           "failed with 1 error, 0 warnings, 0 notes", "passed", assignment=assignment)
        gate.save_nightly_red(directory, saved)
        path = directory / "automatic.json"
        unavailable = subprocess.CompletedProcess([], 3, "", "sender unavailable")
        accepted = subprocess.CompletedProcess([], 0, "SENT: fixture\n", "")
        with (mock.patch.object(subprocess, "run", side_effect=[unavailable, accepted]) as sent,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(gate.main(["nightly-check"]), 1)
            self.assertEqual(gate.read_nightly_red(path), saved)
            self.assertEqual(gate.main(["nightly-check"]), 0)
            self.assertEqual(gate.main(["nightly-check"]), 0)

        first, retry = sent.call_args_list
        command = cast(list[str], retry.args[0])
        self.assertEqual(first.args[0], command)
        self.assertEqual(command[command.index("--key") + 1], "nightly-result:automatic")
        self.assertEqual(command[command.index("--text-id") + 1], "nightly.result")
        self.assertFalse(any(word.startswith(("received=", "unit=")) for word in command))
        updated = gate.read_nightly_red(path)
        self.assertEqual(updated.received, saved.received)
        self.assertEqual(updated.assignment, assignment)
        self.assertIsInstance(updated.delivery, gate.NightlyPushAccepted)

    def red_nightly(self, finished: datetime) -> Path:
        NightlyClock.current = finished
        self.assertEqual(self.nightly(), 1)
        record_path, = (self.root / "state/nightly-red").glob("*.json")
        return record_path

    def test_an_unassigned_red_sends_no_user_push_at_any_age(self) -> None:
        run = gate.GateRun((gate.GateJobResult("production", "test_staff.py", 1, 0, ""),), 0, 0)
        finished = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
        with (self.execution(run) as sent, mock.patch.object(gate, "datetime", NightlyClock),
              redirect_stdout(io.StringIO())):
            _ = self.red_nightly(finished)
            for minutes in (0, 29, 30, 31, 90, 24 * 60):
                NightlyClock.current = finished + timedelta(minutes=minutes)
                self.assertEqual(gate.main(["nightly-check"]), 0)
            sent.assert_not_called()

    def test_an_unassigned_red_alerts_the_production_owner_each_window_until_assigned(self) -> None:
        run = gate.GateRun((gate.GateJobResult("production", "test_staff.py", 1, 0, ""),), 0, 0)
        finished = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
        with (self.execution(run) as sent, mock.patch.object(gate, "datetime", NightlyClock),
              redirect_stdout(io.StringIO())):
            record_path = self.red_nightly(finished)
            alerts = cast(mock.MagicMock, showrunners.tell)
            alerts.reset_mock()
            for minutes, expected in ((29, 0), (30, 1), (32, 1), (59, 1), (60, 2), (61, 2)):
                NightlyClock.current = finished + timedelta(minutes=minutes)
                self.assertEqual(gate.main(["nightly-check"]), 0)
                self.assertEqual(alerts.call_count, expected, f"minute {minutes}")
            owner, key, text = cast(tuple[str, str, str], alerts.call_args.args)
            self.assertEqual(owner, "sdlc")
            self.assertEqual(key, f"merge-gate-nightly-unassigned:{record_path.stem}")
            self.assertIn(f"nightly-assigned --result {record_path.stem} --unit UNIT", text)
            self.assertIn("production/test_staff.py", text)
            alerts.return_value = "could not tell sdlc: offline"
            NightlyClock.current = finished + timedelta(minutes=90)
            self.assertEqual(gate.main(["nightly-check"]), 0)
            NightlyClock.current = finished + timedelta(minutes=92)
            self.assertEqual(gate.main(["nightly-check"]), 0)
            self.assertEqual(alerts.call_count, 4)
            alerts.return_value = "told sdlc"
            self.assertEqual(gate.main(["nightly-assigned", "--unit", str(self.fixer.id)]), 0)
            sent.assert_called_once()
            NightlyClock.current = finished + timedelta(minutes=180)
            self.assertEqual(gate.main(["nightly-check"]), 0)
            self.assertEqual(alerts.call_count, 4)
            sent.assert_called_once()

    def test_a_unit_director_of_another_production_can_take_the_fix(self) -> None:
        builds = fixture_member("showrunner", name="builds")
        queue = fixture_member("unit", name="queue", parent=builds.id)
        run = gate.GateRun((gate.GateJobResult("production", "test_staff.py", 1, 0, ""),), 0, 0)
        for reference in ("builds/queue", str(queue.id)):
            with (self.subTest(reference=reference), tempfile.TemporaryDirectory() as directory,
                  mock.patch.dict(os.environ, {"GATE_STATE_DIR": directory}),
                  self.execution(run) as sent, redirect_stdout(io.StringIO())):
                self.assertEqual(self.nightly(), 1)
                self.assertEqual(gate.main(["nightly-assigned", "--unit", reference]), 0)
                command = cast(list[str], sent.call_args.args[0])
                self.assertIn("unit=queue", command)
        with self.execution(run), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.nightly(), 1)
            self.assertNotEqual(gate.main(["nightly-assigned", "--unit", "builds"]), 0)

    def test_off_disables_both_push_texts_at_use(self) -> None:
        from scripts.notify import push_texts

        for run in (gate.GateRun((gate.GateJobResult("test", "ok", 0, 0, ""),), 0, 0),
                    GateRefused("runner failed")):
            with self.execution(run) as sent, mock.patch.object(push_texts, "render") as render:
                _ = (self.root / "config/gate.conf").write_text("nightly_result_push=off\n")
                _ = self.nightly()
                sent.assert_not_called()
                render.assert_not_called()
                with mock.patch.object(gate, "nightly_git", side_effect=OSError("fetch failed")):
                    with self.assertRaisesRegex(OSError, "fetch failed"):
                        _ = self.nightly()
                sent.assert_not_called()
                render.assert_not_called()

    def test_failed_module_limit_is_read_again_for_each_run(self) -> None:
        jobs = tuple(gate.GateJobResult("test", f"test_{i}.py", 1, 0, "") for i in range(4))
        for limit in (1, 3):
            _ = (self.root / "config/gate.conf").write_text(
                f"nightly_result_push=on\n# whole: 1..\nnightly_result_modules={limit}\n")
            with self.execution(gate.GateRun(jobs, 0, 0)) as sent, redirect_stdout(io.StringIO()):
                self.assertEqual(self.nightly(), 1)
                sent.assert_not_called()
                self.assertEqual(gate.main(["nightly-assigned", "--unit", str(self.fixer.id)]), 0)
                command = cast(list[str], sent.call_args.args[0])
                self.assertIn("tests=failed in " + ", ".join(f"test/test_{i}.py" for i in range(limit))
                              + f" and {4 - limit} more", command)

    def test_failed_push_preserves_green_and_red_exit_and_marker(self) -> None:
        for status in (0, 1):
            run = gate.GateRun((gate.GateJobResult("test", "example", status, 0, ""),), 0, 0)
            for error in (subprocess.CompletedProcess([], 3, "", "unavailable\nmore detail"),
                          OSError("cannot start sender")):
                with self.subTest(status=status, error=error), self.execution(run) as sent:
                    if isinstance(error, OSError):
                        sent.side_effect = error
                    else:
                        sent.return_value = error
                    marker = self.root / "state/full-required"
                    marker.parent.mkdir(exist_ok=True)
                    _ = marker.write_text("older marker")
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(self.nightly(), status)
                    self.assertEqual(marker.exists(), bool(status))
                    if not status:
                        self.assertIn("Nightly result push could not be sent:", output.getvalue())
                    else:
                        sent.assert_not_called()
                    if status:
                        self.assertIn('"failed_modules": ["test/example"]', marker.read_text())

    def test_render_import_error_preserves_green_exit_and_marker(self) -> None:
        from scripts.notify import push_texts

        run = gate.GateRun((gate.GateJobResult("test", "ok", 0, 0, ""),), 0, 0)
        marker = self.root / "state/full-required"
        marker.parent.mkdir()
        _ = marker.write_text("older marker")
        with self.execution(run) as sent, mock.patch.object(push_texts, "render", side_effect=ImportError("render unavailable")):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.nightly(), 0)
            self.assertFalse(marker.exists())
            self.assertIn("Nightly result push could not be sent: render unavailable", output.getvalue())
            sent.assert_not_called()

    def test_setup_failure_push_preserves_the_exception(self) -> None:
        good = gate.GateRun((gate.GateJobResult("test", "ok", 0, 0, ""),), 0, 0)
        for stage in ("fetch", "standing"):
            failure = GateRefused(stage + " failed\nconnection closed")
            with self.subTest(stage=stage), self.execution(good) as sent:
                with (mock.patch.object(gate, "nightly_git", side_effect=["", failure]) if stage == "fetch"
                      else mock.patch.object(copies, "standing", side_effect=failure)):
                    with self.assertRaises(GateRefused) as caught, redirect_stdout(io.StringIO()):
                        _ = self.nightly()
                self.assertIs(caught.exception, failure)
                sent.assert_called_once()
                command = cast(list[str], sent.call_args.args[0])
                self.assertEqual(command[command.index("--text-id") + 1], "nightly.not_run")
                self.assertEqual(command[command.index("--key") + 1], "nightly-result")
                self.assertEqual(command[command.index("--need") + 1], "note")
                self.assertIn(f"reason={stage} failed connection closed", command)
                self.assertIn("action=None. The next nightly run tries again.", command)
                self.assertNotIn("--priority", command)

    def test_interrupt_push_preserves_the_exception(self) -> None:
        run = gate.GateRun((), 0, 0)
        for stage in ("nightly_git", "run_suites"):
            failure = KeyboardInterrupt()
            with self.subTest(stage=stage), self.execution(run) as sent:
                with mock.patch.object(gate, stage, side_effect=failure):
                    with self.assertRaises(KeyboardInterrupt) as caught, redirect_stdout(io.StringIO()):
                        _ = self.nightly()
                self.assertIs(caught.exception, failure)
                sent.assert_called_once()
                command = cast(list[str], sent.call_args.args[0])
                self.assertEqual(command[command.index("--text-id") + 1], "nightly.not_run")
                self.assertIn("reason=interrupted by SIGINT", command)
                self.assertEqual(command[command.index("--key") + 1], "nightly-result")

    def test_empty_exception_reason_uses_its_class_name(self) -> None:
        run = gate.GateRun((), 0, 0)
        failure = OSError()
        with self.execution(run) as sent, mock.patch.object(gate, "nightly_git", side_effect=failure):
            with self.assertRaises(OSError) as caught, redirect_stdout(io.StringIO()):
                _ = self.nightly()
            self.assertIs(caught.exception, failure)
            sent.assert_called_once()
            command = cast(list[str], sent.call_args.args[0])
            self.assertEqual(command[command.index("--text-id") + 1], "nightly.not_run")
            self.assertIn("reason=OSError", command)

    def test_cleanup_failure_does_not_send_a_second_push(self) -> None:
        @contextmanager
        def failing_cleanup() -> Generator[Path, None, None]:
            try:
                yield self.root
            finally:
                raise OSError("cleanup failed")

        run = gate.GateRun((gate.GateJobResult("test", "ok", 0, 0, ""),), 0, 0)
        with self.execution(run) as sent, mock.patch.object(copies, "standing", return_value=failing_cleanup()):
            with self.assertRaisesRegex(OSError, "cleanup failed"), redirect_stdout(io.StringIO()):
                _ = self.nightly()
            sent.assert_called_once()
            self.assertIn("nightly.result", cast(list[str], sent.call_args.args[0]))

    def test_not_run_reason_removes_final_periods(self) -> None:
        from scripts.notify import push_texts

        run = gate.GateRun((), 0, 0)
        with self.execution(run) as sent, mock.patch.object(gate, "nightly_git", side_effect=OSError("fetch\nfailed...")):
            with self.assertRaises(OSError), redirect_stdout(io.StringIO()):
                _ = self.nightly()
            command = cast(list[str], sent.call_args.args[0])
            values = dict(command[i + 1].split("=", 1) for i, word in enumerate(command) if word == "--value")
            self.assertEqual(values["reason"], "fetch failed")
            self.assertIn("action=None. The next nightly run tries again.", command)
            self.assertIn("Why: fetch failed.", push_texts.render("nightly.not_run", **values).message)

    def test_not_run_reason_fits_the_message_limit(self) -> None:
        from scripts.notify import push_texts
        from scripts.notify.pushover import MESSAGE_MAX

        run = gate.GateRun((), 0, 0)
        with self.execution(run) as sent, mock.patch.object(gate, "nightly_git", side_effect=OSError("word\n" * 2000)):
            with self.assertRaises(OSError), redirect_stdout(io.StringIO()):
                _ = self.nightly()
            command = cast(list[str], sent.call_args.args[0])
            values = dict(command[i + 1].split("=", 1) for i, word in enumerate(command) if word == "--value")
            rendered = push_texts.render("nightly.not_run", **values)
            self.assertLessEqual(len(rendered.message), MESSAGE_MAX)
            self.assertNotIn("\n", values["reason"])
            self.assertIn("word word", values["reason"])


if __name__ == "__main__":
    _ = unittest.main()
