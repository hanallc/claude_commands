"""Test the gate suite runner."""

from __future__ import annotations

import unittest
import json
import contextlib
import io
import os
import sys
import signal
import resource
import time
import subprocess
import select
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path
from typing import cast, final, override
from collections.abc import Generator
from unittest import mock
from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_records, placement
from scripts.build_queue.admission_test_support import only_admitted
from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.lib.release_waiter import LocalReleaseEvents
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import affected_modules, checkpoint_gate, gate
from scripts.lint.memory_test_support import write_config_fixture
from scripts.copies import copies
from scripts.lib import git_index, process_group
from scripts.lib.git_test_support import fixture_git_environment
from scripts.test_times import gate_record
from scripts.production.gate_modules import GateRefused, discover_test_modules, test_module_name
from scripts.production.gate_test_support import SUITE_MODULE, gate_suites, install_test_runner, wait_for_file
from scripts.production.gate_suites_test_support import GateSuitesFixture
from scripts.production.test_gate_default_placement import placed


@final
class MergeGateSuitesTests(GateSuitesFixture):
    @override
    def setUp(self) -> None:
        environment = {**fixture_git_environment(self.fixture_root.parent), "GIT_CONFIG_SYSTEM": os.devnull}
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment, clear=True)))
        super().setUp()

    @gate_suites("hooks")
    def test_finished_local_job_wins_when_polling_crosses_the_deadline(self) -> None:
        # The queue admission wait comes before the time limit starts.
        cases = (("whole machine", gate.WholeMachine(), 0.0),
                 ("shared after a 300 s admission wait", gate.SHARED_MACHINE, 300.0))
        for name, machine, admission_wait in cases:
            with self.subTest(name):
                now = 0.0
                began = 0.0
                process = cast(subprocess.Popen[str], cast(object, SimpleNamespace(pid=987654321, returncode=None)))

                @contextlib.contextmanager
                def admitted_after_wait(*_arguments: object, **_options: object) -> Generator[None]:
                    nonlocal now, began
                    now += admission_wait
                    began = now
                    yield None

                def start(*arguments: object) -> gate.RunningGateJob:
                    nonlocal now
                    suite, path, log = cast(str, arguments[0]), cast(Path, arguments[1]), cast(Path, arguments[5])
                    now = began + 9.98
                    self.write(log, "OK\n")
                    return gate.RunningGateJob(suite, path, process, began, 0, log.open("r+"), log, path.name)

                def reap(*arguments: int) -> tuple[int, int, resource.struct_rusage]:
                    return (arguments[0] if now >= began + 9.99 else 0), 0, resource.getrusage(resource.RUSAGE_SELF)

                def advance(seconds: float) -> None:
                    nonlocal now
                    now += seconds

                with (mock.patch.dict(os.environ, self.environment),
                      mock.patch.object(time, "monotonic", side_effect=lambda: now),
                      mock.patch.object(time, "sleep", side_effect=advance),
                      mock.patch.object(gate, "gate_reservation", side_effect=admitted_after_wait),
                      mock.patch.object(gate, "start_gate_job", side_effect=start),
                      mock.patch.object(os, "wait4", side_effect=reap),
                      mock.patch.object(process_group, "send_process_group"),
                      contextlib.redirect_stdout(io.StringIO())):
                    run = gate.run_suites(self.checkout, machine=machine,
                                               time_limit=gate_record.GateTimeLimit(10))
                self.assertTrue(run.passed, run.time_limit_report)
                self.assertEqual(run.jobs[0].returncode, 0)
                self.assertEqual(run.jobs[0].killed_for, "")
                self.assertEqual(run.time_limit_report, "")
                self.assertLess(run.seconds, 20)

    @gate_suites("hooks")
    def test_final_rerun_cannot_pass_after_the_gate_deadline(self) -> None:
        now = 0.0
        attempts = 0

        def start(*arguments: object) -> gate.RunningGateJob:
            nonlocal now, attempts
            attempts += 1
            if attempts == 2:
                now = 9.98
            suite, path, log = cast(str, arguments[0]), cast(Path, arguments[1]), cast(Path, arguments[5])
            process = cast(subprocess.Popen[str], cast(object, SimpleNamespace(pid=987654321, returncode=None)))
            self.write(log, "fixture attempt output\n")
            return gate.RunningGateJob(suite, path, process, now, 0, log.open("r+"), log, path.name)

        def reap(pid: int, _options: int) -> tuple[int, int, resource.struct_rusage]:
            ready = attempts == 1 or now >= 10.01
            status = 1 << 8 if attempts == 1 else 0
            return pid if ready else 0, status, resource.getrusage(resource.RUSAGE_SELF)

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(time, "monotonic", side_effect=lambda: now),
              mock.patch.object(time, "sleep", side_effect=advance),
              mock.patch.object(gate, "start_gate_job", side_effect=start),
              mock.patch.object(os, "wait4", side_effect=reap),
              mock.patch.object(process_group, "send_process_group"),
              contextlib.redirect_stdout(io.StringIO()) as output):
            run = gate.run_suites(self.checkout, machine=gate.WholeMachine(rerun_failed_modules=True),
                                  time_limit=gate_record.GateTimeLimit(10))

        self.assertEqual(attempts, 2)
        self.assertFalse(run.passed, output.getvalue())
        self.assertIn("limit 10 s; elapsed 10.030 s; running modules: hooks/test_first.py", run.time_limit_report)
        self.assertEqual(len(run.executions), 2)
        first, rerun = run.executions
        self.assertEqual(first.returncode, 1)
        self.assertNotEqual(rerun.returncode, 0)
        self.assertTrue(rerun.killed_for.startswith("time limit:"))
        self.assertEqual(run.jobs, (rerun,))
        self.assertNotIn("passed on rerun alone", output.getvalue())

    def test_time_limit_notice_resolves_staff_ids_and_unit_names(self) -> None:
        from scripts.message import roster, send
        from scripts.production import staff
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        report = "time limit: limit 10 s; elapsed 11 s; running modules: hooks/test_busy.py"
        run = gate.GateRun((), 11, 0, time_limit_report=report)
        received: list[tuple[str, str]] = []

        with (mock.patch.dict(os.environ, self.environment),
              isolated_staff_file(Path(self.environment["STAFF_FILE"]), self.environment)):
            parent = fixture_member("showrunner", name="fixture-production")
            owner = fixture_member("unit", name="fixture-unit", parent=parent.id)
            for position, reference in enumerate((str(owner.id), owner.name)):
                with self.subTest(owner=reference), mock.patch.object(subprocess, "run", return_value=
                        subprocess.CompletedProcess([], 0, "SENT: fixture owner", "")) as delivery, \
                        contextlib.redirect_stdout(io.StringIO()):
                    gate.tell_time_limit(reference, self.checkout, run)
                    for call in delivery.call_args_list:
                        arguments = send.parse(cast(list[str], call.args[0])[2:])
                        route = send.resolve_recipient(arguments.to, send.LocalOnly(), roster.Roster([], []),
                                                       membership=staff.snapshot(), records=[])
                        self.assertIsInstance(route, send.QueuedStaffRecipient)
                        if not isinstance(route, send.QueuedStaffRecipient):
                            self.fail("The private owner must resolve through its staff row")
                        received.append((route.address, send.body_text(arguments)))
                    self.assertEqual(received[position:position + 1], [(f"session:{owner.current_session}", report)])
        self.assertEqual(len(received), 2)

    def test_cpu_token_wait_returns_to_the_runner_at_its_deadline(self) -> None:
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        pool = gate.SharedCPUPool(reader, reader, deadline=12)
        now = 10.0
        waits: list[float | None] = []
        def wait(*arguments: object) -> tuple[list[int], list[int], list[int]]:
            nonlocal now
            waits.append(cast(float, arguments[3]))
            now = 13.0
            return [], [], []
        with mock.patch.object(time, "monotonic", side_effect=lambda: now), \
                mock.patch.object(select, "select", side_effect=wait), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(TimeoutError, "gate time limit"):
                _ = pool.acquire()
        self.assertEqual(waits, [2.0])
        self.assertEqual(pool.owned, [])

    def test_deadline_stops_groups_and_keeps_names_and_tracebacks(self) -> None:
        output = io.StringIO()
        log = self.root / "timed-module.log"
        self.write(log, "partial module output")
        process = cast(subprocess.Popen[str], cast(object, SimpleNamespace(pid=987654321, returncode=None)))
        job = gate.RunningGateJob("hooks", Path("test_busy.py"), process, 10, 10, output, log, "test_busy.py")
        failed = gate.GateJobResult("message", "test_red.py", 1, 1,
                                         "Traceback (most recent call last):\nAssertionError: fixture failure")
        signals: list[tuple[int, int]] = []
        def send(pid: int, signum: int) -> None:
            signals.append((pid, signum))
        with mock.patch.object(time, "monotonic", return_value=11), \
                mock.patch.object(process_group, "send_process_group", side_effect=send):
            self.assertEqual(gate.stop_at_time_limit([job], [failed], 10, 12), "")
        self.assertEqual(signals, [])
        with mock.patch.object(time, "monotonic", return_value=13), \
                mock.patch.object(process_group, "send_process_group", side_effect=send), \
                mock.patch.object(os, "wait4", return_value=(process.pid, signal.SIGTERM, resource.getrusage(resource.RUSAGE_SELF))), \
                contextlib.redirect_stdout(io.StringIO()):
            report = gate.stop_at_time_limit([job], [failed], 10, 12)
        self.assertEqual(signals, [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)])
        self.assertIn("limit 2 s; elapsed 3.000 s; running modules: hooks/test_busy.py", report)
        self.assertIn("message/test_red.py", report)
        self.assertIn("Traceback (most recent call last):\nAssertionError: fixture failure", report)
        self.assertIn("partial module output", report)
        self.assertEqual(job.killed_for, report.split("\n", 1)[0])
        run = gate.GateRun((failed,), 3, 0, time_limit_report=report)
        self.assertFalse(run.passed)
        from scripts.message import session_send
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture", "")) as tell:
            gate.tell_time_limit("fixture", self.checkout, run)
        self.assertEqual(tell.call_args.args, (session_send.send_argv(
            "--to", "fixture", "--from", "gate", "--key", f"gate-time-limit:{self.checkout}", "--text", report),))

    @gate_suites("hooks", "message")
    def test_targeted_selection_precedes_the_assigned_aws_route(self) -> None:
        from scripts.aws_target import cloud_gate

        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "gate.conf", "scan_roots=\n")
        subject, base = self.prepare_affected_subject()
        with mock.patch.object(gate, "current_gate_placement", return_value=placed("natedev")):
            first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        rows = (
            ("unchanged", False, 0),
            ("document", False, 0),
            ("subject", False, 0),
            ("subject", True, 1),
            ("committed-subject", False, 1),
            ("fallback-edit", True, 1),
            ("fallback-commit", True, 1),
            ("fallback-full", True, 1),
        )
        aws = placed("claude-aws")
        for change, decline, status in rows:
            with self.subTest(change=change, decline=decline):
                _ = self.git(self.checkout, "reset", "--hard", base)
                (self.checkout / "scripts/hooks/test_second.py").unlink(missing_ok=True)
                if change in ("subject", "committed-subject") or change.startswith("fallback-"):
                    self.write(subject, "VALUE = 2\n")
                    if change == "committed-subject":
                        _ = self.commit(self.checkout, "Change the subject")
                elif change == "document":
                    self.write(self.checkout / "subject.txt", "unrelated document\n")
                self.clear_gate_events()
                if change == "fallback-full":
                    (Path(self.environment["GATE_PASSED_DIR"]) / "passed-trees.jsonl").unlink()
                    for ref in self.git(self.checkout, "for-each-ref", "--format=%(refname)", "refs/remotes/origin").splitlines():
                        _ = self.git(self.checkout, "update-ref", "-d", ref)
                expected_tree = cloud_gate.working_tree(self.checkout)[0]
                routed: list[gate.AllModules | gate.TreeSelection] = []

                def route(repo: Path, *, mode: gate.SuiteMode, full: bool,
                          module_time_limit_s: float, selected: gate.AllModules | gate.TreeSelection,
                          gate_job: StepJob | GateJobNotRecorded,
                          assignment: placement.PlacementAssignment) -> gate.GateRun | str:
                    self.assertEqual(assignment, aws.assignment)
                    self.assertEqual(gate_job, StepJob("test-affected"))
                    self.assertEqual(repo, self.checkout)
                    self.assertEqual(mode, gate.SuiteMode.TARGETED)
                    self.assertFalse(full)
                    self.assertEqual(module_time_limit_s, gate.DEFAULT_MODULE_TIME_LIMIT_S)
                    routed.append(selected)
                    if change.startswith("fallback-"):
                        added = self.checkout / "scripts/hooks/test_second.py"
                        self.write(added, SUITE_MODULE)
                        self.write(added, added.read_text() + '\nclass NewFailure(unittest.TestCase):\n'
                                   + '    def test_new_failure(self):\n        self.fail("new failing module")\n')
                        if change in ("fallback-commit", "fallback-full"):
                            _ = self.commit(self.checkout, "Add a failing test during the route")
                    if decline:
                        return "fixture cloud unavailable"
                    return gate.GateRun((gate.GateJobResult(
                        "hooks", "test_first.py", int(change == "committed-subject"), 1, ""),), 1, 0)

                arguments = ["--repo", str(self.checkout)]
                with (mock.patch.dict(os.environ, {**self.environment, "BUILDLOG_PREPARED_COPY": str(self.checkout)}),
                      mock.patch.object(copies, "take_prepared_gate_handoff", return_value=True),
                      mock.patch.object(gate, "current_gate_placement", return_value=aws),
                      mock.patch.object(cloud_gate, "route", side_effect=route),
                      mock.patch.object(gate, "select_affected", wraps=affected_modules.select_affected) as select_affected,
                      mock.patch.object(gate, "run_suites") as local,
                      contextlib.redirect_stdout(io.StringIO()) as output,
                      contextlib.redirect_stderr(io.StringIO()) as errors):
                    result = gate.main(["--affected", *arguments])
                self.assertEqual(result, status, output.getvalue() + errors.getvalue())
                select_affected.assert_called_once()
                local.assert_not_called()
                if change in ("subject", "committed-subject") or change.startswith("fallback-"):
                    selected, = routed
                    self.assertIsInstance(selected, gate.TreeSelection)
                    assert isinstance(selected, gate.TreeSelection)
                    self.assertEqual(selected.tree, expected_tree)
                    if change == "fallback-full":
                        self.assertCountEqual(selected.paths, ("scripts/hooks/test_first.py", "scripts/message/test_first.py"))
                        self.assertEqual(output.getvalue().splitlines().count("full suite: no certified base"), 1)
                    else:
                        self.assertEqual(selected.paths, ("scripts/hooks/test_first.py",))
                        self.assertRegex(output.getvalue(), r"affected: 1 of 2 modules[^\n]+\(cache \d+ hits, \d+ misses;")
                else:
                    self.assertEqual(routed, [])
                    self.assertRegex(output.getvalue(), r"affected: 0 of 2 modules[^\n]+\(cache \d+ hits, \d+ misses;")
                    self.assertIn("(none)", output.getvalue())
                self.assertEqual(self.module_events(), [])
                if decline:
                    self.assertIn("AWS gate failed: fixture cloud unavailable", errors.getvalue())
                    self.assertFalse(any(record["tree"] == cloud_gate.working_tree(self.checkout)[0]
                                         for record in self.pass_records()))
                self.assertFalse(any(call["tool"] == "basedpyright" for call in self.calls()))
                if change == "committed-subject":
                    tree = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
                    self.assertFalse(any(record["tree"] == tree and record["kind"] == "full"
                                         and record["suites"] == list(self.suites) for record in self.pass_records()))

    @gate_suites("hooks")
    def test_cloud_gate_checks_the_checkout_after_each_return_path(self) -> None:
        from scripts.aws_target import cloud_gate

        local_result = gate.GateRun((), 1, 0, selection_certified=True)
        subject = self.checkout / "subject.txt"

        def run_changed_checkout(*_args: object, **_kwargs: object) -> gate.GateRun:
            self.write(subject, "changed during the local run\n")
            return local_result

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route") as route,
              mock.patch.object(gate, "run_suites", return_value=local_result) as run_local,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertIs(gate.run_gate_suites(self.checkout, target="natedev"), local_result)
            run_local.side_effect = run_changed_checkout
            with self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$"):
                _ = gate.run_gate_suites(self.checkout, target="natedev")
            route.assert_not_called()

        _ = self.git(self.checkout, "restore", "subject.txt")
        remote = gate.GateRun((gate.GateJobResult("hooks", "test_first.py", 0, 1, ""),), 1, 0)
        aws = gate.placement_target(placed("claude-aws"))
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value=remote) as route,
              mock.patch.object(gate, "run_suites", side_effect=run_changed_checkout) as run_types):
            with self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$"):
                _ = gate.run_gate_suites(self.checkout, ("scripts",), target=aws)
            run_types.assert_called_once_with(
                self.checkout, ("scripts",), gate.DEFAULT_MODULE_TIME_LIMIT_S,
                source="tests-only", full=False, mode=gate.SuiteMode.GATE, selected=gate.SelectedModules(()))
            route.assert_called_once()

        _ = self.git(self.checkout, "restore", "subject.txt")
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value=remote) as route):
            self.assertIs(gate.run_gate_suites(self.checkout, target=aws), remote)
            route.side_effect = run_changed_checkout
            with self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$"):
                _ = gate.run_gate_suites(self.checkout, target=aws)

        _ = self.git(self.checkout, "restore", "subject.txt")
        working_tree = cloud_gate.working_tree(self.checkout)
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value=remote),
              mock.patch.object(cloud_gate, "working_tree", side_effect=[
                  working_tree, affected_modules.GraphError("fixture final tree error")])):
            with self.assertRaisesRegex(GateRefused, "^fixture final tree error$"):
                _ = gate.run_gate_suites(self.checkout, target=aws)

    @gate_suites("hooks")
    def test_targeted_returns_refuse_a_changed_checkout(self) -> None:
        from scripts.aws_target import cloud_gate

        subject = self.checkout / "subject.txt"
        selection = affected_modules.AffectedSelection(
            affected_modules.CertifiedBase(self.git(self.checkout, "rev-parse", "HEAD"), "2026-01-15T00:00:00+00:00"),
            (), ())
        aws = gate.placement_target(placed("claude-aws"))
        for reuse, types in ((True, ()), (False, ()), (False, ("scripts",))):
            for changed in (False, True):
                with (self.subTest(reuse=reuse, types=types, changed=changed),
                      mock.patch.dict(os.environ, self.environment),
                      mock.patch.object(cloud_gate, "route") as route,
                      contextlib.redirect_stdout(io.StringIO())):
                    _ = self.git(self.checkout, "restore", "subject.txt")

                    def select(*_args: object, **_kwargs: object) -> affected_modules.AffectedSelection:
                        if changed and not (reuse or types):
                            self.write(subject, "changed during empty selection\n")
                        return selection

                    def local(*_args: object, **_kwargs: object) -> gate.GateRun:
                        if changed:
                            self.write(subject, "changed during local checks\n")
                        jobs = (gate.GateJobResult("types", "basedpyright", 0, 1, "",
                                                   kind=gate.GateJobKind.TYPE_CHECK),) if types else ()
                        return gate.GateRun(jobs, 1, 0, tests_reused=reuse)

                    def reuse_pass(*_args: object, **_kwargs: object) -> bool:
                        if changed and reuse:
                            self.write(subject, "changed during pass reuse\n")
                        return reuse

                    before = self.pass_records()
                    with (mock.patch.object(gate, "select_affected", side_effect=select),
                          mock.patch.object(gate.PassedTreeLedger, "reuse", side_effect=reuse_pass),
                          mock.patch.object(gate, "run_suites", side_effect=local) as run_local):
                        if changed:
                            with self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$"):
                                _ = gate.run_gate_suites(self.checkout, types, mode=gate.SuiteMode.TARGETED,
                                                         target=aws)
                            self.assertEqual(self.pass_records(), before)
                        else:
                            self.assertTrue(gate.run_gate_suites(self.checkout, types, mode=gate.SuiteMode.TARGETED,
                                                                 target=aws).passed)
                    route.assert_not_called()
                    self.assertEqual(run_local.call_count, int(bool(types)))

    @gate_suites("hooks")
    def test_refused_empty_selection_does_not_record_a_pass(self) -> None:
        from scripts.aws_target import cloud_gate

        head = self.git(self.checkout, "rev-parse", "HEAD")
        selection = affected_modules.AffectedSelection(
            affected_modules.CertifiedBase(head, "2026-01-15T00:00:00+00:00"), (), ())
        aws = gate.placement_target(placed("claude-aws"))

        def change_during_selection(*_args: object, **_kwargs: object) -> affected_modules.AffectedSelection:
            self.write(self.checkout / "subject.txt", "changed during empty selection\n")
            return selection

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route") as route,
              mock.patch.object(gate, "select_affected", return_value=selection) as select,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertTrue(gate.run_gate_suites(self.checkout, mode=gate.SuiteMode.TARGETED,
                                                 target=aws).passed)
            accepted, = self.gate_run_records()
            self.assertEqual((accepted["selection"], accepted["status"]), ("affected", 0))
            before = self.pass_records()

            select.side_effect = change_during_selection
            with self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$"):
                _ = gate.run_gate_suites(self.checkout, mode=gate.SuiteMode.TARGETED, target=aws)
            route.assert_not_called()

        self.assertEqual(self.gate_run_records(), [accepted])
        self.assertEqual(self.pass_records(), before)

    @gate_suites("hooks")
    def test_empty_selection_history_keeps_the_validated_commit_and_branch(self) -> None:
        from scripts.aws_target import cloud_gate

        head = self.git(self.checkout, "rev-parse", "HEAD")
        tree = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
        branch = self.git(self.checkout, "branch", "--show-current")
        self.write(self.checkout / "subject.txt", "contents of the second commit\n")
        second = self.commit(self.checkout, "Change the subject")
        _ = self.git(self.checkout, "reset", "--hard", head)
        _ = self.git(self.checkout, "branch", "after-validation", second)
        selection = affected_modules.AffectedSelection(
            affected_modules.CertifiedBase(head, "2026-01-15T00:00:00+00:00"), (), ())
        append_pass = gate.PassedTreeLedger.append_pass

        def move_head_after_validation(ledger: gate.PassedTreeLedger, passed: gate.ValidatedTestPass) -> None:
            append_pass(ledger, passed)
            _ = self.git(self.checkout, "checkout", "after-validation")

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route") as route,
              mock.patch.object(gate, "select_affected", return_value=selection),
              mock.patch.object(gate.PassedTreeLedger, "append_pass", autospec=True,
                                side_effect=move_head_after_validation),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertTrue(gate.run_gate_suites(self.checkout, mode=gate.SuiteMode.TARGETED,
                                                 target=gate.NO_GATE_ASSIGNMENT).passed)
            route.assert_not_called()

        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), second)
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "after-validation")
        record, = self.gate_run_records()
        self.assertEqual((record["sha"], record["tree"], record["branch"], record["status"]), (head, tree, branch, 0))

    @gate_suites("hooks")
    def test_cloud_fallback_records_no_pass_when_the_checkout_changes_during_the_hold(self) -> None:
        from scripts.aws_target import cloud_gate

        added = self.checkout / "scripts/hooks/test_added.py"

        def change_tree_during_hold() -> None:
            if not added.exists():
                self.write(added, SUITE_MODULE)
                _ = self.commit(self.checkout, "Add a module during the build hold")

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value="fixture cloud unavailable"),
              mock.patch.object(gate.BuildHoldWait, "wait_until_free", side_effect=change_tree_during_hold),
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$"):
                _ = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)

        self.assertEqual(self.pass_records(), [])
        starts = [row["module"] for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, ["test_first.py", "test_added.py"])

    @gate_suites("hooks")
    def test_cloud_fallback_records_no_pass_when_the_module_list_changes(self) -> None:
        from scripts.aws_target import cloud_gate

        added = self.checkout / "scripts/hooks/test_ignored.py"
        with (self.checkout / ".git/info/exclude").open("a") as excludes:
            _ = excludes.write(f"\n/{added.relative_to(self.checkout)}\n")
        finish = gate.finish_gate_job

        def add_module_after_job(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
            result = finish(job, status)
            self.write(added, SUITE_MODULE)
            return result

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value="fixture cloud unavailable"),
              mock.patch.object(gate, "finish_gate_job", side_effect=add_module_after_job),
              contextlib.redirect_stdout(io.StringIO())):
            original_tree = cloud_gate.working_tree(self.checkout)
            run = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)
            self.assertEqual(cloud_gate.working_tree(self.checkout), original_tree)

        self.assertTrue(added.is_file())
        self.assertTrue(run.passed)
        self.assertEqual(self.pass_records(), [])

    @gate_suites("hooks")
    def test_cloud_fallback_publishes_the_pass_only_after_final_validation(self) -> None:
        from scripts.aws_target import cloud_gate

        marker, notice = self.write_nightly_red_marker()
        subject = self.checkout / "subject.txt"
        working_tree = cloud_gate.working_tree
        calls = 0

        def change_before_final_check(repo: Path) -> tuple[str, set[str]]:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.write(subject, "changed before the final tree check\n")
            return working_tree(repo)

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value="fixture cloud unavailable"),
              contextlib.redirect_stdout(io.StringIO())):
            with (mock.patch.object(cloud_gate, "working_tree", side_effect=change_before_final_check),
                  self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$")):
                _ = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)
            self.assertEqual(self.pass_records(), [])
            self.assertEqual(marker.read_text(), notice)
            _ = self.git(self.checkout, "restore", "subject.txt")

            unavailable = self.state / "unavailable-pass-ledger"
            self.write(unavailable, "The ledger directory is a file.\n")
            with mock.patch.dict(os.environ, {"GATE_PASSED_DIR": str(unavailable)}):
                self.assertTrue(gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT).passed)
            self.assertEqual(self.pass_records(), [])
            self.assertEqual(marker.read_text(), notice)
            self.assertTrue(gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT).passed)
        record, = self.pass_records()
        self.assertEqual(record["tree"], working_tree(self.checkout)[0])
        self.assertFalse(marker.exists())

    @gate_suites("hooks")
    def test_cloud_fallback_checks_the_tree_after_coverage_validation(self) -> None:
        from scripts.aws_target import cloud_gate

        marker, notice = self.write_nightly_red_marker()
        modules = discover_test_modules(self.checkout)

        def change_during_coverage() -> list[tuple[str, Path]]:
            self.write(self.checkout / "subject.txt", "changed during coverage validation\n")
            _ = self.commit(self.checkout, "Change the tree during coverage validation")
            return modules

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value="fixture cloud unavailable"),
              mock.patch.object(gate, "discover_test_modules") as discover,
              mock.patch.object(cloud_gate, "working_tree", wraps=cloud_gate.working_tree) as read_tree,
              contextlib.redirect_stdout(io.StringIO())):
            def discover_then_change(_repo: Path) -> list[tuple[str, Path]]:
                if discover.call_count == 1:
                    return modules
                self.assertEqual(read_tree.call_count, 1)
                return change_during_coverage()

            discover.side_effect = discover_then_change
            with self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$"):
                _ = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)
            self.assertEqual(read_tree.call_count, 2)
            self.assertEqual(discover.call_count, 2)

        self.assertEqual(self.pass_records(), [])
        self.assertEqual(marker.read_text(), notice)

    @gate_suites("hooks")
    def test_cloud_fallback_refuses_a_mixed_reset_after_clean_status(self) -> None:
        from scripts.aws_target import cloud_gate

        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.write(self.checkout / "subject.txt", "contents of the second commit\n")
        second = self.commit(self.checkout, "Change the subject")
        _ = self.git(self.checkout, "checkout", "--detach", head)
        working_tree = cloud_gate.working_tree
        checked_git = gate.checked_git
        final_read = False

        def checkout_second_before_final_read(repo: Path) -> tuple[str, set[str]]:
            nonlocal final_read
            if read_tree.call_count == 2:
                _ = self.git(repo, "checkout", "--detach", second)
                final_read = True
            return working_tree(repo)

        def reset_before_head_tree(repo: Path, *args: str) -> str:
            nonlocal final_read
            if final_read and args[0] == "rev-parse" and "HEAD^{tree}" in args:
                self.assertEqual(self.git(repo, "status", "--porcelain"), "")
                _ = self.git(repo, "reset", "--mixed", head)
                final_read = False
            return checked_git(repo, *args)

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value="fixture cloud unavailable"),
              mock.patch.object(cloud_gate, "working_tree", side_effect=checkout_second_before_final_read) as read_tree,
              mock.patch.object(gate, "checked_git", side_effect=reset_before_head_tree),
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$"):
                _ = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)

        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "M subject.txt")
        self.assertEqual(self.pass_records(), [])

    @gate_suites("hooks")
    def test_cloud_fallback_publishes_no_pass_when_the_index_changes_during_the_final_read(self) -> None:
        from scripts.aws_target import cloud_gate

        added = "ignored.txt"
        self.write(self.checkout / ".git/info/exclude", f"/{added}\n")
        self.write(self.checkout / added, "ignored until staged\n")
        private_copy = git_index.private_copy

        def stage_and_restore_after_copy(source: Path, destination: Path) -> None:
            if copy.call_count != 2:
                private_copy(source, destination)
                return
            self.write(self.checkout / "subject.txt", "edited before the index copy\n")
            private_copy(source, destination)
            _ = self.git(self.checkout, "add", "-f", added)
            _ = self.git(self.checkout, "restore", "subject.txt")

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value="fixture cloud unavailable"),
              mock.patch.object(git_index, "private_copy", side_effect=stage_and_restore_after_copy) as copy,
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(GateRefused, "^The Git index changed during the checkout read$"):
                _ = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)
            self.assertEqual(copy.call_count, 2)

        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), f"A  {added}")
        self.assertEqual(self.pass_records(), [])

    @gate_suites("hooks")
    def test_cloud_fallback_refuses_an_unreadable_tree_at_pass_publication(self) -> None:
        from scripts.aws_target import cloud_gate

        modules = discover_test_modules(self.checkout)
        working_tree = cloud_gate.working_tree
        for ledger_unavailable in (False, True):
            with (self.subTest(ledger_unavailable=ledger_unavailable),
                  mock.patch.dict(os.environ, self.environment),
                  mock.patch.object(cloud_gate, "route", return_value="fixture cloud unavailable"),
                  mock.patch.object(gate, "discover_test_modules", return_value=modules) as discover,
                  mock.patch.object(cloud_gate, "working_tree") as read_tree,
                  contextlib.redirect_stdout(io.StringIO()) as output):
                def read_after_coverage(repo: Path) -> tuple[str, set[str]]:
                    if discover.call_count == 2:
                        raise affected_modules.GraphError("fixture publication tree read failed")
                    return working_tree(repo)

                read_tree.side_effect = read_after_coverage
                if ledger_unavailable:
                    unavailable = self.state / "unreadable-pass-ledger"
                    self.write(unavailable, "The ledger directory is a file.\n")
                    os.environ["GATE_PASSED_DIR"] = str(unavailable)
                with self.assertRaisesRegex(GateRefused, "^fixture publication tree read failed$"):
                    _ = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)
                self.assertEqual(read_tree.call_count, 2)
                self.assertEqual("Test pass ledger unavailable" in output.getvalue(), ledger_unavailable)
        self.assertEqual(self.pass_records(), [])

    @gate_suites("hooks")
    def test_cloud_fallback_keeps_the_marker_when_the_pass_append_fails(self) -> None:
        from scripts.aws_target import cloud_gate

        marker, notice = self.write_nightly_red_marker()
        path = Path(self.environment["GATE_PASSED_DIR"]) / "passed-trees.jsonl"
        path.mkdir(parents=True)
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value="fixture cloud unavailable"),
              mock.patch.object(gate, "discover_test_modules", wraps=discover_test_modules) as discover,
              mock.patch.object(cloud_gate, "working_tree", wraps=cloud_gate.working_tree) as read_tree,
              contextlib.redirect_stdout(io.StringIO()) as output):
            events = mock.Mock()
            events.attach_mock(discover, "discover")
            events.attach_mock(read_tree, "read_tree")
            self.assertTrue(gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT).passed)
            self.assertIn(f"Is a directory: '{path}'", output.getvalue())
            self.assertEqual(marker.read_text(), notice)
            self.assertEqual(list(path.iterdir()), [])
            self.assertEqual([call[0] for call in events.mock_calls],
                             ["read_tree", "discover", "discover", "read_tree"])

            modules = discover_test_modules(self.checkout)
            with (mock.patch.object(gate, "discover_test_modules", side_effect=[
                      modules, OSError("fixture coverage read failed")]),
                  self.assertRaisesRegex(GateRefused, "^fixture coverage read failed$")):
                _ = gate.run_gate_suites(self.checkout, target=gate.NO_GATE_ASSIGNMENT)

            self.assertEqual(marker.read_text(), notice)
            self.assertEqual(list(path.iterdir()), [])

    @gate_suites("hooks")
    def test_cloud_fallback_accepts_matching_clean_dirty_and_index_only_sources(self) -> None:
        from scripts.aws_target import cloud_gate

        subject = self.checkout / "subject.txt"
        finish = gate.finish_gate_job

        def stage_other_contents(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
            completed = finish(job, status)
            blob = self.git(self.checkout, "rev-parse", "HEAD:scripts/hooks/test_first.py")
            _ = self.git(self.checkout, "update-index", "--cacheinfo", f"100644,{blob},subject.txt")
            return completed

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", return_value="fixture cloud unavailable"),
              contextlib.redirect_stdout(io.StringIO())):
            clean_tree, dirty = cloud_gate.working_tree(self.checkout)
            self.assertEqual(dirty, set())
            self.assertTrue(gate.run_gate_suites(self.checkout, full=True, target=gate.NO_GATE_ASSIGNMENT).passed)
            original, = self.pass_records()
            self.assertEqual(original["tree"], clean_tree)

            self.write(subject, "working contents stay dirty throughout the gate\n")
            dirty_tree, dirty = cloud_gate.working_tree(self.checkout)
            self.assertNotEqual(dirty_tree, clean_tree)
            self.assertEqual(dirty, {"subject.txt"})
            self.assertTrue(gate.run_gate_suites(self.checkout, full=True, target=gate.NO_GATE_ASSIGNMENT).passed)
            self.assertEqual(self.pass_records(), [original])
            _ = self.git(self.checkout, "restore", "subject.txt")

            marker, notice = self.write_nightly_red_marker()
            with mock.patch.object(gate, "finish_gate_job", side_effect=stage_other_contents):
                self.assertTrue(gate.run_gate_suites(self.checkout, full=True, target=gate.NO_GATE_ASSIGNMENT).passed)
            self.assertEqual(cloud_gate.working_tree(self.checkout), (clean_tree, {"subject.txt"}))
            self.assertEqual(self.git(self.checkout, "diff", "--cached", "--name-only"), "subject.txt")
            self.assertTrue(gate.run_gate_suites(self.checkout, full=True, target=gate.NO_GATE_ASSIGNMENT).passed)
            self.assertEqual(marker.read_text(), notice)
            self.assertEqual(self.pass_records(), [original])

    @gate_suites("hooks")
    def test_assigned_aws_gate_accepts_a_staged_deletion_of_an_ignored_tracked_file(self) -> None:
        from scripts.aws_target import cloud_gate

        subject = self.checkout / "subject.txt"
        added = self.checkout / "ignored.txt"
        with (self.checkout / ".git/info/exclude").open("a") as excludes:
            _ = excludes.write("\n/subject.txt\n/ignored.txt\n")
        expected = subject.read_bytes()
        marker, notice = self.write_nightly_red_marker()
        aws = gate.placement_target(placed("claude-aws"))
        remote = gate.GateRun((gate.GateJobResult("hooks", "test_first.py", 0, 1, ""),), 1, 0)

        def stage_deletion(*_args: object, **_kwargs: object) -> gate.GateRun:
            _ = self.git(self.checkout, "rm", "--cached", "subject.txt")
            return remote

        def commit_ignored_file(*_args: object, **_kwargs: object) -> gate.GateRun:
            self.write(added, "new tracked file\n")
            _ = self.git(self.checkout, "add", "-f", "ignored.txt")
            _ = self.git(self.checkout, "commit", "-qm", "Track the ignored file", "--only", "--", "ignored.txt")
            return remote

        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(cloud_gate, "route", side_effect=stage_deletion) as route,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertIs(gate.run_gate_suites(self.checkout, target=aws), remote)
            self.assertEqual(cloud_gate.working_tree(self.checkout),
                             (self.git(self.checkout, "rev-parse", "HEAD^{tree}"), {"subject.txt"}))
            route.side_effect = commit_ignored_file
            with self.assertRaisesRegex(GateRefused, "^The checkout changed during the gate$"):
                _ = gate.run_gate_suites(self.checkout, target=aws)
            self.assertEqual(route.call_count, 2)
        self.assertEqual(subject.read_bytes(), expected)
        self.assertEqual(self.git(self.checkout, "diff", "--cached", "--name-status"), "D\tsubject.txt")
        self.assertEqual(marker.read_text(), notice)
        self.assertEqual(self.pass_records(), [])

    @gate_suites("hooks", "direct")
    def test_whole_machine_orders_modules_from_supplied_times_without_writing_them(self) -> None:
        path = self.state / "module_seconds.json"
        content = json.dumps({"hooks/test_first.py": 100.0, "direct/test_first.py": 1.0})
        self.write(path, content)
        machine = gate.WholeMachine(gate.read_measures(path))
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(os, "cpu_count", return_value=1),
              mock.patch.object(gate, "save_measures", side_effect=AssertionError("measure write")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            run = gate.run_suites(self.checkout, machine=machine)
        self.assertTrue(run.passed, output.getvalue())
        starts = [event["suite"] for event in self.module_events() if event["event"] == "start"]
        self.assertEqual(starts, ["hooks", "direct"])
        self.assertEqual(path.read_text(), content)
        self.assertEqual(machine.module_seconds, {"hooks/test_first.py": 100.0, "direct/test_first.py": 1.0})

    @gate_suites("hooks", "direct")
    def test_whole_machine_without_times_keeps_the_discovery_order(self) -> None:
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(os, "cpu_count", return_value=1),
              contextlib.redirect_stdout(io.StringIO()) as output):
            run = gate.run_suites(self.checkout, machine=gate.WholeMachine())
        self.assertTrue(run.passed, output.getvalue())
        starts = [event["suite"] for event in self.module_events() if event["event"] == "start"]
        self.assertEqual(starts, ["direct", "hooks"])

    @gate_suites("lib")
    def test_whole_machine_uses_all_cpus_without_shared_admission_or_holds(self) -> None:
        self.write(self.state / "barrier", "two")
        output = io.StringIO()
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(os, "cpu_count", return_value=2),
              mock.patch.object(os, "getloadavg", side_effect=[(1.25, 4.0, 5.0), (2.5, 6.0, 7.0)]),
              mock.patch.object(gate, "typecheck_python", return_value=Path(sys.executable)),
              contextlib.ExitStack() as blocked, contextlib.redirect_stdout(output)):
            for name in ("BuildHoldWait", "gate_reservation", "queue_place", "shared_cpu_pool",
                         "suite_scope_prefix", "merge_gate_governor_gib", "SampledRSSAdmission",
                         "PassedTreeLedger", "clean_test_tree", "process_memory_mib"):
                _ = blocked.enter_context(mock.patch.object(gate, name, side_effect=AssertionError(name)))
            run = gate.run_suites(self.checkout, type_dirs=("scripts",), max_running_jobs=1,
                                       machine=gate.WholeMachine())
        self.assertTrue(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), 3)
        self.assertIn("at most 2 modules at once", output.getvalue())
        self.assertIn("One-minute load: start=1.25 max=2.50 end=2.50", output.getvalue())
        active: set[int] = set()
        peak = 0
        for event in self.module_events():
            if event["event"] == "start":
                active.add(event["pid"])
            else:
                active.remove(event["pid"])
            peak = max(peak, len(active))
        self.assertEqual(peak, 2)
        self.assertEqual(active, set())
        self.assertFalse(any(call["tool"] in ("systemd-run", "ssh") for call in self.calls()))
        for job in run.jobs:
            self.assertIn(f"{job.suite}/{job.module} exit=0 seconds=", output.getvalue())

    @gate_suites("hooks")
    def test_whole_machine_does_not_rerun_a_failed_module(self) -> None:
        self.write(self.state / "fail-first-hooks", "once")
        with mock.patch.dict(os.environ, self.environment), contextlib.redirect_stdout(io.StringIO()) as output:
            run = gate.run_suites(self.checkout, machine=gate.WholeMachine())
        self.assertFalse(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), 1)
        self.assertEqual(run.jobs[0].returncode, 1)
        self.assertEqual(len([event for event in self.module_events() if event["event"] == "start"]), 1)
        self.assertIn("hooks/test_first.py exit=1 seconds=", output.getvalue())
        self.assertNotIn("Rerun alone", output.getvalue())

    @gate_suites("lib", "hooks")
    def test_named_modules_use_the_build_hold_and_repository_run_limit(self) -> None:
        checkout = self.root / "named-modules"
        _ = self.git(self.checkout, "worktree", "add", "--detach", str(checkout))
        self.write(checkout / "scripts/hooks/test_first.py", SUITE_MODULE
                   + "\n\nclass Red(unittest.TestCase):\n    def test_red(self):\n        self.fail('red')\n")
        repository = "claude_commands"
        machine = "nixos"
        _ = self.git(checkout, "remote", "set-url", "origin", str(self.root / f"{repository}.git"))
        self.configure_gate(f"# whole: 1..\nmax_runs.{machine}.{repository}=2\nmax_runs.exempt_unit=natemerge\n")
        holder = Path(self.environment["BUILD_HOLD_DIR"]) / "fixture-holder"
        self.write(holder, "Fixture build hold\n")

        occupied: list[wire.Admitted] = []
        events: list[str | admission_records.RunLimitWait] = []
        grants: list[tuple[str, str, int]] = []
        previous = self.admission.server.reply_filter

        def observe(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Admitted):
                reservations = self.admission.server.reservations.values()
                record = next(row for row in reservations if row.reservation_id == reply.reservation)
                grants.append((record.step, record.queue_waiter.get("repository_name", ""), len(reservations)))
                events.append("admitted")
            elif isinstance(reply, wire.Wait):
                limit = reply.snapshot.decision.run_limit
                if isinstance(limit, admission_records.RunLimitWait) and limit not in events:
                    events.append(limit)
                    self.admission.release(occupied.pop(0))
            return previous(reply)

        def release_fixture_hold(holders: Path, release: Path) -> LocalReleaseEvents:
            self.assertEqual(os.environ["BUILDLOG_UNIT"], "fixture-unit")
            self.assertEqual(self.module_events(), [])
            self.assertEqual(len(self.admission.caller_reservations()), 2)
            events.append("hold")
            holder.unlink()
            return LocalReleaseEvents(holders, release)

        output = io.StringIO()
        with (mock.patch.dict(os.environ, self.environment),
              mock.patch.object(self.admission.server, "host", machine),
              mock.patch.object(gate, "checked_gate_target", return_value="natedev"),
              mock.patch.object(self.admission.server, "reply_filter", observe),
              mock.patch.object(gate, "local_release_waiter", side_effect=release_fixture_hold),
              mock.patch("scripts.aws_target.cloud_gate.route", side_effect=AssertionError("unexpected cloud route")),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(output)):
            try:
                for _slot in range(2):
                    self.admission.allow_next_start()
                    request = self.admission.request(need=admission_records.GIB, step="gate")
                    assert isinstance(request.profile, admission_records.StepProfile)
                    request = replace(request, profile=replace(request.profile, repo=repository))
                    with self.admission.loop_turn():
                        occupied.append(only_admitted(self.admission.server.handle_request(request)))
                self.admission.allow_next_start()
                os.environ["BUILDLOG_UNIT"] = "natemerge"
                with (mock.patch.dict(os.environ, {"BUILDLOG_PREPARED_COPY": str(checkout)}),
                      mock.patch.object(copies, "take_prepared_gate_handoff", return_value=True)):
                    code = gate.main(["--affected", "--repo", str(checkout),
                                      "--module", "lib/test_second.py"])
                self.assertEqual(code, 0, output.getvalue())
                self.assertTrue(holder.exists())
                self.assertEqual(events, ["admitted"])
                self.assertEqual(grants, [("test-affected", repository, 3)])
                self.clear_gate_events()

                self.admission.allow_next_start()
                os.environ["BUILDLOG_UNIT"] = "fixture-unit"
                code = gate.main(["--tests-only", "--repo", str(checkout),
                                  "--module", "hooks/test_first.py", "--module", "lib/test_second.py"])
            finally:
                for grant in occupied:
                    self.admission.release(grant)
        self.assertEqual(code, 1, output.getvalue())
        self.assertEqual(events, ["admitted", "hold", admission_records.RunLimitWait(2, 2, repository, machine),
                                  "admitted"])
        self.assertEqual(grants, [("test-affected", repository, 3), ("gate", repository, 2)])
        self.assert_gate_released()
        # A named run writes no gate record.
        self.assertEqual(self.gate_run_records(), [])
        self.assertFalse(Path(self.environment["GATE_PASSED_DIR"], "passed-trees.jsonl").exists())
        starts = sorted((event["suite"], event["module"]) for event in self.module_events() if event["event"] == "start")
        # A red module runs once more alone, as in the gate, so a flake is not a red name.
        self.assertEqual(starts, [("hooks", "test_first.py"), ("hooks", "test_first.py"), ("lib", "test_second.py")])
        self.assertEqual(checkpoint_gate.red_names(["gate.py"], output.getvalue()), frozenset({"hooks/test_first.py"}))

    @gate_suites("lib", "hooks")
    def test_named_modules_report_red_without_a_ledger_or_gate_record(self) -> None:
        self.write(self.checkout / "scripts/hooks/test_first.py", SUITE_MODULE
                   + "\n\nclass Red(unittest.TestCase):\n    def test_red(self):\n        self.fail('red')\n")
        output = io.StringIO()
        with (mock.patch.dict(os.environ, self.environment),
              contextlib.ExitStack() as blocked, contextlib.redirect_stdout(output)):
            _ = blocked.enter_context(mock.patch("scripts.aws_target.cloud_gate.route",
                                                 side_effect=AssertionError("unexpected cloud route")))
            for name in ("PassedTreeLedger", "record_gate_run"):
                _ = blocked.enter_context(mock.patch.object(gate, name, side_effect=AssertionError(name)))
            code = gate.main(["--tests-only", "--repo", str(self.checkout),
                              "--module-time-limit-s", "0.000001",
                              "--module", "hooks/test_first.py", "--module", "lib/test_second.py"])
        self.assertEqual(code, 1, output.getvalue())
        starts = sorted((event["suite"], event["module"]) for event in self.module_events() if event["event"] == "start")
        # A red module runs once more alone, as in the gate, so a flake is not a red name.
        self.assertEqual(starts, [("hooks", "test_first.py"), ("hooks", "test_first.py"), ("lib", "test_second.py")])
        self.assertEqual(checkpoint_gate.red_names(["gate.py"], output.getvalue()), frozenset({"hooks/test_first.py"}))

    @gate_suites("lib")
    def test_named_modules_share_one_reservation_and_one_available_cpu_slot(self) -> None:
        self.supply_jobserver(b"A")
        start, finish = gate.start_gate_job, gate.finish_gate_job
        active: set[int] = set()
        reservations: list[str] = []

        def start_module(suite: str, path: Path, argv: list[str], repo: Path,
                         environment: dict[str, str], log: Path) -> gate.RunningGateJob:
            self.assertEqual(active, set())
            self.assertEqual(self.jobserver_tokens(), b"")
            reservation, = self.admission.server.reservations.values()
            self.assertEqual(reservation.step, "test-affected")
            self.assertEqual(reservation.queue_waiter.get("unit"), "natemerge")
            reservations.append(reservation.reservation_id)
            job = start(suite, path, argv, repo, environment, log)
            active.add(job.process.pid)
            return job

        def finish_module(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
            active.remove(job.process.pid)
            return finish(job, status)

        output = io.StringIO()
        with (mock.patch.dict(os.environ, {**self.environment, "BUILDLOG_UNIT": "natemerge", "TMPDIR": str(self.root),
                                          "BUILDLOG_PREPARED_COPY": str(self.checkout)}),
              mock.patch.object(copies, "take_prepared_gate_handoff", return_value=True),
              mock.patch.object(client, "admit", wraps=client.admit) as admit,
              mock.patch.object(gate, "start_gate_job", side_effect=start_module),
              mock.patch.object(gate, "finish_gate_job", side_effect=finish_module),
              contextlib.redirect_stdout(output)):
            code = gate.main(["--affected", "--repo", str(self.checkout),
                              "--module", "lib/test_first.py", "--module", "lib/test_second.py"])
        self.assertEqual(code, 0, output.getvalue())
        admit.assert_called_once()
        self.assertEqual(len(reservations), 2)
        self.assertEqual(len(set(reservations)), 1)
        self.assertEqual(active, set())
        self.assertEqual(self.jobserver_tokens(), b"A")
        self.assert_gate_released()

    @gate_suites("lib")
    def test_named_modules_use_the_parent_reservation_in_a_custom_state_folder(self) -> None:
        self.supply_jobserver(b"A")
        preamble = """
from unittest.mock import patch
from scripts.build_queue import admission_client
from scripts.copies import copies
patch.object(copies, "take_prepared_gate_handoff", return_value=True).start()
patch.object(admission_client, "admit", side_effect=AssertionError("child requested admission")).start()
patch.object(gate, "shared_cpu_pool", side_effect=AssertionError("child requested a CPU slot")).start()
"""
        with AdmissionFixture(state_dir=self.root / "custom-state") as admission, admission.serving():
            environment = {**self.environment, "ADMISSION_SOCKET": str(admission.socket_path), "TMPDIR": str(self.root)}
            self.assertNotEqual(admission.state_dir, Path(environment["BUILDLOG_DIR"]) / "admission")
            with (mock.patch.dict(os.environ, environment),
                  contextlib.redirect_stdout(io.StringIO()),
                  gate.gate_reservation(self.checkout, self.root, gate.BuildHoldWait()) as parent,
                  gate.shared_cpu_pool() as pool):
                assert isinstance(pool, gate.SharedCPUPool)
                self.assertIsInstance(pool.try_acquire(), gate.SharedCPUSlot)
                self.assertEqual(self.jobserver_tokens(), b"")
                reservations = admission.caller_reservations()
                self.assertEqual(len(reservations), 1)
                child = parent.child_environment(os.environ)
                child["BUILDLOG_PREPARED_COPY"] = str(self.checkout)
                for key, value in (("BUILDLOG_JOBSERVER_FIFO", str(self.root / "other-pool")),
                                   ("ADMISSION_SOCKET", str(admission.socket_dir / "other.sock"))):
                    with self.subTest(shared_resource_override=key):
                        result = subprocess.run(
                            [*self.gate_command(preamble=preamble), "--affected", "--repo", str(self.checkout),
                             "--module", "lib/test_second.py"],
                            cwd=self.root, env={**child, key: value}, capture_output=True, text=True,
                            check=False, timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(admission.caller_reservations(), reservations)
                self.assertEqual(self.jobserver_tokens(), b"")
                parent.status = 0
            self.assertTrue(admission.callers_released())
        self.assertEqual(self.jobserver_tokens(), b"A")
        starts = [(event["suite"], event["module"]) for event in self.module_events() if event["event"] == "start"]
        self.assertEqual(starts, [("lib", "test_second.py"), ("lib", "test_second.py")])

    def test_a_named_module_needs_tests_only(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            _ = gate.main(["--repo", str(self.checkout), "--module", "hooks/test_first.py"])
        self.assertIn("--module needs --tests-only", errors.getvalue())

    def test_gate_commands_refuse_abbreviated_options(self) -> None:
        for arguments, short in (
            (["--tests-on", "--full"], "--tests-on"),
            (["--affected", "--ful"], "--ful"),
        ):
            with (self.subTest(arguments=arguments),
                  mock.patch.object(gate, "run_tests_only") as run,
                  contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as refusal):
                _ = gate.main(arguments)
            self.assertEqual(refusal.exception.code, 2)
            self.assertIn(f"unrecognized arguments: {short}", errors.getvalue())
            run.assert_not_called()

    @gate_suites("hooks")
    def test_whole_machine_stops_a_module_at_its_time_limit(self) -> None:
        self.write(self.state / "hold-module", "held")
        with mock.patch.dict(os.environ, self.environment), contextlib.redirect_stdout(io.StringIO()) as output:
            run = gate.run_suites(self.checkout, module_time_limit_s=2,
                                       machine=gate.WholeMachine())
        self.assertFalse(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), 1)
        self.assertTrue(run.jobs[0].killed_for)
        self.assertIn("seconds=", output.getvalue())
        self.assertIn("stopped", output.getvalue())

    @gate_suites("hooks")
    def test_runner_reaps_a_module_when_journal_publication_fails(self) -> None:
        self.write(self.state / "hold-module", "held")
        save = gate.GateRecovery.save
        launched: list[int] = []

        def fail_publication(recovery: gate.GateRecovery) -> None:
            if recovery.groups:
                launched.extend(recovery.groups)
                wait_for_file(self.state / "module-held")
                raise OSError("fixture journal write failed")
            save(recovery)

        try:
            with (mock.patch.dict(os.environ, self.environment),
                  mock.patch.object(gate.GateRecovery, "save", new=fail_publication),
                  contextlib.redirect_stdout(io.StringIO())):
                with self.assertRaisesRegex(OSError, "fixture journal write failed"):
                    _ = gate.run_suites(
                        self.checkout, machine=gate.WholeMachine(),
                        time_limit=gate_record.NoGateTimeLimit("fixture"))
            self.assertEqual(len(launched), 1)
            with self.assertRaises(ChildProcessError):
                _ = os.waitpid(launched[0], os.WNOHANG)
        finally:
            for pid in launched:
                with contextlib.suppress(ChildProcessError):
                    if os.waitpid(pid, os.WNOHANG)[0] == 0:
                        process_group.send_process_group(pid, signal.SIGKILL)
                        _ = os.waitpid(pid, 0)

    def test_modules_run_the_checkout_runner_and_import_from_the_checkout(self) -> None:
        runner = install_test_runner(self.checkout)
        marker = self.state / "checkout-runner"
        self.write(runner, runner.read_text().replace(
            'if __name__ == "__main__":',
            f'Path({str(marker)!r}).write_text(str(Path(__file__).resolve()))\n\n'
            + 'if __name__ == "__main__":'))
        self.write(self.checkout / "checkout_identity.py", 'VALUE = "tested checkout"\n')
        self.write(self.checkout / "scripts/hooks/test_first.py", SUITE_MODULE + '''
    def test_imports_use_the_checkout(self):
        import checkout_identity
        self.assertEqual(checkout_identity.VALUE, "tested checkout")
''')
        result = self.run_gate("--tests-only", "--full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(), str(runner))
        self.assert_all_modules_started()

    def test_a_missing_checkout_runner_is_refused_before_any_job_starts(self) -> None:
        runner = self.checkout / "scripts/test_times/module_run.py"
        runner.unlink(missing_ok=True)
        result = self.run_gate("--tests-only", "--full")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr.strip(), f"REFUSED: test runner not found: {runner}")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.module_events(), [])

    def test_recovery_suites_include_their_real_regression_modules(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        found = {(suite, test_module_name(repo, suite, path))
                 for suite, path in discover_test_modules(repo)}
        self.assertTrue({
            ("release", "test_release.py"), ("claude_to_codex", "test_sync.py"),
            ("new_rust_project", "test_rust_generate.py"), ("validate_and_push", "test_rerun.py"),
            ("exclude", "test_exclude.py"), ("prioritize", "tests/test_renumber.py"),
            ("prioritize", "tests/test_run_watcher.py"),
        }.issubset(found), found)

    def test_discovery_keeps_path_order_and_skips_cache_folders(self) -> None:
        repo = self.checkout / "discovery"
        for name in ("z/test_last.py", "a/test_last.py", "a/nested/test_first.py",
                     "a/__pycache__/test_hidden.py", "__pycache__/test_hidden.py"):
            self.write(repo / "scripts" / name, "# A fixture module.\n")
        found = discover_test_modules(repo)
        self.assertEqual(found, [
            ("a", repo / "scripts/a/nested/test_first.py"),
            ("a", repo / "scripts/a/test_last.py"),
            ("z", repo / "scripts/z/test_last.py"),
        ])
        self.assertEqual([test_module_name(repo, suite, path) for suite, path in found],
                         ["nested/test_first.py", "test_last.py", "test_last.py"])

    @gate_suites("prioritize")
    def test_a_nested_suite_module_runs_and_retries_at_its_discovered_path(self) -> None:
        flat = self.checkout / "scripts/prioritize/test_first.py"
        nested = flat.parent / "tests/test_first.py"
        # The shared job fixture finds its suite from the checkout path.
        self.write(nested, SUITE_MODULE)
        flat.unlink()
        self.write(self.state / "signal-prioritize", "once")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Rerun alone: prioritize/tests/test_first.py passed", result.stdout)
        self.assertEqual(len([row for row in self.module_events() if row["event"] == "start"]), 2)

    @gate_suites("types")
    def test_a_suite_named_types_runs_tests_and_reports_them_apart_from_the_type_check(self) -> None:
        module = self.checkout / "scripts/types/test_first.py"
        _ = module.rename(module.with_name("test_x.py"))
        self.write(self.state / "signal-types", "once")
        with mock.patch.dict(os.environ, self.environment), contextlib.redirect_stdout(io.StringIO()) as output:
            run = gate.run_suites(self.checkout, type_dirs=("scripts",), full=True)
        report = output.getvalue()
        self.assertTrue(run.passed, report)
        self.assertCountEqual([(job.kind, job.suite, job.module, job.returncode) for job in run.jobs], [
            (gate.GateJobKind.TEST_MODULE, "types", "test_x.py", 0),
            (gate.GateJobKind.TYPE_CHECK, "types", "basedpyright", 0),
        ])
        self.assertEqual([(row["suite"], row["module"], row["event"]) for row in self.module_events()], [
            ("types", "test_x.py", "start"), ("types", "test_x.py", "start"), ("types", "test_x.py", "finish"),
        ])
        self.assertIn("Rerun alone: types/test_x.py passed", report)
        self.assertRegex(report, r"(?m)^types: OK \d+\.\d+s$")
        self.assertIn("basedpyright: OK", report)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)
        self.assertIn("types/test_x.py", gate.read_measures(self.state / "runner/module_seconds.json"))

    def test_a_test_file_directly_under_scripts_is_refused_before_jobs_start(self) -> None:
        self.write(self.checkout / "scripts/test_x.py", SUITE_MODULE)
        result = self.run_gate("--tests-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("test module must be in a suite folder under scripts/: test_x.py", result.stderr)
        self.assertEqual(self.module_events(), [])
        self.assertFalse(any(call["tool"] == "basedpyright" for call in self.calls()))

    @gate_suites("hooks", "notify")
    def test_folder_with_its_last_module_deleted_is_not_a_suite(self) -> None:
        (self.checkout / "scripts" / "notify" / "test_first.py").unlink()
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(starts, [("hooks", "test_first.py")])
        self.assertRegex(result.stdout, r"(?m)^hooks: OK \d+\.\d+s$")
        self.assertNotRegex(result.stdout, r"(?m)^notify:")

    def test_modules_use_only_the_checked_tree_pythonpath(self) -> None:
        caller_path = self.root / "caller-pythonpath"
        caller_path.mkdir()
        self.environment.update({"PYTHONPATH": str(caller_path), "GATE_TEST_PYTHONPATH": str(caller_path),
                                 "GATE_TEST_ISOLATION": "1"})
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @gate_suites("hooks", "message")
    def test_caller_modules_fail_and_plain_python_children_import_the_checked_tree(self) -> None:
        caller_path = self.root / "caller-pythonpath"
        self.write(caller_path / "caller_only.py", 'VALUE = "caller"\n')
        self.environment["PYTHONPATH"] = str(caller_path)
        del self.environment["GATE_PYTHON"]
        self.write(self.checkout / "scripts/hooks/test_first.py", "import caller_only\n")
        identity = self.checkout / "scripts/message/checkout_identity.py"
        self.write(identity, 'VALUE = "checked tree"\n')
        marker = self.state / "plain-python-import"
        self.write(self.checkout / "scripts/message/test_first.py", f'''import subprocess
import select
import sys
import signal
import resource
import time
from types import SimpleNamespace
import unittest
from pathlib import Path

class ChildImports(unittest.TestCase):
    def test_child_imports_the_checked_tree(self):
        result = subprocess.run(
            [sys.executable, "-c", "from scripts.message import checkout_identity; print(checkout_identity.__file__)"],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), {str(identity)!r})
        Path({str(marker)!r}).write_text(result.stdout.strip())
''')
        result = self.run_gate("--tests-only", "--full", own_process=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("No module named 'caller_only'", result.stdout)
        self.assertIn("hooks: RED", result.stdout)
        self.assertIn("message: OK", result.stdout)
        self.assertEqual(marker.read_text(), str(identity))

    @gate_suites("lib")
    def test_modules_use_private_build_logs_and_remove_them(self) -> None:
        caller_log = self.root / "caller-buildlog"
        self.write(caller_log / "keep", "caller log")
        self.environment["BUILDLOG_DIR"] = str(caller_log)
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [cast(dict[str, str], json.loads(line))
                for line in (self.state / "buildlogs.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), self.module_count)
        self.assertEqual(len({row["buildlog"] for row in rows}), self.module_count)
        for row in rows:
            self.assertTrue(Path(row["buildlog"]).is_relative_to(row["tmpdir"]))
            self.assertFalse(Path(row["tmpdir"]).exists())
        gate_records = [cast(dict[str, object], json.loads(line))
                        for path in caller_log.rglob("*")
                        if path.is_file() and path != caller_log / "keep"
                        for line in path.read_text().splitlines()]
        self.assertEqual([record.get("kind") for record in gate_records], ["gate_folder_start", "gate_run"])
        self.assertEqual((caller_log / "keep").read_text(), "caller log")
        self.assertEqual(list((caller_log / "admission").glob("*.reservation")), [])
        self.assertEqual(list((caller_log / "admission").glob("*.waiting")), [])

    def test_tests_only_reuses_results_without_running_a_red_type_check(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        records = self.pass_records()
        self.clear_gate_events()
        self.write(self.state / "types-red", "red")

        result = self.run_gate("--tests-only")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tests reused from", result.stdout)
        self.assertFalse(any(call["tool"] == "basedpyright" for call in self.calls()))
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.pass_records(), records)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

    def test_explicit_fetch_updates_refs_without_matching_remote_configuration(self) -> None:
        _ = self.git(self.checkout, "config", "--unset-all", "remote.origin.fetch")
        _ = self.git(self.checkout, "update-ref", "refs/remotes/origin/unit-phase", self.base)
        result = self.run_gate("--promote")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "origin/unit-phase"), self.checkpoint)

    @gate_suites("lib", "direct")
    def test_recorded_durations_start_unknown_then_slowest_modules(self) -> None:
        timings = {f"{suite}/test_first.py": 1.0 for suite in self.suites}
        timings["direct/test_first.py"] = 100.0
        self.write(self.state / "runner" / "module_seconds.json", json.dumps(timings))
        scripts = self.checkout / "scripts"
        modules = [*((suite, scripts / suite / "test_first.py") for suite in self.suites),
                   ("lib", scripts / "lib" / "test_second.py")]
        ordered = gate.module_order(modules, timings, self.checkout)
        self.assertEqual([(suite, path.name) for suite, path in ordered[:2]],
                         [("lib", "test_second.py"), ("direct", "test_first.py")])
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = cast(object, json.loads((self.state / "runner" / "module_seconds.json").read_text()))
        self.assertIsInstance(updated, dict)
        assert isinstance(updated, dict)
        self.assertEqual(len(cast(dict[str, object], updated)), self.module_count)

    @gate_suites("lib", "hooks")
    def test_the_report_names_a_module_slower_than_its_record(self) -> None:
        runner = self.state / "runner"
        runner.mkdir()
        self.write(runner / "module_history.json", json.dumps({
            "lib/test_second.py": [0.0, 0.0, 1000.0], "hooks/test_first.py": [1000.0] * 5}))
        with mock.patch.object(gate, "SLOW_MODULE_FLOOR_S", 0.0), \
                mock.patch.dict(os.environ, self.environment), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            run = gate.run_suites(self.checkout)
        self.assertTrue(run.passed, output.getvalue())
        self.assertRegex(output.getvalue(), r"(?m)^Slow module: lib/test_second\.py \d+ s, median of the last 3 runs 0 s \(3x or more\)$")
        history = cast(dict[str, list[float]], json.loads((runner / "module_history.json").read_text()))
        self.assertEqual(history["lib/test_second.py"][:3], [0.0, 0.0, 1000.0])
        self.assertEqual(len(history["lib/test_second.py"]), 4)
        self.assertEqual(history["hooks/test_first.py"][:4], [1000.0] * 4)
        self.assertEqual(len(history["hooks/test_first.py"]), 5)
        self.assertNotIn("Slow module: hooks/", output.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
