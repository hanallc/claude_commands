"""Named assignment dispatches through the real recorder and loopback app-server."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import unittest
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast, final, override
from unittest.mock import patch
from uuid import UUID

from scripts.lib.fixture_test_support import set_up_fixture
from scripts.agents import codex_test_support as codex_fixture
from scripts.direct import assign, assign_claude, pass_record, progress_history, recorder_state
from scripts.direct.assign_claude_test_support import AssignClaudeLauncherFixture
from scripts.direct.assign_launcher_test_support import ASSIGN, ROOT, AssignLauncherFixture, HookAssignmentFixture, InProcessAssignment, assignment_helpers, invoke_assignment, read_object
from scripts.direct.findings_test_support import invoke_findings
from scripts.herdr import herdr, turn_end, worker_panes
from scripts.lib import atomic_file, unit_runs
from scripts.lint import style_review
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff, test_worker as worker_fixture, worker, worker_admission
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import fixture_session, fixture_runtime_id, fixture_worker, transcript_fixture
from scripts.style import style_plan
from scripts.message import send, sessions
from scripts.agents import stop_resume
from scripts.usage import agents_log
from scripts.herdr.missing_binary import MISSING_HERDR_BIN

def setUpModule() -> None:
    codex_fixture.setUpModule()


def fail_worker_after_prompt() -> None:
    raise SystemExit(1)


class RepairState(TypedDict):
    rounds: list[dict[str, object]]
    findings: list[dict[str, object]]
    in_flight: list[str]


@final
class FailedLaunchRunTests(HookAssignmentFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "UNIT_RUN_ROOT": str(self.session_dir.parent),
            "SCRATCH_ROOT": str(self.session_dir.parent / "scratch"),
            "SCRATCH_HOLDS": str(self.session_dir.parent / "scratch-holds.json"),
        })))
        for key in ("CLAUDE_CODE_SESSION_ID", "UNIT_RUN_DIR", "PLAN_DELEGATE_RUN_DIR"):
            _ = os.environ.pop(key, None)
        _ = staff.set_run(self.native.fixture.parent.id, staff.OpenRun(self.session_dir))
        directory = Path(os.environ["NOTIFIER_SESSIONS_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        _ = (directory / "director.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": staff.runtime_id(), "name": "fixture director",
            "messagingSocketPath": "", "updatedAt": 1, "procStart": sessions.process_start(os.getpid()),
        }))

    def open_run(self) -> Path | None:
        return unit_runs.open_run(staff.runtime_id())

    def failing_worker(self, code: int = 2, *, after_pass_start: bool = False,
                       replace_pass: bool = False) -> Path:
        """Run a failing child at the worker boundary of the real launcher."""
        command = self.session_dir.parent / "worker-python"
        calls = self.session_dir.parent / "failed-worker-commands.jsonl"
        existing = next(iter(self.assignment_records().values())) if after_pass_start else {}
        _ = command.write_text(f"#!{sys.executable}\n" + "\n".join((
            "import json, os, sys",
            "from pathlib import Path",
            "arguments = sys.argv[1:]",
            "if len(arguments) > 1 and Path(arguments[0]).name == 'worker.py' and arguments[1] == 'assign':",
            f"    with Path({str(calls)!r}).open('a') as output:",
            "        output.write(json.dumps(arguments) + '\\n')",
            *(("    from scripts.direct import progress_history",
               "    from scripts.production import staff",
               "    from uuid import UUID",
               "    def option(flag): return arguments[arguments.index(flag) + 1]",
               "    run = Path(option('--session-dir'))",
               "    fields = dict(slot=option('--slot'), phase=option('--phase'), role=arguments[2],",
               f"        worker_name=option('--name'), worker_id=staff.StaffId({str(existing.get('staff_id', ''))!r}),",
               "        prompt=Path(arguments[3]), kind=option('--kind'), lens=option('--lens'))",
               "    progress_history.begin_dispatch(run, **fields, launcher_command=json.loads(option('--launcher-command')),",
               "        new_pass_instance_id=UUID(option('--new-pass-instance-id')))")
              if after_pass_start else ()),
            *(("    progress_history.begin_dispatch(run, **fields, launcher_command=['another-launcher'])",)
              if replace_pass else ()),
            "    print('The worker command failed at launch.', file=sys.stderr)",
            f"    raise SystemExit({code})",
            f"os.execv({assign.PYTHON!r}, [{assign.PYTHON!r}, *arguments])",
        )) + "\n")
        command.chmod(0o755)
        _ = self.enterContext(patch.object(assign, "PYTHON", str(command)))
        return calls

    def assert_run_active(self, result: subprocess.CompletedProcess[str], code: int = 2) -> None:
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(self.open_run(), self.session_dir)
        self.assertNotIn("status", read_object(self.session_dir / "progress_history_state.json"))
        self.assertEqual([event for event in self.events() if event.get("event_type") == "run_finished"], [])

    def test_a_codex_directors_only_failed_worker_keeps_its_run(self) -> None:
        calls = self.failing_worker()
        result = self.launch()
        self.assert_run_active(result)
        self.assertIn("The worker command failed at launch.", result.stderr.splitlines())
        self.assertEqual([event for event in self.events() if event.get("event_type") in (
            "pass_started", "pass_finished", "phase_finished")], [])
        command = cast(list[str], json.loads(calls.read_text()))
        self.assertEqual(command[1], "assign")
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_a_refused_first_assignment_keeps_both_open_phases(self) -> None:
        self.record("start-phase", "--phase-id", "8", "--phase-title", "Concurrent work")
        calls = self.failing_worker()

        self.assert_run_active(self.launch())
        self.assertTrue(calls.is_file())
        self.assertEqual([event for event in self.events() if event.get("event_type") in (
            "pass_started", "phase_finished")], [])
        state = read_object(self.session_dir / unit_runs.STATE_FILE)
        self.assertEqual(recorder_state.open_phase_ids(state), ["7", "8"])

    def test_a_failed_launch_keeps_a_replacement_pass_and_the_run_files(self) -> None:
        _ = self.successful()
        name = next(iter(self.assignment_records()))
        _ = self.failing_worker(after_pass_start=True, replace_pass=True)
        cache = self.session_dir / "verify_cache"
        cache.mkdir()
        evidence = cache / "result"
        _ = evidence.write_text("Verification evidence.\n")
        checkout = self.session_dir / "phases/7/gate"
        _ = subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
        scratch_file = checkout / "work"
        _ = scratch_file.write_text("Scratch work.\n")

        result = self.launch("implement", "--to", name)
        self.assert_run_active(result)
        state = read_object(self.session_dir / unit_runs.STATE_FILE)
        _, failed, replacement = [event for event in self.events() if event["event_type"] == "pass_started"]
        passed = assign.object_map(assign.object_map(state["pass"])[name])
        self.assertEqual((passed["instance_id"], passed["status"]), (replacement["pass_instance_id"], "active"))
        finished = [event for event in self.events() if event["event_type"] == "pass_finished"]
        self.assertEqual([event["status"] for event in finished], ["completed", "interrupted"])
        self.assertEqual(finished[-1]["pass_instance_id"], failed["pass_instance_id"])
        self.assertEqual(recorder_state.open_phase_ids(state), ["7"])
        self.assertEqual(evidence.read_text(), "Verification evidence.\n")
        self.assertEqual(scratch_file.read_text(), "Scratch work.\n")

    def assert_prepared_marker_is_removed(self) -> None:
        prepared = self.run_command(["bash", str(ROOT / "scripts/direct/prepare_session.sh")])
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        run = Path(prepared.stdout.split("Session ready at ")[-1].strip())
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({
            unit_runs.OWNER_FIELD: self.native.fixture.parent.id,
        }))
        self.assertEqual(self.open_run(), run, prepared.stdout + prepared.stderr)
        ended = self.run_command(["bash", str(ROOT / "scripts/direct/end_session.sh")])
        self.assertEqual(ended.returncode, 0, ended.stdout + ended.stderr)
        self.assertIsNone(self.open_run(), ended.stdout + ended.stderr)
        self.assertIn("run pointer cleared", ended.stdout)

    def test_prepare_and_end_use_the_codex_id_when_the_claude_id_differs(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": fixture_runtime_id("other-director")}):
            self.assert_prepared_marker_is_removed()

    def test_prepare_and_end_use_the_codex_id_without_a_claude_id(self) -> None:
        self.assert_prepared_marker_is_removed()


    def test_prepare_continues_without_a_staff_row_and_refuses_unreadable_staff(self) -> None:
        for state, expected_code in (("missing", 0), ("unreadable", 2)):
            with self.subTest(state=state):
                environment = {"CODEX_THREAD_ID": fixture_runtime_id(f"unregistered-{state}")}
                staff_file = staff.record_path()
                original = staff_file.read_bytes()
                if state == "unreadable":
                    _ = staff_file.write_text("invalid staff JSON\n")
                before = staff_file.read_bytes()
                try:
                    with patch.dict(os.environ, environment):
                        result = self.run_command(["bash", str(ROOT / "scripts/direct/prepare_session.sh")])
                    self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
                    self.assertEqual(staff_file.read_bytes(), before)
                    if state == "missing":
                        self.assertIn("Session ready at ", result.stdout)
                        self.assertEqual(len([line for line in result.stderr.splitlines()
                                              if line.startswith("staff member not found:")]), 1)
                    else:
                        self.assertNotIn("Session ready at ", result.stdout)
                        self.assertIn("cannot read staff record", result.stderr)
                finally:
                    _ = staff_file.write_bytes(original)

    def test_a_sole_claude_implementation_failure_ends_its_pass_and_keeps_the_run(self) -> None:
        _ = write_registry(self.registry, {"assignments": {"direct": "claude"},
                                          "direct.claude": {"impl": "opus:high"}}, update=True)

        def fail_after_pane_exit() -> None:
            pane = self.native.agent.submitted[-1][0]
            self.native.fixture.world.remove_pane(pane)
            raise SystemExit(1)

        self.native.agent.during_prompt = fail_after_pane_exit
        result = self.launch()
        self.assert_run_active(result, 1)
        started = [event for event in self.events() if event.get("event_type") == "pass_started"]
        self.assertEqual([event["worker_name"] for event in started], ["impl"])
        finished, = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(finished["status"], "error")
        self.assertEqual(finished["pass_instance_id"], started[0]["pass_instance_id"])
        self.assertEqual(self.native.fixture.world.requests[-1][0], "claude")


    def test_a_claude_slot_refusal_before_its_pass_starts_keeps_the_run(self) -> None:
        _ = write_registry(self.registry, {"assignments": {"direct": "claude"},
                                          "direct.claude": {"impl": "opus:high"}}, update=True)
        calls = self.failing_worker()
        result = self.launch("implement", "--new-worker", "invalid/name")
        self.assert_run_active(result)
        self.assertIn("invalid worker name", result.stderr)
        self.assertEqual([event for event in self.events() if event.get("event_type") == "pass_started"], [])
        self.assertFalse(calls.exists())

    def assert_unnamed_review_keeps_run(self, *, finished: bool) -> None:
        self.record("start-phase", "--phase-id", "8", "--phase-title", "Parallel review")
        prompt = self.session_dir.parent / "parallel-review.txt"
        _ = prompt.write_text("Phase 8 — Parallel review\nReview the assigned change.\n")
        with patch.dict(os.environ, {"UNIT_TEAM_ROLE": "other-review", "UNIT_PASS_OWNER": "launcher"}):
            self.record("start-pass", "--pass-kind", "review", "--phase-id", "8",
                        "--activity", "Parallel review",
                        "--prompt-file", str(prompt), "--called-task", "direct.review",
                        "--called-family", "claude", "--called-model", "opus")
            if finished:
                self.record("finish-pass", "--status", "error")
        state = read_object(self.session_dir / "progress_history_state.json")
        review = assign.object_map(state["pass"])["other-review"]
        self.assertFalse(assign.object_map(review)["worker_name"])
        _ = self.failing_worker()
        self.assert_run_active(self.launch())
        after = read_object(self.session_dir / "progress_history_state.json")
        self.assertEqual(assign.object_map(after["pass"])["other-review"], review)

    def test_an_open_review_without_a_worker_name_keeps_the_run(self) -> None:
        self.assert_unnamed_review_keeps_run(finished=False)

    def test_a_finished_review_without_a_worker_name_keeps_the_run(self) -> None:
        self.assert_unnamed_review_keeps_run(finished=True)

    def test_a_completed_pass_keeps_the_run_after_a_later_launch_failure(self) -> None:
        _ = self.successful()
        name = next(iter(self.assignment_records()))
        _ = self.failing_worker(after_pass_start=True)
        self.assert_run_active(self.launch("implement", "--to", name))
        self.assertEqual(len(self.assignment_records()), 1)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual([event["status"] for event in finished], ["completed", "error"])

    def test_a_second_worker_keeps_the_run_after_a_launch_failure(self) -> None:
        other = self.native.fixture.worker("other-test")
        _ = self.failing_worker()
        self.assert_run_active(self.launch())
        self.assertIn(other.name, self.assignment_records())
        self.assertEqual(staff.by_id(other.id), other)

    def test_a_second_held_run_keeps_both_runs_after_a_launch_failure(self) -> None:
        other = self.session_dir.parent / "other-run"
        other.mkdir()
        state = self.session_dir / "progress_history_state.json"
        _ = (other / state.name).write_bytes(state.read_bytes())
        before = (other / state.name).read_bytes()
        _ = self.failing_worker()
        self.assert_run_active(self.launch())
        self.assertEqual((other / state.name).read_bytes(), before)

    def test_a_herdr_exit_that_keeps_a_worker_keeps_the_run(self) -> None:
        self.commit_project()
        calls = self.failing_worker(7)
        self.assert_run_active(self.launch(), 7)
        command = cast(list[str], json.loads(calls.read_text()))
        self.assertEqual(command[1], "assign")
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_a_refusal_before_the_worker_command_keeps_the_run(self) -> None:
        calls = self.failing_worker()
        self.assert_run_active(self.launch(paths=False))
        self.assertFalse(calls.exists())
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_a_retry_without_a_session_dir_reuses_the_run_after_launch_failure(self) -> None:
        self.commit_project()
        calls = self.failing_worker()
        self.assert_run_active(self.launch())
        self.assert_run_active(self.launch(session=False))
        commands = [cast(list[str], json.loads(line)) for line in calls.read_text().splitlines()]
        self.assertEqual([command[command.index("--session-dir") + 1] for command in commands],
                         [str(self.session_dir), str(self.session_dir)])


@final
class PaneReviewLauncherTests(unittest.TestCase):
    """The assignment launcher uses the real dispatch and its transcript receipts."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture = worker_fixture.WorkerCommandTests("runTest")
        self.router = worker_fixture.PaneCommandRouter()
        self.ready = Path()
        self.phase = Path()

    @override
    def setUp(self) -> None:
        bytecode_setting = os.environ.get("PYTHONDONTWRITEBYTECODE", "")
        _ = set_up_fixture(self.addCleanup, self.fixture)
        _ = self.enterContext(patch.object(unit_runs, "ASTRA_RUN_ROOT", self.fixture.fixture.root))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"ASTRA_REVIEW_DIR": str(self.fixture.fixture.root)})))
        _ = self.enterContext(assignment_helpers(bytecode_setting))
        self.ready = self.fixture.run_dir / "final.ready"
        self.phase = self.fixture.run_dir / "phases/8"
        write_text_atomic(self.fixture.prompt, "Phase 8 — Workers\nReview Stage 1, then review the final diff in Stage 2.\n")
        opened = self.fixture.opened("impl", "impl")
        _ = progress_history.begin_dispatch(self.fixture.run_dir, slot="impl", phase="8", role="impl",
            worker_name="impl", worker_id=opened.row.id, launcher_command=["previous-launcher"], prompt=self.fixture.prompt, kind="impl", lens="")
        self.router.setup(self, self.fixture.fixture.root)

    def launch_review(self) -> subprocess.CompletedProcess[str]:
        arguments = ["--role", "review", "--to", "review", "--cwd", str(self.fixture.fixture.root),
                     "--phase", "8", "--session-dir", str(self.fixture.run_dir),
                     "--prompt", str(self.fixture.prompt), "--early-ready", str(self.ready)]
        return invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)

    def test_durable_design_dispatch_without_a_summary_saves_the_reply_beside_the_prompt(self) -> None:
        arguments = ["--role", "review", "--target", "durable-design-role", "--policy", "astra-reviewer",
                     "--parent", str(self.fixture.fixture.parent.id), "--new-worker", "TEST-astra-reviewer",
                     "--cwd", str(self.fixture.fixture.root), "--phase", "8",
                     "--session-dir", str(self.fixture.run_dir), "--prompt", str(self.fixture.prompt)]
        result = invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.finish("TEST-astra-reviewer"), turn_end.TurnOutcome.DONE)
        reply = self.fixture.prompt.with_name(self.fixture.prompt.stem + "_reply.txt")
        self.assertTrue(reply.read_text().startswith(self.fixture.agent.final))
        self.assertEqual(len(self.fixture.agent.submitted), 1)

    def test_astra_launch_refusals_do_not_start_a_pane(self) -> None:
        parent = self.fixture.fixture.parent
        member = fixture_worker("TEST-reviewer", parent.id)
        world = self.fixture.fixture.world
        launches = world.launches.copy()
        before = staff.snapshot().rows
        durable = ["--target", "durable-design-role", "--policy", "astra-reviewer", "--parent", parent.id]
        with self.subTest(refusal="missing name"):
            result = invoke_cli(assign, assign.main, ["--role", "review", *durable], os.environ.copy(), ROOT)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("durable-design-role requires --to or --new-worker", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        for locked in (parent, member):
            run = self.fixture.fixture.root / f"busy-astra-{locked.name}"
            base = ["--role", "review", "--new-worker", member.name, "--session-dir", str(run)]
            one_shot = ["--operation", "astra-review", "--cwd", str(self.fixture.fixture.root),
                        "--prompt", str(self.fixture.prompt), "--reply-file", str(run / "astra-review.md")]
            with patch.object(sys, "argv", ["holder", "first part\nlast part"]), staff.row_operation(locked.id):
                env = staff.child_environment(os.environ)
                for target, options in (("durable", durable), ("one-shot", one_shot)):
                    with self.subTest(locked=locked.name, target=target):
                        result = invoke_cli(assign, assign.main, [*base, *options], env, ROOT)
                        self.assertEqual(result.returncode, worker.ExitCode.REFUSED, result.stdout + result.stderr)
                        lines = result.stderr.splitlines()
                        self.assertEqual(len(lines), 1, lines)
                        self.assertIn(f"{locked.name}: another operation holds this row", lines[0])
                        self.assertIn("first part last part", lines[0])
        self.assertEqual(world.launches, launches)
        self.assertEqual(staff.snapshot().rows, before)
        self.assertEqual(self.fixture.agent.submitted, [])

    def test_completed_astra_review_rerun_keeps_its_notice_and_reply(self) -> None:
        for changed_session in (False, True):
            with self.subTest(changed_session=changed_session):
                with ExitStack() as cleanup:
                    case = set_up_fixture(cleanup.callback, PaneReviewLauncherTests("runTest"))
                    write_text_atomic(case.fixture.prompt, "Phase astra-review — Astra review\nReview the assigned change.\n")
                    case.fixture.run_dir = case.fixture.fixture.root / f"astra-run-{changed_session}"
                    reply = case.fixture.run_dir / "astra-review.md"
                    arguments = ["--role", "review", "--operation", "astra-review", "--new-worker", "astra-reviewer",
                                 "--cwd", str(case.fixture.fixture.root), "--session-dir", str(case.fixture.run_dir),
                                 "--prompt", str(case.fixture.prompt), "--reply-file", str(reply)]
                    with patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(case.fixture.fixture.root / "roles")}), \
                            case.fixture.fixture.world.process_starts():
                        result = invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(len(case.fixture.agent.submitted), 1)
                        row = case.fixture.agent.row(case.fixture.agent.submitted[-1][0])
                        if changed_session:
                            backend = staff.backend(row)
                            assert isinstance(backend, staff.CodexBackend)
                            source = worker_admission.transcript_source(row, backend)
                            identity = staff.native_id(fixture_runtime_id("astra-review-new-session"))
                            fixture_session(identity, backend)
                            row = staff.update_current_session(row.current_session, identity)
                            target = worker_admission.transcript_source(row, backend)
                            write_text_atomic(target, source.read_text())
                            pid = case.fixture.fixture.world.foreground[case.fixture.agent.submitted[-1][0]][0]
                            process = case.fixture.fixture.world.processes[pid]
                            case.fixture.fixture.world.processes[pid] = replace(process, identity=herdr.PaneSessionReport(identity))
                            case.fixture.agent.complete(case.fixture.agent.submitted[-1][0])
                        self.assertEqual(case.fixture.finish("astra-reviewer"), turn_end.TurnOutcome.DONE)
                        self.assertIsNone(staff.by_id(row.id))
                        notice, = case.fixture.agent.notices
                        saved = reply.read_bytes(), reply.stat().st_mtime_ns
                        submitted = case.fixture.agent.submitted.copy()
                        launches = case.fixture.fixture.world.launches.copy()
                        repeated = invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)
                    self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
                    self.assertEqual(case.fixture.agent.notices, [notice])
                    self.assertEqual(case.fixture.agent.notice_senders, [send.CodexSender(str(row.current_session))])
                    self.assertEqual(case.fixture.agent.submitted, submitted)
                    self.assertEqual(case.fixture.fixture.world.launches, launches)
                    self.assertEqual((reply.read_bytes(), reply.stat().st_mtime_ns), saved)

    def test_review_stage_two_uses_its_own_done_line_and_reuses_both_receipts(self) -> None:
        summary = self.phase / "review_summary_1.txt"
        summary.parent.mkdir(parents=True)
        write_text_atomic(summary, "The previous full report.\n")
        prompt = self.fixture.prompt.read_bytes()
        completed_commands: list[list[str]] = []
        original_call = assign.call

        def observe_call(command: list[str], env: dict[str, str], *, capture: bool = False,
                         check: bool = True) -> subprocess.CompletedProcess[str]:
            if "finish-pass" in command and command[-1] == "completed":
                completed_commands.append(command)
            return original_call(command, env, capture=capture, check=check)

        with patch.object(assign, "call", side_effect=observe_call):
            result = self.launch_review()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(completed_commands, [])
        self.assertEqual(len(self.fixture.agent.submitted), 1)
        self.assertEqual(self.fixture.finish("review"), turn_end.TurnOutcome.WAITING_ON_STAGE_TWO)
        write_text_atomic(self.ready.with_suffix(".diff"), "+the final change\n")
        write_text_atomic(self.ready, "ready\n")
        self.assertEqual(self.fixture.finish("review", turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
        first, second = self.fixture.agent.submitted
        dispatch_id = first[1].split("DONE ")[-1].split()[0].removesuffix(".")
        self.assertEqual(first[0], second[0])
        self.assertIn(f"DONE {dispatch_id}-2\n", second[1])
        self.assertIn(str(self.ready.with_suffix(".diff")), second[1])
        self.assertEqual(summary.read_text(), "The previous full report.\n")
        self.assertEqual(self.fixture.finish("review"), turn_end.TurnOutcome.DONE)
        self.assertTrue(summary.read_text().endswith(f"DONE {dispatch_id}-2\n"))
        self.assertEqual((self.phase / "review_status_1").read_text(), "done\n")
        self.assertEqual(self.fixture.finish("review"), turn_end.TurnOutcome.IGNORED)
        self.assertEqual(self.fixture.agent.submitted, [first, second])
        saved = summary.read_bytes(), (self.phase / "review_status_1").read_bytes()
        notices = self.fixture.agent.notices.copy()
        self.fixture.agent.notice_failures = 1
        settled = self.launch_review()
        self.assertEqual(settled.returncode, 0, settled.stdout + settled.stderr)
        self.assertEqual(self.fixture.agent.notice_failures, 1)
        self.assertEqual((self.phase / "review_status_1").read_bytes(), saved[1])
        self.assertEqual(summary.read_bytes(), saved[0])
        self.assertEqual(self.fixture.agent.submitted, [first, second])
        self.assertEqual(self.fixture.agent.notices, notices)
        repeated = self.launch_review()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.fixture.agent.submitted, [first, second])
        self.assertEqual(self.fixture.agent.notices, notices)
        self.assertEqual((summary.read_bytes(), (self.phase / "review_status_1").read_bytes()), saved)
        self.assertEqual(self.fixture.prompt.read_bytes(), prompt)

    def test_review_stage_two_retry_waits_for_the_submitted_turn_without_another_prompt(self) -> None:
        first = self.launch_review()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        write_text_atomic(self.ready, "ready\n")
        self.fixture.agent.finish_after = 1
        self.assertEqual(self.fixture.finish("review", turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
        submitted = self.fixture.agent.submitted.copy()
        self.assertEqual(len(submitted), 2)
        repeated = self.launch_review()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.fixture.agent.submitted, submitted)
        self.assertEqual(self.fixture.agent.waited, [])
        self.fixture.agent.complete(submitted[-1][0])
        self.assertEqual(self.fixture.finish("review"), turn_end.TurnOutcome.DONE)
        second_id = submitted[-1][1].split("DONE ")[-1].split()[0].removesuffix(".")
        self.assertTrue((self.phase / "review_summary_1.txt").read_text().endswith(f"DONE {second_id}\n"))

    def test_review_stage_one_done_does_not_complete_stage_two_or_replace_the_report(self) -> None:
        summary = self.phase / "review_summary_1.txt"
        summary.parent.mkdir(parents=True)
        write_text_atomic(summary, "The previous full report.\n")
        result = self.launch_review()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        write_text_atomic(self.ready, "ready\n")
        self.fixture.agent.without_done = True
        self.assertEqual(self.fixture.finish("review", turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
        self.assertEqual(self.fixture.finish("review"), turn_end.TurnOutcome.NO_DONE)
        self.assertEqual(len(self.fixture.agent.submitted), 2)
        self.assertEqual(summary.read_text(), "The previous full report.\n")
        self.assertEqual((self.phase / "review_status_1").read_text(), "running\n")
        state = read_object(self.fixture.run_dir / "progress_history_state.json")
        self.assertEqual(assign.object_map(assign.object_map(state["pass"])["review"])["status"], "active")

    def assert_start_not_delivered(self, code: worker.ExitCode, reason: str) -> None:
        result = self.launch_review()
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertIn(reason, (self.phase / "review_agent_1.log").read_text())
        self.assertEqual(self.fixture.agent.submitted, [])
        self.assertFalse((self.phase / "review_status_1").exists())

    def test_review_admission_failure_sends_neither_stage(self) -> None:
        launches = self.fixture.fixture.world.launches.copy()
        with patch.object(worker_admission, "worker_operation", side_effect=worker_admission.AdmissionRefused("admission unavailable")):
            self.assert_start_not_delivered(worker.ExitCode.REFUSED, "staff: review: not opened: admission unavailable")
        self.assertEqual(self.fixture.fixture.world.launches, launches)

    def test_review_unknown_process_keeps_the_worker_without_either_stage(self) -> None:
        opened = self.fixture.opened("review", "review")
        self.fixture.fixture.world.unknown[opened.pane] = "native reader unavailable"
        before = self.fixture.fixture.staff_state()
        self.assert_start_not_delivered(worker.ExitCode.UNCERTAIN, "native reader unavailable")
        self.assertEqual(self.fixture.fixture.staff_state(), before)

    def test_review_missing_terminal_keeps_the_worker_without_either_stage(self) -> None:
        opened = self.fixture.opened("review", "review")
        self.fixture.fixture.world.locations[opened.pane]["terminal_id"] = ""
        before = self.fixture.fixture.staff_state()
        self.assert_start_not_delivered(worker.ExitCode.UNCERTAIN, "terminal")
        self.assertEqual(self.fixture.fixture.staff_state(), before)

    def test_review_role_refusal_keeps_the_active_worker_and_types_neither_stage(self) -> None:
        for sole_worker in (False, True):
            with self.subTest(sole_worker=sole_worker):
                with ExitStack() as cleanup:
                    case = set_up_fixture(cleanup.callback, PaneReviewLauncherTests("runTest"))
                    name = "impl" if sole_worker else "review"
                    opened = case.fixture.opened(name, "impl")
                    _ = progress_history.begin_dispatch(case.fixture.run_dir, slot=name, phase="8", role="impl",
                        worker_name=name, worker_id=opened.row.id, launcher_command=["previous-launcher"],
                        prompt=case.fixture.prompt, kind="impl", lens="")
                    if sole_worker:
                        case.fixture.agent.without_done = True
                        code = worker.dispatch(case.fixture.fixture.parent.id, staff.WorkerName.parse(name), "impl",
                            case.fixture.prompt, session_dir=case.fixture.run_dir, slot=name, phase="8", kind="impl",
                            launcher_command=["previous-launcher"], reply=case.fixture.reply)
                        self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
                    if sole_worker:
                        _ = staff.set_run(case.fixture.fixture.parent.id, staff.OpenRun(case.fixture.run_dir))
                    before = case.fixture.fixture.staff_state()
                    active = progress_history.active_dispatch(opened.row.id, case.fixture.run_dir)
                    submitted = case.fixture.agent.submitted.copy()
                    if sole_worker:
                        root = case.fixture.fixture.root
                        state_path = case.fixture.run_dir / unit_runs.STATE_FILE
                        state = read_object(state_path)
                        _ = state.pop("status", None)
                        write_text_atomic(state_path, json.dumps(state))
                        arguments = ["--role", "review", "--to", name, "--cwd", str(root), "--phase", "8",
                                     "--session-dir", str(case.fixture.run_dir), "--prompt", str(case.fixture.prompt),
                                     "--early-ready", str(case.ready)]
                        with patch.dict(os.environ, {"UNIT_RUN_ROOT": str(root)}):
                            self.assertEqual(unit_runs.held(staff.runtime_id(), root), [str(case.fixture.run_dir)])
                            result = invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)
                        self.assertEqual(result.returncode, worker.ExitCode.REFUSED, result.stdout + result.stderr)
                        self.assertIn("role change blocked by active work: submitted pass awaits completion",
                                      (case.phase / "impl_agent_1.log").read_text())
                        self.assertNotIn("status", read_object(state_path))
                        self.assertEqual(unit_runs.open_run(staff.runtime_id()), case.fixture.run_dir)
                        self.assertFalse((case.phase / "impl_status_1").exists())
                    else:
                        with patch("scripts.agents.detached_runs.live_runs", return_value=[object()]):
                            case.assert_start_not_delivered(worker.ExitCode.REFUSED,
                                "role change blocked by active work: a detached run is live")
                    self.assertEqual(case.fixture.agent.submitted, submitted)
                    self.assertEqual(progress_history.active_dispatch(opened.row.id, case.fixture.run_dir), active)
                    self.assertEqual(case.fixture.fixture.staff_state(), before)
                    self.assertIn(opened.pane, case.fixture.fixture.world.foreground)
                    self.assertEqual(case.fixture.fixture.world.stops, [])
                    if sole_worker:
                        case.fixture.agent.without_done = False
                        case.fixture.agent.complete(opened.pane)
                        self.assertEqual(case.fixture.finish(name), turn_end.TurnOutcome.DONE)
                        self.assertEqual(worker_panes.worker_pass(opened.row, case.fixture.run_dir)["status"], "completed")
                        self.assertNotIn(opened.pane, case.fixture.fixture.world.locations)


@final
class HerdrClaimFixture(AssignClaudeLauncherFixture):
    """Use the shared claim fixture with the active launcher environment."""

    @override
    def environment(self) -> dict[str, str]:
        return os.environ.copy()


@final
class AssignLauncherTests(HookAssignmentFixture):

    def test_claude_worker_returns_through_start_and_then_follows_its_codex_thread(self) -> None:
        self.commit_project()
        cases = ((False, True), (True, True), (True, False))
        for source_live, named in cases:
            with self.subTest(source_live=source_live, named=named):
                name = "impl"
                self.prompt = self.session_dir.parent / f"switch-{source_live}-{named}.md"
                current = worker_admission.find_worker(staff.snapshot(), self.native.fixture.parent.id, name)
                world = self.native.fixture.world
                if current is not None:
                    for pane, (pid, _, _) in tuple(world.foreground.items()):
                        if world.processes[pid].identity.id == current.current_session:
                            world.end_agent(pane)
                source_identity = staff.native_id(fixture_runtime_id(f"source-{source_live}-{named}"))
                fixture_session(source_identity, staff.ClaudeBackend())
                row = (staff.update_current_session(current.current_session, source_identity) if current is not None else
                       fixture_worker(name, self.native.fixture.parent.id, runtime_id=source_identity,
                                      backend=staff.ClaudeBackend()))
                source, evidence = transcript_fixture(row, staff.ClaudeBackend())
                self.native.fixture.world.add(row, "w1:p1", family="claude", live=source_live)
                agents_log.append(row.current_session, "impl", None, datetime.now(timezone.utc))
                self.write_phase_prompt("7", "Assignment core", f"Switch the private worker {source_live} {named}.")
                source_pass = progress_history.begin_dispatch(self.session_dir, slot=name, phase="7", role="impl",
                    worker_name=name, worker_id=row.id, launcher_command=["source-launcher"], prompt=self.prompt,
                    kind="assign", lens="")
                with source.open("a") as stream:
                    _ = stream.write(json.dumps({"type": "assistant", "message": {
                        "role": "assistant", "content": "DONE " + source_pass}}) + "\n")
                with patch.dict(os.environ, {"UNIT_TEAM_ROLE": name, "UNIT_PASS_OWNER": "launcher"}):
                    self.record("finish-pass", "--status", "completed")
                image = self.session_dir.parent / "attached.png"
                image.touch()
                arguments = ["--role", "implement", "--cwd", str(self.project), "--phase", "7",
                             "--prompt", str(self.prompt), "--paths", str(self.paths), "--session-dir", str(self.session_dir),
                             "--image", str(image)]
                if named:
                    arguments += ["--to", name]
                def stop_source(reference: str, kind: str = "") -> stop_resume.Stopped | stop_resume.NotRunning:
                    del kind
                    source_row = staff.snapshot().by_member_reference(reference)
                    assert source_row is not None
                    world = self.native.fixture.world
                    pid = next((pid for pid, _, _ in world.foreground.values()
                                if world.processes[pid].current_identity == source_row.current_session), 0)
                    _ = world.stop(source_row, world)
                    return (stop_resume.Stopped(str(source_row.current_session), pid) if pid else
                            stop_resume.NotRunning(str(source_row.current_session)))

                with patch.object(stop_resume, "stop", side_effect=stop_source):
                    result = invoke_assignment(arguments, os.environ.copy())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr +
                                 "\n".join(path.read_text() for path in (self.session_dir / "phases/7").glob("*_agent.log")))
                self.assertEqual(self.native.finish(name), turn_end.TurnOutcome.DONE)
                moved = staff.by_id(row.id)
                assert moved is not None
                self.assertNotEqual(moved.current_session, row.current_session)
                self.assertIsInstance(staff.backend(moved), staff.CodexBackend)
                self.assertEqual(moved.parent, row.parent)
                self.assertEqual(moved.to_json() | {"current_session": row.current_session},
                                 replace(row, role=staff.KnownWorkerRole("impl"),
                                         home=staff.SessionHome(self.native.fixture.root / "codex")).to_json())
                self.assertNotEqual(self.native.agent.submitted[-1][0], "w1:p1")
                self.assertIsInstance(world.launches[-1], herdr.SplitPane)
                extract = worker_admission.handoff_path(self.session_dir, row)
                self.assertIn(str(source), extract.read_text())
                self.assertIn(evidence[0][0], extract.read_text())
                self.assertIn(str(extract), self.native.agent.submitted[-1][1])
                passed = assign.object_map(assign.object_map(read_object(
                    self.session_dir / "progress_history_state.json")["pass"])[name])
                self.assertIn(str(image.resolve()), Path(str(passed["prompt_file"])).read_text())
                before = moved.to_json()
                launches = len(self.native.fixture.world.launches)
                _ = self.successful("implement", "--to", name)
                current = staff.by_id(row.id)
                assert current is not None
                self.assertEqual(current.to_json(), before)
                self.assertEqual(len(self.native.fixture.world.launches), launches + 1)


    def assert_lock_held(self, path: Path) -> None:
        with path.open("a") as lock, self.assertRaises(BlockingIOError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_incomplete_dispatch_rerun_keeps_its_pass_and_only_waits(self) -> None:
        self.native.agent.without_done = True
        first = self.launch()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.native.finish("impl"), turn_end.TurnOutcome.NO_DONE)
        state = read_object(self.session_dir / "progress_history_state.json")
        passed = assign.object_map(assign.object_map(state["pass"])["impl"])
        self.assertEqual(passed["status"], "active")
        identity = passed["instance_id"]
        submitted = self.native.agent.submitted.copy()
        self.assertEqual(len(submitted), 1)
        pane = submitted[0][0]
        self.native.fixture.world.locations[pane]["agent_status"] = "working"
        self.native.agent.without_done = False
        self.native.agent.finish_after = 1
        prompt = Path(str(passed["prompt_file"]))
        content = prompt.read_bytes()
        phase = prompt.parent
        manifests = {path: path.read_bytes() for path in phase.glob("dispatch_*_paths.json")}
        prompts = set(phase.glob("*_prompt.*.txt"))
        write_bytes = atomic_file.write_bytes_atomic

        def restore_under_lock(path: Path, data: bytes, *, mode: int | None = None) -> None:
            if path == prompt:
                self.assert_lock_held(pass_record.recorder_lock_path(self.session_dir))
            write_bytes(path, data, mode=mode)

        for pass_status in ("active", "completed"):
            if pass_status == "completed":
                self.assertEqual(self.native.finish("impl"), turn_end.TurnOutcome.DONE)
            cases = ("intact", "missing", "changed")
            for damage in (*cases, "pending completion") if pass_status == "active" else cases:
                with self.subTest(status=pass_status, damage=damage):
                    if damage == "missing":
                        prompt.unlink()
                    elif damage == "changed":
                        write_text_atomic(prompt, "Changed prompt bytes.\n")
                    elif damage == "pending completion":
                        self.native.agent.complete(pane)
                        reply = pass_record.dispatch_delivery(passed).reply_file
                        progress_history.save_hook_completion(self.session_dir, str(identity),
                                                              progress_history.HookCompletion("done", reply))
                    with patch.object(atomic_file, "write_bytes_atomic", side_effect=restore_under_lock):
                        if damage == "pending completion":
                            result = AssignLauncherFixture.launch(self)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            self.assertEqual((phase / "impl_status").read_text(), "done\n")
                        else:
                            _ = self.successful()
                    state = read_object(self.session_dir / "progress_history_state.json")
                    saved = assign.object_map(assign.object_map(state["pass"])["impl"])
                    expected = "completed" if damage == "pending completion" else pass_status
                    self.assertEqual((saved["instance_id"], saved["status"]), (identity, expected))
                    self.assertEqual(prompt.read_bytes(), content)
                    self.assertEqual(self.native.agent.submitted, submitted)
                    self.assertEqual(self.native.agent.waited, [])
                    self.assertEqual(sum(event.get("event_type") == "pass_started" for event in self.events()), 1)
                    self.assertEqual(set(phase.glob("*_prompt.*.txt")), prompts)
                    self.assertEqual({path: path.read_bytes() for path in phase.glob("dispatch_*_paths.json")}, manifests)

        with self.subTest(retry="history is unavailable during cleanup"):
            history = Path(str(state["history_file"]))
            unavailable = history.with_suffix(".unavailable")
            record_launch = assign.record_worker_launch_exit

            def remove_history(run: Path, instance: UUID, outcome: progress_history.AssignmentLaunchExit
                               ) -> progress_history.RecordedAssignmentLaunch | progress_history.AssignmentLaunchNotRecorded:
                result = record_launch(run, instance, outcome)
                _ = history.replace(unavailable)
                return result

            try:
                with patch.object(assign, "record_worker_launch_exit", side_effect=remove_history):
                    result = self.launch()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                if unavailable.is_file():
                    _ = unavailable.replace(history)
            retained, = set(phase.glob("*_prompt.*.txt")) - prompts
            self.assertEqual(retained.read_bytes(), content)
            self.assertEqual(self.native.agent.submitted, submitted)

        with self.subTest(retry="phase closes during the receipt check"):
            receipt = worker.dispatch_receipt
            write_text_atomic(prompt, "Changed prompt bytes.\n")
            changed = prompt.read_bytes()

            def close_phase(row: staff.StaffRow, line: str, dispatch_id: str, *,
                            run: Path) -> worker.DispatchReceipt:
                result = receipt(row, line, dispatch_id, run=run)
                self.record("finish-phase", "--phase-id", "7", "--status", "completed")
                return result

            with patch.object(worker, "dispatch_receipt", side_effect=close_phase):
                refused = self.launch()
            self.assertEqual(refused.returncode, worker.ExitCode.BUSY, refused.stdout + refused.stderr)
            self.assertEqual(prompt.read_bytes(), changed)
            self.assertEqual(self.native.agent.submitted, submitted)

    def test_unnamed_assignment_finishes_the_selected_workers_pass_and_artifacts(self) -> None:
        opened = self.native.opened("impl2")
        dispatch = progress_history.begin_dispatch(self.session_dir, slot="impl2", phase="7", role="impl",
            worker_name="impl2", worker_id=opened.row.id, launcher_command=["previous-launcher"],
            prompt=self.prompt, kind="assign", lens="")
        self.native.fixture.reply_to_dispatch(opened.row, dispatch)
        with patch.dict(os.environ, {"UNIT_TEAM_ROLE": "impl2", "UNIT_PASS_OWNER": "launcher"}):
            self.record("finish-pass", "--status", "completed")
        arguments = ["--role", "implement", "--cwd", str(self.project), "--phase", "7",
                     "--prompt", str(self.prompt), "--paths", str(self.paths), "--session-dir", str(self.session_dir)]
        result = invoke_assignment(arguments, os.environ.copy())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        submitted = self.native.agent.submitted.copy()
        self.native.fixture.world.locations[submitted[0][0]]["agent_status"] = "working"
        repeated = invoke_assignment(arguments, os.environ.copy())
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.native.agent.submitted, submitted)
        self.assertEqual(self.native.finish("impl2"), turn_end.TurnOutcome.DONE)
        state = read_object(self.session_dir / "progress_history_state.json")
        passed = assign.object_map(assign.object_map(state["pass"])["impl2"])
        self.assertEqual((passed["worker_id"], passed["status"]), (opened.row.id, "completed"))
        self.assertNotIn("worker_name", passed)
        phase = self.session_dir / "phases/7"
        self.assertEqual((phase / "impl2_status").read_text(), "done\n")
        self.assertIn(self.native.agent.final, (phase / "impl2_summary.txt").read_text())
        self.assertIn("submitted", (phase / "impl2_agent.log").read_text())
        self.assertEqual(read_object(phase / "dispatch_2_paths.json")["worker"], "impl2")
        self.assertEqual(staff.by_id(opened.row.id), replace(opened.row, role=staff.KnownWorkerRole("impl")))

    def test_assignment_reads_current_model_settings_for_the_next_launch(self) -> None:
        _ = self.successful("implement", "--new-worker", "first")
        first = self.native.fixture.world.requests[-1][1]
        self.assertEqual(first[first.index("--model") + 1], "gpt-kept-sol")
        _ = write_registry(self.registry, {"direct.codex": {"impl": "gpt-other-sol:max"}}, update=True)
        _ = self.successful("implement", "--new-worker", "second")
        second = self.native.fixture.world.requests[-1][1]
        self.assertEqual(second[second.index("--model") + 1], "gpt-other-sol")
        self.assertIn('model_reasoning_effort="max"', second)
        self.assertEqual(self.turn_starts, 2)

    def test_native_assignment_retains_unknown_workers(self) -> None:
        self.commit_project()
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        prompt = self.prompt.read_bytes()
        self.native.fixture.world.unreadable = True
        with patch.dict(os.environ, {"HERDR_BIN": MISSING_HERDR_BIN}):
            result = self.launch(execution=InProcessAssignment())
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])
        self.assertEqual(self.native.agent.submitted, [])
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
        self.assertEqual(self.prompt.read_bytes(), prompt)
        self.assertFalse((self.session_dir / "phases/7/impl_status").exists())

    def herdr_claim_launch(self, claim_status: str, role: str = "implement") -> subprocess.CompletedProcess[str]:
        self.commit_project()
        self.record("start-phase", "--phase-id", "3", "--phase-title", "Claim before assignment")
        write_text_atomic(self.prompt, "Phase 3 — Claim before assignment\nImplement the assigned change.\n")
        write_text_atomic(self.session_dir / "dispatch_events.txt", "")
        environment, calls = HerdrClaimFixture().claim_fixture(self.session_dir, claim_status)
        environment["HERDR_BIN"] = MISSING_HERDR_BIN
        self.native.fixture.world.unreadable = True
        with patch.dict(os.environ, environment):
            self.instrument_launcher("\n".join((
                "from pathlib import Path",
                "from scripts.direct import assign",
                "from scripts.lib.atomic_file import write_text_atomic",
                "run_command = assign.subprocess.run",
                "def observe(command, *arguments, **options):",
                "    if len(command) > 2 and Path(command[1]).name == 'worker.py' and command[2] == 'assign':",
                f"        events = Path({str(self.session_dir / 'dispatch_events.txt')!r})",
                "        write_text_atomic(events, events.read_text() + 'assign\\n')",
                *((f"        baseline = Path({str(self.session_dir / 'phases/3/phase_baseline')!r})",
                   f"        write_text_atomic(Path({str(self.session_dir / 'baseline-at-worker-assign')!r}), baseline.read_text())")
                  if role != "review" else ()),
                "    return run_command(command, *arguments, **options)",
                "assign.subprocess.run = observe",
            )))
            result = self.launch(role, "--phase", "3")
        if role != "review":
            self.assertEqual(calls.read_text().splitlines(), ["sid-director.phase-3 check reviewed.txt --json"])
        return result

    def test_herdr_assignment_claims_the_phase_before_the_worker_assignment(self) -> None:
        result = self.herdr_claim_launch("clear")
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "dispatch_events.txt").read_text().splitlines(), ["claim", "assign"])
        head = subprocess.run(["git", "-C", str(self.project), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout
        self.assertEqual((self.session_dir / "phases/3/phase_baseline").read_text(), head)
        self.assertEqual((self.session_dir / "baseline-at-worker-assign").read_text(), head)

    def test_herdr_assignment_refuses_a_failed_phase_claim_before_the_worker_assignment(self) -> None:
        status = self.session_dir / "phases/3/impl_status"
        status.parent.mkdir(parents=True)
        write_text_atomic(status, "done\n")
        result = self.herdr_claim_launch("blocked")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        log = self.session_dir / "phases/3/impl_agent.log"
        self.assertEqual(result.stderr,
                         f"ERROR: phase 3 could not claim its files before its worker opens; see {log}.\n")
        self.assertEqual((self.session_dir / "dispatch_events.txt").read_text().splitlines(), ["claim"])
        self.assertEqual(status.read_text(), "done\n")
        self.assertIn("launcher: phase 3 could not claim its files; its worker did not open",
                      (self.session_dir / "board.log").read_text())

    def test_herdr_review_assigns_the_worker_without_a_phase_claim(self) -> None:
        result = self.herdr_claim_launch("blocked", "review")
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "dispatch_events.txt").read_text().splitlines(), ["assign"])

    def finding_command(self, verb: str, *options: str, phase: str = "7") -> str:
        result = invoke_findings([verb, "--session-dir", str(self.session_dir), "--phase", phase, *options],
                                 os.environ.copy(), self.project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def repair_state(self, phase: str = "7") -> RepairState:
        return cast(RepairState, json.loads(self.finding_command("status", phase=phase)))

    def start_repair(self, phase: str = "7") -> None:
        _ = self.finding_command("open", "--severity", "blocker", "--title", "Repair the worker",
                                 "--caught-by", "main", phase=phase)
        _ = self.finding_command("dispatch", "--covers", "F001", "--accepted-checks",
                                 "none: the fixture has no accepted behavior", phase=phase)

    def test_a_clean_fix_resolves_only_its_named_phase(self) -> None:
        self.start_repair()
        self.record("start-phase", "--phase-id", "8", "--phase-title", "Other repairs")
        self.start_repair("8")
        other = self.repair_state("8")
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "1"}):
            _ = self.successful("fix", execution=InProcessAssignment())
        repaired = self.repair_state()
        self.assertEqual(repaired["rounds"][-1]["outcome"], "landed")
        self.assertEqual(repaired["findings"][0]["state"], "fixed_pending_review")
        self.assertEqual(self.repair_state("8"), other)
        self.assertEqual(self.turn_starts, 1)

    def test_a_failed_fix_abandons_the_round_without_refunding_the_attempt(self) -> None:
        self.start_repair()
        self.native.agent.during_prompt = fail_worker_after_prompt
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "1"}):
            result = self.launch("fix", execution=InProcessAssignment())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        repaired = self.repair_state()
        self.assertEqual(repaired["rounds"][-1]["outcome"], "abandoned")
        self.assertTrue(repaired["rounds"][-1]["edits_landed"])
        self.assertEqual(repaired["findings"][0]["state"], "open")
        self.assertEqual(repaired["findings"][0]["fix_attempts"], 1)

    def test_failed_fix_closes_the_remapped_pass_when_an_earlier_slot_shares_its_command(self) -> None:
        self.start_repair()
        first = self.native.opened("fix", "fix")
        second = self.native.opened("fix2", "fix")
        arguments = ["--role", "fix", "--cwd", str(self.project), "--phase", "7",
                     "--prompt", str(self.prompt), "--paths", str(self.paths),
                     "--session-dir", str(self.session_dir), "--fix-cause", "Repair the worker"]
        _ = progress_history.begin_dispatch(self.session_dir, slot="fix", phase="7", role="fix",
            worker_name="fix", worker_id=first.row.id, launcher_command=["bash", str(ASSIGN), *arguments],
            prompt=self.prompt, kind="assign", lens="", fix_cause="Earlier repair")
        self.native.fixture.world.locations[first.pane]["agent_status"] = "working"
        phase = self.session_dir / "phases/7"
        status = phase / "fix_status"
        write_text_atomic(status, "running\n")
        original = status.read_bytes(), status.stat().st_ino
        self.native.agent.during_prompt = fail_worker_after_prompt
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "1"}):
            result = invoke_assignment(arguments, os.environ.copy())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([pane for pane, _ in self.native.agent.submitted], [second.pane])
        self.assertEqual((status.read_bytes(), status.stat().st_ino), original)
        self.assertEqual((phase / "fix2_status").read_text(), "error\n")
        self.assertEqual(worker_panes.worker_pass(first.row, self.session_dir)["status"], "active")
        self.assertEqual(worker_panes.worker_pass(second.row, self.session_dir)["status"], "error")
        self.assertEqual(self.repair_state()["rounds"][-1]["outcome"], "abandoned")

    def test_a_fix_with_round_resolution_disabled_keeps_the_round_in_flight(self) -> None:
        self.start_repair()
        self.native.agent.finish_after = 100
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "0"}):
            _ = self.successful("fix", execution=InProcessAssignment())
        self.assertEqual(self.repair_state()["in_flight"], ["F001"])
        with self.subTest(retry="the repair round ends after preflight"):
            prompt = Path(str(pass_record.pass_slots(pass_record.read_state(self.session_dir))["fix"]["prompt_file"]))
            write_text_atomic(prompt, "Damaged prompt.\n")
            claim_phase = cast(Callable[..., bool], assign_claude.claim_phase)

            def abandon_round(*args: object, **kwargs: object) -> bool:
                _ = self.finding_command("abandon", "--reason", "The repair round ended.")
                return claim_phase(*args, **kwargs)

            with patch.object(assign_claude, "claim_phase", side_effect=abandon_round):
                refused = self.launch("fix")
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("Fix launch has no ledger round in flight", refused.stderr)
            self.assertEqual(prompt.read_text(), "Damaged prompt.\n")
            self.assertEqual(self.turn_starts, 1)

    def test_an_implementation_dispatch_does_not_resolve_a_repair_round(self) -> None:
        self.start_repair()
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "1"}):
            _ = self.successful(execution=InProcessAssignment())
        self.assertEqual(self.repair_state()["in_flight"], ["F001"])

    def run_resolution_commands(self, stderr: str) -> subprocess.CompletedProcess[str]:
        printed = stderr.split("assign.sh: repair resolution failed; run these commands:\n", 1)[1]
        commands = "\n".join(line for line in printed.splitlines() if line.startswith("env "))
        environment = os.environ.copy()
        _ = environment.pop("PYTHONPATH", None)
        return subprocess.run(["bash", "-e", "-c", commands], env=environment, cwd=self.session_dir,
                              capture_output=True, text=True)

    def complete_repair_after_ledger_failure(self, worker_failed: bool) -> None:
        self.start_repair()
        if worker_failed:
            self.native.agent.during_prompt = fail_worker_after_prompt
        ledger = self.session_dir / "findings_state.json"
        saved = self.session_dir / "saved-findings.json"
        self.instrument_launcher("\n".join((
            "from pathlib import Path",
            "from scripts.direct import assign",
            "original_call = assign.call",
            "def call(command, env, **options):",
            f"    ledger, saved = Path({str(ledger)!r}), Path({str(saved)!r})",
            "    if any(verb in command for verb in ('landed', 'abandon')) and not saved.exists():",
            "        ledger.rename(saved)",
            "        ledger.mkdir()",
            "    return original_call(command, env, **options)",
            "assign.call = call",
        )))
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "1"}):
            result = self.launch("fix")
        self.assertEqual(result.returncode, 1 if worker_failed else 0, result.stdout + result.stderr)
        details = result.stderr if worker_failed else self.native.agent.notices[-1]
        self.assertIn("repair resolution failed", details)
        name = next(iter(self.assignment_records()))
        status = self.session_dir / f"phases/7/{name}_status"
        self.assertEqual(status.read_text(), "error\n")
        ended = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual([event["status"] for event in ended], ["error"])
        ledger.rmdir()
        _ = saved.rename(ledger)
        repaired = self.run_resolution_commands(details)
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(self.repair_state()["rounds"][-1]["outcome"], "abandoned" if worker_failed else "landed")
        self.assertEqual(status.read_text(), "error\n")

    def test_manual_resolution_lands_a_clean_fix_without_another_worker_turn(self) -> None:
        self.complete_repair_after_ledger_failure(False)

    def test_manual_resolution_abandons_a_failed_fix_without_another_worker_turn(self) -> None:
        self.complete_repair_after_ledger_failure(True)

    def test_a_fix_without_an_open_pass_resolves_without_finishing_a_pass(self) -> None:
        self.start_repair()
        directory = self.session_dir / "phases/7"
        directory.mkdir(parents=True, exist_ok=True)
        summary, status, reply = (directory / name for name in ("fix-summary.txt", "fix-status", "fix-reply.txt"))
        _ = summary.write_text("The worker failed before its pass opened.\n")
        args = assign.Arguments()
        args.role = "fix"
        dispatch = assign.AssignmentResult(args, self.session_dir, "7", "fix", "unstarted-fix",
                                   {**os.environ, "UNIT_RESOLVES_ROUND": "1"})
        finished: list[list[str]] = []
        original_call = assign.call

        def observe(command: list[str], env: dict[str, str], *, capture: bool = False,
                    check: bool = True) -> subprocess.CompletedProcess[str]:
            if any("finish-pass" in arg for arg in command):
                finished.append(command)
            return original_call(command, env, capture=capture, check=check)

        with patch.object(assign, "call", observe):
            result = assign.settle_dispatch(dispatch, 2, False, "unstarted-fix", status, summary, reply)
        self.assertEqual(result, 2)
        self.assertEqual(self.repair_state()["rounds"][-1]["outcome"], "abandoned")
        self.assertEqual(finished, [])
        self.assertEqual(status.read_text(), "error\n")

    def test_each_pass_records_the_staff_id_from_the_worker_roster(self) -> None:
        _ = self.successful(execution=InProcessAssignment())
        name, worker = next(iter(self.assignment_records().items()))
        passes = [event for event in self.events() if event.get("event_type") == "pass_started"]
        self.assertEqual([event["worker_id"] for event in passes], [worker["staff_id"]])
        self.assertEqual([event["worker_name"] for event in passes], [name])
        command = cast(list[str], passes[0]["launcher_command"])
        self.assertEqual(command[:4], ["bash", str(ASSIGN), "--role", "implement"])
        self.assertEqual(command[command.index("--session-dir") + 1], str(self.session_dir))

    def test_only_a_live_claude_or_codex_ancestor_permits_assignment(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                result = self.run_launcher(["bash", str(ASSIGN), "--help"], family=family)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for family in ("systemd", "bash", "codex-helper"):
            with self.subTest(parent=family):
                result = self.run_launcher(["bash", str(ASSIGN), "--help"], family=family,
                                           isolate_ancestors=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("requires a live Claude or Codex ancestor", result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_dispatch_has_a_durable_worker_row_before_submitting_work(self) -> None:
        observed: list[staff.StaffRow] = []
        def before_prompt() -> None:
            observed.extend(staff.snapshot().find(kind="worker", parent=self.native.fixture.parent.id))
        self.native.agent.during_prompt = before_prompt
        _ = self.successful()
        row, = observed
        self.assertEqual(row.name, "impl")
        self.assertEqual(self.assignment_records()[row.name]["staff_id"], row.id)
        self.assertEqual(self.turn_starts, 1)

    def test_assignment_dispatch_uses_the_backend_thread_as_the_published_worker_id(self) -> None:
        identities: list[str] = []

        def observe_process() -> None:
            world = self.native.fixture.world
            pane = self.native.agent.submitted[-1][0]
            identities.append(world.processes[world.foreground[pane][0]].current_identity)

        self.native.agent.during_prompt = observe_process
        _ = self.successful()
        name, saved = next(iter(self.assignment_records().items()))
        row = staff.by_id(staff.StaffId(str(saved["staff_id"])))
        assert row is not None
        self.assertEqual(identities, [row.current_session])
        self.assertEqual(row.name, name)

    def test_unreadable_staff_starts_no_worker(self) -> None:
        record = Path(os.environ["STAFF_FILE"])
        _ = record.write_text("{broken")
        result = self.launch(execution=InProcessAssignment())
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("invalid staff JSON:", result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])
        self.assertEqual(record.read_text(), "{broken")

    def test_rerun_review_dispatch_keeps_its_lens_and_read_only_thread(self) -> None:
        _ = self.successful("review", "--lens", "rerun", "--pass", "1", execution=InProcessAssignment())
        self.assertEqual(self.native.fixture.world.requests[0][1][self.native.fixture.world.requests[0][1].index("--sandbox") + 1], "read-only")
        self.assertTrue((self.session_dir / "phases/7/review_status_rerun_1").exists())

    def test_recorder_ad_hoc_phase_dispatches_into_adhoc_directory(self) -> None:
        self.record("start-phase", "--phase-id", "ad hoc", "--phase-title", "Ad hoc review")
        self.write_phase_prompt("ad hoc", "Ad hoc review")
        _ = self.successful("review", "--phase", "ad hoc", execution=InProcessAssignment())
        phase = self.session_dir / "phases/adhoc"
        self.assertEqual((phase / "review_status_1").read_text(), "done\n")
        passed = assign.object_map(assign.object_map(read_object(self.session_dir / unit_runs.STATE_FILE)["pass"])["review"])
        prompt = Path(str(passed["prompt_file"]))
        self.assertEqual(prompt.parent, phase)
        self.assertTrue(prompt.is_file())
        self.assertTrue((phase / "review_summary_1.txt").read_text().strip())
        self.assertFalse((self.session_dir / "phases/ad hoc").exists())
        self.assertEqual(self.native.fixture.world.requests[0][1][self.native.fixture.world.requests[0][1].index("--sandbox") + 1], "read-only")
        self.assertEqual(len(self.native.agent.submitted), 1)

    def test_unsafe_phase_ids_are_refused_before_opening_a_thread(self) -> None:
        for phase in (".", "..", "../escape", "nested/phase", "other phase"):
            with self.subTest(phase=phase):
                result = self.launch("review", "--phase", phase, execution=InProcessAssignment())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("invalid phase id:", result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_writer_paths_are_required_and_review_paths_are_refused(self) -> None:
        for role in ("implement", "test", "fix"):
            with self.subTest(role=role):
                self.assertEqual(self.launch(role, paths=False, execution=InProcessAssignment()).returncode, 2)
        self.assertEqual(self.launch("review", "--paths", str(self.paths), execution=InProcessAssignment()).returncode, 2)
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_unsafe_empty_and_escaping_paths_refuse_before_claim(self) -> None:
        outside = self.project.parent / "outside"
        outside.mkdir()
        (self.project / "linked").symlink_to(outside, target_is_directory=True)
        for path in ("", "../secret", "/tmp/secret", "src/../secret", "linked/secret"):
            with self.subTest(path=path):
                _ = self.paths.write_text(path + "\n")
                result = self.launch(execution=InProcessAssignment())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                if path:
                    self.assertIn(path, result.stdout + result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])

    def test_writer_dispatch_records_paths_rules_identity_and_a_finished_pass(self) -> None:
        reply_file = self.project / "worker reply.md"
        for option, value in (("--reply-file", "Work completes without findings."),
                              ("--reply-file", "relative-reply.md"),
                              ("--reply-file", ""),
                              ("--summary", str(reply_file))):
            with self.subTest(option=option, value=value):
                files = {path: path.read_bytes() for path in self.project.rglob("*") if path.is_file()}
                submitted = self.native.agent.submitted.copy()
                launched = self.native.fixture.world.launches.copy()
                result = self.launch("implement", option, value, execution=InProcessAssignment())
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                if option == "--reply-file":
                    self.assertEqual(result.stderr.splitlines(), [
                        f"assign: --reply-file takes an absolute path to the reply file, not text: {value}"])
                else:
                    self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                    self.assertIn("--reply-file", result.stderr)
                self.assertEqual(self.native.agent.submitted, submitted)
                self.assertEqual(self.native.fixture.world.launches, launched)
                self.assertEqual({path: path.read_bytes() for path in self.project.rglob("*") if path.is_file()}, files)
        manifest = self.session_dir / "phases/7/dispatch_1_paths.json"

        def check_admission() -> None:
            self.assertEqual(read_object(manifest)["paths"], ["src/worker.py", "tests/test_worker.py"])
            self.assertIn("[impl] register:", (self.session_dir / "board.log").read_text())

        self.native.agent.during_prompt = check_admission
        _ = self.successful("implement", "--reply-file", str(reply_file))
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(self.turn_starts, 1)
        passed = assign.object_map(assign.object_map(read_object(self.session_dir / unit_runs.STATE_FILE)["pass"])[name])
        prompt = Path(str(passed["prompt_file"])).read_text()
        self.assertIn(self.prompt.read_text().strip(), prompt)
        for text in ("detached_runs.py start", "--kind python-tests before --", "Simple, Fast and Beautiful", "poll"):
            self.assertIn(text, prompt)
        self.assertEqual(read_object(manifest), {"worker": name, "round": 1,
                                               "paths": ["src/worker.py", "tests/test_worker.py"],
                                               "pass_instance_id": passed["instance_id"]})
        self.assertEqual((manifest.parent / f"{name}_status").read_text().strip(), "done")
        self.assertTrue((manifest.parent / f"{name}_summary.txt").read_text().strip())
        self.assertEqual(reply_file.read_text(), (manifest.parent / f"{name}_summary.txt").read_text())
        self.assertTrue((manifest.parent / f"{name}_agent.log").is_file())
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        started = next(event for event in self.events() if event.get("event_type") == "pass_started")
        self.assertEqual(started["worker_id"], record["staff_id"])

        run_command = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
        start_pass = pass_record.start_pass
        write_manifest = atomic_file.write_text_atomic
        interrupted_ticket = "style-ticket-interrupted"
        legacy_ticket = "style-ticket-legacy"
        widened_ticket = "style-ticket-widened"
        unregistered_worker = "impl-board-retry"
        widened = "src/widened.py"
        blocked_board = self.session_dir.parent / "blocked-board"
        (blocked_board / "board.log").mkdir(parents=True)
        interrupted = False
        widen_on_launch = False
        blocked_registration = ""

        def check_worker_arguments(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            nonlocal widen_on_launch
            if len(command) > 2 and Path(command[1]).name == "worker.py" and command[2] == "assign":
                _ = run_command([sys.executable, "-c", "pass", *command[2:]], check=True)
                if widen_on_launch:
                    widen_on_launch = False
                    style_plan.widen_live_dispatch(self.session_dir, "legacy", (widened,))
            if (len(command) > 5 and Path(command[1]).name == "board.sh"
                    and command[4:6] == [blocked_registration, "register"]):
                return run_command([*command[:3], str(blocked_board), *command[4:]], capture_output=True, **options)
            return run_command(command, **options)

        def start_with_ticket_lock(args: pass_record.WorkerDispatchRequest,
                              selection: pass_record.DispatchSelection | pass_record.SelectDispatchAtRecorder
                              ) -> str:
            instance = start_pass(args, selection)
            if args.team_slot.startswith("style-ticket-"):
                self.assert_lock_held(manifest.parent / "ticket-paths.lock")
            return instance

        def write_with_ticket_lock(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
            nonlocal interrupted
            if path == manifest and (pass_record.json_object(text) or {}).get("worker") == interrupted_ticket:
                self.assert_lock_held(manifest.parent / "ticket-paths.lock")
                if not interrupted:
                    interrupted = True
                    raise OSError("Manifest publication interrupted.")
            write_manifest(path, text, new_file_mode=new_file_mode)

        def check_registration(name: str) -> None:
            board = (self.session_dir / "board.log").read_text().splitlines()
            self.assertEqual(sum(f"[{name}] register:" in line for line in board), 1)

        self.native.agent.during_prompt = lambda: None
        append_event = pass_record.append_event

        def interrupt_history(state: dict[str, object], event: dict[str, object]) -> None:
            if event.get("event_type") == "pass_started":
                raise OSError("History publication interrupted.")
            append_event(state, event)

        cases = ("stale manifest", interrupted_ticket, legacy_ticket, unregistered_worker,
                 widened_ticket, "interrupted history", "large paths", "invalid board bytes", "changed scope")
        for round_number, case in enumerate(cases, start=2):
            with self.subTest(admission=case):
                paths = self.paths.read_text().splitlines()
                manifest = manifest.parent / f"dispatch_{round_number}_paths.json"
                if case in ("stale manifest", interrupted_ticket):
                    write_text_atomic(manifest, json.dumps({"worker": "refused", "round": round_number, "paths": ["stale"]}))
                elif case == "large paths":
                    paths = [f"src/{index:04d}-{'x' * 48}.py" for index in range(3300)]
                    write_text_atomic(self.paths, "\n".join(paths) + "\n")
                elif case == "invalid board bytes":
                    with (self.session_dir / "board.log").open("ab") as board:
                        _ = board.write(b"Invalid board entry: \xff\n")
                worker_name = case if case.startswith("style-ticket-") or case == unregistered_worker else f"impl{round_number}"
                pass_id = ""
                admitted_prompt: tuple[Path, bytes] | None = None
                if case == "interrupted history":
                    _ = self.native.opened(worker_name)
                    with patch.object(pass_record, "append_event", side_effect=interrupt_history):
                        failed = self.launch("implement", "--to", worker_name)
                    self.assertEqual(failed.returncode, worker.ExitCode.UNCERTAIN, failed.stderr)
                    self.assertIn("History publication interrupted.", failed.stderr)
                    self.assertEqual(self.turn_starts, round_number - 1)
                    pass_id = str(pass_record.pass_slots(pass_record.read_state(self.session_dir))[worker_name]["instance_id"])
                with (patch.object(subprocess, "run", side_effect=check_worker_arguments),
                      patch.object(pass_record, "start_pass", side_effect=start_with_ticket_lock),
                      patch.object(atomic_file, "write_text_atomic", side_effect=write_with_ticket_lock)):
                    if case == interrupted_ticket:
                        refused = self.launch("implement", "--to", worker_name)
                        self.assertEqual(refused.returncode, worker.ExitCode.UNCERTAIN, refused.stderr)
                        self.assertIn("Manifest publication interrupted.", refused.stderr)
                        self.assertEqual(self.turn_starts, round_number - 1)
                    if case in (legacy_ticket, unregistered_worker, widened_ticket):
                        blocked_registration = worker_name
                        saved_manifest = b""
                        for attempt in range(2 if case == unregistered_worker else 1):
                            refused = self.launch("implement", "--to", worker_name)
                            self.assertEqual(refused.returncode, worker.ExitCode.UNCERTAIN, refused.stderr)
                            self.assertIn("unable to register the assignment on the board", refused.stderr)
                            self.assertEqual(self.turn_starts, round_number - 1)
                            if attempt:
                                self.assertEqual(manifest.read_bytes(), saved_manifest)
                            saved_manifest = manifest.read_bytes()
                        published = read_object(manifest)
                        pass_id = pass_record.string(published["pass_instance_id"])
                        if case == legacy_ticket:
                            del published["pass_instance_id"]
                            write_text_atomic(manifest, json.dumps(published))
                            widen_on_launch = True
                            paths.append(widened)
                        elif case == widened_ticket:
                            ticket_paths = manifest.with_name("ticket-paths.txt")
                            write_text_atomic(self.paths, "\n".join([*paths, widened]) + "\n")
                            refused = self.launch("implement", "--to", worker_name)
                            self.assertEqual(refused.returncode, worker.ExitCode.BUSY, refused.stderr)
                            self.assertEqual(manifest.read_bytes(), saved_manifest)
                            style_plan.widen_live_dispatch(self.session_dir, "widened", (widened,))
                            paths = ticket_paths.read_text().splitlines()
                            saved = pass_record.pass_slots(pass_record.read_state(self.session_dir))[worker_name]
                            saved_path = Path(str(saved["prompt_file"]))
                            admitted_prompt = saved_path, saved_path.read_bytes()
                            saved_path.unlink()
                        blocked_registration = ""
                        self.native.agent.during_prompt = lambda: check_registration(worker_name)
                    _ = self.successful("implement", "--to", worker_name)
                    self.native.agent.during_prompt = lambda: None
                passed = pass_record.pass_slots(read_object(self.session_dir / unit_runs.STATE_FILE))[worker_name]
                if pass_id:
                    self.assertEqual(passed["instance_id"], pass_id)
                self.assertEqual(read_object(manifest), {"worker": worker_name, "round": round_number,
                                                       "paths": paths, "pass_instance_id": passed["instance_id"]})
                if admitted_prompt is not None:
                    _ = self.successful("implement", "--to", worker_name)
                    self.assertEqual(admitted_prompt[0].read_bytes(), admitted_prompt[1])
                    self.assertEqual(self.turn_starts, round_number)
                if case == "changed scope":
                    write_text_atomic(self.paths, "src/worker.py\n")
                    _ = self.successful("implement", "--to", worker_name)
                    self.assertEqual(self.turn_starts, round_number + 1)
                    updated = pass_record.pass_slots(pass_record.read_state(self.session_dir))[worker_name]
                    self.assertNotEqual(updated["instance_id"], passed["instance_id"])

    def test_review_is_read_only_and_thread_board_and_summary_use_the_shown_context(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
        name = next(iter(self.assignment_records()))
        command = self.native.fixture.world.requests[0][1]
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        board = (self.session_dir / "board.log").read_text()
        self.assertIn(f"[{name}]", board)
        summary = self.session_dir / f"phases/7/{name}_summary_contract_1.txt"
        self.assertIn("fixture", summary.read_text())
        self.assertEqual((summary.parent / f"{name}_status_contract_1").read_text(), "done\n")

    def test_style_assignment_is_prepared_and_captured_through_assign_on_retry(self) -> None:
        self.prepare_style_project()
        _ = (self.project / "new.rs").write_text("fn first() {}\n")
        outcomes = ["Error", "Findings"]
        prepared: list[Path] = []
        def style_reply() -> None:
            state = read_object(self.session_dir / "phases/7/style_review_state.json")
            directory = Path(str(state["input_directory"]))
            prompt = (directory / "prompt.md").read_text()
            self.assertEqual(prompt.count("## Phase style review"), 1)
            self.assertNotIn("write your summary to", prompt)
            diff = Path(next(line for line in prompt.splitlines() if line.startswith("- Diff ")).split(": ", 1)[1])
            self.assertIn("+fn ", diff.read_text())
            prepared.append(diff)
            self.native.agent.final = ("1. minor: fixture finding at new.rs:1\n" + style_review.BEGIN +
                "\n| # | Rule | Result |\n| 1 | Fixture | Pass |\nStyle review: " + outcomes.pop(0) + "\n" + style_review.END)
        self.native.agent.during_prompt = style_reply
        phase = self.session_dir / "phases/7"
        _ = self.successful("review", "--lens", "contract", "--style-since", "HEAD")
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "owed")
        name, record = next(iter(self.assignment_records().items()))
        before = prepared[0].read_bytes()
        _ = (self.project / "new.rs").write_text("fn second() {}\n")
        _ = self.successful("review", "--to", name, "--pass", "2", "--style-since", "HEAD")
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "complete")
        self.assertIn("Style review: Findings", (phase / "phase_style_review.md").read_text())
        self.assertIn("fixture finding", (phase / f"{name}_summary_2.txt").read_text())
        self.assertNotEqual(prepared[0].parent, prepared[1].parent)
        self.assertEqual(prepared[0].read_bytes(), before)
        self.assertIn("+fn second", prepared[1].read_text())
        self.assertEqual(self.assignment_records()[name]["thread_id"], record["thread_id"])
        command = self.native.fixture.world.requests[-1][1]
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")

    def test_style_preparation_failure_through_assign_keeps_code_review_running(self) -> None:
        self.prepare_style_project()
        _ = self.successful("review", "--style-since", "missing-baseline", execution=InProcessAssignment())
        phase = self.session_dir / "phases/7"
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "owed")
        self.assertIn("invalid baseline", (phase / "phase_style_review.md").read_text())
        self.assertEqual((phase / "review_status_1").read_text().strip(), "done")
        self.assertEqual(len(self.native.agent.submitted), 1)

    def test_assign_style_range_excludes_other_work_in_flight(self) -> None:
        self.prepare_style_project()
        baseline = style_review.git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        _ = (self.project / "included.txt").write_text("pinned addition\n")
        self.commit_project("pinned range")
        end = style_review.git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        _ = (self.project / "included.txt").write_text("outside the range\n")
        _ = (self.project / "untracked.rs").write_text("fn outside() {}\n")
        _ = self.successful("review", "--style-since", baseline, "--style-until", end, execution=InProcessAssignment())
        phase = self.session_dir / "phases/7"
        state = read_object(phase / "style_review_state.json")
        directory = Path(str(state["input_directory"]))
        self.assertEqual(state["baseline"], baseline)
        self.assertEqual(state["until"], end)
        self.assertEqual((directory / "style-review-additions.txt").read_text(), "+pinned addition\n")
        self.assertIn("- Rust change: no", (directory / "prompt.md").read_text())
        self.assertIn(f"- Range: {baseline}..{end}", (directory / "prompt.md").read_text())

    def test_missing_run_calls_prepare_and_prints_the_prepared_folder(self) -> None:
        prepared = self.prepare_launcher()
        environment = os.environ.copy()
        for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR"):
            _ = environment.pop(key, None)
        with patch.dict(os.environ, environment, clear=True):
            result = self.launch(session=False, execution=InProcessAssignment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(prepared.exists())
        self.assertIn(str(self.session_dir), result.stdout)
        self.assertEqual(self.turn_starts, 1)

    def test_a_fresh_run_takes_its_phase_title_from_the_dispatch_prompt(self) -> None:
        _ = staff.clear_run(self.native.fixture.parent.id, self.session_dir)
        (self.session_dir / "progress_history_state.json").unlink()
        shutil.rmtree(self.history)
        shutil.rmtree(self.session_dir / "phases", ignore_errors=True)
        prepared = self.prepare_launcher()
        director = os.environ["CODEX_THREAD_ID"]
        transcript = Path.home() / ".codex/sessions" / f"rollout-{director}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text("\n".join(json.dumps(record) for record in (
            {"type": "session_meta", "payload": {"id": director}},
            {"type": "turn_context", "payload": {"model": "fixture-director", "effort": "high"}},
        )) + "\n")
        self.addCleanup(transcript.unlink, missing_ok=True)
        environment = os.environ.copy()
        for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR"):
            _ = environment.pop(key, None)
        environment["CODEX_THREAD_ID"] = director
        with patch.dict(os.environ, environment, clear=True):
            result = self.launch(session=False, execution=InProcessAssignment())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(prepared.exists())
        phase = cast(dict[str, object], cast(list[object], read_object(self.session_dir / "progress_history_state.json")["open_phases"])[0])
        self.assertEqual((phase["id"], phase["title"]), ("7", "Assignment core"))
        self.assertEqual(self.turn_starts, 1)
        self.assertTrue(any(event.get("event_type") == "pass_finished" for event in self.events()))

    def test_default_session_uses_the_callers_marker_instead_of_starting_another_run(self) -> None:
        caller = os.environ["CODEX_THREAD_ID"]
        _ = staff.set_run(self.native.fixture.parent.id, staff.OpenRun(self.session_dir))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": caller}):
            result = self.launch(session=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(unit_runs.open_run(caller), self.session_dir)

    def test_fix_requires_a_cause_and_a_wrong_phase_prompt_never_claims_the_worker(self) -> None:
        refused = self.launch("fix", execution=InProcessAssignment())
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(self.turn_starts, 0)
        _ = self.successful("fix", "--fix-cause", "repair failed acceptance gate", execution=InProcessAssignment())
        self.assertTrue(any(event.get("pass_kind") == "fix" for event in self.events()))
        _ = self.prompt.write_text("Phase 6 — Another phase\nDo the wrong work.\n")
        result = self.launch(execution=InProcessAssignment())
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)

    def test_busy_launch_preserves_the_completed_slot_status(self) -> None:
        _ = self.successful()
        status = self.session_dir / "phases/7/impl_status"
        self.assertEqual(status.read_text(), "done\n")
        saved = status.read_bytes(), status.stat().st_ino
        phase = status.parent
        manifests = {path: path.read_bytes() for path in phase.glob("dispatch_*_paths.json")}
        self.write_phase_prompt("7", "Assignment core", "Implement the second change.")
        with worker_panes.worker_lock(self.native.fixture.parent.id, staff.WorkerName.parse("impl")) as hold:
            self.assertIsInstance(hold, worker_panes.WorkerLockHold)
            result = self.launch()
        self.assertEqual(result.returncode, worker.ExitCode.BUSY, result.stdout + result.stderr)
        self.assertEqual((status.read_bytes(), status.stat().st_ino), saved)
        self.assertEqual({path: path.read_bytes() for path in phase.glob("dispatch_*_paths.json")}, manifests)
        self.assertEqual(self.turn_starts, 1)
        self.assertIn("role=impl; exit 6", (self.session_dir / "board.log").read_text())

        self.native.agent.finish_after = 100
        _ = self.successful("implement", "--to", "impl")
        pane = self.native.agent.submitted[-1][0]
        self.native.fixture.world.locations[pane]["agent_status"] = "idle"
        log = phase / "impl_agent.log"
        saved_files = {path: path.read_bytes() for path in phase.iterdir() if path.is_file() and path != log}
        run_files = (self.session_dir / "heartbeat.log", self.session_dir / unit_runs.STATE_FILE)
        saved_run_files = {path: path.read_bytes() for path in run_files}
        original_order = self.prompt.read_text()
        self.write_phase_prompt("7", "Assignment core", "Implement the third change.")
        _ = self.paths.write_text("tests/test_worker.py\n")
        arguments = ["--role", "implement", "--cwd", str(self.project), "--phase", "7", "--prompt", str(self.prompt),
                     "--paths", str(self.paths), "--session-dir", str(self.session_dir),
                     "--reply-file", str(self.project / "report.txt")]
        for target, change in (([], "both"), (["--to", "impl"], "both"),
                               (["--new-worker", "impl"], "both"), (["--to", "impl"], "paths only")):
            with self.subTest(target=target, change=change):
                if change == "paths only":
                    write_text_atomic(self.prompt, original_order)
                refused = invoke_assignment(arguments + target, os.environ.copy())
                self.assertEqual(refused.returncode, worker.ExitCode.BUSY, refused.stdout + refused.stderr)
                self.assertIn("has different prompt content", refused.stderr)
                self.assertEqual({path: path.read_bytes() for path in phase.iterdir()
                                  if path.is_file() and path != log}, saved_files)
                self.assertEqual({path: path.read_bytes() for path in run_files}, saved_run_files)
                self.assertEqual(self.turn_starts, 2)
        with self.subTest(target="named worker with a status file to move"):
            source = phase / "requested_status"
            write_text_atomic(source, "requested worker status\n")
            direct = ["assign", "impl", str(self.prompt), "--session-dir", str(self.session_dir),
                      "--phase", "7", "--slot", "requested", "--kind", "assign", "--name", "impl",
                      "--launcher-command", json.dumps(["assign.sh", "requested"]),
                      "--completion-actions", json.dumps({"status_file": str(source)})]
            with worker_panes.worker_lock(self.native.fixture.parent.id, staff.WorkerName.parse("impl")) as hold:
                self.assertIsInstance(hold, worker_panes.WorkerLockHold)
                refused = invoke_cli(worker, lambda: worker.main(direct), direct, os.environ.copy(), self.project)
            self.assertEqual(refused.returncode, worker.ExitCode.BUSY, refused.stdout + refused.stderr)
            self.assertEqual({path: path.read_bytes() for path in phase.iterdir() if path.is_file() and path != log},
                             {**saved_files, source: b"requested worker status\n"})
            self.assertEqual({path: path.read_bytes() for path in run_files}, saved_run_files)
            self.assertEqual(self.turn_starts, 2)
        self.native.agent.complete(pane)
        self.assertEqual(self.native.finish("impl"), turn_end.TurnOutcome.DONE)

    def test_a_failed_worker_records_an_error_pass_and_returns_worker_failure(self) -> None:
        status = self.session_dir / "phases/7/impl_status"

        def fail_owned_launch() -> None:
            self.assertEqual(status.read_text(), "running\n")
            fail_worker_after_prompt()

        self.native.agent.during_prompt = fail_owned_launch
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(status.read_text(), "error\n")
        ended = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(ended), 1, self.events())
        self.assertEqual(ended[0].get("status"), "error")
        self.assertNotIn("launcher_pid", next(iter(self.assignment_records().values())))

    def test_confirmed_retry_keeps_running_status_after_a_delayed_launch_failure(self) -> None:
        opened = self.native.opened()
        self.native.agent.error = "agent_prompt_stalled"
        failed = self.launch()
        self.assertEqual(failed.returncode, worker.ExitCode.UNCERTAIN, failed.stdout + failed.stderr)
        status = self.session_dir / "phases/7/impl_status"
        self.assertEqual(status.read_text(), "error\n")
        passed = worker_panes.worker_pass(opened.row, self.session_dir)
        self.assertEqual(passed["status"], "active")
        self.native.agent.error = ""
        _ = self.successful()
        self.assertEqual(self.turn_starts, 1)
        confirmed = worker_panes.worker_pass(opened.row, self.session_dir)
        self.assertEqual(confirmed["instance_id"], passed["instance_id"])
        self.assertEqual(confirmed["status"], "active")
        events = self.events()
        recorded = progress_history.record_assignment_launch_exit(
            self.session_dir, UUID(str(confirmed["instance_id"])), progress_history.AssignmentLaunchExit.CLOSE_FAILED_PASS,
        )
        self.assertEqual(recorded, progress_history.RecordedAssignmentLaunch("impl", failure_recorded=False))
        self.assertEqual(worker_panes.worker_pass(opened.row, self.session_dir), confirmed)
        self.assertEqual(self.events(), events)
        self.assertEqual(status.read_text(), "running\n")
        self.native.agent.complete(opened.pane)
        self.assertEqual(self.native.finish("impl"), turn_end.TurnOutcome.DONE)
        self.assertEqual(status.read_text(), "done\n")

    def test_failed_launcher_waits_for_the_selected_worker_to_confirm_submission(self) -> None:
        self.assert_failed_launcher_waits_for_confirmation("impl2")

    def test_failed_launcher_waits_for_the_renamed_worker_to_confirm_submission(self) -> None:
        self.assert_failed_launcher_waits_for_confirmation("impl3")

    def assert_failed_launcher_waits_for_confirmation(self, current_name: str) -> None:
        opened = self.native.opened("impl2")
        parent = self.native.fixture.parent.id
        name = staff.WorkerName.parse(opened.row.name)
        status = self.session_dir / "impl2_status"
        with patch.dict(os.environ, {progress_history.ASSIGN_STATUS_FILE_ENV: str(status)}):
            instance = progress_history.begin_dispatch(self.session_dir, slot=name.value, phase="7", role="impl",
                worker_name=name.value, worker_id=opened.row.id, launcher_command=["assign.sh"],
                prompt=self.prompt, kind="assign", lens="")
        row = opened.row
        if current_name != name.value:
            with patch.object(herdr.Herdr, "retitle_row"):
                row = staff.rename(row.id, current_name)
        events = self.events()
        acquire = worker_panes.worker_id_lock

        with ExitStack() as submission:
            held = submission.enter_context(worker_panes.worker_lock(parent, staff.WorkerName.parse(row.name)))
            assert isinstance(held, worker_panes.WorkerLockHold)
            self.assertTrue(held.owns(row))

            @contextmanager
            def finish_submission_before_acquiring(identity: staff.StaffId, *,
                                                   blocking: bool = False) -> Generator[None]:
                self.assertEqual((identity, blocking), (row.id, True))
                with self.assertRaises(BlockingIOError), acquire(identity):
                    self.fail("The retry must hold the fixed-ID lock.")
                self.assertEqual(self.events(), events)
                progress_history.confirm_assignment_submission(self.session_dir, instance, instance)
                submission.close()
                with acquire(identity):
                    yield

            with patch.object(worker_panes, "worker_id_lock", new=finish_submission_before_acquiring):
                recorded = assign.record_worker_launch_exit(
                    self.session_dir, UUID(instance), progress_history.AssignmentLaunchExit.CLOSE_FAILED_PASS,
                )
            self.assertFalse(held.active)

        self.assertEqual(recorded, progress_history.RecordedAssignmentLaunch(name.value, failure_recorded=False))
        passed = worker_panes.worker_pass(row, self.session_dir)
        self.assertEqual((passed["instance_id"], passed["status"], passed["confirmed_delivery_ids"]),
                         (instance, "active", [instance]))
        self.assertEqual(status.read_text(), "running\n")
        self.assertEqual(self.events(), events)

    def test_retry_saves_the_remapped_status_path_for_an_older_pass_without_delivery(self) -> None:
        self.assert_retry_saves_remapped_status_path("active")

    def test_retry_saves_the_remapped_status_path_for_a_refused_pass_without_delivery(self) -> None:
        self.assert_retry_saves_remapped_status_path("refused")

    def assert_retry_saves_remapped_status_path(self, pass_status: Literal["active", "refused"]) -> None:
        opened = self.native.opened("impl2")
        arguments = ["--role", "implement", "--cwd", str(self.project), "--phase", "7",
                     "--prompt", str(self.prompt), "--paths", str(self.paths), "--session-dir", str(self.session_dir)]
        with patch.object(progress_history, "save_dispatch_delivery", side_effect=SystemExit(worker.ExitCode.UNCERTAIN)):
            failed = invoke_assignment(arguments, os.environ.copy())
        self.assertEqual(failed.returncode, worker.ExitCode.UNCERTAIN, failed.stdout + failed.stderr)
        self.assertEqual(self.turn_starts, 0)
        state_path = self.session_dir / unit_runs.STATE_FILE
        state = read_object(state_path)
        passed = assign.object_map(assign.object_map(state["pass"])["impl2"])
        self.assertNotIn("delivery", passed)
        status = Path(str(passed.pop("assign_status_file")))
        self.assertEqual(status, self.session_dir / "phases/7/impl2_status")
        passed["status"] = pass_status
        if pass_status == "active":
            _ = passed.pop("dispatch_number")
        write_text_atomic(state_path, json.dumps(state))
        write_text_atomic(status, "running\n")
        board = self.session_dir / "board.log"
        if pass_status == "active":
            write_text_atomic(board, board.read_text().replace(f"pass={passed['instance_id']}; ", ""))
        location = self.native.fixture.world.locations[opened.pane]
        location["agent_status"] = "working"
        self.native.fixture.world.first_turn_timeout = True
        saved_state = state_path.read_bytes()
        manifest = status.parent / "dispatch_1_paths.json"
        published = read_object(manifest)
        published["paths"] = [*pass_record.string_list(published["paths"]), "src/widened.py"]
        write_text_atomic(manifest, json.dumps(published))
        saved_manifest = manifest.read_bytes()
        prompt = Path(str(passed["prompt_file"]))
        content = prompt.read_bytes()
        prompt.unlink()
        refused = invoke_assignment(arguments, os.environ.copy())
        self.assertEqual(refused.returncode, worker.ExitCode.BUSY, refused.stdout + refused.stderr)
        self.assertEqual(state_path.read_bytes(), saved_state)
        self.assertEqual(manifest.read_bytes(), saved_manifest)
        self.assertEqual(set(manifest.parent.glob("dispatch_*_paths.json")), {manifest})
        self.assertEqual(self.turn_starts, 0)
        self.assertEqual(self.native.agent.submitted, [])
        location["agent_status"] = "idle"
        self.native.fixture.world.first_turn_timeout = False
        retried = invoke_assignment(arguments, os.environ.copy())
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        saved = worker_panes.worker_pass(opened.row, self.session_dir)
        self.assertEqual(prompt.read_bytes(), content)
        self.assertEqual(manifest.read_bytes(), saved_manifest)
        self.assertEqual(saved["instance_id"], passed["instance_id"])
        self.assertEqual(saved["assign_status_file"], str(status))
        self.assertEqual(progress_history.dispatch_delivery(saved).actions.status_file, str(status))
        self.assertEqual(sum("[impl2] register:" in line for line in board.read_text().splitlines()), 1)
        self.assertEqual(self.native.finish("impl2"), turn_end.TurnOutcome.DONE)
        self.assertEqual(status.read_text(), "done\n")
        self.assertFalse(status.with_name("impl_status").exists())
