"""Named assignment dispatches through the real recorder and loopback app-server."""
from __future__ import annotations

import fcntl
import json
import os
import select
import shutil
import subprocess
import sys
import time
import unittest
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Literal, TypedDict, cast, final, override
from unittest.mock import patch
from uuid import UUID

from scripts.lib.fixture_test_support import set_up_fixture
from scripts.agents import codex_test_support as codex_fixture
from scripts.direct import assign, phase_reservation, progress_history, recorder_state, remove_run_scratch
from scripts.direct.assign_claude_test_support import AssignClaudeLauncherFixture
from scripts.direct.assign_launcher_test_support import ASSIGN, ROOT, HookAssignmentFixture, InProcessAssignment, assignment_helpers, invoke_assignment, read_object
from scripts.direct.findings_test_support import invoke_findings
from scripts.direct.test_phase_reservation import make_fixture_worktree
from scripts.herdr import herdr, turn_end, worker_panes
from scripts.herdr.test_worker_panes import idle_transcript, live_detached_run
from scripts.lib import conf, unit_runs
from scripts.lint import style_review
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff, test_worker as worker_fixture, worker, worker_admission
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import fixture_session, fixture_runtime_id, fixture_worker, transcript_fixture
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
            "    print(os.environ.get('FIXTURE_WORKER_DIAGNOSTIC', 'The worker command failed at launch.'), file=sys.stderr)",
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
        for name, code, diagnostic in (
            ("worker kept", worker.ExitCode.KEPT, ""),
            ("helper exit before startup", worker.ExitCode.REFUSED, "compaction helper did not start: exit 1 before startup."),
            ("uncertain with worker lock held", worker.ExitCode.UNCERTAIN, "compaction helper submission is uncertain."),
        ):
            with self.subTest(name=name):
                case = FailedLaunchRunTests("runTest")
                case.setUp()
                try:
                    case.commit_project()
                    message = f"{diagnostic} Log {case.session_dir / 'compact-fixture.log'}" if diagnostic else ""
                    with patch.dict(os.environ, {"FIXTURE_WORKER_DIAGNOSTIC": message} if message else {}):
                        if code is worker.ExitCode.UNCERTAIN:
                            row = case.native.fixture.worker("impl", family="codex")
                            calls = case.failing_worker(code, after_pass_start=True)
                            result = case.launch_with_held_worker_lock(row.id)
                            passed = worker_panes.worker_pass(row, case.session_dir)
                            case.assertEqual(passed["status"], "active")
                            case.assertEqual((case.session_dir / "phases/7/impl_status").read_text(), "running\n")
                            case.assertEqual([event for event in case.events() if event["event_type"] == "pass_finished"], [])
                        else:
                            calls = case.failing_worker(code)
                            result = case.launch()
                    case.assert_run_active(result, code)
                    command = cast(list[str], json.loads(calls.read_text()))
                    case.assertEqual(command[1], "assign")
                    case.assertEqual(case.native.fixture.world.launches, [])
                    if message:
                        case.assertIn(message, result.stderr.splitlines())
                        case.assertIn(message, (case.session_dir / "phases/7/impl_agent.log").read_text())
                finally:
                    case.doCleanups()

    def launch_with_held_worker_lock(self, identity: staff.StaffId) -> subprocess.CompletedProcess[str]:
        timeout = conf.read("direct").number("worker_settle_seconds")
        source = """import select, sys
from scripts.herdr.worker_panes import worker_id_lock
from scripts.production.staff import StaffId
with worker_id_lock(StaffId(sys.argv[1])):
    print("locked", flush=True)
    select.select([sys.stdin], [], [], float(sys.argv[2]))
"""
        with subprocess.Popen([assign.PYTHON, "-c", source, str(identity), str(timeout)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as holder:
            assert holder.stdin is not None and holder.stdout is not None
            try:
                self.assertTrue(select.select([holder.stdout], [], [], timeout)[0], "The child must acquire the worker lock.")
                self.assertEqual(holder.stdout.readline(), "locked\n")
                result = self.launch()
                self.assertIsNone(holder.poll(), "The launcher must return before the child releases the lock.")
                return result
            finally:
                holder.stdin.close()

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

    def launch_astra_review(self, name: str) -> subprocess.CompletedProcess[str]:
        run = self.fixture.fixture.root / name
        prompt = run.with_suffix(".md")
        write_text_atomic(prompt, "Phase astra-review — Astra review\nReview the assigned change.\n")
        arguments = ["--role", "review", "--operation", "astra-review", "--new-worker", name,
                     "--cwd", str(self.fixture.fixture.root), "--session-dir", str(run),
                     "--prompt", str(prompt), "--reply-file", str(run / "review.md")]
        world = self.fixture.fixture.world
        with patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(world.process_root / "roles")}), world.process_starts():
            return invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)

    @contextmanager
    def held_review_lock(self, path: Path, command: str) -> Generator[BinaryIO]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b", buffering=0) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            holder = {"pid": os.getpid(), "command": command, "acquired_at": time.monotonic()}
            _ = lock.write((json.dumps(holder) + "\n").encode())
            yield lock

    def release_at_contention(self, lock: BinaryIO, *, blocking_only: bool = False) -> Callable[[int | BinaryIO, int], None]:
        flock = fcntl.flock
        def acquire(candidate: int | BinaryIO, mode: int) -> None:
            fd = candidate if isinstance(candidate, int) else candidate.fileno()
            blocking = not mode & fcntl.LOCK_NB
            if mode & fcntl.LOCK_EX and blocking == blocking_only and os.path.samestat(os.fstat(fd), os.fstat(lock.fileno())):
                try:
                    flock(candidate, mode | fcntl.LOCK_NB)
                except BlockingIOError:
                    flock(lock, fcntl.LOCK_UN)
                    if not blocking:
                        raise
                    flock(candidate, mode)
            else:
                flock(candidate, mode)
        return acquire

    def test_astra_start_waits_for_a_caller_row_held_by_a_move(self) -> None:
        parent = self.fixture.fixture.parent
        path = staff.record_path().parent / "rows" / f"{parent.id}.lock"
        with self.held_review_lock(path, "unit move") as lock, patch.object(fcntl, "flock", side_effect=self.release_at_contention(lock)):
            result = self.launch_astra_review("astra-reviewer")
        self.assertEqual(result.returncode, worker.DispatchSubmission.SUBMITTED, result.stdout + result.stderr)
        self.assertEqual(len(self.fixture.agent.submitted), 1)

    def test_astra_start_times_out_and_names_the_startup_holder(self) -> None:
        parent = self.fixture.fixture.parent
        path = staff.record_path().parent / "locks" / f"astra-review-start-{parent.id}.lock"
        _ = self.fixture.fixture.staff_config(lock_seconds=0.1)
        _ = write_config_fixture(conf.path("direct"), "pane_agent_start_seconds=1\n")
        with self.held_review_lock(path, "earlier reviewer") as lock:
            # Release an unbounded attempt so the original code can finish.
            with patch.object(fcntl, "flock", side_effect=self.release_at_contention(lock, blocking_only=True)):
                result = self.launch_astra_review("astra-reviewer")
        self.assertEqual(result.returncode, worker.ExitCode.REFUSED, result.stdout + result.stderr)
        self.assertIn(f"{parent.name}: reviewer startup: timed out waiting for lock", result.stderr)
        self.assertIn(f"pid {os.getpid()}: earlier reviewer", result.stderr)
        self.assertEqual(self.fixture.agent.submitted, [])

    def test_astra_start_gives_a_stalled_holder_one_full_budget(self) -> None:
        parent = self.fixture.fixture.parent
        path = staff.record_path().parent / "locks" / f"astra-review-start-{parent.id}.lock"
        _ = self.fixture.fixture.staff_config(lock_seconds=30)
        _ = write_config_fixture(conf.path("direct"), "pane_agent_start_seconds=45\n")
        flock = fcntl.flock
        clock = time.monotonic
        attempts: list[int] = []
        offset = 0

        def advance_on_contention(candidate: int | BinaryIO, mode: int) -> None:
            nonlocal offset
            try:
                flock(candidate, mode)
            except BlockingIOError:
                fd = candidate if isinstance(candidate, int) else candidate.fileno()
                if os.path.samestat(os.fstat(fd), path.stat()):
                    attempts.append(offset)
                    offset += 40
                raise

        with (patch.object(time, "monotonic", side_effect=lambda: clock() + offset),
              self.held_review_lock(path, "stalled reviewer"),
              patch.object(fcntl, "flock", side_effect=advance_on_contention)):
            result = self.launch_astra_review("astra-reviewer")
        self.assertEqual(result.returncode, worker.ExitCode.REFUSED, result.stdout + result.stderr)
        self.assertIn(f"pid {os.getpid()}: stalled reviewer", result.stderr)
        self.assertEqual(attempts, [0, 40])
        self.assertEqual(self.fixture.agent.submitted, [])

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
        _ = self.fixture.fixture.staff_config(lock_seconds=0.1)
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
                        reason = ("caller row: timed out waiting for lock" if locked == parent and target == "one-shot"
                                  else "another operation holds this row")
                        self.assertIn(f"{locked.name}: {reason}", lines[0])
                        self.assertIn("first part last part", lines[0])
        self.assertEqual(world.launches, launches)
        self.assertEqual(staff.snapshot().rows, before)
        self.assertEqual(self.fixture.agent.submitted, [])

    def test_concurrent_astra_launches_submit_all_reviews_under_one_caller(self) -> None:
        self.check_concurrent_astra_launches(pane_duration=0)

    def test_concurrent_astra_launches_allow_forty_seconds_per_pane(self) -> None:
        self.check_concurrent_astra_launches(pane_duration=40)

    def check_concurrent_astra_launches(self, *, pane_duration: int) -> None:
        program = r"""
import fcntl
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scripts.direct.test_assign_launcher import PaneReviewLauncherTests
from scripts.direct import progress_history
from scripts.herdr import herdr, turn_end
from scripts.lib import conf
from scripts.lib.fixture_test_support import set_up_fixture
from scripts.lint.memory_test_support import write_config_fixture

staff_file, name, clock_path = sys.argv[1:]
flock = fcntl.flock
monotonic = time.monotonic
contended = False

def observe_lock(fd, mode):
    global contended
    try:
        flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        descriptor = fd if isinstance(fd, int) else fd.fileno()
        startup_wait = startup_path.is_file() and os.path.samestat(os.fstat(descriptor), startup_path.stat())
        if mode & fcntl.LOCK_EX and not contended and (not clock_path or startup_wait):
            contended = True
            print("contended", file=sys.__stdout__, flush=True)
        if mode & fcntl.LOCK_NB:
            raise
        flock(fd, mode)

def open_pane(*args, **kwargs):
    if clock_path:
        print("opening", file=sys.__stdout__, flush=True)
        assert sys.stdin.readline() == "opened\n"
    return world.launch_through_herdr(*args, **kwargs)

with ExitStack() as cleanup:
    case = set_up_fixture(cleanup.callback, PaneReviewLauncherTests("runTest"))
    world = case.fixture.fixture.world
    startup_path = Path(staff_file).parent / "locks" / f"astra-review-start-{case.fixture.fixture.parent_id}.lock"
    if clock_path:
        case.fixture.fixture.staff_config(lock_seconds=30)
        write_config_fixture(conf.path("direct"), "pane_agent_start_seconds=45\n")
        clock = Path(clock_path)
        cleanup.enter_context(patch.object(time, "monotonic", lambda: monotonic() + int(clock.read_text())))
    with (patch.dict(os.environ, {"STAFF_FILE": staff_file}),
          patch.object(fcntl, "flock", side_effect=observe_lock),
          patch.object(herdr, "launch_prepared_agent", side_effect=open_pane)):
        case.fixture.agent.finish_after = 1
        result = case.launch_astra_review(name)
        assert result.returncode == 0, result.stdout + result.stderr
        (pane, _), = case.fixture.agent.submitted
        row = case.fixture.agent.row(pane)
        assert row.name == name
        run = case.fixture.fixture.root / name
        assert isinstance(progress_history.active_dispatch(row.id, run), progress_history.ActiveDispatch)
        assert world.locations[pane]["agent_status"] == "working"
        assert not (run / "review.md").exists()
        print("active", file=sys.__stdout__, flush=True)
        assert sys.stdin.readline() == "finish\n"
        case.fixture.agent.complete(pane)
        assert case.fixture.finish(name) is turn_end.TurnOutcome.DONE
        assert (run / "review.md").read_text().startswith(case.fixture.agent.final)
"""
        parent = self.fixture.fixture.parent
        staff_file = staff.record_path()
        caller_lock = staff_file.parent / "rows" / f"{parent.id}.lock"
        names = ("review-a", "review-b", "review-c")
        env = staff.child_environment(os.environ)
        clock = self.fixture.fixture.root / "launch-clock"
        if pane_duration:
            write_text_atomic(clock, "0")
        with ExitStack() as children:
            def launch(name: str) -> subprocess.Popen[str]:
                child = subprocess.Popen([sys.executable, "-c", program, str(staff_file), name,
                                          str(clock) if pane_duration else ""],
                    cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                _ = children.enter_context(child)
                _ = children.callback(child.kill)
                return child

            def receive(child: subprocess.Popen[str], message: str) -> None:
                assert child.stdout is not None
                self.assertTrue(select.select([child.stdout], [], [], 12)[0], f"Expected {message}.")
                line = cast(str, child.stdout.readline())
                if not line:
                    _, diagnostics = child.communicate(timeout=6)
                    self.fail(diagnostics)
                self.assertEqual(line, message + "\n")

            if pane_duration:
                first = launch(names[0])
                receive(first, "opening")
                waiting = [launch(name) for name in names[1:]]
                for child in waiting:
                    receive(child, "contended")
                peers = [first]
                for elapsed in (pane_duration, 2 * pane_duration, 3 * pane_duration):
                    opened = peers[-1]
                    write_text_atomic(clock, str(elapsed))
                    assert opened.stdin is not None
                    _ = opened.stdin.write("opened\n")
                    opened.stdin.flush()
                    if waiting:
                        streams = {child.stdout: child for child in waiting if child.stdout is not None}
                        ready, _, _ = select.select(list(streams), [], [], 12)
                        self.assertTrue(ready, "Expected the next reviewer to open.")
                        next_child = streams[ready[0]]
                        receive(next_child, "opening")
                        waiting.remove(next_child)
                        peers.append(next_child)
            else:
                with self.held_review_lock(caller_lock, "unit move"):
                    peers = [launch(name) for name in names]
                    for child in peers:
                        receive(child, "contended")
            for child in peers:
                receive(child, "active")
            self.assertEqual({row.name for row in staff.snapshot().find(kind="worker", parent=parent.id)}
                             & set(names), set(names))
            for child in peers:
                output, diagnostics = child.communicate("finish\n", timeout=6)
                self.assertEqual(child.returncode, worker.DispatchSubmission.SUBMITTED, output + diagnostics)

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
                        self.assertEqual(result.returncode, worker.ExitCode.BUSY, result.stdout + result.stderr)
                        self.assertIn("workers unavailable: impl: submitted pass awaits completion",
                                      (case.phase / "impl_agent_1.log").read_text())
                        self.assertNotIn("status", read_object(state_path))
                        self.assertEqual(unit_runs.open_run(staff.runtime_id()), case.fixture.run_dir)
                        self.assertFalse((case.phase / "impl_status_1").exists())
                    else:
                        with live_detached_run(case.fixture.run_dir, opened.row):
                            case.assert_start_not_delivered(worker.ExitCode.BUSY,
                                "workers unavailable: review: a detached run is live")
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
        repeated = self.launch()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.native.agent.complete(pane)
        self.assertEqual(self.native.finish("impl"), turn_end.TurnOutcome.DONE)
        state = read_object(self.session_dir / "progress_history_state.json")
        finished = assign.object_map(assign.object_map(state["pass"])["impl"])
        self.assertEqual((finished["instance_id"], finished["status"]), (identity, "completed"))
        self.assertEqual(self.native.agent.submitted, submitted)
        self.assertEqual(self.native.agent.waited, [])
        self.assertEqual(sum(event.get("event_type") == "pass_started" for event in self.events()), 1)

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
            director = self.native.fixture.parent.current_session
            self.assertEqual(calls.read_text().splitlines(), [f"{director}.phase-3 check reviewed.txt --json"])
        return result

    def test_herdr_assignment_claims_the_phase_before_the_worker_assignment(self) -> None:
        result = self.herdr_claim_launch("clear")
        self.assertEqual(result.returncode, 8, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "dispatch_events.txt").read_text().splitlines(), ["claim", "assign"])
        head = subprocess.run(["git", "-C", str(self.project), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout
        self.assertEqual((self.session_dir / "phases/3/phase_baseline").read_text(), head)
        self.assertEqual((self.session_dir / "baseline-at-worker-assign").read_text(), head)

    def test_herdr_assignment_starts_the_worker_in_the_claimed_checkout(self) -> None:
        other = make_fixture_worktree(self.project, "fixture", "phase-7")
        directories: list[object] = []

        def read_directory() -> None:
            pane = self.native.agent.submitted[-1][0]
            directories.append(self.native.fixture.world.locations[pane]["cwd"])

        self.native.agent.during_prompt = read_directory
        _ = self.successful("implement", "--cwd", str(other))

        self.assertEqual((self.session_dir / "phases/7/worktree").read_text().strip(), str(other))
        self.assertEqual(directories, [str(other)])

    def test_herdr_assignments_in_open_phases_use_distinct_berth_sessions(self) -> None:
        key = phase_reservation.SESSION_ENVIRONMENT
        director = self.native.fixture.parent.current_session
        self.record("start-phase", "--phase-id", "8", "--phase-title", "Second phase")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": director}):
            _ = self.successful("implement", "--new-worker", "impl", "--phase", "7")
            self.write_phase_prompt("8", "Second phase")
            _ = self.successful("implement", "--new-worker", "impl2", "--phase", "8")
        self.assertEqual([env.get(key) for _, _, env in self.native.fixture.world.requests],
                         [f"{director}.phase-7", f"{director}.phase-8"])

    def test_herdr_assignment_relaunches_an_idle_worker_without_the_phase_key(self) -> None:
        parent = self.native.fixture.parent
        world = self.native.fixture.world
        name = staff.WorkerName.parse("impl")
        opened = worker_panes.start_or_resume(parent.id, name, "impl")
        assert isinstance(opened, worker_panes.OpenWorker)
        idle_transcript(opened.row)
        key = phase_reservation.SESSION_ENVIRONMENT
        _ = world.processes[world.foreground[opened.pane][0]].environment.pop(key, None)
        keys: list[str | None] = []

        def read_key() -> None:
            pane = self.native.agent.submitted[-1][0]
            self.assertEqual(pane, opened.pane)
            keys.append(world.processes[world.foreground[pane][0]].environment.get(key))

        self.native.agent.during_prompt = read_key
        _ = self.successful("implement", "--to", name.value)
        self.assertEqual(keys, [f"{parent.current_session}.phase-7"])
        self.assertEqual(world.launches[-1], herdr.ExistingPane(opened.pane, opened.terminal))
        self.assertEqual(self.assignment_records()[name.value]["thread_id"], opened.row.current_session)

    def test_herdr_assignment_uses_a_codex_directors_phase_session(self) -> None:
        director = self.native.fixture.parent.current_session
        with patch.dict(os.environ, {"CODEX_THREAD_ID": director}):
            _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            _ = self.successful("implement")
        _, _, environment = self.native.fixture.world.requests[-1]
        self.assertEqual(environment.get(phase_reservation.SESSION_ENVIRONMENT), f"{director}.phase-7")

    def test_herdr_assignment_replaces_an_inherited_berth_session_for_another_phase(self) -> None:
        director = self.native.fixture.parent.current_session
        key = phase_reservation.SESSION_ENVIRONMENT
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": director, key: f"{director}.phase-8"}):
            _ = self.successful("implement")
        _, _, environment = self.native.fixture.world.requests[-1]
        self.assertEqual(environment.get(key), f"{director}.phase-7")

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
        with patch.dict(os.environ, {"UNIT_RESOLVES_ROUND": "0"}):
            _ = self.successful("fix", execution=InProcessAssignment())
        self.assertEqual(self.repair_state()["in_flight"], ["F001"])

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
        self.assertTrue((phase / "review_prompt_1.txt").is_file())
        self.assertTrue((phase / "review_summary_1.txt").read_text().strip())
        self.assertFalse((self.session_dir / "phases/ad hoc").exists())
        self.assertEqual(self.native.fixture.world.requests[0][1][self.native.fixture.world.requests[0][1].index("--sandbox") + 1], "read-only")
        self.assertEqual(len(self.native.agent.submitted), 1)

    def test_as_built_dispatches_after_the_last_phase_finishes(self) -> None:
        self.record("finish-phase", "--phase-id", "7", "--status", "completed")
        state_path = self.session_dir / recorder_state.STATE_FILENAME
        before = read_object(state_path)
        phase_events = [event for event in self.events() if str(event["event_type"]).startswith("phase_")]
        _ = self.prompt.write_text("Write the run's as-built documentation.\n")

        _ = self.successful("implement", "--phase", "as-built")
        _ = self.successful("implement", "--phase", "as-built")

        self.assertEqual(self.turn_starts, 1)
        after = read_object(state_path)
        self.assertEqual(recorder_state.open_phases(after), [])
        self.assertEqual(after["phase"], before["phase"])
        self.assertEqual([event for event in self.events() if str(event["event_type"]).startswith("phase_")], phase_events)
        started, = [event for event in self.events() if event["event_type"] == "pass_started"]
        finished, = [event for event in self.events() if event["event_type"] == "pass_finished"]
        self.assertEqual((started["phase_id"], started["phase_instance_id"]), ("as-built", ""))
        self.assertEqual((finished["pass_instance_id"], finished["status"]), (started["pass_instance_id"], "completed"))
        directory = self.session_dir / "phases/as-built"
        self.assertEqual((directory / "impl_status").read_text(), "done\n")
        self.assertFalse((directory / "phase_baseline").exists())

    def test_as_built_dispatch_refuses_an_open_phase(self) -> None:
        _ = self.prompt.write_text("Write the run's as-built documentation.\n")

        result = self.launch("implement", "--phase", "as-built")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Run as-built requires no open phases. Open phases: 7.", result.stderr)
        self.assertEqual(self.turn_starts, 0)
        self.assertFalse((self.session_dir / "phases/as-built").exists())

    def test_as_built_dispatch_requires_a_valid_home_checkout(self) -> None:
        self.record("finish-phase", "--phase-id", "7", "--status", "completed")
        _ = self.prompt.write_text("Write the run's as-built documentation.\n")
        membership = staff.local_snapshot()
        owner_id = self.native.fixture.parent.id
        for worktree in (staff.WorktreeUnfilled(),
                         staff.PresentWorktree("natedev", self.home / "missing-home")):
            with self.subTest(worktree=worktree):
                rows = tuple(replace(row, worktree=worktree) if row.id == owner_id else row for row in membership.rows)
                write_text_atomic(Path(os.environ["STAFF_FILE"]), replace(membership, rows=rows).encode())

                result = self.launch("implement", "--phase", "as-built")

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("home", result.stderr)
                self.assertEqual(self.native.fixture.world.launches, [])
                self.assertFalse((self.session_dir / "phases/as-built").exists())

    def test_as_built_dispatch_refuses_a_finished_run_before_check_launch(self) -> None:
        self.record("finish-run", "--status", "completed")
        _ = self.prompt.write_text("Write the run's as-built documentation.\n")

        result = self.launch("implement", "--phase", "as-built")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Run as-built requires an open run.", result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])
        self.assertFalse((self.session_dir / "phases/as-built").exists())
        self.assertFalse(any(event["event_type"] == "launch_refused" for event in self.events()))

    def test_as_built_dispatch_refuses_a_cwd_outside_the_home_checkout(self) -> None:
        self.record("finish-phase", "--phase-id", "7", "--status", "completed")
        _ = self.prompt.write_text("Write the run's as-built documentation.\n")
        other_checkout = self.home / "rust" / "project"

        result = self.launch("implement", "--phase", "as-built", "--cwd", str(other_checkout))

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"--cwd must resolve to the run home checkout {self.project}.", result.stderr)
        self.assertEqual(self.native.fixture.world.launches, [])
        self.assertFalse((self.session_dir / "phases/as-built").exists())
        self.assertFalse(any(event["event_type"] == "pass_started" for event in self.events()))

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
        _ = self.successful("implement", "--reply-file", str(reply_file))
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(self.turn_starts, 1)
        prompt = (self.session_dir / f"phases/7/{name}_prompt.txt").read_text()
        self.assertIn(self.prompt.read_text().strip(), prompt)
        for text in ("detached_runs.py start", "--kind python-tests before --", "Simple, Fast and Beautiful", "poll"):
            self.assertIn(text, prompt)
        manifest = self.session_dir / "phases/7/dispatch_1_paths.json"
        self.assertEqual(read_object(manifest), {"worker": name, "round": 1,
                                               "paths": ["src/worker.py", "tests/test_worker.py"]})
        self.assertEqual((manifest.parent / f"{name}_status").read_text().strip(), "done")
        self.assertTrue((manifest.parent / f"{name}_summary.txt").read_text().strip())
        self.assertEqual(reply_file.read_text(), (manifest.parent / f"{name}_summary.txt").read_text())
        self.assertTrue((manifest.parent / f"{name}_agent.log").is_file())
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        started = next(event for event in self.events() if event.get("event_type") == "pass_started")
        self.assertEqual(started["worker_id"], record["staff_id"])

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

    def test_refused_assignment_keeps_the_active_prompt(self) -> None:
        self.native.agent.finish_after = 100
        first = self.launch("implement", "--to", "impl")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        (pane, delivery), = self.native.agent.submitted
        row = self.native.agent.row(pane)
        passed = worker_panes.worker_pass(row, self.session_dir)
        output = Path(str(passed["prompt_file"]))
        original = output.read_bytes(), output.stat().st_ino

        retry = self.launch("implement", "--to", "impl")
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(worker_panes.worker_pass(row, self.session_dir)["prompt_file"], str(output))

        self.native.fixture.world.locations[pane]["agent_status"] = "idle"
        self.write_phase_prompt("7", "Assignment core", "Implement the second change.")
        refused = self.launch("implement", "--to", "impl")
        self.assertEqual(refused.returncode, worker.ExitCode.BUSY, refused.stdout + refused.stderr)
        self.assertEqual(self.native.agent.submitted, [(pane, delivery)])
        self.assertEqual((output.read_bytes(), output.stat().st_ino), original)
        self.assertEqual(worker_panes.worker_pass(row, self.session_dir)["instance_id"], passed["instance_id"])

    def test_scratch_cleanup_keeps_only_prompts_that_a_dispatch_can_retry(self) -> None:
        self.native.agent.finish_after = 100
        first = self.launch("implement", "--to", "impl")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        (pane, _), = self.native.agent.submitted
        row = self.native.agent.row(pane)
        passed = worker_panes.worker_pass(row, self.session_dir)
        prompt = Path(str(passed["prompt_file"]))
        saved = prompt.read_bytes()
        stale = prompt.with_name("a" * 64 + ".txt")
        write_text_atomic(stale, "An unused prompt.\n")

        remove_run_scratch.remove_assignment_prompts(self.session_dir)
        self.assertFalse(stale.exists())
        self.assertEqual(prompt.read_bytes(), saved)
        retry = self.launch("implement", "--to", "impl")
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(len(self.native.agent.submitted), 1)
        self.assertEqual(worker_panes.worker_pass(row, self.session_dir)["prompt_file"], str(prompt))

    def test_busy_launch_preserves_the_completed_slot_status(self) -> None:
        _ = self.successful()
        status = self.session_dir / "phases/7/impl_status"
        self.assertEqual(status.read_text(), "done\n")
        saved = status.read_bytes(), status.stat().st_ino
        self.write_phase_prompt("7", "Assignment core", "Implement the second change.")
        with worker_panes.worker_lock(self.native.fixture.parent.id, staff.WorkerName.parse("impl")) as hold:
            self.assertIsInstance(hold, worker_panes.WorkerLockHold)
            result = self.launch()
        self.assertEqual(result.returncode, worker.ExitCode.BUSY, result.stdout + result.stderr)
        self.assertEqual((status.read_bytes(), status.stat().st_ino), saved)
        self.assertEqual(self.turn_starts, 1)
        self.assertIn("role=impl; exit 6", (self.session_dir / "board.log").read_text())

        self.native.agent.finish_after = 100
        _ = self.successful("implement", "--to", "impl")
        phase = status.parent
        files = {path: path.read_bytes() for path in phase.iterdir() if path.is_file()}
        self.assertIn(b"Implement the second change.", files[phase / "impl_prompt.txt"])
        run_files = (self.session_dir / "board.log", self.session_dir / "heartbeat.log",
                     self.session_dir / unit_runs.STATE_FILE)
        saved_run = {path: path.read_bytes() for path in run_files}
        history = self.events()
        pane, line = self.native.agent.submitted[-1]
        row = self.native.agent.row(pane)
        backend = staff.backend(row)
        assert isinstance(backend, staff.CodexBackend)
        source = worker_admission.transcript_source(row, backend)
        transcript = source.read_text()
        question = "Which output format must I use?"
        world = self.native.fixture.world
        locations = world.locations
        for case, role, expected in (("working", "test", worker.ExitCode.BUSY),
                                     ("missing receipt", "implement", worker.ExitCode.BUSY),
                                     ("held lock with receipt", "implement", worker.ExitCode.BUSY),
                                     ("held name lock with receipt", "implement", worker.ExitCode.BUSY),
                                     ("done with an active pass", "implement", worker.ExitCode.BUSY),
                                     ("completion waits for a detached run", "test", worker.ExitCode.BUSY),
                                     ("blocked", "test", worker.ExitCode.BLOCKED)):
            with self.subTest(state=case), ExitStack() as cleanup:
                locations[pane]["agent_status"] = "working"
                world.first_turn_timeout = case in ("working", "missing receipt")
                _ = cleanup.callback(setattr, world, "first_turn_timeout", False)
                task = "Implement" if role == "implement" else "Test"
                self.write_phase_prompt("7", "Assignment core", f"{task} the second change.")
                if case == "missing receipt":
                    entry = json.dumps(line)
                    write_text_atomic(source, "".join(item for item in transcript.splitlines(keepends=True)
                                                       if entry not in item))
                    _ = cleanup.callback(write_text_atomic, source, transcript)
                if case == "blocked":
                    locations[pane]["agent_status"] = "blocked"
                    self.native.agent.questions[pane] = question
                if case == "done with an active pass":
                    locations[pane]["agent_status"] = "done"
                    self.write_phase_prompt("7", "Assignment core", "Change the active prompt.")
                    self.assertEqual(worker_panes.worker_pass(row, self.session_dir).get("status"), "active")
                if case == "completion waits for a detached run":
                    passed = worker_panes.worker_pass(row, self.session_dir)
                    delivery = progress_history.dispatch_delivery(passed)
                    self.native.agent.append(pane, "assistant", f"Changes complete.\nDONE {delivery.completion_id}")
                    _ = cleanup.callback(write_text_atomic, source, transcript)
                    path, detached = self.native.detached_record(row, "verification")
                    supervisor = cleanup.enter_context(subprocess.Popen(
                        [sys.executable, "-c", "import sys; sys.stdin.read()", "detached_runs.py", "supervise", str(path)],
                        stdin=subprocess.PIPE))
                    detached.update(status="running", pid=supervisor.pid, pane_dispatch_id=str(passed["instance_id"]))
                    write_text_atomic(path, json.dumps(detached))
                    _ = cleanup.callback(path.unlink)
                if case == "held lock with receipt":
                    hold = cleanup.enter_context(worker_panes.worker_lock(self.native.fixture.parent.id,
                                                                          staff.WorkerName.parse("impl")))
                    self.assertIsInstance(hold, worker_panes.WorkerLockHold)
                if case == "held name lock with receipt":
                    name_lock = staff.record_path().parent / "locks" / f"{self.native.fixture.parent.id}-impl.lock"
                    name_handle = cleanup.enter_context(name_lock.open("r"))
                    fcntl.flock(name_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if role == "implement":
                    delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.session_dir))
                    receipt = worker.dispatch_receipt(row, delivery.line, delivery.completion_id, run=self.session_dir)
                    self.assertIsInstance(receipt, worker.DispatchNotSubmitted if case == "missing receipt"
                                          else worker.DispatchSubmitted)
                refused = self.launch(role, "--to", "impl")
                self.assertEqual(refused.returncode, expected, refused.stdout + refused.stderr)
                if case in ("held lock with receipt", "held name lock with receipt"):
                    self.assertIn("dispatch lock is held", refused.stderr)
                if case == "done with an active pass":
                    self.assertIn("has different prompt content", refused.stderr)
                if case == "completion waits for a detached run":
                    self.assertEqual(locations[pane]["agent_status"], "done")
                    self.assertIn(worker.CompletionRecovery.WAITING_ON_RUNS.value, refused.stderr)
                if case == "blocked":
                    self.assertIn(question, refused.stderr)
                self.assertEqual({path: path.read_bytes() for path in phase.iterdir() if path.is_file()}, files)
                self.assertEqual({path: path.read_bytes() for path in run_files}, saved_run)
                self.assertEqual(self.events(), history)
                self.assertEqual(self.turn_starts, 2)
        locations[pane]["agent_status"] = "working"
        alternate = self.native.opened("impl2")
        self.write_phase_prompt("7", "Assignment core", "Implement the third change.")
        arguments = ["--role", "implement", "--cwd", str(self.project), "--phase", "7",
                     "--prompt", str(self.prompt), "--paths", str(self.paths), "--session-dir", str(self.session_dir)]
        for case in ("idle alternate", "repeat on the alternate"):
            with self.subTest(state=case):
                result = invoke_assignment(arguments, os.environ.copy())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.turn_starts, 3)
                self.assertEqual(self.native.agent.submitted[-1][0], alternate.pane)
                self.assertEqual({path: path.read_bytes() for path in phase.glob("impl_*")},
                                 {path: content for path, content in files.items() if path.name.startswith("impl_")})

        settling = self.native.opened("impl3")
        locations[settling.pane]["agent_status"] = "working"
        world.turn_end_status = herdr.AgentStatus.DONE
        world.commands.clear()
        self.write_phase_prompt("7", "Assignment core", "Implement the fourth change.")
        _ = self.successful("implement", "--to", "impl3")
        waits = [command for command in world.commands if command[:2] == ("agent", "wait")]
        self.assertEqual(len(waits), 1)
        self.assertEqual(self.turn_starts, 4)
        self.assertEqual(self.native.agent.submitted[-1][0], settling.pane)
        self.assertIn("Implement the fourth change.", (phase / "impl3_prompt.txt").read_text())

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
        self.assertEqual(status.read_text(), "running\n")
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
        write_text_atomic(state_path, json.dumps(state))
        write_text_atomic(status, "running\n")
        retried = invoke_assignment(arguments, os.environ.copy())
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        saved = worker_panes.worker_pass(opened.row, self.session_dir)
        self.assertEqual(saved["instance_id"], passed["instance_id"])
        self.assertEqual(saved["assign_status_file"], str(status))
        self.assertEqual(progress_history.dispatch_delivery(saved).actions.status_file, str(status))
        self.assertEqual(self.native.finish("impl2"), turn_end.TurnOutcome.DONE)
        self.assertEqual(status.read_text(), "done\n")
        self.assertFalse(status.with_name("impl_status").exists())
