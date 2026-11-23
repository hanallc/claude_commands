"""Worker commands preserve native identity and use transcripts as dispatch receipts."""
from __future__ import annotations

from scripts.direct import pass_record as pass_recorder

import io
import fcntl
import json
import math
import os
import re
import select
import shlex
import signal
from pathlib import Path
from datetime import datetime, timedelta, timezone
import subprocess
import sys
import time
from threading import Event, Timer, current_thread
from types import SimpleNamespace
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable, Generator, Iterator
from dataclasses import asdict, replace
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from typing import Literal, TextIO, cast, final, override
import unittest
from unittest.mock import ANY, Mock, call, patch
from uuid import UUID, uuid4

from scripts.lib.fixture_test_support import set_up_fixture
from scripts.agents import detached_run_process, detached_runs, worker_lifecycle
from scripts.agents import pane_record
from scripts.astra import astra_roles
from scripts.herdr import herdr_types, pane_launch
from scripts.accounts import accounts
from scripts.buildlog import treekey
from scripts.herdr import test_worker_panes as pane_fixtures
from scripts.herdr import pass_notices, turn_end, worker_panes
from scripts.direct import assign, progress_history
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.production.cli_fixture import invoke_cli
from scripts.production import fake_unit_host, staff, worker, worker_admission
from scripts.production.staff_test_support import fixture_member, fixture_session, record_fixture_claude_start, fixture_runtime_id, fixture_worker
from scripts.lib import atomic_file, conf, file_lock, process_start, unit_runs
from scripts.lib.fork_test_support import ForkChildren
from scripts.lib.unit_runs_test_support import start_new_command
from scripts.lib.transcript_test_support import compressed_copy
from scripts.lib.transcripts import open_transcript
from scripts.lib.script_envelope import script_prompt
from scripts.lib.test_models_registry import claude_catalog_environment, write_registry
from scripts.lib.test_hang_guard import LauncherHangGuard
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, write_config_fixture
from scripts.message import send, sessions
from scripts.usage import agents_log

from scripts.herdr import herdr
from scripts.herdr.missing_binary import MISSING_HERDR_BIN


def run_compaction_helper_fixture() -> int:
    """Run the helper CLI with a private pane transport and notice sink."""
    case, worker_pane, worker_terminal, transcript, notices, done, *arguments = sys.argv[1:]
    source = Path(transcript)
    run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

    class HelperPane(herdr.Herdr):
        @override
        def composer(self, pane: str) -> herdr.NoComposer:
            assert pane == worker_pane
            return herdr.NoComposer()

        @override
        def wait_turn_end(self, pane: str, timeout_ms: int) -> herdr.WaitTimedOut:
            assert pane == worker_pane and timeout_ms >= 0
            return herdr.WaitTimedOut()

    def observe(parent: staff.StaffId, row: staff.StaffRow, record: staff.StaffSnapshot) -> worker_panes.OpenWorker:
        assert row.reports_to(parent) and record.by_id(row.id) == row
        return worker_panes.OpenWorker(row, worker_pane, worker_terminal, herdr.AgentStatus.IDLE)

    def close_pane(parent: staff.StaffId, name: staff.WorkerName, *,
                   lock: worker_panes.WorkerLockHold) -> worker_panes.Closed:
        row = worker_admission.find_worker(staff.snapshot(), parent, name.value)
        assert row is not None and lock.owns(row)
        return worker_panes.Closed()

    def check_target(target: str, *, action: str, place: herdr.HerdrPlace, terminal: str,
                     **options: object) -> bool:
        del options
        row = staff.snapshot().by_current_session(target)
        return (row is not None and row.kind == "worker" and action == "keys"
                and place.pane == worker_pane and terminal == worker_terminal)

    def record_prompt(line: str) -> None:
        with source.open("a") as stream:
            _ = stream.write(json.dumps({"type": "event_msg", "payload": {
                "type": "user_message", "message": line}}) + "\n")

    def pane_transport(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        if command[0] != os.environ["HERDR_BIN"]:
            return run_process(command, **options)
        assert case != "receipt with failed confirmation", command
        assert command[3:6] == ["agent", "prompt", worker_pane], command
        line = command[6]
        if line == "/compact":
            if case == "failure before input":
                raise OSError("fixture compaction failed")
            with source.open("a") as stream:
                _ = stream.write('{"type":"compacted"}\n')
            return subprocess.CompletedProcess(command, 0, "{}", "")
        record_prompt(line)
        raise OSError("fixture lost the prompt response")

    def refuse_confirmation(run: Path, instance_id: str, completion_id: str, *, worker_thread: str) -> None:
        assert run == Path(arguments[arguments.index("--session-dir") + 1])
        assert instance_id == arguments[arguments.index("--dispatch-id") + 1]
        assert completion_id == arguments[arguments.index("--completion-id") + 1]
        assert worker_thread
        raise OSError("fixture could not save confirmation")

    def notify(options: send.Options, *, membership: staff.StaffSnapshot) -> send.Result:
        assert membership.rows
        with Path(notices).open("a") as stream:
            _ = stream.write(json.dumps(send.body_text(options)) + "\n")
        return send.Result("sent", "private director")

    with (ExitStack() as faults, patch.object(worker_panes, "observe_worker", new=observe),
          patch.object(worker_panes, "close", new=close_pane),
          patch.object(herdr, "check_target", new=check_target),
          patch.object(herdr, "Herdr", new=HelperPane),
          patch.object(subprocess, "run", new=pane_transport),
          patch.object(send, "_send", new=notify)):
        if case == "receipt with failed confirmation":
            run = Path(arguments[arguments.index("--session-dir") + 1])
            record = staff.snapshot()
            row = record.by_id(staff.StaffId(arguments[arguments.index("--worker-id") + 1]))
            assert row is not None
            delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, run, record=record))
            assert delivery.line
            record_prompt(delivery.line)
            _ = faults.enter_context(patch.object(progress_history, "confirm_assignment_submission", new=refuse_confirmation))
        code = worker.main(arguments)
    with Path(done).open("w") as stream:
        _ = stream.write(f"{int(code)}\n")
    return code


DETACHED_FIXTURE_DEAD_PID = 2 ** 22 + 12345
DETACHED_FIXTURE_MAX_PID = 2_147_483_647


@contextmanager
def detached_fixture_processes() -> Generator[None]:
    """Report the synthetic supervisor as absent without asking the host."""
    read = detached_run_process.process_args

    def process_args(pid: int) -> (
        list[str] | detached_run_process.ProcessCommandText
        | detached_run_process.ProcessArgumentsUnavailable | detached_run_process.ProcessAbsent
    ):
        absent = pid in (DETACHED_FIXTURE_DEAD_PID, DETACHED_FIXTURE_MAX_PID)
        return detached_run_process.ProcessAbsent() if absent else read(pid)

    with patch.object(detached_run_process, "process_args", side_effect=process_args):
        yield


@final
class DispatchAgent:
    """A private agent writes real transcript records at the process boundary."""

    def __init__(self, fixture: pane_fixtures.WorkerPaneTests) -> None:
        self.fixture = fixture
        self.submitted: list[tuple[str, str]] = []
        self.compactions: list[tuple[str, int]] = []
        self.compacting: set[str] = set()
        self.compact_error = ""
        self.compact_proof: Literal["compacted", "task_started", "absent"] = "compacted"
        self.compact_inputs: dict[str, str] = {}
        self.notice_failures = 0
        self.members: dict[str, staff.StaffId] = {}
        self.replies: list[str] = []
        self.last_replies: dict[str, str] = {}
        self.notices: list[str] = []
        self.notice_senders: list[send.SenderStamp] = []
        self.questions: dict[str, str | herdr_types.HerdrFailure] = {}
        self.answers: list[tuple[str, str]] = []
        self.answer_inputs: dict[str, str] = {}
        self.answer_enters: dict[str, int] = {}
        self.waited: list[str] = []
        self.defer_prompt_transcript = False
        self.finish_after = 0
        self.wait_count = 0
        self.final = "The work is complete."
        self.error = ""
        self.interrupt = ""
        self.during_prompt: Callable[[], None] = lambda: None
        self.without_done = False
        self.after_complete: Callable[[str], None] = lambda pane: None
        self.consultation_delivery: Literal["prompt", "second wait", "never"] = "prompt"
        self.run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

    def row(self, pane: str) -> staff.StaffRow:
        record = staff.snapshot()
        if pane in self.fixture.world.foreground:
            pid = self.fixture.world.foreground[pane][0]
            identity = self.fixture.world.processes[pid].current_identity
            row = record.by_current_session(identity)
        else:
            row = record.by_id(self.members[pane])
        assert row is not None
        return row

    def append(self, pane: str, role: str, text: str) -> None:
        if pane not in self.fixture.world.foreground:
            row = self.row(pane)
            identity = row.current_session
        else:
            pid = self.fixture.world.foreground[pane][0]
            identity = self.fixture.world.processes[pid].current_identity
            row = staff.snapshot().by_current_session(identity)
        if row is None:
            pid, family, _ = self.fixture.world.foreground[pane]
            process = self.fixture.world.processes[pid]
            backend = staff.CodexBackend(Path(process.environment["CODEX_HOME"])) if family == "codex" else staff.ClaudeBackend()
            paths = (list((backend.codex_home / "sessions").rglob(f"*{process.current_identity}.jsonl"))
                     if isinstance(backend, staff.CodexBackend) else sessions.transcript_locations(process.current_identity))
            path = max(paths, key=lambda source: source.stat().st_mtime)
        else:
            self.members[pane] = row.id
            backend = staff.backend(row)
            assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
            if isinstance(backend, staff.ClaudeBackend) and not sessions.transcript_locations(row.current_session):
                fixture_session(row.current_session, backend)
            path = worker_admission.transcript_source(row, backend)
        record = ({"type": "event_msg", "payload": {"type": "user_message" if role == "user" else "agent_message", "message": text}}
                  if isinstance(backend, staff.CodexBackend) else
                  {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}})
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(record) + "\n")
        if role == "assistant":
            self.last_replies[str(identity)] = text

    def complete(self, pane: str) -> None:
        line = next(text for target, text in reversed(self.submitted) if target == pane)
        dispatch_id = line.split("DONE ")[-1].split()[0].removesuffix(".").strip()
        response = self.final if self.without_done else self.final + "\nDONE " + dispatch_id
        self.replies.append(response)
        self.append(pane, "assistant", response)
        self.fixture.world.locations[pane]["agent_status"] = "done"
        self.after_complete(pane)

    def pane_commands(self, case: unittest.TestCase) -> None:
        run = self.fixture.world.run
        composer = self.fixture.world.composer

        def notify(options: send.Options, *, membership: staff.StaffSnapshot) -> send.Result:
            del membership
            if self.notice_failures:
                self.notice_failures -= 1
                return send.Result("failed", "director message failed")
            self.notices.append(send.body_text(options))
            origin = options.sender_origin
            self.notice_senders.append(origin.stamp if isinstance(origin, send.ForwardedSender) else send.sender_stamp())
            return send.Result("sent", "private director")

        _ = case.enterContext(patch.object(send, "_send", side_effect=notify))

        def accept(pane: str, text: str) -> None:
            del self.questions[pane]
            _ = self.answer_inputs.pop(pane, None)
            _ = self.answer_enters.pop(pane, None)
            self.answers.append((pane, text))
            self.append(pane, "user", text)
            self.error = ""
            self.fixture.world.locations[pane]["agent_status"] = "working"

        def question_input(pane: str) -> herdr.ComposerInput | herdr.NoComposer:
            return herdr.ComposerInput(self.answer_inputs.get(pane, "")) if pane in self.questions else composer(pane)

        def command(*arguments: str, deadline: float = math.inf) -> str:
            result = run(*arguments, deadline=deadline)
            if arguments[:2] == ("pane", "read"):
                question = self.questions.get(arguments[2], result)
                if isinstance(question, herdr_types.HerdrFailure):
                    raise question
                return question
            if arguments[:2] == ("agent", "prompt") and arguments[2] in self.questions:
                accept(*arguments[2:4])
            elif arguments[:2] == ("pane", "send-text") and arguments[2] in self.questions:
                self.answer_inputs[arguments[2]] = arguments[3]
            elif arguments[:2] == ("pane", "send-keys") and arguments[2] in self.questions:
                pane, key = arguments[2:4]
                if key == "shift+left":
                    self.answer_inputs[pane] = ""
                    self.answer_enters[pane] = 0
                elif key == "enter":
                    self.answer_enters[pane] += 1
                    if self.answer_enters[pane] == 2:
                        accept(pane, self.answer_inputs[pane])
            return result

        _ = case.enterContext(patch.object(self.fixture.world, "run", side_effect=command))
        _ = case.enterContext(patch.object(self.fixture.world, "composer", side_effect=question_input))

    def consultations(self, case: unittest.TestCase) -> None:
        run = self.fixture.world.run
        wait_turn_end = self.fixture.world.wait_turn_end

        def submit(*arguments: str, deadline: float = math.inf) -> str:
            result = run(*arguments, deadline=deadline)
            if arguments[:2] == ("agent", "prompt"):
                pane, line = arguments[2:4]
                pid = self.fixture.world.foreground[pane][0]
                if staff.snapshot().by_current_session(self.fixture.world.processes[pid].current_identity) is None:
                    self.submitted.append((pane, line))
                    self.during_prompt()
                    if self.error:
                        raise herdr_types.HerdrFailure(self.error)
                    if self.consultation_delivery == "prompt":
                        self.append(pane, "user", line)
                        self.complete(pane)
            return result

        def wait(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
            self.waited.append(pane)
            self.wait_count += 1
            if self.consultation_delivery != "prompt":
                self.fixture.world.turn_end_status = herdr.AgentStatus.IDLE
                if self.consultation_delivery == "second wait" and self.wait_count == 2:
                    line = next(text for target, text in reversed(self.submitted) if target == pane)
                    self.append(pane, "user", line)
                    self.complete(pane)
                    self.fixture.world.turn_end_status = herdr.AgentStatus.DONE
            return wait_turn_end(pane, timeout_ms)

        _ = case.enterContext(patch.object(self.fixture.world, "run", side_effect=submit))
        _ = case.enterContext(patch.object(self.fixture.world, "wait_turn_end", side_effect=wait))
        _ = case.enterContext(patch.object(os, "kill", side_effect=self.fixture.world.signal))

    def run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != MISSING_HERDR_BIN:
            return self.run_process(args, **kwargs)
        action, pane = args[4:6]
        if not (action == "prompt" and args[6] == "/compact"):
            self.fixture.assertNotIn("--timeout", args)
        if "--until" in args:
            self.fixture.assertEqual(args[-2:], ["--until", "working"])
        if action == "prompt":
            line = args[6]
            if line == "/compact":
                self.fixture.assertEqual(args[-1], "--wait")
                if self.compact_error:
                    return subprocess.CompletedProcess(args, 1, json.dumps({"error": {
                        "code": "compact_failed", "message": self.compact_error}}), "")
                self.compacting.add(pane)
                self.fixture.world.locations[pane]["agent_status"] = "working"
                self.compactions.append((pane, len(self.submitted)))
                self.compact_inputs[pane] = "/compact" if self.compact_proof == "absent" else ""
                if self.compact_proof != "absent":
                    row = self.row(pane)
                    backend = staff.backend(row)
                    assert isinstance(backend, staff.CodexBackend)
                    source = worker_admission.transcript_source(row, backend)
                    with open_transcript(source) as stream:
                        content = stream.read()
                    source = source.with_suffix("") if source.suffix == ".zst" else source
                    record = ({"type": "compacted"} if self.compact_proof == "compacted" else
                              {"type": "event_msg", "payload": {"type": "task_started"}})
                    atomic_file.write_bytes_atomic(source, content + (json.dumps(record) + "\n").encode())
                return subprocess.CompletedProcess(args, 0, "{}", "")
            if pane in self.compacting:
                self.fixture.assertIn(self.fixture.world.locations[pane]["agent_status"], ("idle", "done"))
                self.compacting.remove(pane)
            self.fixture.assertNotIn("--wait", args)
            self.fixture.assertNotIn("--until", args)
            self.submitted.append((pane, line))
            if not self.defer_prompt_transcript:
                self.append(pane, "user", line)
            self.during_prompt()
            if self.interrupt == "prompt":
                self.interrupt = ""
                raise OSError("interrupted after prompt")
            self.fixture.world.locations[pane]["agent_status"] = "idle" if self.error == "agent_prompt_stalled" else "working"
            if self.finish_after == 0 and not self.error and not self.defer_prompt_transcript:
                self.complete(pane)
        elif action == "wait":
            self.waited.append(pane)
            self.wait_count += 1
            if self.wait_count >= self.finish_after:
                self.complete(pane)
        else:
            raise AssertionError(args)
        if self.error:
            if self.error == "agent_blocked":
                self.fixture.world.locations[pane]["agent_status"] = "blocked"
            return subprocess.CompletedProcess(args, 1, json.dumps({"error": {"code": self.error, "message": self.error}}), "")
        return subprocess.CompletedProcess(args, 0, "{}", "")


@final
class PaneCommandRouter:
    """Run private pane commands through their real parsers and handlers."""

    def __init__(self) -> None:
        self.cwd = Path()
        self.commands: list[list[str]] = []
        self.worker_commands: list[list[str]] = []
        self.recorder_commands: list[list[str]] = []
        self.run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

    def setup(self, case: unittest.TestCase, cwd: Path) -> None:
        self.cwd = cwd
        self.run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
        _ = case.enterContext(patch.object(subprocess, "run", side_effect=self.run))
        _ = case.enterContext(patch.object(assign, "require_live_director"))

    def run(self, command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        path = Path(command[1]).resolve() if len(command) > 1 else Path()
        root = Path(__file__).resolve().parents[2]
        arguments = command[2:]
        environment = cast(dict[str, str], options.get("env", os.environ.copy()))
        cwd = Path(cast(str | Path, options.get("cwd", self.cwd)))
        if path == root / "scripts/direct/assign.sh":
            self.commands.append(arguments.copy())
            result = invoke_cli(assign, assign.main, arguments, environment, cwd)
        elif path == root / "scripts/production/worker.py":
            self.worker_commands.append(arguments.copy())
            result = invoke_cli(worker, lambda: worker.main(arguments), arguments, environment, cwd)
        elif path == root / "scripts/direct/progress_history.py":
            self.recorder_commands.append(arguments.copy())
            result = invoke_recorder(arguments, environment, cwd)
        else:
            return self.run_process(command, **options)
        output = options.get("stdout")
        if output is not None:
            _ = cast(TextIO, output).write(result.stdout + result.stderr)
        elif not options.get("capture_output"):
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
        return result


@final
class WorkerCommandTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture = pane_fixtures.WorkerPaneTests("runTest")
        self.agent = DispatchAgent(self.fixture)
        self.run_dir = Path()
        self.prompt = Path()
        self.reply = Path()

    @override
    def setUp(self) -> None:
        self.fixture = set_up_fixture(self.addCleanup, pane_fixtures.WorkerPaneTests("runTest"))
        _ = write_config_fixture(self.fixture.root / "conf/shutdown.conf")
        _ = write_config_fixture(self.fixture.root / "conf/detached_runs.conf")
        _ = self.enterContext(detached_fixture_processes())
        _ = (self.fixture.root / "conf/build_targets.conf").write_text(
            "targets=natedev,mac\nkind.natedev=local\nkind.mac=ssh\n" + UNREACHABLE_WAIT_FIXTURE)
        self.fixture.world.binary = MISSING_HERDR_BIN
        config = self.fixture.root / "conf/direct.conf"
        atomic_file.write_text_atomic(config, config.read_text()
                                      + "# range: 1..600\nWORKER_SETTLE_SECONDS=30\n")
        atomic_file.write_text_atomic(config.with_name("send.conf"),
            "# range: 0.001..\nwaits_review_seconds=1\n"
            + "# range: 0.001..\ndelivery_timeout=5\n"
            + "# range: 0.001..\nkill_grace=1\n"
            + "# range: 0.001..\ncaller_margin_seconds=2\n"
            + "# range: 0.001..\nremote_transport_seconds=2\n"
            + "# one of: on, off\nreview_hold=off\n")
        atomic_file.write_text_atomic(config.with_name("scratch.conf"),
            "# whole: 1..\nsuffix_length=6\n"
            + "# whole: 1..\nrecord_lock_attempts=3\n"
            + "# whole: 1..\nrepo_depth=3\n"
            + "repo_skip=target node_modules .git\n")
        root = self.fixture.root
        _ = self.enterContext(patch.object(send, "STATE", root / "message-state"))
        (root / "worker-tmp").mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "TMPDIR": str(root / "worker-tmp"),
            "SCRATCH_ROOT": str(root / "scratch"),
            "SCRATCH_VISIBLE_ROOT": str(root / "visible-scratch"),
            "SCRATCH_COPIES_ROOT": str(root / "copies"),
            "SCRATCH_HOLDS": str(root / "scratch-holds.json"),
        })))
        self.agent = DispatchAgent(self.fixture)
        self.agent.pane_commands(self)
        self.run_dir = self.fixture.root / "unit" / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _ = self.enterContext(patch.object(unit_runs, "RUN_ROOT", self.run_dir.parent))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"SCRATCH_ROOT": str(self.fixture.root / "scratch")})))
        _ = staff.set_run(self.fixture.parent.id, staff.OpenRun(self.run_dir))
        atomic_file.write_text_atomic(Path.home() / ".codex/sessions" / f"{self.fixture.parent.current_session}.jsonl",
                                      json.dumps({"type": "turn_context", "payload": {
                                          "model": "fixture", "effort": "high"}}) + "\n")
        self.prompt = self.run_dir / "prompt.md"
        atomic_file.write_text_atomic(self.prompt, "Read the source and preserve its behavior.\n")
        self.reply = self.run_dir / "reply.md"
        atomic_file.write_text_atomic(self.run_dir / "progress_history_state.json", json.dumps({
            "open_phases": [{"id": "8", "title": "Workers", "instance_id": "phase-eight", "status": "active", "started_at": 1}],
            "pass": {}, "events": [], "project": {}, "unit": "unit",
            "history_file": str(self.run_dir / "history.jsonl"),
            "owner_staff_id": self.fixture.parent.id,
            "main_agent": {"session_id": str(self.fixture.parent.current_session),
                           "family": "claude", "model": "fixture", "effort": "high"},
        }))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.fixture.parent.current_session),
            "UNIT_PASS_OWNER": "launcher", "UNIT_TEAM_ROLE": "impl", "UNIT_SYNC_WRITES": "0"})))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.agent.run))

    def opened(self, name: str = "impl", role: staff.WorkerRole = "impl") -> worker_panes.OpenWorker:
        result = worker_panes.start_or_resume(self.fixture.parent.id, staff.WorkerName.parse(name), role)
        self.assertIsInstance(result, worker_panes.OpenWorker)
        assert isinstance(result, worker_panes.OpenWorker)
        pane_fixtures.idle_transcript(result.row)
        return result

    def dispatch(self, name: str = "impl", role: staff.WorkerRole = "impl", dispatch_id: str = "") -> worker.DispatchSubmission | worker.ExitCode:
        return worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(name), role, self.prompt, dispatch_id,
            session_dir=self.run_dir, slot="impl", phase="8", kind=role,
            launcher_command=["assign.sh", "--phase", "8", "--prompt", str(self.prompt)], reply=self.reply)

    @contextmanager
    def rename_at_worker_lock(self, row: staff.StaffRow, name: str, *, replacement: staff.StaffRow | None = None
                              ) -> Generator[None]:
        acquire = worker_panes.worker_id_lock

        @contextmanager
        def rename_on_acquire(identity: staff.StaffId, *, blocking: bool = False,
                              deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> Generator[None]:
            with acquire(identity, blocking=blocking, deadline=deadline):
                if identity == row.id:
                    current = staff.by_id(identity)
                    if current is not None and current.name == row.name:
                        _ = staff.rename(identity, name)
                        if replacement is not None:
                            _ = staff.rename(replacement.id, row.name)
                yield

        with patch.object(worker_panes, "worker_id_lock", new=rename_on_acquire):
            yield

    def unfinished_turn(self, role: staff.WorkerRole = "impl") -> None:
        """Prepare a submitted reply before the hook completes its pass."""
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "interrupted after prompt"):
            _ = self.dispatch(role=role)
        pane = self.agent.submitted[-1][0]
        self.agent.complete(pane)
        atomic_file.write_text_atomic(self.reply, self.agent.replies[-1] + "\n")

    def assert_pass_confirmed(self, previous: bytes) -> bytes:
        expected = cast(dict[str, dict[str, dict[str, object]]], json.loads(previous))
        passed = expected["pass"]["impl"]
        self.assertNotIn("confirmed_delivery_ids", passed)
        completion_id = progress_history.dispatch_delivery(passed).completion_id
        self.assertTrue(completion_id)
        passed["confirmed_delivery_ids"] = [completion_id]
        row = staff.by_id(staff.StaffId(str(passed["worker_id"])))
        assert row is not None
        passed["submitted_worker_thread"] = str(row.current_session)
        current = (self.run_dir / unit_runs.STATE_FILE).read_bytes()
        self.assertEqual(current, (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode())
        return current

    def assert_pass_completed(self, previous: bytes) -> None:
        before = cast(dict[str, dict[str, dict[str, object]]], json.loads(previous))["pass"]["impl"]
        after = cast(dict[str, dict[str, dict[str, object]]],
                     json.loads((self.run_dir / "progress_history_state.json").read_text()))["pass"]["impl"]
        changed = {"status", "finished_at", "hook_completion"}
        self.assertEqual({key: value for key, value in after.items() if key not in changed},
                         {key: value for key, value in before.items() if key not in changed})
        self.assertEqual(after["status"], "completed")

    def finish(self, name: str = "impl", kind: turn_end.EventKind = turn_end.EventKind.TURN_END) -> turn_end.TurnOutcome:
        row = next(row for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id) if row.name == name)
        return turn_end.handle(turn_end.WorkerEvent(str(row.current_session), kind,
            self.agent.last_replies.get(str(row.current_session), "") if kind == turn_end.EventKind.TURN_END else ""))

    def receipt(self, name: str = "impl") -> worker.DispatchReceipt:
        row = next(row for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id) if row.name == name)
        delivery = pass_recorder.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        return worker.dispatch_receipt(row, delivery.line, delivery.completion_id, run=self.run_dir)

    def command(self, *args: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = worker.main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def start_orphaned_worker_run(self) -> tuple[worker_panes.OpenWorker, Path, detached_runs.RunRecord]:
        config = self.fixture.root / "conf"
        atomic_file.write_text_atomic(config / "postmaster.conf",
            "# range: 0.001..\nwait_seconds=1\n"
            + "# range: 0.001..\nclaim_grace_seconds=1\n")
        opened = self.opened()
        instance = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.prompt, kind="impl", lens="")
        line = f"Review the files. End your final reply with DONE {instance}."
        progress_history.save_dispatch_delivery(self.run_dir, instance,
            pass_recorder.DispatchDelivery(line, instance, str(self.reply)))
        folder = self.run_dir / detached_runs.DETACHED_RUNS_DIR
        folder.mkdir()
        path = folder / "orphan.json"
        run: detached_runs.RunRecord = {
            "run_id": "orphan", "status": "running", "session_dir": str(self.run_dir),
            "worker": opened.row.name, "staff_id": opened.row.id, "label": "fixture gate",
            "command": ["true"], "log": str(folder / "orphan.log"), "started_at": time.time(),
            "pid": DETACHED_FIXTURE_DEAD_PID, "director_dir": str(self.run_dir), "done_message_file": "",
        }
        atomic_file.write_text_atomic(path, json.dumps(run))
        return opened, path, run

    def test_restore_reaps_detached_orphans_once_before_resuming(self) -> None:
        opened, path, run = self.start_orphaned_worker_run()
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        folder = path.parent
        delivery = send.Result("sent", "the holder received the result")

        def resume(_row: staff.StaffRow, _plan: worker.WorkerResume) -> worker.ExitCode:
            saved = cast(dict[str, object], json.loads(path.read_text()))
            self.assertEqual(saved["status"], "finished")
            return worker.ExitCode.OK

        with (patch.object(send, "deliver_worker_text", return_value=delivery),
              patch.object(detached_runs, "deliver", wraps=detached_runs.deliver) as deliver,
              patch.object(detached_runs, "reap_orphans", wraps=detached_runs.reap_orphans) as reap,
              patch.object(unit_runs, "held", return_value=[str(self.run_dir)] * 2),
              patch.object(worker, "_resume_launcher", side_effect=resume)):
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    code, output, errors = self.command("restore")
                    self.assertEqual((code, errors), (0, ""), errors)
                    self.assertEqual(output.count("reaped detached run orphan"), int(attempt == 0))
            deliver.assert_called_once()
            self.assertEqual(reap.call_args_list, [call(str(self.run_dir))] * 2)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["delivery"], f"sent: {delivery.detail}")
        self.assertIn("delivered_at", saved)

        sibling = self.opened("review", "review")
        for selection, selected in (("selected", frozenset({opened.row.id})),
                                    ("empty", frozenset[staff.StaffId]()),
                                    ("missing", frozenset({staff.StaffId("missing-worker")}))):
            paths: dict[str, Path] = {}
            for row in (opened.row, sibling.row):
                identity = f"{selection}-{row.name}"
                paths[row.name] = folder / f"{identity}.json"
                atomic_file.write_text_atomic(paths[row.name], json.dumps({**run, "run_id": identity,
                    "status": "running", "worker": row.name, "staff_id": row.id,
                    "log": str(folder / f"{identity}.log")}))
            sibling_before = paths[sibling.row.name].read_bytes()
            with (self.subTest(selection=selection),
                  patch.object(send, "deliver_worker_text", return_value=delivery),
                  patch.object(detached_runs, "reap_orphans", wraps=detached_runs.reap_orphans) as reap,
                  patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(worker.restore(self.fixture.parent.id, only=selected), worker.ExitCode.OK)
                self.assertEqual(paths[sibling.row.name].read_bytes(), sibling_before)
                status = cast(dict[str, object], json.loads(paths[opened.row.name].read_text()))["status"]
                self.assertEqual(status, "finished" if opened.row.id in selected else "running")
                self.assertEqual(reap.call_args_list,
                                 [call(str(self.run_dir), opened.row.name)] if selected == frozenset({opened.row.id}) else [])

    def test_restore_retries_orphan_result_deferred_before_input(self) -> None:
        opened, path, _ = self.start_orphaned_worker_run()
        world = self.fixture.world
        run = world.run
        pane = world.locations[opened.pane]
        pane["agent_status"] = "working"

        def record_input(*arguments: str, deadline: float = math.inf) -> str:
            result = run(*arguments, deadline=deadline)
            if arguments[:3] == ("agent", "prompt", opened.pane):
                self.assertIn("Detached run: orphan", arguments[3].splitlines())
                self.agent.append(opened.pane, "user", arguments[3])
                pane["agent_status"] = "working"
            return result

        with (patch.object(world, "run", side_effect=record_input),
              patch.object(send, "deliver_worker_text", wraps=send.deliver_worker_text) as deliver,
              patch.object(unit_runs, "held", return_value=[str(self.run_dir)]),
              patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK)):
            code, _, errors = self.command("restore")
            self.assertEqual((code, errors), (0, ""), errors)
            deliver.assert_called_once()
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual(saved["delivery"], detached_runs.DEFERRED_WORKER_RESULT + "worker is working")
            self.assertIs(saved["completion_pending"], True)
            self.assertNotIn("delivered_at", saved)

            passed = worker_panes.worker_pass(opened.row, self.run_dir)
            self.agent.append(opened.pane, "user", pass_recorder.dispatch_delivery(passed).line)
            pane["agent_status"] = "idle"
            code, _, errors = self.command("restore")
            self.assertEqual((code, errors), (0, ""), errors)
            self.assertEqual(deliver.call_count, 2)
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual(saved["delivery"], f"sent: worker {opened.row.name} at {opened.pane}")
            self.assertIn("delivered_at", saved)

            code, _, errors = self.command("restore")
            self.assertEqual((code, errors), (0, ""), errors)
            self.assertEqual(deliver.call_count, 2)

    def test_restore_lists_finished_results_newest_first(self) -> None:
        for launcher in ("explicit", "assign"):
            with self.subTest(launcher=launcher):
                opened = self.opened()
                expected: list[str] = []
                completed: list[tuple[str, Path]] = []
                for number in range(2):
                    reply = (self.run_dir / f"reply-{number}.md" if launcher == "explicit" else
                             self.run_dir / "phases/8/impl_reply.txt")
                    prompt = self.run_dir / f"work-{number}.md"
                    atomic_file.write_text_atomic(prompt, f"Complete work {number}.\n")
                    command = (["assign.sh", "--reply-file", str(reply)] if launcher == "explicit" else
                               ["bash", str(Path(__file__).resolve().parents[1] / "direct/assign.sh"),
                                "--role", "implement", "--to", "impl", "--session-dir", str(self.run_dir),
                                "--phase", "8", "--prompt", str(prompt)])
                    dispatch_id = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
                        worker_name=opened.row.name, worker_id=opened.row.id,
                        launcher_command=command,
                        prompt=prompt, kind="impl", lens="")
                    progress_history.save_dispatch_delivery(self.run_dir, dispatch_id,
                        pass_recorder.DispatchDelivery("fixture", dispatch_id, str(reply)))
                    atomic_file.write_text_atomic(reply, f"result {number}\n")
                    progress_history.complete_dispatch(self.run_dir, "impl", dispatch_id)
                    expected.insert(0, f"impl: phase 8; dispatch {dispatch_id}; reply {reply}")
                    completed.append((dispatch_id, reply))
                state_path = self.run_dir / unit_runs.STATE_FILE
                state = cast(dict[str, object], json.loads(state_path.read_text()))
                phase = cast(list[dict[str, object]], state["open_phases"])[0]
                with (self.run_dir / "history.jsonl").open("a") as history:
                    _ = history.write(json.dumps({"padding": "x" * worker.WORKER_HISTORY_BYTES}) + "\n")
                for placement in ("current", "parallel", "closed", "reopened"):
                    state["open_phases"] = {
                        "current": [phase],
                        "parallel": [phase, {"id": "9", "instance_id": "phase-nine", "status": "active"}],
                        "closed": [],
                        "reopened": [{**phase, "instance_id": f"new-phase-eight-{launcher}"}],
                    }[placement]
                    state["phase"] = {**phase, "status": "completed"} if placement in ("closed", "reopened") else {}
                    atomic_file.write_text_atomic(state_path, json.dumps(state))
                    before = state_path.read_bytes(), len(self.fixture.world.launches)
                    for attempt in range(2):
                        with self.subTest(placement=placement, attempt=attempt):
                            code, output, errors = self.command("restore")
                            self.assertEqual((code, errors), (0, ""))
                            replies = expected if placement in ("current", "parallel") else []
                            self.assertEqual(output.splitlines(), ["unit: workers unchanged", *replies])
                    self.assertEqual((state_path.read_bytes(), len(self.fixture.world.launches)), before)
                self.assertEqual(self.agent.submitted, [])
                code, output, errors = self.command("list", "--json")
                self.assertEqual((code, errors), (0, ""))
                listing = cast(list[dict[str, object]], json.loads(output))
                dispatch_id, reply = completed[-1]
                self.assertEqual({key: listing[0][key] for key in
                                  ("dispatch_id", "run_path", "completion_state", "reply_path")},
                                 {"dispatch_id": dispatch_id, "run_path": str(self.run_dir),
                                  "completion_state": "completed", "reply_path": str(reply)})

    def test_restore_reuses_given_runs_for_two_workers(self) -> None:
        first = self.fixture.worker_run("a-first", "unit", self.fixture.parent.current_session,
                                       director_run=True)
        for state in (self.opened("impl"), self.opened("review", "review")):
            _ = progress_history.begin_dispatch(self.run_dir, slot=state.row.name, phase="8", role="impl",
                worker_name=state.row.name, worker_id=state.row.id, launcher_command=["assign.sh"],
                prompt=self.prompt, kind="impl", lens="")
            _ = self.fixture.world.stop(state.row, self.fixture.world)
        for sibling in ("absent", "unreadable", "still listed", "replaced terminal", "proved gone"):
            with self.subTest(sibling=sibling):
                for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id):
                    _ = self.fixture.world.stop(row, self.fixture.world)
                pane = "w1:p-unread"
                self.fixture.world.locations[pane] = {"pane_id": pane, "terminal_id": "unread-terminal",
                    "workspace_id": "w1", "tab_id": "w1:t1", "label": "unread"}
                if sibling == "unreadable":
                    self.fixture.world.unknown[pane] = "private process facts are unavailable"
                elif sibling == "absent":
                    del self.fixture.world.locations[pane]
                before = self.fixture.world.process_reads.count(pane)
                first_starting = False
                missing_terminals: list[str] = []
                events: list[str] = []
                read_pane = self.fixture.world.result
                active_dispatch = progress_history.active_dispatch

                def process_info(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
                    reply = read_pane(*arguments, deadline=deadline)
                    if arguments == ("pane", "process-info", "--pane", pane):
                        terminal = str(self.fixture.world.locations[pane]["terminal_id"])
                        if sibling == "proved gone" and first_starting:
                            del self.fixture.world.locations[pane]
                            events.append("pane disappeared")
                            raise herdr_types.HerdrFailure("pane disappeared during the process read", code="pane_not_found")
                        if sibling in ("still listed", "replaced terminal"):
                            missing_terminals.append(terminal)
                            raise herdr_types.HerdrFailure(f"process facts for {terminal} are missing", code="pane_not_found")
                    return reply

                def before_startup(identity: staff.StaffId, run: Path
                                   ) -> progress_history.ActiveDispatch | progress_history.NoActiveDispatch | progress_history.ActiveDispatchUnavailable:
                    nonlocal first_starting
                    result = active_dispatch(identity, run)
                    if not first_starting:
                        first_starting = True
                        if sibling == "replaced terminal":
                            self.fixture.world.locations[pane]["terminal_id"] = "replacement-terminal"
                    return result

                def resume(row: staff.StaffRow, plan: worker.WorkerResume) -> worker.ExitCode:
                    del plan
                    events.append(row.name)
                    return worker.ExitCode.OK

                with redirect_stderr(io.StringIO()) as errors:
                    record = staff.snapshot()
                    runs = {str(self.fixture.parent.current_session): [str(first), str(self.run_dir)]}
                    with (patch.object(unit_runs, "held_by_session", return_value=runs) as searches,
                          patch.object(progress_history, "active_dispatch", side_effect=before_startup) as active,
                          patch.object(self.fixture.world, "result", new=process_info),
                          patch.object(worker, "_resume_launcher", new=resume),
                          redirect_stdout(io.StringIO())):
                        self.assertEqual(worker.restore(self.fixture.parent.id, record=record, runs=runs), worker.ExitCode.OK)
                    self.assertEqual(active.call_count, 4)
                    self.assertEqual(searches.call_count, 0)
                    self.assertEqual([call.args[1] for call in active.call_args_list], [self.run_dir, first] * 2)
                if sibling in ("unreadable", "still listed"):
                    self.assertEqual(self.fixture.world.process_reads.count(pane) - before, 1)
                    self.assertEqual(sum("pane evidence is unknown" in line
                                         for line in errors.getvalue().splitlines()), 1)
                if sibling == "replaced terminal":
                    self.assertEqual(missing_terminals, ["unread-terminal", "replacement-terminal"])
                    self.assertEqual(sum("pane evidence is unknown" in line
                                         for line in errors.getvalue().splitlines()), 2)
                if sibling == "proved gone":
                    self.assertEqual(events[:1], ["pane disappeared"])
                    self.assertCountEqual(events[1:], ["impl", "review"])
                    self.assertNotIn("pane evidence is unknown", errors.getvalue())
                _ = self.fixture.world.locations.pop(pane, None)
                _ = self.fixture.world.unknown.pop(pane, None)

    def test_restore_resumes_a_stopped_idle_worker_with_live_runs_and_exact_context(self) -> None:
        self.assert_restore_live_run(active=False)

    def test_restore_resumes_a_stopped_busy_worker_with_live_runs_and_exact_context(self) -> None:
        self.assert_restore_live_run(active=True)
        for cause in ("saved deferral", "orphan", "unidentified at reap", "unidentified at startup",
                      "assignment changes after reap", "pane closes during recovery",
                      "legacy result without the prompt", "result with unknown ownership"):
            with self.subTest(parked_result=cause):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    case.check_restore_parked_account(cause)
                finally:
                    _ = case.doCleanups()

    def test_restore_checks_the_saved_result_receipt_on_a_second_invocation(self) -> None:
        for evidence in (
            "valid", "different session", "different pane", "different prefix", "different input",
            "different run id", "different worker", "different folder", "conflicting pass",
            "wrong record id", "wrong record owner", "wrong record folder",
            "absent", "malformed", "previous pass", "previous stage", "idle", "closed",
        ):
            with self.subTest(evidence=evidence):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    case.check_restore_parked_account("legacy result without the prompt", retry=evidence)
                finally:
                    _ = case.doCleanups()

    def test_restore_rejects_incomplete_result_receipt_evidence(self) -> None:
        for evidence in ("partial input", "missing digest", "invalid digest", "empty saved stage",
                         "current stage lost", "malformed outer record", "different terminal"):
            with self.subTest(evidence=evidence):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    case.check_restore_parked_account("legacy result without the prompt", retry=evidence)
                finally:
                    _ = case.doCleanups()

    def test_restore_reports_director_notification_separately_from_acceptance(self) -> None:
        opened = self.opened()
        row = opened.row
        instance = progress_history.begin_dispatch(self.run_dir, slot=row.name, phase="8", role="impl",
            worker_name=row.name, worker_id=row.id, launcher_command=["assign.sh"], prompt=self.prompt, kind="impl", lens="")
        progress_history.save_dispatch_delivery(self.run_dir, instance,
            pass_recorder.DispatchDelivery(f"Review the files. End your final reply with DONE {instance}.",
                                           instance, str(self.reply)))
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        report = worker.RestoreReport()
        with patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK) as resume:
            self.assertIs(worker.restore(self.fixture.parent.id, only=frozenset({row.id}), report=report), worker.ExitCode.OK)
        resume.assert_called_once()
        self.assertEqual(report.restored, set())
        self.assertEqual(report.skipped[row.id], "The director received the launcher instruction")

    def test_restore_reports_no_worker_mode_as_an_intentional_omission(self) -> None:
        from scripts.whoami import quota_state

        opened = self.opened()
        row = opened.row
        self.assertIsInstance(staff.backend(row), staff.CodexBackend)
        instance = progress_history.begin_dispatch(self.run_dir, slot=row.name, phase="8", role="impl",
            worker_name=row.name, worker_id=row.id, launcher_command=["assign.sh"], prompt=self.prompt, kind="impl", lens="")
        self.agent.append(opened.pane, "user", f"End your final reply with DONE {instance}.")
        _ = self.fixture.world.stop(row, self.fixture.world)
        report = worker.RestoreReport()
        with (patch.object(quota_state, "no_worker_mode", return_value=True),
              patch.object(worker_panes, "start_or_resume") as start):
            self.assertIs(worker.restore(self.fixture.parent.id, only=frozenset({row.id}), report=report), worker.ExitCode.OK)
        start.assert_not_called()
        self.assertEqual(report.restored, set())
        self.assertEqual(report.skipped[row.id], "No-worker mode keeps this worker stopped")

    def test_restore_requires_assignment_acceptance_before_using_a_result_receipt(self) -> None:
        for state in ("working", "idle", "stopped", "closed"):
            with self.subTest(state=state):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    case.check_unaccepted_result(state)
                finally:
                    _ = case.doCleanups()

    def test_restore_filters_metadata_from_assignment_evidence(self) -> None:
        self.check_unaccepted_result("working", assignment="metadata only")

    def test_restore_accepts_a_real_assignment_after_metadata(self) -> None:
        self.check_unaccepted_result("working", assignment="real after metadata")

    def check_unaccepted_result(self, state: str, *, assignment: str = "absent") -> None:
        opened = self.opened()
        row, pane = opened.row, opened.pane
        instance = progress_history.begin_dispatch(self.run_dir, slot=row.name, phase="8", role="impl",
            worker_name=row.name, worker_id=row.id, launcher_command=["assign.sh"], prompt=self.prompt, kind="impl", lens="")
        line = f"Review the files. End your final reply with DONE {instance}."
        progress_history.save_dispatch_delivery(self.run_dir, instance, pass_recorder.DispatchDelivery(
            line, instance, str(self.reply)))
        if assignment != "absent":
            backend = staff.backend(row)
            assert isinstance(backend, staff.CodexBackend)
            source = worker_admission.transcript_source(row, backend)
            with source.open("a", encoding="utf-8") as stream:
                _ = stream.write(json.dumps({"type": "user", "isMeta": True,
                                             "message": {"role": "user", "content": line}}) + "\n")
            if assignment == "real after metadata":
                self.agent.append(pane, "user", line)
        path, record = self.detached_record(row, "never-accepted")
        record.update(pane_dispatch_id=instance, exit_code=0, delivery="deferred: the worker was busy",
                      finished_at=datetime.now(timezone.utc).isoformat())
        atomic_file.write_text_atomic(path, json.dumps(record))
        send_input = self.fixture.world.run

        def accept_result(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:3] == ("agent", "prompt", pane):
                self.agent.append(pane, "user", arguments[3])
            return send_input(*arguments, deadline=deadline)

        self.fixture.world.locations[pane]["agent_status"] = "idle"
        with patch.object(self.fixture.world, "run", new=accept_result):
            self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                             f"sent: worker {row.name} at {pane}")
        if state == "closed":
            self.fixture.world.remove_pane(pane)
        elif state == "stopped":
            _ = self.fixture.world.stop(row, self.fixture.world)
        else:
            self.fixture.world.locations[pane]["agent_status"] = state
        report = worker.RestoreReport()
        launches = len(self.fixture.world.launches)
        accepted = assignment == "real after metadata"
        with patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK) as resume:
            result = worker.restore(self.fixture.parent.id, only=frozenset({row.id}), report=report)
        if accepted:
            self.assertIs(result, worker.ExitCode.OK)
            self.assertEqual(report.restored, {row.id})
            self.assertEqual(report.failures, {})
        else:
            self.assertIs(result, worker.ExitCode.UNCERTAIN)
            self.assertIn("does not prove assignment acceptance", report.failures[row.id][1])
            self.assertEqual(report.restored, set())
        dispatch = progress_history.active_dispatch(row.id, self.run_dir)
        assert isinstance(dispatch, progress_history.ActiveDispatch)
        self.assertIsInstance(worker.resume_receipt(row, dispatch),
                              worker.DispatchAwaitingReply if accepted else worker.DispatchNotSubmitted)
        self.assertEqual(len(self.fixture.world.launches), launches)
        resume.assert_not_called()

    def test_claude_launcher_requires_conversational_assignment_evidence(self) -> None:
        from scripts.direct import claude_worker

        config = self.fixture.root / "conf" / "direct.conf"
        with config.open("a", encoding="utf-8") as stream:
            _ = stream.write("\n# whole: 1..300\nCLAUDE_WORKER_REGISTER_WAIT_SECS=1\n")
        row = self.fixture.worker("claude-retry", family="claude")
        turn = claude_worker.ClaudeTurn(row.name, self.run_dir, self.fixture.root, self.prompt, self.reply,
            self.run_dir / "claude.log", "", "", "", str(row.id), False, {**os.environ, "CLAUDE_BIN": "/fixture/claude"})
        allowed = worker_admission.admit(row.name, self.run_dir, reference=str(row.id))
        request = worker_admission.assignment_id(self.prompt)
        worker_admission.record_dispatch(allowed, request, worker_admission.SwitchState.DISPATCH_IN_FLIGHT)
        source = worker_admission.transcript_source(row, staff.ClaudeBackend())
        entry = {"type": "user", "isMeta": True, "message": {"role": "user", "content": self.prompt.read_text()}}
        atomic_file.write_text_atomic(source, json.dumps(entry) + "\n")

        with (patch.object(claude_worker, "_deliver") as deliver, patch.object(claude_worker, "_start") as start,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertIsInstance(claude_worker.open_worker(turn), claude_worker.Delivered)
            deliver.assert_called_once()
            self.assertEqual(deliver.call_args.args[1], row.current_session)
            self.assertIs(worker_admission.dispatch_record(self.run_dir, row.name).state,
                          worker_admission.SwitchState.ASSIGNMENT_ACCEPTED)

            entry["isMeta"] = False
            with source.open("a", encoding="utf-8") as stream:
                _ = stream.write(json.dumps(entry) + "\n")
            worker_admission.record_dispatch(allowed, request, worker_admission.SwitchState.DISPATCH_IN_FLIGHT)
            deliver.reset_mock()
            self.assertIsInstance(claude_worker.open_worker(turn), claude_worker.Unchanged)
            deliver.assert_not_called()
            start.assert_not_called()
        self.assertIs(worker_admission.dispatch_record(self.run_dir, row.name).state,
                      worker_admission.SwitchState.ASSIGNMENT_ACCEPTED)

    def test_restart_plain_report_directs_inspection_of_an_unresolved_worker(self) -> None:
        self.check_restore_parked_account("legacy result without the prompt", retry="plain report")

    def test_restart_bulk_report_directs_inspection_of_an_unresolved_worker(self) -> None:
        self.check_restore_parked_account("legacy result without the prompt", retry="bulk report")

    def test_restore_keeps_an_accepted_result_for_director_review(self) -> None:
        cases = ("retained working", "retained idle", "retained stopped", "retained closed",
                 "cleared idle", "cleared stopped", "cleared closed", "creation crash",
                 "continuation crash", "clearance crash", "changed during clearance", "waiting for runs")
        for evidence in cases:
            with self.subTest(evidence=evidence):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    case.check_restore_parked_account("legacy result without the prompt", retry="review " + evidence)
                finally:
                    _ = case.doCleanups()

    def check_result_review(self, row: staff.StaffRow, pane: str, prior_park: dict[str, object],
                            evidence: str, *, report_format: Literal["plain report", "bulk report", "none"] = "none") -> None:
        from scripts.agents import restart, shutdown_record

        original_park = progress_history.record_worker_park
        deliver = pass_recorder.dispatch_delivery(prior_park)
        instance = str(prior_park["instance_id"])
        saved_launch = progress_history.parked_worker_launch(prior_park)
        assert isinstance(saved_launch, progress_history.ParkedWorkerLaunch)
        saved_launch = replace(saved_launch, model="retained-result-model")
        parked = not evidence.startswith("cleared") and evidence not in ("creation crash", "clearance crash")
        if parked:
            self.assertTrue(original_park(self.run_dir, row.id, instance, deliver.completion_id, launch=saved_launch))
        expects_working = evidence in ("retained working", "changed during clearance")
        if evidence == "creation crash":
            backend = staff.backend(row)
            assert isinstance(backend, staff.CodexBackend)
            source = worker_admission.transcript_source(row, backend)
            transcript = source.read_bytes()
            self.fixture.world.remove_pane(pane)
            replacement = "w1:p99"
            with patch.dict(os.environ, {"CODEX_HOME": str(backend.codex_home)}):
                self.fixture.world.add(row, replacement, family="codex")
            record = staff.snapshot()
            observed = worker_panes.observe_worker(self.fixture.parent.id, row, record)
            assert isinstance(observed, worker_panes.OpenWorker)
            self.assertNotEqual(observed.pane, pane)
            self.assertEqual(observed.pane, replacement)
            self.assertEqual(observed.row.current_session, row.current_session)
            self.assertEqual(worker_admission.transcript_source(observed.row, backend).read_bytes(), transcript)
        elif evidence.endswith("closed"):
            self.fixture.world.remove_pane(pane)
        elif evidence.endswith("stopped"):
            _ = self.fixture.world.stop(row, self.fixture.world)
        else:
            self.fixture.world.locations[pane]["agent_status"] = "working" if expects_working else "idle"

        sent: list[tuple[str, str]] = []
        before = len(self.fixture.world.launches)
        run_command = self.fixture.world.run
        restart_entry = [str(row.id)]
        shutdown_record.save(restart_entry)

        def record_input(*args: str, deadline: float = math.inf) -> str:
            if args[:2] == ("agent", "prompt"):
                sent.append((args[2], args[3]))
            return run_command(*args, deadline=deadline)

        def change_stage(run: Path, _identity: staff.StaffId, pass_id: str, _stage: str, *,
                         launch: progress_history.ParkedWorkerLaunch | progress_history.ClearWorkerPark) -> bool:
            self.assertIs(launch, progress_history.ClearWorkerPark.MARKER)
            progress_history.save_dispatch_delivery(run, pass_id, replace(deliver, completion_id="changed-stage"))
            return False

        def restore(report: worker.RestoreReport) -> worker.ExitCode:
            return worker.restore(self.fixture.parent.id, only=frozenset({row.id}), report=report)

        with (patch.object(self.fixture.world, "run", new=record_input),
              patch.object(turn_end, "recover_completion", return_value=worker.CompletionRecovery.WAITING_ON_REPLY)
              as recovery):
            if evidence == "waiting for runs":
                recovery.return_value = worker.CompletionRecovery.WAITING_ON_RUNS
                failure = worker.RestoreReport()
                self.assertIs(restore(failure), worker.ExitCode.UNCERTAIN)
                self.assertEqual(failure.restored, set())
                self.assertEqual(sent, [])
                self.assertTrue(progress_history.parked_worker_pass(worker_panes.worker_pass(row, self.run_dir)))
                recovery.return_value = worker.CompletionRecovery.WAITING_ON_REPLY
            if evidence == "changed during clearance":
                failure = worker.RestoreReport()
                with patch.object(progress_history, "record_worker_park", new=change_stage):
                    self.assertIs(restore(failure), worker.ExitCode.UNCERTAIN)
                self.assertEqual(failure.restored, set())
                self.assertIn("changed before park clearance", failure.failures[row.id][1])
                self.assertEqual(sent, [])
                self.assertEqual(self.fixture.world.launches[before:], [])
                return
            for _attempt in range(2):
                report = worker.RestoreReport()
                result = restore(report)
                current = worker_panes.worker_pass(row, self.run_dir)
                if expects_working:
                    self.assertIs(result, worker.ExitCode.OK, report.failures)
                    self.assertEqual(report.restored, {row.id})
                    self.assertFalse(progress_history.parked_worker_pass(current))
                else:
                    self.assertIs(result, worker.ExitCode.UNCERTAIN, report.failures)
                    self.assertEqual(report.restored, set())
                    self.assertIn("The director must inspect the worker", report.failures[row.id][1])
                    self.assertEqual(progress_history.parked_worker_pass(current), parked)
                    if parked:
                        self.assertEqual(progress_history.parked_worker_launch(current), saved_launch)
                    owner = self.fixture.parent
                    results = restart.WorkerRestoreReports({owner.id: (owner, report)})
                    self.assertNotIn(row.id, results.resolved(owner.id))
                    self.assertEqual(shutdown_record.read(), restart_entry)
                    if evidence == "creation crash":
                        self.assertIn("result delivery is uncertain", report.failures[row.id][1])
                    if _attempt == 0 and report_format != "none":
                        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                            if report_format == "bulk report":
                                def report_bulk() -> int:
                                    results.report()
                                    return 0

                                self.assertEqual(restart.run_command(report_bulk, restart.BulkCommand()), 0)
                                self.assertIn("The affected directors inspect the workers.", output.getvalue())
                                message = output.getvalue()
                            else:
                                results.report()
                                message = errors.getvalue()
                        self.assertIn("The director must inspect the worker.", message)
                        self.assertNotIn("worker.py restore", message)
                        self.assertEqual(shutdown_record.read(), restart_entry)
        self.assertEqual(sent, [])
        self.assertEqual(self.fixture.world.launches[before:], [])

    def check_restore_parked_account(self, cause: str, *, retry: str = "") -> None:
        from scripts.whoami import agent_accounts

        source = Path(__file__).resolve().parents[2] / "config"
        for config in ("detached_runs.conf", "postmaster.conf", "codex_daemon.conf"):
            atomic_file.write_text_atomic(self.fixture.root / "conf" / config, (source / config).read_text())
        name = "parked-account"
        self.agent.finish_after = 1
        self.assertIs(self.dispatch(name), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        row = self.agent.row(pane)
        active = progress_history.active_dispatch(row.id, self.run_dir)
        assert isinstance(active, progress_history.ActiveDispatch)
        path = self.run_dir / worker_lifecycle.DETACHED_RUNS_DIR / f"{name}.json"
        path.parent.mkdir(exist_ok=True)
        with pane_fixtures.live_detached_run(self.run_dir, row):
            record = worker_lifecycle.read_json_object(path)
            record["pane_dispatch_id"] = active.instance_id
            atomic_file.write_text_atomic(path, json.dumps(record))
            self.agent.without_done = True
            self.agent.complete(pane)
            self.assertEqual(self.finish(name), turn_end.TurnOutcome.WAITING_ON_RUNS)
            self.assertNotIn(pane, self.fixture.world.locations)
        record["exit_code"] = 0
        if cause in ("legacy result without the prompt", "result with unknown ownership"):
            if cause == "legacy result without the prompt":
                del record["pane_dispatch_id"]
            else:
                record["pane_dispatch_id"] = {"unavailable": "the fixture could not read the pass"}
            backend = staff.backend(row)
            assert isinstance(backend, staff.CodexBackend)
            transcript = worker_admission.transcript_source(row, backend)
            atomic_file.write_text_atomic(transcript, "")
        if cause != "orphan":
            record.update(status="finished", completion_pending=True,
                          finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        atomic_file.write_text_atomic(path, json.dumps(record))
        if cause == "orphan":
            self.assertEqual(record["status"], "running")
            self.assertNotIn("delivery", record)
        else:
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        run_command = self.fixture.world.run
        received: list[str] = []

        def accept_result(*arguments: str, deadline: float = math.inf) -> str:
            result = run_command(*arguments, deadline=deadline)
            if arguments[:2] == ("agent", "prompt") and f"Detached run: {name}\n" in arguments[3]:
                target, text = arguments[2:4]
                received.append(target)
                self.agent.append(target, "user", text)
                self.fixture.world.locations[target]["agent_status"] = "working"
            return result

        target_home = self.fixture.root / ".codex2"
        started_panes: dict[staff.StaffId, str] = {}
        launches = len(self.fixture.world.launches)
        reap = detached_runs.reap_orphans
        launch_policy = worker_panes.parked_launch_policy

        def add_unidentified_pane() -> None:
            sibling = self.fixture.worker("unidentified", family="codex")
            self.fixture.world.add(sibling, "w1:p9", family="codex")
            self.fixture.world.unknown["w1:p9"] = "process evidence is unavailable"

        def add_before_reap(run: str, name: str = "") -> list[str]:
            add_unidentified_pane()
            return reap(run, name)

        def add_before_startup(member: staff.StaffRow, passed: dict[str, object]
                               ) -> worker_panes.LaunchPolicy | progress_history.ParkedLaunchUnavailable:
            add_unidentified_pane()
            return launch_policy(member, passed)

        replacement = ""

        def replace_after_reap(run: str, name: str = "") -> list[str]:
            nonlocal replacement
            reaped = reap(run, name)
            if received and not replacement:
                progress_history.complete_dispatch(self.run_dir, "impl", active.instance_id)
                replacement = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
                    worker_name=row.name, worker_id=row.id, launcher_command=["assign.sh"], prompt=self.prompt,
                    kind="impl", lens="")
            return reaped

        def close_during_recovery(*_args: object, **_kwargs: object) -> worker.CompletionRecovery:
            self.assertIn(pane, self.fixture.world.locations)
            self.fixture.world.remove_pane(pane)
            return worker.CompletionRecovery.NO_PENDING_NOTICE

        parked_before = worker_panes.worker_pass(row, self.run_dir)
        resumed: list[worker.WorkerResume] = []

        def capture_resume(_member: staff.StaffRow, plan: worker.WorkerResume) -> worker.ExitCode:
            self.assertEqual(plan.step, worker.UnsubmittedAssignment(replacement, ("assign.sh",)))
            resumed.append(plan)
            return worker.ExitCode.OK

        def record_resume(_member: staff.StaffRow, plan: worker.WorkerResume) -> worker.ExitCode:
            resumed.append(plan)
            return worker.ExitCode.OK

        report = worker.RestoreReport()
        with (ExitStack() as context, patch.object(self.fixture.world, "run", side_effect=accept_result),
              patch.object(agent_accounts, "codex_account_home",
                           return_value=agent_accounts.CodexHomeHoldingLogin("codex2", target_home, "fixture"))):
            if cause == "unidentified at reap":
                _ = context.enter_context(patch.object(detached_runs, "reap_orphans", new=add_before_reap))
            if cause == "unidentified at startup":
                _ = context.enter_context(patch.object(worker_panes, "parked_launch_policy", new=add_before_startup))
            if cause == "assignment changes after reap":
                _ = context.enter_context(patch.object(detached_runs, "reap_orphans", new=replace_after_reap))
                _ = context.enter_context(patch.object(worker, "_resume_launcher", new=capture_resume))
            if cause in ("legacy result without the prompt", "result with unknown ownership"):
                _ = context.enter_context(patch.object(worker, "_resume_launcher", new=record_resume))
            if cause == "pane closes during recovery":
                self.fixture.world.add(row, pane, family="codex")
                self.fixture.world.locations[pane]["agent_status"] = "working"
                _ = context.enter_context(patch.object(detached_runs, "reap_orphans", return_value=[]))
                _ = context.enter_context(patch.object(turn_end, "recover_completion", side_effect=close_during_recovery))
            outcome = worker.restore(self.fixture.parent.id, account="codex2", only=frozenset({row.id}),
                                     started_panes=started_panes, report=report)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        if cause == "assignment changes after reap":
            self.assertTrue(replacement)
            self.assertEqual(len(resumed), 1, report.failures)
        if cause == "pane closes during recovery":
            self.assertIs(outcome, worker.ExitCode.OK)
            current = worker_panes.worker_pass(row, self.run_dir)
            self.assertTrue(progress_history.parked_worker_pass(current))
            self.assertEqual(progress_history.parked_worker_launch(current),
                             progress_history.parked_worker_launch(parked_before))
            self.assertNotIn(pane, self.fixture.world.locations)
            return
        if cause in ("legacy result without the prompt", "result with unknown ownership"):
            self.assertEqual(resumed, [])
        if cause == "result with unknown ownership":
            self.assertIs(outcome, worker.ExitCode.UNCERTAIN)
            self.assertIn("result delivery is uncertain", report.failures[row.id][1])
            self.assertEqual(len(self.fixture.world.launches), launches)
            self.assertEqual(started_panes, {})
            self.assertEqual(received, [])
            self.assertTrue(progress_history.parked_worker_pass(worker_panes.worker_pass(row, self.run_dir)))
            return
        if cause.startswith("unidentified"):
            self.assertIs(outcome, worker.ExitCode.UNCERTAIN)
            self.assertEqual(len(self.fixture.world.launches), launches)
            self.assertEqual(started_panes, {})
            self.assertEqual(received, [])
            return
        self.assertIs(outcome, worker.ExitCode.OK)
        self.assertEqual(worker_lifecycle.read_json_object(path)["status"], "finished")
        self.assertEqual(received, [started_panes[row.id]])
        self.assertEqual(self.fixture.world.requests[-1][2]["CODEX_HOME"], str(target_home))
        self.assertEqual(self.agent.row(started_panes[row.id]).current_session, row.current_session)
        if cause == "legacy result without the prompt":
            self.assertEqual(report.restored, {row.id})
            saved_record = worker_lifecycle.read_json_object(path)
            self.assertIn("worker_result_receipt", saved_record)
            evidence = detached_runs.read_worker_result_receipt(saved_record)
            assert isinstance(evidence, detached_runs.WorkerResultReceipt)
            self.assertIs(evidence.park_acceptance, detached_runs.ParkedAssignmentAcceptance.VERIFIED)
            if retry:
                reopened = started_panes[row.id]
                if retry.startswith("review "):
                    self.check_result_review(self.agent.row(reopened), reopened, parked_before,
                                             retry.removeprefix("review "))
                elif retry in ("plain report", "bulk report"):
                    self.check_result_review(self.agent.row(reopened), reopened, parked_before, "retained idle",
                                             report_format=retry)
                else:
                    self.check_saved_result_restore(self.agent.row(reopened), reopened, active, path, retry)

    def check_saved_result_restore(self, row: staff.StaffRow, pane: str, active: progress_history.ActiveDispatch,
                                   path: Path, evidence: str) -> None:
        passed = worker_panes.worker_pass(row, self.run_dir)
        self.assertFalse(progress_history.parked_worker_pass(passed))
        original = pass_recorder.dispatch_delivery(passed)
        backend = staff.backend(row)
        assert isinstance(backend, staff.CodexBackend)
        source = worker_admission.transcript_source(row, backend)
        record = worker_lifecycle.read_json_object(path)
        receipt = cast(dict[str, object], record["worker_result_receipt"])
        expected_pass = active.instance_id
        expected_command = active.launcher_command
        if evidence == "different session":
            transcript = source.read_bytes()
            self.fixture.world.remove_pane(pane)
            row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("new-receipt-session")))
            self.fixture.world.add(row, pane, family="codex")
            _ = worker_admission.transcript_source(row, backend).write_bytes(transcript)
            self.fixture.world.locations[pane]["agent_status"] = "working"
        elif evidence == "different pane":
            receipt["pane"] = "w1:p999"
        elif evidence == "different terminal":
            receipt["terminal"] = "another-terminal"
        elif evidence == "different prefix":
            first, separator, rest = source.read_text().partition("\n")
            fields = cast(dict[str, object], json.loads(first))
            fields["fixture_rewrite"] = True
            atomic_file.write_text_atomic(source, json.dumps(fields) + separator + rest)
        elif evidence in ("different input", "partial input"):
            self.agent.append(pane, "user", f"Detached run: {record['run_id']}\nPartial message."
                              if evidence == "partial input" else "An ordinary input.")
            stream = worker_panes.transcript_stream(row, source)
            _, cursor = list(worker_panes.transcript_record_cursors(
                worker_admission.transcript_records(source), stream))[-1]
            receipt["cursor"] = asdict(cursor)
        elif evidence == "missing digest":
            _ = receipt.pop("input_digest", None)
        elif evidence == "invalid digest":
            receipt["input_digest"] = "invalid digest"
        elif evidence in ("empty saved stage", "current stage lost"):
            progress_history.save_dispatch_delivery(self.run_dir, active.instance_id, replace(original, completion_id=""))
            if evidence == "empty saved stage":
                receipt["completion_id"] = ""
        elif evidence == "malformed outer record":
            record["command"] = "not an argument list"
        elif evidence == "different run id":
            receipt["run_id"] = "different-run"
        elif evidence == "different worker":
            receipt["worker"] = self.fixture.parent.id
        elif evidence == "different folder":
            receipt["session_dir"] = str(self.fixture.root)
        elif evidence == "conflicting pass":
            record["pane_dispatch_id"] = active.instance_id
            receipt["pass_id"] = "different-pass"
        elif evidence == "wrong record id":
            record["run_id"] = "different-record"
        elif evidence == "wrong record owner":
            record["staff_id"] = self.fixture.parent.id
        elif evidence == "wrong record folder":
            record["session_dir"] = str(self.fixture.root)
        elif evidence == "absent":
            del record["worker_result_receipt"]
        elif evidence == "malformed":
            record["worker_result_receipt"] = "invalid receipt"
        elif evidence == "previous pass":
            progress_history.complete_dispatch(self.run_dir, "impl", active.instance_id)
            expected_pass = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
                worker_name=row.name, worker_id=row.id, launcher_command=["assign.sh"], prompt=self.prompt,
                kind="impl", lens="")
            expected_command = ("assign.sh",)
            progress_history.save_dispatch_delivery(self.run_dir, expected_pass,
                replace(original, line=original.line.replace(original.completion_id, expected_pass),
                        completion_id=expected_pass))
        elif evidence == "previous stage":
            second_stage = worker_panes.stage_two_completion_id(original.completion_id)
            progress_history.save_dispatch_delivery(self.run_dir, active.instance_id,
                replace(original, line=original.line.replace(original.completion_id, second_stage),
                        completion_id=second_stage))
        elif evidence == "idle":
            self.fixture.world.locations[pane]["agent_status"] = "idle"
        elif evidence == "closed":
            self.fixture.world.remove_pane(pane)

        atomic_file.write_text_atomic(path, json.dumps(record))
        report = worker.RestoreReport()
        steps: list[worker.WorkerResume] = []
        launches = len(self.fixture.world.launches)

        def capture_resume(_member: staff.StaffRow, plan: worker.WorkerResume,
                           **_options: object) -> worker.ExitCode:
            steps.append(plan)
            if evidence in ("previous pass", "previous stage"):
                delivery = pass_recorder.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
                self.agent.append(pane, "user", delivery.line)
            return worker.ExitCode.OK

        with patch.object(worker, "_resume_launcher", new=capture_resume):
            result = worker.restore(self.fixture.parent.id, only=frozenset({row.id}), report=report)
        if evidence == "valid":
            self.assertIs(result, worker.ExitCode.OK, report.failures)
            self.assertEqual(report.restored, {row.id})
            self.assertEqual(steps, [])
            self.assertEqual(len(self.fixture.world.launches), launches)
        elif evidence in ("previous pass", "previous stage"):
            self.assertIs(result, worker.ExitCode.OK, report.failures)
            self.assertEqual(report.restored, {row.id})
            self.assertEqual([step.step for step in steps], [worker.UnsubmittedAssignment(expected_pass, expected_command)])
        elif evidence in ("idle", "closed"):
            self.assertIs(result, worker.ExitCode.UNCERTAIN, report.failures)
            self.assertEqual(report.restored, set())
            self.assertEqual(steps, [])
            self.assertEqual(len(self.fixture.world.launches), launches)
            self.assertIn("The director must inspect the worker", report.failures[row.id][1])
        else:
            self.assertIs(result, worker.ExitCode.UNCERTAIN, report.failures)
            self.assertIn("result delivery is uncertain", report.failures[row.id][1])
            self.assertEqual(report.restored, set())
            self.assertEqual(steps, [])
            self.assertEqual(len(self.fixture.world.launches), launches)


    def assert_restore_live_run(self, *, active: bool) -> None:
        from scripts.whoami import agent_accounts

        opened = self.opened("active-run" if active else "idle-run")
        dispatch_id = ""
        if active:
            dispatch_id = progress_history.begin_dispatch(self.run_dir, slot=opened.row.name, phase="8", role="impl",
                worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
                prompt=self.prompt, kind="impl", lens="")
            self.agent.append(opened.pane, "user", f"End your final reply with DONE {dispatch_id}.")
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        run = self.run_dir / "detached_runs" / opened.row.name
        context = (f"Detached run {opened.row.name} remains live. "
                   + f"Result: {run}.json; log: {run}.log. Read it with: `cat -- {run}.json {run}.log`. "
                   + "Its result follows this worker after the move.")
        target = self.fixture.root / ".codex2"
        target.mkdir()
        (target / "sessions").symlink_to(self.fixture.root / "codex/sessions", target_is_directory=True)
        with (pane_fixtures.live_detached_run(self.run_dir, opened.row), redirect_stdout(io.StringIO()),
              patch.object(agent_accounts, "codex_account_home", return_value=agent_accounts.CodexHomeHoldingLogin(
                  "codex2", self.fixture.root / ".codex2", "fixture")),
              patch.object(accounts, "account_directory_readiness", return_value=accounts.AccountDirectoryReady("fixture"))):
            code = worker.restore(self.fixture.parent.id, account="codex2", only=frozenset({opened.row.id}))
        self.assertEqual(code, worker.ExitCode.OK)
        self.assertEqual(self.fixture.world.requests[-1][2]["CODEX_HOME"], str(self.fixture.root / ".codex2"))
        prompt = self.fixture.prompts()[-1]
        self.assertEqual(prompt[2], opened.pane)
        self.assertEqual(prompt[3], (
            '<system-reminder source="script">\nYou were stopped and resumed because .\n'
            + f"Continue pass {dispatch_id} in this conversation. Keep the same pass id.\n"
            + context + "\n</system-reminder>" if active else
            '<system-reminder source="script"> ' + context + " </system-reminder>"))
        self.assertEqual(self.fixture.world.closed, [])
        current = staff.snapshot().by_id(opened.row.id)
        assert current is not None
        self.assertEqual(current.current_session, opened.row.current_session)

    def test_restore_starts_only_the_worker_with_an_active_dispatch(self) -> None:
        for cause in ("active dispatch", "renamed", "unreadable sibling pane",
                      "pane changed at lock", "pane changed before startup", "enclosing observation snapshot"):
            with self.subTest(cause=cause):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    case.check_restore_active_dispatch(cause)
                finally:
                    _ = case.doCleanups()

    def check_restore_active_dispatch(self, cause: str) -> None:
        idle = self.opened("review", "review")
        _ = self.fixture.world.stop(idle.row, self.fixture.world)
        self.assertIsInstance(worker_panes.close(self.fixture.parent.id, staff.WorkerName.parse(idle.row.name)),
                              worker_panes.Closed)
        busy = self.opened()
        dispatch_id = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name=busy.row.name, worker_id=busy.row.id, launcher_command=["assign.sh", "--to", "impl"],
            prompt=self.prompt, kind="impl", lens="")
        _ = self.fixture.world.stop(busy.row, self.fixture.world)
        if cause == "unreadable sibling pane":
            self.fixture.world.remove_pane(busy.pane)
            self.fixture.world.add(idle.row, "w1:p9")
            self.fixture.world.unknown["w1:p9"] = "process evidence is unavailable"
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        renamed = cause == "renamed"
        acquire = worker_panes.worker_lock
        active_dispatch = progress_history.active_dispatch
        submitted = list(self.agent.submitted)
        pass_before = worker_panes.worker_pass(busy.row, self.run_dir)

        def change_before_startup(identity: staff.StaffId, run: Path
                                  ) -> progress_history.ActiveDispatch | progress_history.NoActiveDispatch | progress_history.ActiveDispatchUnavailable:
            result = active_dispatch(identity, run)
            if identity == busy.row.id:
                self.fixture.world.unknown[busy.pane] = "pane process changed before startup"
            return result

        @contextmanager
        def change_pane(parent: staff.StaffId, name: staff.WorkerName
                        ) -> Generator[worker_panes.WorkerLockHold | worker_panes.Kept]:
            with acquire(parent, name) as hold:
                if name.value == busy.row.name:
                    self.fixture.world.unknown[busy.pane] = "pane process changed at the worker lock"
                yield hold

        with ExitStack() as context, patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK) as resume:
            if renamed:
                context.enter_context(self.rename_at_worker_lock(busy.row, "renamed-impl"))
            if cause == "pane changed at lock":
                _ = context.enter_context(patch.object(worker_panes, "worker_lock", new=change_pane))
            if cause == "pane changed before startup":
                _ = context.enter_context(patch.object(progress_history, "active_dispatch", new=change_before_startup))
            if cause == "enclosing observation snapshot":
                context.enter_context(herdr.observation_snapshot())
                self.fixture.world.unknown[busy.pane] = "earlier process evidence is unavailable"
                with redirect_stderr(io.StringIO()):
                    prior = worker_panes.observe_worker(self.fixture.parent.id, busy.row, staff.snapshot())
                self.assertIsInstance(prior, worker_panes.ProcessUnknownWorker)
                del self.fixture.world.unknown[busy.pane]
            if cause in ("renamed", "unreadable sibling pane", "pane changed at lock", "pane changed before startup"):
                code, _, errors = self.command("restore")
                self.assertEqual(code, worker.ExitCode.UNCERTAIN)
                if cause == "unreadable sibling pane":
                    self.assertIn("absence is unproved", errors)
                if cause == "pane changed at lock":
                    self.assertIn("pane process changed at the worker lock", errors)
                if cause == "pane changed before startup":
                    self.assertIn("pane process changed before startup", errors)
                resume.assert_not_called()
                self.assertEqual(len(self.fixture.world.launches), before[1])
                saved = worker_panes.worker_pass(busy.row, self.run_dir)
                self.assertEqual((saved["instance_id"], saved["status"]), (dispatch_id, "active"))
                if cause == "pane changed before startup":
                    self.assertEqual(saved, pass_before)
                    self.assertEqual(self.agent.submitted, submitted)
                current = staff.by_id(busy.row.id)
                self.assertEqual(current, replace(busy.row, name="renamed-impl") if renamed else busy.row)
                return
            self.assertEqual(self.command("restore"), (0, f"{self.fixture.parent.name}: workers restored\n", ""))
        resume.assert_called_once_with(replace(busy.row, role=staff.KnownWorkerRole("impl")),
            worker.WorkerResume(worker.UnsubmittedAssignment(dispatch_id, ("assign.sh", "--to", busy.row.name))))
        self.assertEqual(self.fixture.staff_state(), before[0])
        self.assertEqual(len(self.fixture.world.launches), before[1] + 1)
        states = {state.row.id: state for state in worker_panes.observe(self.fixture.parent.id, staff.snapshot())}
        self.assertIsInstance(states[busy.row.id], worker_panes.OpenWorker)
        resumed = states[busy.row.id]
        assert isinstance(resumed, worker_panes.OpenWorker)
        self.assertEqual(resumed.pane, busy.pane)
        self.assertEqual(resumed.terminal, busy.terminal)
        self.assertIsInstance(states[idle.row.id], worker_panes.ClosedWorker)
        self.assertEqual(states[busy.row.id].row.current_session, busy.row.current_session)
        self.assertEqual(states[idle.row.id].row.current_session, idle.row.current_session)

    def test_restart_prefilter_leaves_worker_completion_to_restore(self) -> None:
        from scripts.whoami import quota_state

        idle = self.opened("review", "review")
        busy = self.opened()
        _ = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name=busy.row.name, worker_id=busy.row.id, launcher_command=["assign.sh", "--to", "impl"],
            prompt=self.prompt, kind="impl", lens="")
        record = staff.snapshot()
        self.assertIsNone(worker.not_restored(idle.row, record))
        self.assertIsNone(worker.not_restored(busy.row, record))
        with patch.object(quota_state, "no_worker_mode", return_value=True):
            self.assertIsNone(worker.not_restored(busy.row, record))

    def test_closing_a_worker_preserves_its_restart_entry(self) -> None:
        from scripts.agents import shutdown_record

        idle = self.opened("review", "review")
        self.assertTrue(shutdown_record.record_path().is_relative_to(self.fixture.root))
        saved = [str(self.fixture.parent.id), str(idle.row.id)]
        shutdown_record.save(saved)
        _ = self.fixture.world.stop(idle.row, self.fixture.world)
        self.assertIsInstance(worker_panes.close(self.fixture.parent.id, staff.WorkerName.parse(idle.row.name)),
                              worker_panes.Closed)
        self.assertEqual(shutdown_record.read(), saved)

    def test_restore_keeps_a_busy_codex_worker_stopped_in_no_worker_mode(self) -> None:
        self.assert_held_in_no_worker_mode(refused=False)

    def test_restore_keeps_a_refused_codex_pass_stopped_in_no_worker_mode(self) -> None:
        self.assert_held_in_no_worker_mode(refused=True)

    def assert_held_in_no_worker_mode(self, *, refused: bool) -> None:
        from scripts.whoami import quota_state

        busy = self.opened()
        _ = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name=busy.row.name, worker_id=busy.row.id, launcher_command=["assign.sh", "--to", "impl"],
            prompt=self.prompt, kind="impl", lens="")
        if refused:
            progress_history.mark_pass_refused(self.run_dir, busy.row.name)
            self.assertEqual(worker_panes.worker_pass(busy.row, self.run_dir).get("status"), "refused")
        _ = self.fixture.world.stop(busy.row, self.fixture.world)
        launches = len(self.fixture.world.launches)
        with (patch.object(quota_state, "no_worker_mode", return_value=True),
              patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK) as resume):
            self.assertEqual(self.command("restore"), (0, f"{busy.row.name}: not resumed: no-worker mode\n"
                                                       + f"{self.fixture.parent.name}: workers unchanged\n", ""))
        resume.assert_not_called()
        self.assertEqual(len(self.fixture.world.launches), launches)
        states = {state.row.id: state for state in worker_panes.observe(self.fixture.parent.id, staff.snapshot())}
        self.assertIsInstance(states[busy.row.id], worker_panes.StoppedWorker)

    def test_restore_rerun_after_agent_resume(self) -> None:
        opened = self.opened()
        _ = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name="impl", worker_id=opened.row.id, launcher_command=["bash", "assign.sh", "saved argument"],
            prompt=self.prompt, kind="impl", lens="")
        active = progress_history.active_dispatch(opened.row.id, self.run_dir)
        self.assertIsInstance(active, progress_history.ActiveDispatch)
        assert isinstance(active, progress_history.ActiveDispatch)
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        with patch.object(worker, "_resume_launcher", side_effect=OSError("interrupted before launcher")):
            self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.UNCERTAIN)
        launches = len(self.fixture.world.launches)
        with patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK) as resume:
            self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.OK)
        resume.assert_called_once_with(replace(opened.row, role=staff.KnownWorkerRole("impl")),
            worker.WorkerResume(worker.UnsubmittedAssignment(active.instance_id, active.launcher_command)))
        self.assertEqual(len(self.fixture.world.launches), launches)
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(progress_history.active_dispatch(opened.row.id, self.run_dir), active)

    def test_launcher_recovery_uses_the_real_lock_and_delivers_once(self) -> None:
        opened = self.opened()
        active = progress_history.ActiveDispatch("saved-pass", ("bash", "assign.sh", "argument with spaces"))
        self.fixture.dispatch(opened.row, "impl")
        opened = replace(opened, row=staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("impl")))
        director = herdr.TabSession("director-pane", "unit", "workspace", str(self.fixture.parent.current_session))
        lock_path = staff.record_path().parent / "locks" / f"{opened.row.id}.lock"
        atomic_file.write_text_atomic(lock_path, "")
        for held in (True, False):
            _ = self.fixture.world.stop(opened.row, self.fixture.world)
            with (self.subTest(held=held), lock_path.open("r") as lock,
                  patch.object(self.fixture.world, "tab_sessions", return_value=[director]),
                  patch.object(progress_history, "active_dispatch", return_value=active),
                  patch.object(self.fixture.world, "prompt") as prompt,
                  patch.object(herdr, "check_target", return_value=True),
                  patch.object(subprocess, "run", wraps=self.agent.run) as start):
                if held:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.REFUSED if held else worker.ExitCode.OK)
                if held:
                    prompt.assert_not_called()
                    self.assertFalse(any("assign.sh" in str(call.args) for call in start.call_args_list))
                else:
                    prompt.assert_called_once()
                    self.assertFalse(any("assign.sh" in str(call.args) for call in start.call_args_list))
                    self.assertEqual(prompt.call_args.args[0], director.pane_id)
                    text = cast(str, prompt.call_args.args[1])
                    self.assertIn("Run this saved command from your session shell: bash assign.sh 'argument with spaces'", text)
                    self.assertIn("saved-pass", text)
                    self.assertIn(str(opened.row.id), text)
                    self.assertNotIn('<system-reminder source="script">', text)
                    self.assertEqual(prompt.call_args.kwargs, {"target": str(self.fixture.parent.current_session)})
                    with lock_path.open("r") as available:
                        fcntl.flock(available, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_launcher_delivery_failure_keeps_restore_uncertain(self) -> None:
        opened = self.opened()
        active = progress_history.ActiveDispatch("saved-pass", ("bash", "assign.sh"))
        self.fixture.dispatch(opened.row, "impl")
        opened = replace(opened, row=staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("impl")))
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        director = herdr.TabSession("director-pane", "unit", "workspace", str(self.fixture.parent.current_session))
        with (patch.object(self.fixture.world, "tab_sessions", return_value=[director]),
              patch.object(progress_history, "active_dispatch", return_value=active),
              patch.object(self.fixture.world, "prompt", side_effect=herdr_types.HerdrFailure("delivery failed")),
              patch.object(herdr, "check_target", return_value=True),
              patch.object(subprocess, "run", side_effect=self.agent.run),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.UNCERTAIN)
        self.assertNotIn("unchanged", output.getvalue())
        self.assertEqual(errors.getvalue(), "anomaly: impl: delivery failed\n")
        self.assertEqual(staff.by_id(opened.row.id), opened.row)

    def test_resume_workers_names_the_active_pass_and_idle_worker(self) -> None:
        from scripts.lib.resume_message import UnsubmittedAssignment, IdleWorker, PaneWorkerResume

        busy = self.opened()
        idle = self.opened("review", "review")
        self.fixture.dispatch(busy.row, "impl")
        path = self.run_dir / "progress_history_state.json"
        fields = {"owner_staff_id": self.fixture.parent.id,
                  "pass": {"impl": {"status": "active", "instance_id": "dispatch-impl",
                                    "worker_id": busy.row.id, "launcher_command": ["assign.sh", "same command"]}}}
        atomic_file.write_text_atomic(path, json.dumps(fields))
        with patch.object(unit_runs, "held", return_value=[str(self.run_dir)]):
            result = worker.resume_workers(self.fixture.parent.id, staff.snapshot())
        self.assertCountEqual(result.panes, [PaneWorkerResume(busy.row.id, UnsubmittedAssignment("dispatch-impl", ("assign.sh", "same command"))),
                                             PaneWorkerResume(idle.row.id, IdleWorker())])
        self.assertEqual(result.results, [])

    def test_resume_workers_keeps_a_claude_child_launcher_without_a_native_pass(self) -> None:
        from scripts.lib.resume_message import BusyPass, IdleWorker, PaneWorkerResume, Resumable, Unavailable, resume_message

        child = self.fixture.worker("legacy-child", family="claude")
        unavailable = self.fixture.worker("unavailable-child", family="claude")
        native = self.fixture.worker("native-child", family="claude")
        idle = self.fixture.worker("native-idle", family="claude")
        command = ["bash", str(self.fixture.root / "assign.sh"), "--role", "implement",
                   "--phase", "8", "--session-dir", str(self.run_dir), "--cwd", str(self.fixture.root),
                   "--prompt", str(self.prompt)]
        process = self.fixture.root / "proc" / str(os.getpid())
        process.mkdir(parents=True)
        _ = (process / "cmdline").write_bytes(b"\0".join(os.fsencode(word) for word in command) + b"\0")
        atomic_file.write_text_atomic(process / "stat", f"{os.getpid()} (private launcher) S 1 0 0")
        atomic_file.write_text_atomic(self.run_dir / "impl_worker_legacy-child.json", json.dumps({
            "staff_id": child.id, "slot": "legacy-child", "launcher_pid": os.getpid()}))
        atomic_file.write_text_atomic(self.run_dir / "impl_status_legacy-child", "implementing")
        for row in (unavailable, native):
            atomic_file.write_text_atomic(self.run_dir / f"impl_worker_{row.name}.json", json.dumps({
                "staff_id": row.id, "slot": row.name, "launcher_pid": 0}))
        atomic_file.write_text_atomic(self.run_dir / "impl_status_native-child", "implementing")
        darwin_launcher = SimpleNamespace(
            pid=os.getpid(), parent=1, arguments=herdr.ReadProcessArguments(tuple(command)))
        with (patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.fixture.root / "usage")}),
              patch.object(herdr, "_darwin_processes", return_value=[darwin_launcher])):
            dispatch = progress_history.begin_dispatch(self.run_dir, slot="native-child", phase="8", role="impl",
                worker_name=native.name, worker_id=native.id, launcher_command=["assign.sh"], prompt=self.prompt,
                kind="impl", lens="")
            backend = staff.backend(native)
            assert isinstance(backend, staff.ClaudeBackend)
            source = worker_admission.transcript_source(native, backend)
            atomic_file.write_text_atomic(source, json.dumps({"type": "user", "message": {
                "role": "user", "content": f"End your final reply with DONE {dispatch}."}}) + "\n")
            agents_log.append(child.current_session, "impl", None, datetime.now(timezone.utc))
            agents_log.append(native.current_session, "impl", None, datetime.now(timezone.utc))
            _ = staff.set_worker_role(child.id, staff.KnownWorkerRole("impl"))
            _ = staff.set_worker_role(native.id, staff.KnownWorkerRole("impl"))
            result = worker.resume_workers(self.fixture.parent.id, staff.snapshot())
        saved = next(item for item in result.results if isinstance(item, Resumable))
        self.assertEqual(saved.seat["staff_id"], child.id)
        self.assertEqual(saved.seat["launcher_command"], command)
        self.assertTrue(saved.seat["turn_live"])
        self.assertEqual(result.cwd, str(self.fixture.root))
        missing = next(item for item in result.results if isinstance(item, Unavailable))
        self.assertEqual(missing.staff_id, unavailable.id)
        self.assertIn("staff worker role is unavailable", missing.reason)
        child_ids = {staff.StaffId(item.staff_id if isinstance(item, Unavailable) else item.seat["staff_id"])
                     for item in result.results}
        self.assertEqual(child_ids, {child.id, unavailable.id})
        self.assertTrue(child_ids.isdisjoint(pane.staff_id for pane in result.panes))
        self.assertIn(PaneWorkerResume(native.id, BusyPass(dispatch)), result.panes)
        self.assertIn(PaneWorkerResume(idle.id, IdleWorker()), result.panes)
        text = resume_message("a shutdown", workers=result)
        self.assertIn("legacy-child (impl, was working):", text)
        self.assertIn(str(self.run_dir / "legacy-child.resume-prompt.md"), text)
        self.assertIn(missing.reason, text)

    def test_own_pass_reads_the_workers_parent_and_names_only_its_dispatch(self) -> None:
        from scripts.lib.resume_message import UnsubmittedAssignment, NO_OWN_PASS

        opened = self.opened()
        for active, expected in ((progress_history.ActiveDispatch("own-dispatch", ("assign.sh",)), UnsubmittedAssignment("own-dispatch", ("assign.sh",))),
                                 (progress_history.NoActiveDispatch(), NO_OWN_PASS),
                                 (progress_history.ActiveDispatchUnavailable("record unreadable"), NO_OWN_PASS)):
            with (self.subTest(active=active),
                  patch.object(worker_panes, "worker_run", return_value=self.run_dir) as run,
                  patch.object(progress_history, "active_dispatch", return_value=active) as read,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(worker.own_pass(opened.row, staff.snapshot()), expected)
            run.assert_called_once_with(opened.row, record=staff.snapshot())
            read.assert_called_once_with(opened.row.id, self.run_dir)
            self.assertEqual(errors.getvalue(), "anomaly: impl: record unreadable\n"
                             if isinstance(active, progress_history.ActiveDispatchUnavailable) else "")

    def test_later_user_message_keeps_the_submitted_assignment_receipt(self) -> None:
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "after prompt"):
            _ = self.dispatch()
        pane = self.agent.submitted[0][0]
        self.agent.append(pane, "user", "Preserve the existing public interface too.")
        self.fixture.world.locations[pane]["agent_status"] = "working"
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.agent.complete(pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())

    def test_claude_done_reply_after_an_api_request_blob_completes_the_receipt(self) -> None:
        dispatch_id = "phase-28d"
        source = self.run_dir / "claude-transcript.jsonl"
        for instruction, lookup, suffix, completed in (
                ("old", "exact prompt", "", True), ("old", "dispatch id", "", True),
                ("new", "dispatch id", "", True), ("new", "dispatch id", ".", True),
                ("new", "dispatch id", "..", False), ("new", "dispatch id", " extra", False),
                ("new", "dispatch id", "\nLater text", False), ("new", "dispatch id", "-another", False)):
            with self.subTest(instruction=instruction, lookup=lookup, suffix=suffix):
                prompt = (f'Read the unchanged prompt file "{self.prompt}". ' + (
                    f"End your final reply with DONE {dispatch_id}." if instruction == "old" else
                    f"End your final reply with this line alone: DONE {dispatch_id}\n"
                    + "A reply that waits on a detached run is not final."))
                reply = "The work is complete.\nDONE " + dispatch_id + suffix
                records = [
                    {"type": "user", "message": {"role": "user", "content": prompt}},
                    {"type": "api-request-blob", "message": {"role": "user", "content": [{
                        "type": "text", "text": json.dumps({"messages": [{"role": "user", "content": prompt}]}),
                    }]}},
                    {"type": "assistant", "message": {"role": "assistant", "content": reply}},
                ]
                _ = source.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
                receipt = worker.transcript_receipt(worker_admission.transcript_entries(source, set()),
                    prompt if lookup == "exact prompt" else worker.DispatchPrompt(dispatch_id), worker.DoneReply(dispatch_id))
                expected = worker.DispatchComplete(reply) if completed else worker.DispatchSubmitted(reply)
                self.assertEqual(receipt, worker.TranscriptReceipt(expected, frozenset()))

        with self.subTest(lookup="exact with an earlier fallback timestamp overflow"):
            row = self.opened().row
            exact = "Read the current review."
            reply = f"The exact review is complete.\nDONE {dispatch_id}"
            records = [
                {"type": "assistant", "message": {"role": "assistant", "content": f"DONE {dispatch_id}"},
                 "timestamp": "0001-01-01T00:00:00+01:00"},
                {"type": "user", "message": {"role": "user", "content": exact}},
                {"type": "assistant", "message": {"role": "assistant", "content": reply},
                 "timestamp": "2026-11-21T12:00:00Z"},
            ]
            atomic_file.write_text_atomic(source, "".join(json.dumps(record) + "\n" for record in records))
            with patch.object(worker_admission, "transcript_source", return_value=source):
                receipt = worker.dispatch_receipt(row, exact, dispatch_id, run=self.run_dir)
            self.assertEqual(receipt, worker.DispatchComplete(reply))

    def test_agent_exit_after_done_still_publishes_the_reply(self) -> None:
        opened = self.opened()
        self.agent.finish_after = 1
        def exit_after_reply(pane: str) -> None:
            self.assertEqual(pane, opened.pane)
            _ = self.fixture.world.stop(opened.row, self.fixture.world)
        self.agent.after_complete = exit_after_reply
        with redirect_stderr(io.StringIO()) as errors:
            code = self.dispatch()
        self.assertEqual(code, worker.ExitCode.OK, errors.getvalue())
        self.agent.complete(opened.pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())
        self.assertNotIn(opened.pane, self.fixture.world.foreground)
        self.assertEqual(len(self.agent.submitted), 1)

    def test_named_assign_depends_on_its_own_pane_and_keeps_sibling_list_states(self) -> None:
        opened = self.opened("impl4")
        closed = [self.fixture.worker(name) for name in ("impl3", "fix1", "rev10a", "rev10c", "fix2", "port6", "ft7", "cr7")]
        for pane, reason in (("w1:p9", "agent argv is unavailable"), ("w2:p9", "pane_not_found"),
                             ("w2:p8", "current Codex rollout changed")):
            foreign = fixture_member("producer", name="foreign-" + pane.replace(":", "-"))
            self.fixture.world.add(foreign, pane, workspace=pane.split(":")[0],
                                   tab="w1:t1" if pane.startswith("w1:") else "w2:t2")
            self.fixture.world.unknown[pane] = reason
        self.fixture.world.process_reads.clear()
        self.fixture.world.reads.clear()
        panes_before = set(self.fixture.world.locations)
        self.agent.finish_after = 1
        with redirect_stderr(io.StringIO()) as errors:
            code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl4", phase="8", kind="impl", name="impl4", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(code, worker.ExitCode.OK, errors.getvalue())
        self.assertEqual(len(self.agent.submitted), 1)
        self.agent.complete(opened.pane)
        self.assertIsInstance(self.receipt("impl4"), worker.DispatchComplete)
        self.assertEqual(set(self.fixture.world.process_reads), panes_before)
        self.assertIn(("agent", "list"), self.fixture.world.reads)
        self.assertIn("agent argv is unavailable", errors.getvalue())
        for row in closed:
            self.assertNotIn(f"anomaly: {row.name}:", errors.getvalue())
        opened = self.opened("impl4")
        self.fixture.world.unknown[opened.pane] = "selected pane evidence is unavailable"
        for label in ("impl4", "unit-impl4"):
            with self.subTest(label=label), redirect_stderr(io.StringIO()) as errors:
                self.fixture.world.locations[opened.pane]["label"] = label
                code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                    slot="impl4", phase="8", kind="impl", name="impl4", launcher_command=["assign.sh"], reply=self.reply)
            self.assertEqual(code, worker.ExitCode.UNCERTAIN)
            self.assertIn("selected pane evidence is unavailable", errors.getvalue())
            self.assertEqual(len(self.agent.submitted), 1)
        del self.fixture.world.unknown[opened.pane]
        code, output, errors_text = self.command("list", "all", "--json")
        self.assertEqual(code, worker.ExitCode.OK, errors_text)
        listing = {str(row["name"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual(listing[opened.row.name]["state"], "OpenWorker")
        for row in closed:
            self.assertEqual(listing[row.name]["state"], "UncertainWorker")
        self.fixture.world.remove_leaf(opened.pane)
        del self.fixture.world.locations[opened.pane]
        del self.fixture.world.foreground[opened.pane]
        other = fixture_member("unit", name="other-unit", parent=self.fixture.parent.parent)
        foreign = fixture_member("worker", name="impl4", parent=other.id)
        self.fixture.world.add(foreign, "w2:p7", workspace="w2", tab="w2:t2")
        launches = len(self.fixture.world.launches)
        with redirect_stderr(io.StringIO()) as errors:
            code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl4", phase="8", kind="impl", name="impl4", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(code, worker.ExitCode.OK, errors.getvalue())
        self.assertEqual(len(self.fixture.world.launches), launches + 1)
        self.assertEqual(self.agent.row("w2:p7"), foreign)

    def test_named_assign_resumes_with_its_reported_codex_thread(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", live=False)
        native_process = self.fixture.world.native_process
        published: list[str] = []

        def read_process(pid: int, kind: str, reported: str = "") -> herdr.NativeAgentProcess:
            observed = native_process(pid, kind, reported)
            if kind == "codex" and observed.current_identity not in published:
                published.append(observed.current_identity)
            return observed

        with (patch.object(herdr, "native_process", side_effect=read_process),
              redirect_stderr(io.StringIO()) as errors):
            code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl", phase="8", kind="impl", name="impl", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(code, worker.ExitCode.OK, errors.getvalue())
        self.assertEqual(published, [str(row.current_session)])
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIsInstance(self.receipt("impl"), worker.DispatchComplete)
        self.assertEqual(staff.by_id(row.id), replace(row, role=staff.KnownWorkerRole("impl")))

    def test_successful_delivery_returns_before_a_later_process_read(self) -> None:
        opened = self.opened()
        def lose_process_evidence() -> None:
            self.fixture.world.unknown[opened.pane] = "dispatch process reader unavailable"
        self.agent.during_prompt = lose_process_evidence
        self.agent.finish_after = 1
        with redirect_stderr(io.StringIO()) as errors:
            code = self.dispatch()
        self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])

    def test_unknown_assignment_evidence_stays_uncertain_without_creating_workers(self) -> None:
        opened = self.opened()
        _ = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["prior-assign.sh"], prompt=self.prompt, kind="impl", lens="")
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        for evidence in ("process", "structure"):
            with self.subTest(evidence=evidence):
                if evidence == "process":
                    self.fixture.world.unknown[opened.pane] = "assignment process reader unavailable"
                else:
                    self.fixture.world.unknown.clear()
                    self.fixture.world.locations[opened.pane]["terminal_id"] = ""
                with redirect_stderr(io.StringIO()) as errors:
                    code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                        slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
                self.assertEqual(code, worker.ExitCode.UNCERTAIN, errors.getvalue())
                self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())
                self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches)), before)
                self.assertEqual(self.agent.submitted, [])

    def test_restore_continues_after_one_worker_layout_failure(self) -> None:
        for name in ("impl", "impl2"):
            opened = self.opened(name)
            _ = progress_history.begin_dispatch(self.run_dir, slot=name, phase="8", role="impl",
                worker_name=name, worker_id=opened.row.id, launcher_command=["assign.sh", name], prompt=self.prompt, kind="impl", lens="")
            _ = self.fixture.world.stop(opened.row, self.fixture.world)
        states = worker_panes.observe(self.fixture.parent.id, staff.snapshot())
        first, second = states
        request = self.fixture.world.socket_request
        failed = False
        def fail_one_layout(method: str, **params: object) -> dict[str, object]:
            nonlocal failed
            if method == "layout.export" and not failed:
                failed = True
                raise herdr_types.HerdrFailure("restore layout unavailable")
            return request(method, **params)
        with patch.object(self.fixture.world, "socket_request", side_effect=fail_one_layout), patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK):
            with redirect_stderr(io.StringIO()) as errors:
                code = worker.restore(self.fixture.parent.id)
        self.assertEqual(code, worker.ExitCode.UNCERTAIN)
        self.assertEqual(errors.getvalue().splitlines(), [f"anomaly: {first.row.name}: restore layout unavailable"])
        resumed = {state.row.id for state in worker_panes.observe(self.fixture.parent.id, staff.snapshot())
                   if isinstance(state, worker_panes.OpenWorker)}
        self.assertIn(second.row.id, resumed)
        before = len(self.fixture.world.launches)
        with patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK):
            self.assertEqual(self.command("restore"), (0, f"{self.fixture.parent.name}: workers unchanged\n", ""))
        self.assertEqual(len(self.fixture.world.launches), before)

    def test_restore_reports_each_worker_with_pending_completion(self) -> None:
        impl = self.opened("impl")
        cm = self.opened("cm")
        reap = self.opened("reap")
        dispatches: dict[str, str] = {}
        for opened in (impl, cm, reap):
            name = opened.row.name
            dispatch = progress_history.begin_dispatch(self.run_dir, slot=name, phase="8", role="impl",
                worker_name=name, worker_id=opened.row.id, launcher_command=["assign.sh", name],
                prompt=self.prompt, kind="impl", lens="")
            dispatches[name] = dispatch
            line = f"Complete the work. End your final reply with DONE {dispatch}."
            reply = self.run_dir / f"{name}_reply.md"
            progress_history.save_dispatch_delivery(self.run_dir, dispatch,
                pass_recorder.DispatchDelivery(line, dispatch, str(reply)))
            self.agent.append(opened.pane, "user", line)

        backend = staff.backend(impl.row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        transcript = worker_admission.transcript_source(impl.row, backend)
        with transcript.open("a", encoding="utf-8") as stream:
            _ = stream.write('{"torn":')
        result_path, result = self.detached_record(cm.row, "private-check")
        result.update(delivery=f"sent: worker cm at {cm.pane}", pane_dispatch_id=dispatches["cm"])
        atomic_file.write_text_atomic(result_path, json.dumps(result))
        self.agent.append(cm.pane, "user", "Detached run: private-check\nRun finished: private verification")
        self.agent.append(cm.pane, "assistant", "The check is on hold.")
        self.fixture.world.locations[cm.pane]["agent_status"] = "done"

        reap_path, orphan = self.detached_record(reap.row, "unreaped-check")
        process = process_start.process_start(os.getpid())
        assert isinstance(process, process_start.Started)
        orphan.update(status="running", pid=DETACHED_FIXTURE_MAX_PID, pane_dispatch_id=dispatches["reap"],
                      command_pid=os.getpid(), command_started=process.start)
        atomic_file.write_text_atomic(reap_path, json.dumps(orphan))
        self.agent.append(reap.pane, "assistant", f"DONE {dispatches['reap']}")
        self.fixture.world.locations[reap.pane]["agent_status"] = "done"

        observe = worker_panes.observe

        def observe_impl_first(parent: staff.StaffId, record: staff.StaffSnapshot) -> list[worker_panes.WorkerPane]:
            return sorted(observe(parent, record), key=lambda state: (state.row.name != "impl", state.row.name))

        _ = self.enterContext(patch.object(worker_panes, "observe", side_effect=observe_impl_first))
        # Round 18: a "sent" delivery without a saved receipt is uncertain, and its DONE is no proof.
        # Round 21: restore never continues a pass from a result receipt; the director inspects the worker.
        uncertain = "cm: Detached run private-check: the result delivery is uncertain. The director must inspect the worker."
        with patch.object(self.fixture.world, "prompt") as send_resume:
            code, _, errors = self.command("restore")
        send_resume.assert_not_called()
        self.assertEqual(code, worker.ExitCode.UNCERTAIN)
        self.assertEqual(errors.splitlines(), ["anomaly: impl: worker transcript has incomplete records",
                                               uncertain,
                                               "reap: reap must record the run result"])
        self.assertIn(impl.pane, self.fixture.world.locations)
        self.assertIn(cm.pane, self.fixture.world.locations)

        self.agent.append(cm.pane, "assistant", f"The check is complete.\nDONE {dispatches['cm']}")
        with patch.object(self.fixture.world, "prompt") as send_resume:
            code, _, errors = self.command("restore")
        send_resume.assert_not_called()
        self.assertEqual(code, worker.ExitCode.UNCERTAIN)
        self.assertEqual(errors.splitlines(), ["anomaly: impl: worker transcript has incomplete records",
                                               uncertain,
                                               "reap: reap must record the run result"])
        self.assertIn(impl.pane, self.fixture.world.locations)
        self.assertIn(cm.pane, self.fixture.world.locations)

    def test_herdr_dispatch_records_the_worker_staff_id(self) -> None:
        for foreign_tab in (False, True):
            with self.subTest(foreign_tab=foreign_tab):
                opened = self.opened("foreign-tab" if foreign_tab else "impl")
                def relocate(pane: str) -> None:
                    if foreign_tab:
                        self.fixture.world.remove_leaf(pane)
                        self.fixture.world.layouts["w1:t5"] = {"type": "pane", "pane_id": pane}
                        self.fixture.world.locations[pane]["tab_id"] = "w1:t5"
                self.agent.after_complete = relocate
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.dispatch(opened.row.name), worker.ExitCode.OK)
                    self.assertEqual(worker_panes.worker_pass(opened.row, self.run_dir)["status"], "active")
                    self.assertEqual(self.finish(opened.row.name), turn_end.TurnOutcome.DONE)
                state = cast(dict[str, dict[str, dict[str, object]]],
                             json.loads((self.run_dir / "progress_history_state.json").read_text()))
                self.assertEqual(state["pass"]["impl"]["worker_id"], opened.row.id)
                self.assertEqual(state["pass"]["impl"]["status"], "completed")
                self.assertEqual(staff.snapshot().by_id(opened.row.id), replace(opened.row, role=staff.KnownWorkerRole("impl")))
                self.assertIn(self.agent.final, self.reply.read_text())
                self.assertIsInstance(self.fixture.observation(opened.row), worker_panes.ClosedWorker)
                self.assertNotIn(opened.pane, self.fixture.world.locations)
                self.assertEqual(errors.getvalue(), "")

    def test_launched_agent_reads_the_kept_launch_settings(self) -> None:
        row = fixture_worker("called-agent", self.fixture.parent.id)
        codex = pane_launch.launch("w1:p9", str(row.current_session), str(self.fixture.root), {},
                                   ["codex", "--model", "gpt-called", "-c", 'model_reasoning_effort="high"'],
                                   kind="codex")
        claude = pane_launch.launch("w1:p9", str(row.current_session), str(self.fixture.root), {},
                                    ["claude", "--model", "opus"])
        unnamed = pane_launch.launch("w1:p9", str(row.current_session), str(self.fixture.root), {}, ["claude"])
        cases: list[tuple[pane_launch.CurrentLaunch | None, progress_history.CalledAgent]] = [
            (codex, progress_history.CalledAgent("codex", "gpt-called", "high")),
            (claude, progress_history.CalledAgent("claude", "opus", "unset")),
            (unnamed, progress_history.CALLED_AGENT_NOT_RECORDED),
            (None, progress_history.CALLED_AGENT_NOT_RECORDED),
        ]
        for entry, expected in cases:
            with self.subTest(expected=expected, recorded=entry is not None):
                with patch.object(pane_record, "launch_for_session", return_value=entry):
                    self.assertEqual(worker.launched_agent(row), expected)

    def test_fast_dispatch_writes_reply_without_changing_the_prompt(self) -> None:
        before = self.prompt.read_bytes(), self.prompt.stat().st_mtime_ns
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())
        self.assertIn("DONE ", self.reply.read_text())
        self.assertEqual((self.prompt.read_bytes(), self.prompt.stat().st_mtime_ns), before)
        self.assertNotIn("--until", self.agent.submitted[0][1])
        for summary in ("", str(self.run_dir / "durable-reply.md")):
            with self.subTest(durable_summary=summary):
                name = "reviewer-default" if not summary else "reviewer-summary"
                identities: list[str] = []
                for round_number in (1, 2):
                    prompt = self.prompt.with_name(f"{name}-{round_number}.md")
                    atomic_file.write_text_atomic(prompt, f"Review round {round_number}.")
                    destination = Path(summary).with_name(f"reply-{name}-{round_number}.md") if summary else prompt.with_name(prompt.stem + "_reply.txt")
                    args = assign.Arguments(target="durable-design-role", parent=str(self.fixture.parent.id),
                        to=name, policy="astra-reviewer", prompt=str(prompt), reply_file=str(destination) if summary else "",
                        session_dir=str(self.run_dir), phase="8")
                    router = PaneCommandRouter()
                    with (patch.object(subprocess, "run", side_effect=router.run),
                          patch.object(sys, "argv", ["assign.py", "--prompt", str(prompt)])):
                        self.assertIs(assign.execute_durable(args), worker.DispatchSubmission.SUBMITTED)
                    pane = self.agent.submitted[-1][0]
                    row = self.agent.row(pane)
                    identities.append(row.current_session)
                    self.assertEqual(router.recorder_commands, [])
                    self.assertFalse(destination.exists())
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
                    self.assertEqual(self.finish(name), turn_end.TurnOutcome.DONE)
                    self.assertIn(self.agent.final, destination.read_text())
                    self.assertTrue(progress_history.reply_completed(self.run_dir, destination))
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
                    self.assertEqual(staff.snapshot().by_id(row.id), row)
                    self.assertNotIn(pane, self.fixture.world.locations)
                self.assertEqual(identities[0], identities[1])

    def test_compaction_proof_precedes_the_dispatch_prompt(self) -> None:
        class CompactionClock:
            seconds: float = 100.0

            def monotonic(self) -> float:
                return self.seconds

        for proof in ("absent", "compacted", "task_started", "delayed", "bounded"):
            with self.subTest(proof=proof):
                self.agent.compact_proof = ("absent" if proof == "delayed" else
                                            "compacted" if proof == "bounded" else proof)
                current = self.opened("compact-proof-" + proof)
                backend = staff.backend(current.row)
                assert isinstance(backend, staff.CodexBackend)
                source = worker_admission.transcript_source(current.row, backend)
                with source.open("a") as stream:
                    _ = stream.write(json.dumps({"type": "compacted"}) + "\n")
                    _ = stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 43001, "cached_input_tokens": 0, "output_tokens": 0}}}}) + "\n")
                before = len(self.agent.submitted)
                send_keys = self.fixture.world.send_keys
                wait_turn_end = self.fixture.world.wait_turn_end
                order: list[str] = []
                compact_commands: list[list[str]] = []
                clock = CompactionClock()

                def run_compaction(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                    result = self.agent.run(arguments, **options)
                    if len(arguments) > 6 and arguments[6] == "/compact":
                        compact_commands.append(arguments)
                        if proof == "bounded":
                            clock.seconds = 195.0
                    return result

                def finish_compaction(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
                    if proof == "bounded":
                        self.assertEqual(timeout_ms, 205000)
                    if proof == "delayed" and pane in self.agent.compacting:
                        # Model a 95-second compaction without a real delay.
                        if timeout_ms < 95000:
                            return herdr.WaitTimedOut()
                        with source.open("a") as stream:
                            _ = stream.write(json.dumps({"type": "compacted"}) + "\n")
                        self.agent.compact_inputs[pane] = ""
                        order.append("compacted")
                    return wait_turn_end(pane, timeout_ms)

                def clear(pane: str, *keys: str, target: str, expected_text: str = "") -> None:
                    send_keys(pane, *keys, target=target, expected_text=expected_text)
                    self.agent.compact_inputs[pane] = ""

                def compact_composer(pane: str) -> herdr.ComposerInput:
                    return herdr.ComposerInput(self.agent.compact_inputs.get(pane, ""))

                with (patch.object(self.fixture.world, "composer", side_effect=compact_composer),
                      patch.object(worker, "time", new=clock),
                      patch.object(subprocess, "run", side_effect=run_compaction),
                      patch.object(self.fixture.world, "send_keys", side_effect=clear) as keys,
                      patch.object(self.fixture.world, "wait_turn_end", side_effect=finish_compaction),
                      patch.object(self.agent, "during_prompt", new=lambda: order.append("prompt")),
                      worker_panes.worker_id_lock(current.row.id),
                      redirect_stderr(io.StringIO()) as errors):
                    result = worker.dispatch_turn(self.fixture.parent.id, current, self.fixture.world,
                        "End your final reply with DONE compact-proof", "compact-proof",
                        run=self.run_dir, instance_id="compact-proof",
                        before_input=lambda _row: worker_panes.CloseAllowed())
                if proof == "bounded":
                    self.assertEqual(len(compact_commands), 1)
                    command = compact_commands[0]
                    self.assertIn("--timeout", command)
                    self.assertEqual(command[command.index("--timeout") + 1], "300000")
                if proof in {"absent", "task_started"}:
                    self.assertEqual(result, worker.ExitCode.REFUSED)
                    self.assertEqual(len(self.agent.submitted), before)
                    self.assertIn("compaction has no transcript proof", errors.getvalue())
                    self.assertIn("no prompt sent", errors.getvalue())
                    if proof == "absent":
                        keys.assert_called_once_with(current.pane, "ctrl-u", target=str(current.row.current_session),
                                                     expected_text="/compact")
                    else:
                        keys.assert_not_called()
                else:
                    self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                    self.assertEqual(self.agent.submitted[before:], [(current.pane,
                        script_prompt("End your final reply with DONE compact-proof"))])
                    keys.assert_not_called()
                    if proof == "delayed":
                        self.assertEqual(order, ["compacted", "prompt"])
                self.assertEqual(self.agent.compact_inputs[current.pane], "")

    def test_compaction_dispatch_returns_before_completion_and_reports_detached_failure(self) -> None:
        class DispatchClock:
            seconds: float = 100.0

            def monotonic(self) -> float:
                self.seconds += 1
                return self.seconds

            def sleep(self, seconds: float) -> None:
                self.seconds += seconds

        for outcome in ("new worker", "old helper arguments", "helper import failure", "exit zero without a byte",
                        "missing startup deadline", "failure before input", "failure after input",
                        "receipt with failed confirmation", "compacted", "timeout with proof", "no proof", "timeout", "no receipt", "first staff read",
                        "hook write",
                        "reply write", "notice failure", "attached", "admission waits for dispatch",
                        "live detached run", "busy twice", "confirmed before notice", "replaced before notice",
                        "replaced after handoff", "run starts during compaction", "rename before helper lock",
                        "confirmed closed pane", "changed native session", "confirmed held compaction lock",
                        "missing worker", "moved worker", "first confirmation read fails", "second confirmation read fails",
                        "confirmation fails with lock held", "compaction lock acquisition fails",
                        "recover first confirmation failure", "confirmed before uncertainty notice",
                        "pass replaced after read failure"):
            with self.subTest(outcome=outcome):
                if outcome == "new worker":
                    self.check_new_worker_before_pass()
                    continue
                current = self.opened("detached-" + outcome.replace(" ", "-"))
                backend = staff.backend(current.row)
                assert isinstance(backend, staff.CodexBackend)
                source = worker_admission.transcript_source(current.row, backend)
                with source.open("a") as stream:
                    _ = stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 43000 if outcome == "attached" else 43001,
                                             "cached_input_tokens": 0, "output_tokens": 0}}}}) + "\n")
                if outcome == "missing startup deadline":
                    config = self.fixture.root / "conf/direct.conf"
                    original = config.read_text()
                    atomic_file.write_text_atomic(config, original.replace("WORKER_SETTLE_SECONDS=30\n", ""))
                    before = len(self.agent.submitted)
                    try:
                        code, output, errors = self.command("assign", "impl", str(self.prompt), "--name", current.row.name,
                            "--slot", current.row.name, "--phase", "8", "--kind", "impl", "--session-dir", str(self.run_dir),
                            "--launcher-command", json.dumps(["assign.sh", "--to", current.row.name]))
                    finally:
                        atomic_file.write_text_atomic(config, original)
                    self.assertEqual(code, worker.ExitCode.REFUSED)
                    self.assertNotIn("submitted", output)
                    self.assertIn("WORKER_SETTLE_SECONDS", errors)
                    self.assertEqual(worker_panes.worker_pass(current.row, self.run_dir)["status"], "error")
                    self.assertEqual(len(self.agent.submitted), before)
                    continue
                if outcome in ("old helper arguments", "helper import failure", "exit zero without a byte",
                               "failure before input", "failure after input", "receipt with failed confirmation"):
                    self.check_compaction_helper_process(outcome, current, source)
                    continue
                self.agent.compact_proof = "absent"
                self.agent.compact_inputs[current.pane] = ""
                self.agent.defer_prompt_transcript = outcome in ("no receipt", "confirmed before uncertainty notice")
                self.agent.finish_after = 1
                self.agent.notice_failures = int(outcome == "notice failure")
                notices_before = len(self.agent.notices)
                failure_steps: list[str] = []
                commands: list[list[str]] = []
                order: list[str] = []
                before = len(self.agent.submitted)
                compacted = len(self.agent.compactions)
                launcher = ["assign.sh", "--activity", "Private compaction proof"]
                status = self.run_dir / (current.row.name + "_status")
                atomic_file.write_text_atomic(status, "running\n")

                def queue(command: list[str], _lock: file_lock.ExclusiveLock, _log: Path) -> None:
                    commands.append(command)

                wait_turn_end = self.fixture.world.wait_turn_end

                def finish_compaction(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
                    if outcome == "timeout":
                        return herdr.WaitTimedOut()
                    if outcome == "run starts during compaction":
                        live_runs.enter_context(pane_fixtures.live_detached_run(self.run_dir, current.row))
                    ended = wait_turn_end(pane, timeout_ms)
                    if outcome not in {"no proof", "hook write", "reply write", "notice failure"}:
                        with source.open("a") as stream:
                            _ = stream.write(json.dumps({"type": "compacted"}) + "\n")
                        self.agent.compact_inputs[pane] = ""
                        order.append("compacted")
                    return herdr.WaitTimedOut() if outcome == "timeout with proof" else ended

                send_keys = self.fixture.world.send_keys

                def clear(pane: str, *keys: str, target: str, expected_text: str = "") -> None:
                    send_keys(pane, *keys, target=target, expected_text=expected_text)
                    self.agent.compact_inputs[pane] = ""

                def compact_composer(pane: str) -> herdr.ComposerInput:
                    return herdr.ComposerInput(self.agent.compact_inputs.get(pane, ""))

                with (ExitStack() as live_runs, patch.object(worker, "_spawn_compaction", side_effect=queue),
                      patch.object(worker, "time", new=DispatchClock()),
                      patch.object(self.fixture.world, "wait_turn_end", side_effect=finish_compaction),
                      patch.object(self.fixture.world, "composer", side_effect=compact_composer),
                      patch.object(self.fixture.world, "send_keys", side_effect=clear),
                      patch.object(self.agent, "during_prompt", new=lambda: order.append("prompt")),
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                    result = worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(current.row.name), "impl", self.prompt,
                        session_dir=self.run_dir, slot=current.row.name, phase="8", kind="impl", launcher_command=launcher,
                        reply=self.run_dir / (current.row.name + "_reply.md"),
                        completion_actions=pass_recorder.CompletionActions(status_file=str(status),
                            summary_file=str(self.run_dir / "failed-summary.md") if outcome == "reply write" else ""))
                    self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                    if outcome == "attached":
                        self.assertEqual(commands, [])
                        self.assertEqual(order, ["prompt"])
                        self.assertEqual(len(self.agent.submitted), before + 1)
                        continue
                    self.assertEqual(len(commands), 1)
                    self.assertEqual(len(self.agent.compactions), compacted)
                    self.assertEqual(len(self.agent.submitted), before)
                    self.assertIn("compaction runs detached; the prompt follows", output.getvalue())
                    self.assertIsInstance(progress_history.active_dispatch(current.row.id, self.run_dir), progress_history.ActiveDispatch)
                    order.append("returned")
                    if outcome in ("confirmed before uncertainty notice", "pass replaced after read failure"):
                        pending = worker_panes.worker_pass(current.row, self.run_dir)
                        completion_id = progress_history.dispatch_delivery(pending).completion_id
                        state_path = self.run_dir / unit_runs.STATE_FILE
                        changed_state: list[bytes] = []
                        dispatch_notice = worker._dispatch_notice  # pyright: ignore[reportPrivateUsage]
                        read_confirmation = progress_history.dispatch_submission_confirmed

                        def confirm_at_notice(owner: staff.StaffId, identity: staff.StaffId, run: Path,
                                              pass_id: str, passed: dict[str, object],
                                              result: Literal["busy", "error", "uncertain"], detail: str) -> None:
                            with file_lock.exclusive(run / f"compact-{completion_id}.lock", wait=False) as lock:
                                self.assertIs(lock, file_lock.LockResult.ACQUIRED)
                                with worker_panes.worker_id_lock(identity):
                                    progress_history.confirm_assignment_submission(run, pass_id, completion_id)
                                    changed_state.append(state_path.read_bytes())
                            dispatch_notice(owner, identity, run, pass_id, passed, result, detail)

                        def replace_on_first_read(run: Path, pass_id: str, delivery_id: str) -> bool:
                            if not changed_state:
                                replacement = progress_history.begin_dispatch(run, slot=current.row.name,
                                    phase="8", role="impl", worker_name=current.row.name, worker_id=current.row.id,
                                    launcher_command=["replacement"], prompt=self.prompt, kind="impl", lens="")
                                self.assertNotEqual(replacement, pass_id)
                                changed_state.append(state_path.read_bytes())
                                raise OSError("first confirmation read failed")
                            return read_confirmation(run, pass_id, delivery_id)

                        if outcome == "confirmed before uncertainty notice":
                            with patch.object(worker, "_dispatch_notice", new=confirm_at_notice):
                                code = worker.main(commands[0][2:])
                            self.assertEqual(code, worker.ExitCode.UNCERTAIN)
                            self.assertIn("submission is unproved", errors.getvalue())
                            self.assertEqual(len(self.agent.submitted), before + 1)
                            self.assertEqual(len(self.agent.compactions), compacted + 1)
                        else:
                            with patch.object(progress_history, "dispatch_submission_confirmed", new=replace_on_first_read):
                                code = worker.main(commands[0][2:])
                            self.assertEqual(code, worker.ExitCode.OK)
                            self.assertEqual(len(self.agent.submitted), before)
                            self.assertEqual(len(self.agent.compactions), compacted)
                        self.assertEqual(len(self.agent.notices), notices_before)
                        self.assertEqual([state_path.read_bytes()], changed_state)
                        self.assertEqual(status.read_text(), "running\n")
                        continue
                    if outcome == "admission waits for dispatch":
                        self.check_admission_waits_for_compaction(current, commands[0][2:], finish_compaction, errors)
                        self.assertEqual(order, ["returned", "compacted", "prompt"])
                        self.assertEqual(len(self.agent.submitted), before + 1)
                        continue
                    if outcome == "confirmed held compaction lock":
                        passed = worker_panes.worker_pass(current.row, self.run_dir)
                        instance = str(passed["instance_id"])
                        completion_id = progress_history.dispatch_delivery(passed).completion_id
                        progress_history.confirm_assignment_submission(self.run_dir, instance, completion_id)
                        pane_reads = list(self.fixture.world.reads)
                        path = self.run_dir / f"compact-{completion_id}.lock"
                        with (ThreadPoolExecutor(max_workers=1) as executor,
                              patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot):
                            lock = file_lock.take_exclusive(path)
                            assert isinstance(lock, file_lock.ExclusiveLock)
                            try:
                                finished = executor.submit(worker.main, commands[0][2:])
                                self.assertEqual(finished.result(timeout=30), worker.ExitCode.OK)
                                inherited = os.dup(lock.descriptor)
                                self.assertEqual(worker.main([*commands[0][2:], "--lock-fd", str(inherited)]),
                                                 worker.ExitCode.OK)
                                with self.assertRaises(OSError):
                                    _ = os.fstat(inherited)
                                with file_lock.exclusive(path, wait=False) as competing:
                                    self.assertIs(competing, file_lock.LockResult.BUSY)
                            finally:
                                lock.close()
                            snapshot.assert_not_called()
                        self.assertEqual(self.fixture.world.reads, pane_reads)
                        self.assertEqual(len(self.agent.submitted), before)
                        self.assertEqual(len(self.agent.compactions), compacted)
                        self.assertEqual(len(self.agent.notices), notices_before)
                        continue
                    if outcome in ("first confirmation read fails", "second confirmation read fails"):
                        pending = worker_panes.worker_pass(current.row, self.run_dir)
                        instance = str(pending["instance_id"])
                        completion_id = progress_history.dispatch_delivery(pending).completion_id
                        path = self.run_dir / f"compact-{completion_id}.lock"
                        state_path = self.run_dir / unit_runs.STATE_FILE
                        confirmed_state: list[bytes] = []
                        confirmation_reads = 0
                        failing_read = 1 if outcome == "first confirmation read fails" else 2
                        read_confirmation = progress_history.dispatch_submission_confirmed

                        def fail_confirmation(run: Path, pass_id: str, delivery_id: str) -> bool:
                            nonlocal confirmation_reads
                            confirmation_reads += 1
                            if confirmation_reads == failing_read:
                                progress_history.confirm_assignment_submission(run, pass_id, delivery_id)
                                confirmed_state.append(state_path.read_bytes())
                                raise SystemExit("transient confirmation read failure")
                            return read_confirmation(run, pass_id, delivery_id)

                        helper = commands[0][2:]
                        with patch.object(progress_history, "dispatch_submission_confirmed", new=fail_confirmation):
                            if failing_read == 1:
                                lock = file_lock.take_exclusive(path)
                                assert isinstance(lock, file_lock.ExclusiveLock)
                                try:
                                    self.assertEqual(worker.main([*helper, "--lock-fd", str(lock.descriptor)]),
                                                     worker.ExitCode.OK)
                                finally:
                                    with self.assertRaises(OSError):
                                        _ = os.fstat(lock.descriptor)
                            else:
                                self.assertEqual(worker.main(helper), worker.ExitCode.OK)
                        with file_lock.exclusive(path, wait=False) as available:
                            self.assertIs(available, file_lock.LockResult.ACQUIRED)
                        self.assertEqual(confirmation_reads, failing_read + 1)
                        self.assertEqual([state_path.read_bytes()], confirmed_state)
                        self.assertEqual(errors.getvalue(), "")
                        self.assertEqual(len(self.agent.submitted), before)
                        self.assertEqual(len(self.agent.compactions), compacted)
                        self.assertEqual(len(self.agent.notices), notices_before)
                        self.assertEqual(status.read_text(), "running\n")
                        continue
                    if outcome in ("confirmation fails with lock held", "compaction lock acquisition fails"):
                        passed = worker_panes.worker_pass(current.row, self.run_dir)
                        delivery = progress_history.dispatch_delivery(passed)
                        path = self.run_dir / f"compact-{delivery.completion_id}.lock"
                        state_path = self.run_dir / unit_runs.STATE_FILE
                        state_before = state_path.read_bytes()
                        reply = Path(delivery.reply_file)
                        error_reply = reply.with_name(reply.stem + "_dispatch_error" + reply.suffix)
                        fallback = self.run_dir / f"compact-{passed['instance_id']}_dispatch_error.txt"
                        replies = {reply: "Existing reply\n", error_reply: "Existing error reply\n",
                                   fallback: "Existing compaction error\n"}
                        for target, content in replies.items():
                            atomic_file.write_text_atomic(target, content)
                        if outcome == "confirmation fails with lock held":
                            read_confirmation = progress_history.dispatch_submission_confirmed
                            first = True

                            def fail_first_read(run: Path, instance: str, completion: str) -> bool:
                                nonlocal first
                                if first:
                                    first = False
                                    raise OSError("first confirmation read failed")
                                return read_confirmation(run, instance, completion)

                            with ThreadPoolExecutor(max_workers=1) as executor:
                                with (file_lock.exclusive(path) as owned,
                                      patch.object(progress_history, "dispatch_submission_confirmed", new=fail_first_read)):
                                    self.assertIs(owned, file_lock.LockResult.ACQUIRED)
                                    finished = executor.submit(worker.main, commands[0][2:])
                                    code = finished.result(timeout=30)
                            self.assertEqual(code, worker.ExitCode.BUSY)
                            self.assertIn("compaction lock is busy", errors.getvalue())
                        else:
                            exclusive = file_lock.exclusive

                            @contextmanager
                            def fail_compaction_lock(path_to_lock: Path, *, wait: bool = True) -> Generator[file_lock.LockResult]:
                                if path_to_lock == path:
                                    raise OSError("compaction lock open failed")
                                with exclusive(path_to_lock, wait=wait) as held:
                                    yield held

                            with patch.object(file_lock, "exclusive", new=fail_compaction_lock):
                                code = worker.main(commands[0][2:])
                            self.assertEqual(code, worker.ExitCode.UNCERTAIN)
                            self.assertIn("compaction lock open failed", errors.getvalue())
                        self.assertEqual(state_path.read_bytes(), state_before)
                        self.assertEqual({target: target.read_text() for target in replies}, replies)
                        self.assertEqual(status.read_text(), "running\n")
                        self.assertEqual(len(self.agent.submitted), before)
                        self.assertEqual(len(self.agent.compactions), compacted)
                        self.assertEqual(len(self.agent.notices), notices_before)
                        continue
                    if outcome in ("missing worker", "moved worker"):
                        if outcome == "missing worker":
                            _ = self.fixture.world.stop(current.row, self.fixture.world)
                            self.fixture.world.remove_pane(current.pane)
                            self.assertEqual(staff.retire(current.row.id), current.row)
                        else:
                            other = fixture_member("unit", name="new-worker-owner", parent=self.fixture.parent.parent)
                            _ = staff.move(current.row.id, staff.ReportsTo(other.id))
                        with patch.object(send, "send", wraps=send.send) as notify:
                            self.assertEqual(worker.main(commands[0][2:]), worker.ExitCode.BUSY)
                        notify.assert_called_once()
                        options = cast(send.Options, notify.call_args.args[0])
                        owner = self.fixture.parent
                        family = "codex" if isinstance(staff.backend(owner), staff.CodexBackend) else "session"
                        self.assertEqual(options.selection, send.RosterLookup(f"{family}:{owner.current_session}"))
                        self.assertEqual(len(self.agent.notices), notices_before + 1)
                        self.assertIn(shlex.join(commands[0]), self.agent.notices[-1])
                        state = worker_panes.run_state(self.run_dir)
                        passes = cast(dict[str, dict[str, object]], state["pass"])
                        saved = passes[current.row.name]
                        instance = commands[0][commands[0].index("--dispatch-id") + 1]
                        self.assertEqual((saved["instance_id"], saved["status"]), (instance, "active"))
                        self.assertNotIn("confirmed_delivery_ids", saved)
                        self.assertIsInstance(progress_history.hook_completion(saved), progress_history.NoHookCompletion)
                        self.assertEqual(len(self.agent.submitted), before)
                        self.assertEqual(len(self.agent.compactions), compacted)
                        self.assertEqual(status.read_text(), "running\n")
                        continue
                    if outcome in ("rename before helper lock", "confirmed closed pane", "changed native session"):
                        passed = worker_panes.worker_pass(current.row, self.run_dir)
                        instance = str(passed["instance_id"])
                        completion_id = progress_history.dispatch_delivery(passed).completion_id
                        acquire = worker_panes.worker_lock

                        @contextmanager
                        def change_session(parent: staff.StaffId, name: staff.WorkerName, *, blocking: bool = False
                                           ) -> Generator[worker_panes.WorkerLockHold | worker_panes.Kept]:
                            with acquire(parent, name, blocking=blocking) as hold:
                                _ = staff.update_current_session(current.row.current_session,
                                    staff.StaffId(fixture_runtime_id("compaction-changed-session")))
                                yield hold

                        with ExitStack() as changes:
                            if outcome == "confirmed closed pane":
                                progress_history.confirm_assignment_submission(self.run_dir, instance, completion_id)
                                self.fixture.world.remove_pane(current.pane)
                            elif outcome == "rename before helper lock":
                                _ = changes.enter_context(self.rename_at_worker_lock(current.row, "renamed-compaction"))
                            else:
                                _ = changes.enter_context(patch.object(worker_panes, "worker_lock", new=change_session))
                            code = worker.main(commands[0][2:])
                        confirmed = outcome == "confirmed closed pane"
                        self.assertEqual(code, worker.ExitCode.OK if confirmed else worker.ExitCode.BUSY)
                        saved = worker_panes.worker_pass(current.row, self.run_dir)
                        self.assertEqual((saved["instance_id"], saved["status"]), (instance, "active"))
                        self.assertIsInstance(progress_history.hook_completion(saved), progress_history.NoHookCompletion)
                        if confirmed:
                            self.assertEqual(saved["confirmed_delivery_ids"], [completion_id])
                            self.assertEqual(len(self.agent.notices), notices_before)
                        else:
                            self.assertNotIn("confirmed_delivery_ids", saved)
                            self.assertIn("the worker changed before dispatch", errors.getvalue())
                            self.assertEqual(len(self.agent.notices), notices_before + 1)
                            self.assertIn(f"busy; pass {instance}", self.agent.notices[-1])
                            self.assertIn(shlex.join(commands[0]), self.agent.notices[-1])
                        self.assertEqual(len(self.agent.submitted), before)
                        self.assertEqual(len(self.agent.compactions), compacted)
                        self.assertEqual(self.agent.compact_inputs[current.pane], "")
                        self.assertEqual(status.read_text(), "running\n")
                        continue
                    if outcome in ("live detached run", "busy twice", "confirmed before notice", "replaced before notice",
                                   "replaced after handoff", "run starts during compaction"):
                        passed = worker_panes.worker_pass(current.row, self.run_dir)
                        pass_id = str(passed["instance_id"])
                        completion_id = progress_history.dispatch_delivery(passed).completion_id

                        def retry() -> None:
                            result = worker.assign(self.fixture.parent.id, "impl", self.prompt, name=current.row.name,
                                session_dir=self.run_dir, slot=current.row.name, phase="8", kind="impl",
                                launcher_command=launcher)
                            self.assertIs(result, worker.DispatchSubmission.SUBMITTED)

                        def check_pending() -> None:
                            saved = worker_panes.worker_pass(current.row, self.run_dir)
                            self.assertEqual(saved["status"], "active")
                            self.assertNotIn("confirmed_delivery_ids", saved)
                            self.assertEqual(saved, passed)
                            self.assertEqual(status.read_text(), "running\n")

                        def busy_events() -> list[str]:
                            notices = pass_notices.read(self.run_dir, pass_id)
                            assert notices is not None
                            return [entry.source_event_id for entry in notices.notices.values()
                                    if entry.source_event_id.startswith("busy:")]

                        send_notice = send.send
                        agent_command = worker._agent_command  # pyright: ignore[reportPrivateUsage]

                        def retry_from_notice(options: send.Options) -> None:
                            text = options.text or ""
                            retry_line = text.split("\nRetry this delivery: ", 1)[1].splitlines()[0]
                            command = shlex.split(retry_line)
                            self.assertEqual(command, commands[0])
                            self.assertEqual(worker.main(command[2:]), worker.ExitCode.OK)

                        def replace_pass() -> None:
                            replacement = progress_history.begin_dispatch(self.run_dir, slot=current.row.name,
                                phase="8", role="impl", worker_name=current.row.name, worker_id=current.row.id,
                                launcher_command=["replacement"], prompt=self.prompt, kind="impl", lens="")
                            self.assertNotEqual(replacement, pass_id)

                        def receive(options: send.Options) -> send.Result:
                            with worker_panes.worker_id_lock(current.row.id), file_lock.exclusive(
                                    self.run_dir / f"compact-{completion_id}.lock", wait=False) as lock:
                                self.assertIs(lock, file_lock.LockResult.ACQUIRED)
                            check_pending()
                            self.assertEqual(len(busy_events()), 1)
                            self.assertEqual(len(self.agent.submitted), before)
                            self.assertEqual(len(self.agent.compactions), compacted)
                            if outcome == "replaced after handoff":
                                replace_pass()
                            delivered = send_notice(options)
                            live_runs.close()
                            state_path = self.run_dir / unit_runs.STATE_FILE
                            state_before_retry = state_path.read_bytes()
                            with patch.object(worker, "_agent_command", new=agent_command):
                                retry_from_notice(options)
                            if outcome == "replaced after handoff":
                                self.assertEqual(state_path.read_bytes(), state_before_retry)
                                self.assertEqual(len(self.agent.submitted), before)
                            return delivered

                        dispatch_notice = worker._dispatch_notice  # pyright: ignore[reportPrivateUsage]

                        def change_before_notice(parent: staff.StaffId, identity: staff.StaffId, run: Path,
                                                 instance: str, pending: dict[str, object],
                                                 result: Literal["busy", "error", "uncertain"], detail: str) -> None:
                            check_pending()
                            live_runs.close()
                            if outcome == "replaced before notice":
                                replace_pass()
                            else:
                                retry()
                                self.assertEqual(len(commands), 2)
                                self.assertEqual(worker.main(commands[1][2:]), worker.ExitCode.OK)
                            dispatch_notice(parent, identity, run, instance, pending, result, detail)

                        if outcome != "run starts during compaction":
                            live_runs.enter_context(pane_fixtures.live_detached_run(self.run_dir, current.row))
                        with ExitStack() as notice:
                            if outcome == "live detached run":
                                _ = notice.enter_context(patch.object(send, "send", side_effect=receive))
                                with patch.object(worker, "_agent_command") as input_command:
                                    self.assertEqual(worker.main(commands[0][2:]), worker.ExitCode.BUSY)
                                input_command.assert_not_called()
                            else:
                                if outcome in ("confirmed before notice", "replaced before notice"):
                                    _ = notice.enter_context(patch.object(worker, "_dispatch_notice", side_effect=change_before_notice))
                                if outcome == "replaced after handoff":
                                    _ = notice.enter_context(patch.object(send, "send", side_effect=receive))
                                self.assertEqual(worker.main(commands[0][2:]), worker.ExitCode.BUSY)
                        self.assertIn("a detached run is live", errors.getvalue())
                        if outcome == "confirmed before notice":
                            self.assertEqual(len(self.agent.notices), notices_before)
                        else:
                            if outcome not in ("live detached run", "replaced before notice", "replaced after handoff"):
                                check_pending()
                            if outcome == "replaced before notice":
                                self.assertEqual(len(self.agent.notices), notices_before)
                                self.assertEqual(len(self.agent.submitted), before)
                                continue
                            if outcome == "replaced after handoff":
                                self.assertEqual(len(self.agent.notices), notices_before + 1)
                                self.assertIn(f"the dispatch attempt found the worker busy; pass {pass_id}", self.agent.notices[-1])
                                continue
                            if outcome != "live detached run":
                                self.assertEqual(len(self.agent.submitted), before)
                                self.assertEqual(len(self.agent.compactions), compacted + int(outcome == "run starts during compaction"))
                            self.assertEqual(len(self.agent.notices), notices_before + 1)
                            self.assertIn(f"busy; pass {pass_id}", self.agent.notices[-1])
                            self.assertIn("a detached run is live", self.agent.notices[-1])
                            self.assertIn(shlex.join(launcher), self.agent.notices[-1])
                            if outcome == "run starts during compaction":
                                self.assertEqual(order, ["returned", "compacted"])
                                continue
                            if outcome == "busy twice":
                                first_notice, = busy_events()
                                live_runs.close()
                                retry()
                                with (pane_fixtures.live_detached_run(self.run_dir, current.row),
                                      patch.object(send, "send", wraps=send_notice) as send_again):
                                    self.assertEqual(worker.main(commands[1][2:]), worker.ExitCode.BUSY)
                                check_pending()
                                first, identity = busy_events()
                                self.assertEqual(first, first_notice)
                                send_again.assert_called_once()
                                options = cast(send.Options, send_again.call_args.args[0])
                                notices = pass_notices.read(self.run_dir, str(pass_id))
                                assert notices is not None
                                event, = (key for key, entry in notices.notices.items() if entry.source_event_id == identity)
                                self.assertEqual(options.key, f"worker-completion:{pass_id}:{event}")
                                self.assertEqual(len(self.agent.notices), notices_before + 2)
                                self.assertEqual(self.agent.notices[-1], self.agent.notices[-2])
                                self.assertEqual(len(self.agent.compactions), compacted)
                                self.assertEqual(len(self.agent.submitted), before)
                                continue
                            self.assertEqual(len(commands), 1)
                        submitted = worker_panes.worker_pass(current.row, self.run_dir)
                        self.assertEqual(submitted["instance_id"], passed["instance_id"])
                        self.assertEqual(submitted["confirmed_delivery_ids"], [passed["instance_id"]])
                        self.assertEqual(order, ["returned", "compacted", "prompt"])
                        self.assertEqual(len(self.agent.submitted), before + 1)
                        continue
                    snapshot = staff.snapshot
                    reads = 0

                    def staff_read() -> staff.StaffSnapshot:
                        nonlocal reads
                        reads += 1
                        if reads == 1:
                            raise staff.StaffUnreadable("first staff read failed")
                        return snapshot()

                    complete_dispatch = progress_history.complete_dispatch
                    save_hook_completion = progress_history.save_hook_completion
                    save_reply = worker.save_reply
                    run_finished = turn_end.run_finished

                    def check_recovery_lock() -> None:
                        if outcome == "recover first confirmation failure":
                            with file_lock.exclusive(recovery_lock, wait=False) as competing:
                                self.assertIs(competing, file_lock.LockResult.BUSY)

                    def end_pass(run: Path, slot: str, instance: str, *,
                                 status: Literal["completed", "error"] = "completed") -> None:
                        check_recovery_lock()
                        failure_steps.append("pass end")
                        complete_dispatch(run, slot, instance, status=status)

                    def save_completion(run: Path, instance: str, completion: progress_history.HookCompletion) -> None:
                        check_recovery_lock()
                        failure_steps.append("hook completion")
                        save_hook_completion(run, instance, completion)

                    def write_reply(path: Path, text: str) -> None:
                        check_recovery_lock()
                        failure_steps.append("error reply")
                        save_reply(path, text)

                    def notify(owner: staff.StaffId) -> detached_runs.CompletionReplay:
                        if outcome == "recover first confirmation failure":
                            with file_lock.exclusive(recovery_lock, wait=False) as available:
                                self.assertIs(available, file_lock.LockResult.ACQUIRED)
                        failure_steps.append("notice")
                        with (patch.object(progress_history, "complete_dispatch", new=complete_dispatch),
                              patch.object(progress_history, "save_hook_completion", new=save_hook_completion),
                              patch.object(worker, "save_reply", new=save_reply)):
                            return run_finished(owner)

                    pending = worker_panes.worker_pass(current.row, self.run_dir)
                    delivery = progress_history.dispatch_delivery(pending)
                    recovery_lock = self.run_dir / f"compact-{delivery.completion_id}.lock"
                    read_confirmation = progress_history.dispatch_submission_confirmed

                    def fail_before_recovery(run: Path, instance: str, completion: str) -> bool:
                        nonlocal reads
                        reads += 1
                        if reads == 1:
                            raise OSError("first confirmation read failed")
                        return read_confirmation(run, instance, completion)

                    with ExitStack() as failures:
                        if outcome == "first staff read":
                            _ = failures.enter_context(patch.object(staff, "snapshot", side_effect=staff_read))
                        if outcome == "hook write":
                            _ = failures.enter_context(patch.object(progress_history, "save_hook_completion",
                                side_effect=OSError("hook completion write failed")))
                        if outcome == "recover first confirmation failure":
                            _ = failures.enter_context(patch.object(progress_history, "dispatch_submission_confirmed",
                                                                     new=fail_before_recovery))
                        if outcome in ("notice failure", "recover first confirmation failure"):
                            _ = failures.enter_context(patch.object(progress_history, "complete_dispatch", side_effect=end_pass))
                            _ = failures.enter_context(patch.object(progress_history, "save_hook_completion", side_effect=save_completion))
                            _ = failures.enter_context(patch.object(worker, "save_reply", side_effect=write_reply))
                            _ = failures.enter_context(patch.object(turn_end, "run_finished", side_effect=notify))
                        if outcome == "reply write":
                            _ = failures.enter_context(patch.object(worker, "save_reply", side_effect=OSError("reply write failed")))
                        code = worker.main(commands[0][2:])
                if outcome in {"compacted", "timeout with proof"}:
                    self.assertEqual(code, worker.ExitCode.OK)
                    self.assertEqual(order, ["returned", "compacted", "prompt"])
                    self.assertEqual(len(self.agent.submitted), before + 1)
                    self.assertIsInstance(progress_history.active_dispatch(current.row.id, self.run_dir), progress_history.ActiveDispatch)
                elif outcome == "no receipt":
                    self.assertEqual(code, worker.ExitCode.UNCERTAIN)
                    passed = worker_panes.worker_pass(current.row, self.run_dir)
                    self.assertEqual(passed.get("status"), "active")
                    self.assertIsInstance(progress_history.hook_completion(passed), progress_history.NoHookCompletion)
                    self.assertIn(current.pane, self.fixture.world.locations)
                    self.assertIn("submission is unproved", self.agent.notices[-1])
                    self.assertEqual(len(self.agent.submitted), before + 1)
                    self.assertEqual(status.read_text(), "running\n")
                else:
                    self.assertNotEqual(code, worker.ExitCode.OK)
                    self.assertEqual(worker_panes.worker_pass(current.row, self.run_dir).get("status"), "error")
                    self.assertEqual(status.read_text(), "running\n" if outcome in {"hook write", "reply write"} else "error\n")
                    if outcome == "notice failure":
                        self.assertEqual(len(self.agent.notices), notices_before)
                        self.assertEqual(failure_steps[:4], ["pass end", "hook completion", "error reply", "notice"])
                        self.assertEqual(errors.getvalue().count("dispatch notice failed: director message failed"), 1)
                        # The notice record keeps the failed notice, and a retry sends it once.
                        pass_notices.retry(self.run_dir)
                        self.assertEqual(len(self.agent.notices), notices_before + 1)
                        self.assertIn("error", self.agent.notices[-1])
                    else:
                        self.assertEqual(len(self.agent.notices), notices_before + 1)
                        self.assertIn("error", self.agent.notices[-1])
                    if outcome == "recover first confirmation failure":
                        self.assertEqual(code, worker.ExitCode.UNCERTAIN)
                        self.assertIn("first confirmation read failed", errors.getvalue())
                        self.assertEqual(failure_steps, ["pass end", "hook completion", "error reply", "notice"])
                    self.assertEqual(len(self.agent.submitted), before)
                self.assertEqual(self.agent.compact_inputs[current.pane], "")

    def check_compaction_helper_process(self, outcome: str, current: worker_panes.OpenWorker, source: Path) -> None:
        row = current.row
        notices = self.run_dir / f"{row.name}-notices.jsonl"
        _ = notices.write_text("")
        done = self.run_dir / f"{row.name}-exit"
        os.mkfifo(done)
        spawn = cast(Callable[[list[str], file_lock.ExclusiveLock, Path], None], getattr(worker, "_spawn_compaction"))
        before = len(self.agent.submitted)
        startup_failure = outcome in ("old helper arguments", "helper import failure")

        def start(command: list[str], lock: file_lock.ExclusiveLock, log: Path) -> None:
            if outcome == "old helper arguments":
                index = command.index("--completion-id")
                command = command[:index] + command[index + 2:]
            elif outcome == "helper import failure":
                command = [command[0], "-c", "raise ImportError('fixture helper import failed')"]
            elif outcome == "exit zero without a byte":
                command = [command[0], "-c", "raise SystemExit(0)"]
            else:
                fixture = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                           + "from scripts.production.test_worker import run_compaction_helper_fixture; "
                           + "raise SystemExit(run_compaction_helper_fixture())")
                command = [command[0], "-c", fixture, str(Path(command[1]).parents[2]), outcome, current.pane, current.terminal,
                           str(source), str(notices), str(done), *command[2:]]
            spawn(command, lock, log)

        descriptor = os.open(done, os.O_RDWR | os.O_NONBLOCK)
        try:
            with patch.object(worker, "_spawn_compaction", new=start):
                code, output, errors = self.command("assign", "impl", str(self.prompt), "--name", row.name,
                    "--slot", row.name, "--phase", "8", "--kind", "impl", "--session-dir", str(self.run_dir),
                    "--launcher-command", json.dumps(["assign.sh", "--to", row.name]))
            passed = worker_panes.worker_pass(row, self.run_dir)
            instance = str(passed["instance_id"])
            log = self.run_dir / f"compact-{instance}.log"
            if startup_failure:
                self.assertEqual(code, worker.ExitCode.REFUSED)
                self.assertNotIn("submitted", output)
                self.assertIn(str(log), errors)
                expected = "exit 2" if outcome == "old helper arguments" else "exit 1"
                self.assertIn(expected, errors)
                self.assertIn("required: --completion-id" if outcome == "old helper arguments"
                              else "fixture helper import failed", log.read_text())
            elif outcome == "exit zero without a byte":
                self.assertEqual(code, worker.ExitCode.UNCERTAIN)
                self.assertNotIn("submitted", output)
                self.assertIn(f"compaction helper exited without a startup byte. Log {log}", errors)
            else:
                self.assertEqual(code, worker.ExitCode.OK, errors)
                self.assertIn("submitted\n", output)
                guard = LauncherHangGuard(f"{outcome}: {log}")
                self.assertTrue(select.select([descriptor], [], [], guard.remaining())[0], log.read_text())
                helper_exit = int(os.read(descriptor, 16))
                self.assertEqual(helper_exit, worker.ExitCode.REFUSED if outcome == "failure before input"
                                 else worker.ExitCode.UNCERTAIN, log.read_text())
        finally:
            os.close(descriptor)

        passed = worker_panes.worker_pass(row, self.run_dir)
        delivery = progress_history.dispatch_delivery(passed)
        receipt = worker.dispatch_receipt(row, delivery.line, delivery.completion_id, run=self.run_dir)
        delivered = [cast(str, json.loads(line)) for line in notices.read_text().splitlines()]
        self.assertEqual(len(self.agent.submitted), before)
        self.assertNotIn("confirmed_delivery_ids", passed)
        with file_lock.exclusive(self.run_dir / f"compact-{delivery.completion_id}.lock", wait=False) as lock:
            self.assertIs(lock, file_lock.LockResult.ACQUIRED)
        if outcome in ("receipt with failed confirmation", "failure after input"):
            self.assertEqual(passed["status"], "active")
            self.assertIsInstance(receipt, worker.DispatchSubmitted)
            notice, = delivered
            self.assertIn(f"uncertain; pass {instance}", notice)
            self.assertIn("fixture could not save confirmation" if outcome == "receipt with failed confirmation"
                          else "fixture lost the prompt response", notice)
        elif outcome == "exit zero without a byte":
            self.assertEqual(passed["status"], "active")
            self.assertIsInstance(receipt, worker.DispatchNotSubmitted)
            self.assertEqual(delivered, [])
        else:
            self.assertEqual(passed["status"], "error")
            self.assertIsInstance(receipt, worker.DispatchNotSubmitted)
            if startup_failure:
                self.assertEqual(delivered, [])
            else:
                notice, = delivered
                self.assertIn(f"{row.name}: phase 8: error;", notice)
                self.assertIn(f"; pass {instance}", notice)
                self.assertIn("fixture compaction failed", notice)

    def check_new_worker_before_pass(self) -> None:
        name = staff.WorkerName.parse("fresh-dispatch")
        before = len(self.agent.submitted)
        check = worker.CheckAssignmentAvailability.check
        checked: list[staff.StaffId] = []

        def observe_check(availability: worker.CheckAssignmentAvailability, row: staff.StaffRow,
                          hold: worker_panes.WorkerLockHold, *,
                          retry: worker.RetryingAssignment | worker.AssignmentCheckStage,
                          ) -> worker_panes.CloseAllowed | worker_panes.DispatchHoldsWorker:
            if retry is worker.AssignmentCheckStage.DELIVERY:
                self.assertTrue(hold.owns(row))
                self.assertEqual(staff.snapshot().by_id(row.id), row)
                self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.NoActiveDispatch)
                checked.append(row.id)
            return check(availability, row, hold, retry=retry)

        self.assertIsNone(worker_admission.find_worker(staff.snapshot(), self.fixture.parent.id, name.value))
        with patch.object(worker.CheckAssignmentAvailability, "check", new=observe_check):
            result = worker.dispatch(self.fixture.parent.id, name, "impl", self.prompt, session_dir=self.run_dir,
                slot=name.value, phase="8", kind="impl", launcher_command=["fresh-dispatch"])
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(len(self.agent.submitted), before + 1)
        self.assertEqual(checked, [self.agent.row(self.agent.submitted[-1][0]).id])

    def check_admission_waits_for_compaction(self, current: worker_panes.OpenWorker, helper: list[str],
                                            finish: Callable[[str, int], herdr.ObservedAgentState | herdr.WaitTimedOut],
                                            errors: io.StringIO) -> None:
        guard = LauncherHangGuard("detached admission during compaction")
        attempted = Event()
        dispatch_thread = current_thread()
        acquire = worker_panes.worker_id_lock
        atomic_file.write_text_atomic(self.fixture.root / "conf/detached_runs.conf",
            (Path(__file__).resolve().parents[2] / "config/detached_runs.conf").read_text())
        runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        existing = set(runs.glob("*.json"))
        admissions: list[Future[int]] = []
        spawn = Mock()
        real_popen = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)

        def start_process(arguments: list[str], **options: object) -> subprocess.Popen[bytes] | SimpleNamespace:
            if "supervise" in arguments:
                _ = cast(object, spawn(arguments, **options))
                return SimpleNamespace(pid=4321)
            return real_popen(arguments, **options)

        @contextmanager
        def observe_worker_lock(identity: staff.StaffId, *, blocking: bool = False,
                                deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> Generator[None]:
            if current_thread() is not dispatch_thread:
                self.assertEqual(identity, current.row.id)
                self.assertTrue(blocking)
                with self.assertRaises(BlockingIOError), acquire(identity):
                    pass
                attempted.set()
            with acquire(identity, blocking=blocking, deadline=deadline):
                yield

        with (ThreadPoolExecutor(max_workers=1) as executor,
              patch.object(worker_panes, "worker_id_lock", new=observe_worker_lock),
              patch.object(treekey, "tree_key", return_value="fixture-tree"),
              patch.object(subprocess, "Popen", side_effect=start_process)):
            def publish_during_compaction(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
                admission = executor.submit(detached_runs.main, [
                    "start", "--session-dir", str(self.run_dir), "--to", current.row.name, "--", "fixture-check",
                ])
                admissions.append(admission)
                admission.add_done_callback(lambda _result: attempted.set())
                guard.event(attempted)
                if admission.done():
                    self.fail(f"Detached admission returned before dispatch released the lock. Exit {admission.result()}.")
                self.assertFalse(admission.done())
                with file_lock.exclusive(runs / ".start.lock", wait=False) as lock:
                    self.assertIs(lock, file_lock.LockResult.ACQUIRED)
                    self.assertEqual(set(runs.glob("*.json")), existing)
                    spawn.assert_not_called()
                return finish(pane, timeout_ms)

            self.assertIsInstance(detached_runs.caller_from_environment(dict(os.environ)), detached_runs.DirectorCaller)
            with patch.object(self.fixture.world, "wait_turn_end", side_effect=publish_during_compaction):
                self.assertEqual(worker.main(helper), worker.ExitCode.OK, errors.getvalue())
            admission, = admissions
            self.assertEqual(guard.result(admission), 0, errors.getvalue())
            spawn.assert_called_once()
        path, = set(runs.glob("*.json")) - existing
        saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual((saved["staff_id"], saved["pid"]), (current.row.id, 4321))
        passed = worker_panes.worker_pass(current.row, self.run_dir)
        self.assertEqual(passed["confirmed_delivery_ids"], [saved.get("pane_dispatch_id")])

    def test_assignment_retry_uses_the_pending_compaction(self) -> None:
        with self.subTest(outcome="helper lock held"):
            self.check_pending_compaction_retry()
        with self.subTest(outcome="retry proves startup failure"):
            self.check_retry_closes_compaction_startup_failure()

    def check_pending_compaction_retry(self) -> None:
        opened = self.opened()
        row = opened.row
        backend = staff.backend(row)
        assert isinstance(backend, staff.CodexBackend)
        command = ("assign", "impl", str(self.prompt), "--name", row.name, "--slot", row.name,
            "--phase", "8", "--kind", "assign", "--session-dir", str(self.run_dir),
            "--launcher-command", json.dumps(["assign.sh", "--to", row.name]))

        with ExitStack() as child:
            def queue(_command: list[str], lock: file_lock.ExclusiveLock, _log: Path) -> None:
                _ = child.callback(os.close, os.dup(lock.descriptor))

            with (patch.object(worker, "_spawn_compaction", side_effect=queue) as spawn,
                  patch.object(worker, "_compaction_plan", return_value=worker.CompactionRequired(backend, 0))):
                code, output, errors = self.command(*command)
                self.assertEqual(code, 0, errors)
                self.assertIn(worker.COMPACTION_STARTED_TEXT, output)
                passed = worker_panes.worker_pass(row, self.run_dir)
                for worker_locked, recorder_locked in ((False, False), (True, False), (False, True), (True, True)):
                    with self.subTest(worker_locked=worker_locked, recorder_locked=recorder_locked), ExitStack() as retry:
                        if worker_locked:
                            hold = retry.enter_context(worker_panes.worker_lock(self.fixture.parent.id,
                                staff.WorkerName.parse(row.name)))
                            self.assertIsInstance(hold, worker_panes.WorkerLockHold)
                        if recorder_locked:
                            lock = retry.enter_context(file_lock.exclusive(self.run_dir / ".progress_history.lock"))
                            self.assertIs(lock, file_lock.LockResult.ACQUIRED)
                            timeout = conf.read("direct").number("WORKER_SETTLE_SECONDS")

                            def retry_assignment() -> int:
                                code, output, errors = self.command(*command)
                                print(output, end="")
                                print(errors, end="", file=sys.stderr)
                                return code

                            with ForkChildren() as children:
                                result = children.start(retry_assignment).result(timeout=timeout)
                            code, output, errors = result.returncode, result.stdout, result.stderr
                        else:
                            code, output, errors = self.command(*command)
                        self.assertEqual(code, worker.ExitCode.UNCERTAIN)
                        self.assertNotIn("submitted", output)
                        self.assertIn("compaction helper submission is uncertain", errors)
                self.assertEqual(worker_panes.worker_pass(row, self.run_dir), passed)
                spawn.assert_called_once()
            self.assertEqual(self.agent.submitted, [])
            self.assertNotIn("confirmed_delivery_ids", passed)

        delivery = progress_history.dispatch_delivery(passed)
        self.assertEqual(worker.finish_dispatch(self.fixture.parent.id, row.id, self.run_dir,
                                                str(passed["instance_id"]), delivery.completion_id), 0)
        self.assertEqual(len(self.agent.submitted), 1)
        current = worker_panes.worker_pass(row, self.run_dir)
        self.assertEqual(current["confirmed_delivery_ids"], [delivery.completion_id])

    def check_retry_closes_compaction_startup_failure(self) -> None:
        row = self.opened("impl2").row
        backend = staff.backend(row)
        assert isinstance(backend, staff.CodexBackend)
        command = ("assign", "impl", str(self.prompt), "--name", row.name, "--slot", row.name,
                   "--phase", "8", "--kind", "assign", "--session-dir", str(self.run_dir),
                   "--launcher-command", json.dumps(["assign.sh", "--to", row.name]))
        status = self.run_dir / f"{row.name}_status"
        retry_status = self.run_dir / "retry_status"
        atomic_file.write_text_atomic(retry_status, "running\n")
        proposed = str(uuid4())
        first = str(uuid4())
        spawn = cast(Callable[[list[str], file_lock.ExclusiveLock, Path], None], getattr(worker, "_spawn_compaction"))

        def start_without_byte(helper: list[str], lock: file_lock.ExclusiveLock, log: Path) -> None:
            spawn([helper[0], "-c", f"raise SystemExit({int(retrying)})"], lock, log)

        submitted = len(self.agent.submitted)
        retrying = False
        with (patch.object(worker, "_spawn_compaction", new=start_without_byte),
              patch.object(worker, "_compaction_plan", return_value=worker.CompactionRequired(backend, 0))):
            with patch.dict(os.environ, {progress_history.ASSIGN_STATUS_FILE_ENV: str(status)}):
                code, output, errors = self.command(*command, "--new-pass-instance-id", first)
            self.assertEqual(code, worker.ExitCode.UNCERTAIN, errors)
            self.assertNotIn("submitted", output)
            passed = worker_panes.worker_pass(row, self.run_dir)
            self.assertEqual((passed["instance_id"], passed["status"]), (first, "active"))
            self.assertEqual(status.read_text(), "running\n")

            retrying = True
            with patch.dict(os.environ, {progress_history.ASSIGN_STATUS_FILE_ENV: str(retry_status)}):
                code, output, errors = self.command(*command, "--new-pass-instance-id", proposed)
            self.assertEqual(code, worker.ExitCode.REFUSED, errors)
            self.assertNotIn("submitted", output)
            self.assertIn(f"exit 1 before startup. Log {self.run_dir / f'compact-{first}.log'}", errors)
        passed = worker_panes.worker_pass(row, self.run_dir)
        self.assertEqual((passed["instance_id"], passed["status"]), (first, "error"))
        self.assertEqual(status.read_text(), "error\n")
        self.assertEqual(retry_status.read_text(), "running\n")
        self.assertEqual(len(self.agent.submitted), submitted)

        following = self.run_dir / "next-prompt.md"
        atomic_file.write_text_atomic(following, "Check the next change.\n")
        code, output, errors = self.command(*command[:2], str(following), *command[3:])
        self.assertEqual(code, worker.ExitCode.OK, errors)
        self.assertIn("submitted", output)
        self.assertEqual(len(self.agent.submitted), submitted + 1)

    def test_launcher_death_after_spawn_keeps_the_compaction_lock(self) -> None:
        for timing in ("before the byte", "after the byte"):
            with self.subTest(launcher_death=timing):
                self.check_launcher_death_during_compaction(timing == "after the byte")

    def check_launcher_death_during_compaction(self, acknowledged: bool) -> None:
        opened = self.opened("impl-after-byte" if acknowledged else "impl-before-byte")
        backend = staff.backend(opened.row)
        assert isinstance(backend, staff.CodexBackend)
        name = opened.row.name
        command = ("assign", "impl", str(self.prompt), "--name", name, "--slot", name,
                   "--phase", "8", "--kind", "assign", "--session-dir", str(self.run_dir),
                   "--launcher-command", json.dumps(["assign.sh", "--to", name]))
        gate = self.run_dir / f"{name}-helper-gate"
        os.mkfifo(gate)
        descriptor = os.open(gate, os.O_RDWR)
        spawn = cast(Callable[[list[str], file_lock.ExclusiveLock, Path], None], getattr(worker, "_spawn_compaction"))
        timeout = conf.read("direct").number("WORKER_SETTLE_SECONDS")

        def crash_after_spawn(_command: list[str], lock: file_lock.ExclusiveLock, log: Path) -> None:
            startup = ("os.write(started, b'1'); os.close(started); " if acknowledged
                       else "assert os.getppid() == int(sys.argv[3]); os.kill(int(sys.argv[3]), signal.SIGKILL); ")
            child = [
                sys.executable, "-c", "import os, select, signal, sys; "
                + "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK); "
                + "started = int(sys.argv[sys.argv.index('--started-fd') + 1]); " + startup
                + "select.select([fd], [], [], float(sys.argv[2]))",
                str(gate), str(timeout), str(os.getpid()),
            ]
            spawn(child, lock, log)
            os._exit(91)

        def launch() -> int:
            with (patch.object(worker, "_spawn_compaction", side_effect=crash_after_spawn),
                  patch.object(worker, "_compaction_plan", return_value=worker.CompactionRequired(backend, 0))):
                code, _, _ = self.command(*command)
                return code

        try:
            with ForkChildren() as children:
                crashed = children.start(launch)
                result = crashed.result(timeout=timeout)
                self.assertEqual(result.returncode, 91 if acknowledged else -signal.SIGKILL, result.stderr)
                with patch.object(worker, "_spawn_compaction") as duplicate:
                    code, output, errors = self.command(*command)
                    self.assertEqual(code, worker.ExitCode.UNCERTAIN)
                    self.assertNotIn("submitted", output)
                    self.assertIn("compaction helper submission is uncertain", errors)
                duplicate.assert_not_called()
                passed = worker_panes.worker_pass(opened.row, self.run_dir)
                self.assertEqual(passed["status"], "active")
                self.assertNotIn("confirmed_delivery_ids", passed)
                self.assertEqual(self.agent.submitted, [])
        finally:
            _ = os.write(descriptor, b"x")
            os.close(descriptor)

    def test_detached_dispatch_failure_preserves_a_retry_confirmed_after_lock_release(self) -> None:
        opened = self.opened()
        parent = self.fixture.parent.id
        name = staff.WorkerName.parse(opened.row.name)
        instance = progress_history.begin_dispatch(self.run_dir, slot=name.value, phase="8", role="impl",
            worker_name=name.value, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.prompt, kind="assign", lens="")
        progress_history.save_dispatch_delivery(self.run_dir, instance,
            progress_history.DispatchDelivery("Read the prompt.", instance, str(self.reply)))
        acquire = worker_panes.worker_lock
        retried = False

        @contextmanager
        def retry_after_release(owner: staff.StaffId, target: staff.WorkerName, *, blocking: bool = False
                                ) -> Generator[worker_panes.WorkerLockHold | worker_panes.Kept]:
            nonlocal retried
            with acquire(owner, target, blocking=blocking) as hold:
                yield hold
            if not retried:
                with acquire(owner, target) as retry_hold:
                    assert isinstance(retry_hold, worker_panes.WorkerLockHold)
                    progress_history.confirm_assignment_submission(self.run_dir, instance, instance)
                retried = True

        with (patch.object(worker_panes, "worker_lock", new=retry_after_release),
              patch.object(worker_panes, "observe_worker", return_value=opened),
              patch.object(worker, "dispatch_turn", return_value=worker.ExitCode.REFUSED),
              patch.object(worker, "_dispatch_notice") as dispatch_notice,
              patch.object(turn_end, "run_finished") as completion_notice):
            code = worker.finish_dispatch(parent, opened.row.id, self.run_dir, instance, instance)

        self.assertTrue(retried)
        self.assertEqual(code, worker.ExitCode.OK)
        passed = worker_panes.worker_pass(opened.row, self.run_dir)
        self.assertEqual((passed["instance_id"], passed["status"], passed["confirmed_delivery_ids"]),
                         (instance, "active", [instance]))
        self.assertIsInstance(progress_history.hook_completion(passed), progress_history.NoHookCompletion)
        self.assertFalse(self.reply.with_name("reply_dispatch_error.md").exists())
        dispatch_notice.assert_not_called()
        completion_notice.assert_not_called()

    def test_confirmed_stage_one_does_not_hide_a_detached_stage_two_failure(self) -> None:
        opened = self.opened("review", "review")
        parent = self.fixture.parent.id
        instance = progress_history.begin_dispatch(self.run_dir, slot="review", phase="8", role="review",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.prompt, kind="assign", lens="")
        first = progress_history.DispatchDelivery(f"DONE {instance}", instance, str(self.reply))
        second_id = worker_panes.stage_two_completion_id(instance)
        second = replace(first, line=f"Stage 2: DONE {second_id}", completion_id=second_id)
        with worker_panes.worker_lock(parent, staff.WorkerName.parse(opened.row.name)) as hold:
            assert isinstance(hold, worker_panes.WorkerLockHold)
            progress_history.save_dispatch_delivery(self.run_dir, instance, first)
            progress_history.confirm_assignment_submission(self.run_dir, instance, first.completion_id)
            progress_history.save_dispatch_delivery(self.run_dir, instance, second)

        recorded = assign.record_worker_launch_exit(
            self.run_dir, UUID(instance), progress_history.AssignmentLaunchExit.CLOSE_FAILED_PASS,
        )
        self.assertEqual(recorded, progress_history.RecordedAssignmentLaunch("review", failure_recorded=False))

        with (patch.object(worker_panes, "observe_worker", return_value=opened),
              patch.object(worker, "_client", return_value=self.fixture.world),
              patch.object(worker, "dispatch_turn", return_value=worker.ExitCode.REFUSED) as dispatch,
              patch.object(turn_end, "run_finished") as completion_notice,
              redirect_stderr(io.StringIO())):
            code = worker.finish_dispatch(parent, opened.row.id, self.run_dir, instance, second_id)

        dispatch.assert_called_once_with(parent, opened, self.fixture.world, second.line, second_id,
                                         run=self.run_dir, instance_id=instance, before_input=ANY, on_submission=ANY)
        self.assertTrue(callable(cast(object, dispatch.call_args.kwargs["before_input"])))
        self.assertTrue(callable(cast(object, dispatch.call_args.kwargs["on_submission"])))
        self.assertEqual(code, worker.ExitCode.REFUSED)
        passed = worker_panes.worker_pass(opened.row, self.run_dir)
        self.assertEqual(passed["status"], "error")
        self.assertEqual(passed["confirmed_delivery_ids"], [instance])
        completion = progress_history.hook_completion(passed)
        assert isinstance(completion, progress_history.HookCompletion)
        self.assertEqual(completion.outcome, "error")
        self.assertIn("dispatch failed", Path(completion.reply_file).read_text())
        completion_notice.assert_called_once_with(opened.row.id)

    def test_detached_stage_one_failure_keeps_stage_two_pending_after_a_staff_read_failure(self) -> None:
        opened = self.opened("review", "review")
        parent = self.fixture.parent.id
        name = staff.WorkerName.parse(opened.row.name)
        backend = staff.backend(opened.row)
        assert isinstance(backend, staff.CodexBackend)
        with (patch.object(worker, "_compaction_plan", return_value=worker.CompactionRequired(backend, 0)),
              patch.object(worker, "_spawn_compaction", return_value=None) as queued,
              redirect_stdout(io.StringIO())):
            result = worker.dispatch(parent, name, "review", self.prompt, session_dir=self.run_dir,
                slot=name.value, phase="8", kind="assign", launcher_command=["assign.sh"],
                early_ready=self.run_dir / "final.ready", reply=self.reply)
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        queued.assert_called_once()
        command = cast(list[str], queued.call_args.args[0])

        passed = worker_panes.worker_pass(opened.row, self.run_dir)
        instance = str(passed["instance_id"])
        first = progress_history.dispatch_delivery(passed)
        second_id = worker_panes.stage_two_completion_id(instance)
        second = replace(first, line=f"Stage 2: DONE {second_id}", completion_id=second_id)

        acquire = worker_panes.worker_id_lock
        advanced = False
        snapshot = staff.snapshot
        first_read = True

        def staff_read() -> staff.StaffSnapshot:
            nonlocal first_read
            if first_read:
                first_read = False
                raise staff.StaffUnreadable("first staff read failed")
            return snapshot()

        @contextmanager
        def advance_before_recovery(identity: staff.StaffId, *, blocking: bool = False,
                                    deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> Generator[None]:
            nonlocal advanced
            if not advanced and identity == opened.row.id:
                with acquire(identity):
                    progress_history.confirm_assignment_submission(self.run_dir, instance, first.completion_id)
                    progress_history.save_dispatch_delivery(self.run_dir, instance, second)
                advanced = True
            with acquire(identity, blocking=blocking, deadline=deadline):
                yield

        with (patch.object(staff, "snapshot", side_effect=staff_read),
              patch.object(worker_panes, "worker_id_lock", new=advance_before_recovery),
              patch.object(worker, "dispatch_turn") as dispatch,
              patch.object(worker, "save_reply") as reply,
              patch.object(worker, "_dispatch_notice") as dispatch_notice,
              patch.object(turn_end, "run_finished") as completion_notice):
            code, _, errors = self.command(*command[2:])

        self.assertTrue(advanced)
        self.assertEqual((code, errors), (worker.ExitCode.OK, ""))
        passed = worker_panes.worker_pass(opened.row, self.run_dir)
        self.assertEqual(passed["status"], "active")
        self.assertEqual(progress_history.dispatch_delivery(passed), second)
        self.assertEqual(passed["confirmed_delivery_ids"], [instance])
        self.assertIsInstance(progress_history.hook_completion(passed), progress_history.NoHookCompletion)
        dispatch.assert_not_called()
        reply.assert_not_called()
        dispatch_notice.assert_not_called()
        completion_notice.assert_not_called()

    def test_closed_conversation_compacts_before_the_next_dispatch(self) -> None:
        for index, (tokens, failure) in enumerate(((43000, ""), (43001, ""), (43001, "compact unavailable"))):
            with self.subTest(tokens=tokens, failure=failure):
                self.agent.compact_error = failure
                name = f"impl-compact-{index}"
                self.assertIs(self.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.agent.submitted[-1][0]
                row = self.agent.row(pane)
                backend = staff.backend(row)
                assert isinstance(backend, staff.CodexBackend)
                source = worker_admission.transcript_source(row, backend)
                with source.open("a") as stream:
                    _ = stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": tokens, "cached_input_tokens": 0, "output_tokens": 0}}}}) + "\n")
                self.assertEqual(self.finish(name), turn_end.TurnOutcome.DONE)
                self.assertNotIn(pane, self.fixture.world.locations)
                following = self.prompt.with_name(f"next-{index}.md")
                atomic_file.write_text_atomic(following, "Review the next change.")
                before = len(self.agent.submitted)
                commands: list[list[str]] = []

                def queue(command: list[str], _lock: file_lock.ExclusiveLock, _log: Path) -> None:
                    commands.append(command)

                with (patch.object(worker, "_spawn_compaction", side_effect=queue),
                      redirect_stderr(io.StringIO()) as errors):
                    result = worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(name), "impl", following,
                        session_dir=self.run_dir, slot="impl", phase="8", kind="impl", launcher_command=["assign.sh", str(following)],
                        reply=self.reply)
                    if commands:
                        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                        result = worker.main(commands[0][2:])
                if failure:
                    self.assertIs(result, worker.ExitCode.REFUSED)
                    self.assertEqual(len(self.agent.submitted), before)
                    self.assertIn(failure, errors.getvalue())
                    self.assertIn(f"{name}: compaction failed:", errors.getvalue())
                    self.assertIn("no prompt sent", errors.getvalue())
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertNotIn("prompt delivered to pane", errors.getvalue())
                    continue
                self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                reopened = self.agent.submitted[-1][0]
                self.assertNotEqual(reopened, pane)
                self.assertEqual(self.agent.row(reopened).current_session, row.current_session)
                expected = [(reopened, before)] if tokens > 43000 and not failure else []
                self.assertEqual(self.agent.compactions, expected)
                self.assertEqual(self.finish(name), turn_end.TurnOutcome.DONE)
                self.agent.compactions.clear()

        with self.subTest(storage="compressed"):
            self.agent.compact_error = ""
            current = self.opened("impl-compact-saved")
            backend = staff.backend(current.row)
            assert isinstance(backend, staff.CodexBackend)
            source = worker_admission.transcript_source(current.row, backend)
            with source.open("a") as stream:
                _ = stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 43001, "cached_input_tokens": 0, "output_tokens": 0}}}}) + "\n")
            content = source.read_bytes()
            _ = compressed_copy(source)
            before = len(self.agent.submitted)

            def resume_transcript() -> None:
                atomic_file.write_bytes_atomic(source, content)

            with worker_panes.worker_id_lock(current.row.id), patch.object(staff, "backend", return_value=backend):
                result = worker.dispatch_turn(self.fixture.parent.id, current, self.fixture.world,
                    "End your final reply with DONE compressed-dispatch", "compressed-dispatch",
                    run=self.run_dir, instance_id="compressed-dispatch", on_submission=resume_transcript,
                    before_input=lambda _row: worker_panes.CloseAllowed())
            self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
            self.assertEqual(self.agent.compactions, [(current.pane, before)])

    def test_long_dispatch_waits_without_submitting_again(self) -> None:
        self.agent.finish_after = 3
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.agent.submitted[0][0]
        active = progress_history.active_dispatch(self.agent.row(pane).id, self.run_dir)
        self.assertIsInstance(active, progress_history.ActiveDispatch)
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.assertEqual(progress_history.active_dispatch(self.agent.row(pane).id, self.run_dir), active)

    def test_dispatch_rerun_after_prompt_uses_the_same_pass(self) -> None:
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "after prompt"):
            _ = self.dispatch()
        state = (self.run_dir / "progress_history_state.json").read_bytes()
        pane = self.agent.submitted[0][0]
        self.fixture.world.locations[pane]["agent_status"] = "working"
        self.agent.finish_after = 1
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        confirmed = self.assert_pass_confirmed(state)
        self.agent.complete(pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assert_pass_completed(confirmed)

    def test_dispatch_rerun_after_done_restores_the_reply_once(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        write = atomic_file.write_text_atomic
        def interrupted(path: Path, text: str) -> None:
            if path == self.reply:
                raise OSError("interrupted before reply publication")
            write(path, text)
        with patch.object(atomic_file, "write_text_atomic", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "before reply"):
                _ = self.finish()
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        before = self.reply.read_bytes(), self.reply.stat().st_mtime_ns, self.fixture.staff_state()
        self.assertEqual(self.finish(), turn_end.TurnOutcome.IGNORED)
        self.assertEqual((self.reply.read_bytes(), self.reply.stat().st_mtime_ns, self.fixture.staff_state()), before)
        self.assertEqual(len(self.agent.submitted), 1)

    def assert_reassignment_after_launcher_error(self, *, fail_notice: bool) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.agent.submitted[-1][0]
        row = self.agent.row(pane)
        previous = worker_panes.worker_pass(row, self.run_dir)
        instance = str(previous["instance_id"])
        self.assertTrue(self.agent.replies[-1].endswith(f"DONE {instance}"))
        progress_history.complete_dispatch(self.run_dir, "impl", instance, status="error")
        if fail_notice:
            self.agent.notice_failures = 1
            with self.assertRaisesRegex(ValueError, "director message failed"):
                _ = self.finish()
        with patch.object(send, "send") as send_during_assignment:
            self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        send_during_assignment.assert_not_called()
        self.assertEqual(self.agent.notices, [])
        self.assertEqual(self.reply.read_text().rstrip(), self.agent.replies[0])
        _ = detached_runs.reap_orphans(str(self.run_dir))
        notice, = self.agent.notices
        self.assertIn(f"; pass {instance}", notice)
        self.assertIn("The earlier pass ended with state error.", notice)
        self.assertEqual(len(self.agent.submitted), 2)
        next_pane, line = self.agent.submitted[-1]
        self.assertEqual(self.agent.row(next_pane).id, row.id)
        current = worker_panes.worker_pass(row, self.run_dir)
        self.assertNotEqual(current["instance_id"], instance)
        self.assertIn(f"DONE {current['instance_id']}", line)
        self.assertEqual(current["status"], "active")
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(len(self.agent.submitted), 2)
        self.assertEqual(len(self.agent.notices), 1)

    def test_reassignment_after_launcher_error_starts_a_new_turn_before_delivery(self) -> None:
        self.assert_reassignment_after_launcher_error(fail_notice=False)

    def test_reassignment_after_a_failed_notice_starts_one_new_turn(self) -> None:
        self.assert_reassignment_after_launcher_error(fail_notice=True)

    def test_completed_dispatch_rerun_leaves_the_settled_notice_and_pane_alone(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.agent.submitted[-1][0]
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        notice, = self.agent.notices
        path = self.run_dir / "progress_history_state.json"
        before = path.read_bytes(), path.stat().st_mtime_ns, self.fixture.staff_state()
        reply = self.reply.read_bytes(), self.reply.stat().st_mtime_ns
        for repeated in (1, 2, 3, 4):
            with self.subTest(repeated=repeated), ExitStack() as sends:
                if repeated == 4:
                    row = self.agent.row(pane)
                    passed = worker_panes.worker_pass(row, self.run_dir)
                    delivery = pass_recorder.dispatch_delivery(passed)
                    status = self.run_dir / "shared-status"
                    progress_history.save_dispatch_delivery(self.run_dir, str(passed["instance_id"]),
                        replace(delivery, actions=replace(delivery.actions, status_file=str(status))))
                    with ExitStack() as notices:
                        self.assertIs(worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse("impl"), "impl", self.prompt,
                            session_dir=self.run_dir, slot="impl", phase="8", kind="impl",
                            launcher_command=["assign.sh", "--phase", "8", "--prompt", str(self.prompt)], reply=self.reply,
                            notices=notices), worker.DispatchSubmission.SUBMITTED)
                        self.agent.finish_after = 1
                        self.assertIs(worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse("next-pass"), "impl", self.prompt,
                            session_dir=self.run_dir, slot="next-pass", phase="8", kind="impl", launcher_command=["next-assign"],
                            reply=self.run_dir / "next-reply.md",
                            completion_actions=pass_recorder.CompletionActions(status_file=str(status)),
                            on_submission=lambda: atomic_file.write_text_atomic(status, "running\n")), worker.DispatchSubmission.SUBMITTED)
                    self.assertEqual(status.read_text(), "running\n")
                    continue
                if repeated == 3:
                    transport = sends.enter_context(patch.object(send, "send"))
                    with ExitStack() as notices, worker_panes.worker_lock(self.fixture.parent.id, staff.WorkerName.parse("impl")) as hold:
                        self.assertIsInstance(hold, worker_panes.WorkerLockHold)
                        assert isinstance(hold, worker_panes.WorkerLockHold)
                        result = worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse("impl"), "impl", self.prompt,
                            session_dir=self.run_dir, slot="impl", phase="8", kind="impl",
                            launcher_command=["assign.sh", "--phase", "8", "--prompt", str(self.prompt)], reply=self.reply,
                            lock=hold, notices=notices)
                    self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                    transport.assert_not_called()
                else:
                    self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
                self.assertEqual(self.agent.notices, [notice])
                self.assertEqual(len(self.agent.submitted), 1)
                self.assertEqual(len(self.fixture.world.launches), 1)
                self.assertNotIn(pane, self.fixture.world.locations)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, self.fixture.staff_state()), before)
                self.assertEqual((self.reply.read_bytes(), self.reply.stat().st_mtime_ns), reply)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.IGNORED)
        self.assertEqual(len(self.agent.notices), 1)

    def test_dispatch_rerun_before_prompt_records_one_submission(self) -> None:
        target = herdr.check_target
        def interrupted(identity: str | herdr.AgentProcessIdentity, **kwargs: object) -> bool:
            del identity, kwargs
            raise OSError("interrupted before prompt")
        with patch.object(herdr, "check_target", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "before prompt"):
                _ = self.dispatch()
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIs(herdr.check_target, target)

    def test_an_earlier_done_line_does_not_complete_another_dispatch(self) -> None:
        opened = self.opened()
        self.agent.append(opened.pane, "assistant", "An old result.\nDONE another-pass")
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertNotIn("old result", self.reply.read_text())

    def test_settled_without_done_preserves_the_existing_report(self) -> None:
        atomic_file.write_text_atomic(self.reply, "Full report from the prior turn.\n")
        self.agent.without_done = True
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.NO_DONE)
        self.assertEqual(self.reply.read_text(), "Full report from the prior turn.\n")
        self.assertIn("no DONE", self.agent.notices[-1])

        for end_worker in (False, True):
            with self.subTest(turn="compaction without an assistant reply", end_worker=end_worker):
                name = "compacted-ending" if end_worker else "compacted"
                self.agent.finish_after = 1
                self.assertIs(worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(name), "impl", self.prompt,
                    session_dir=self.run_dir, slot=name, phase="8", kind="impl", launcher_command=[name],
                    reply=self.run_dir / f"{name}-reply.md",
                    completion_actions=pass_recorder.CompletionActions(end_worker=end_worker)),
                    worker.DispatchSubmission.SUBMITTED)
                pane = self.agent.submitted[-1][0]
                row = self.agent.row(pane)
                backend = staff.backend(row)
                assert isinstance(backend, staff.CodexBackend)
                with worker_admission.transcript_source(row, backend).open("a") as stream:
                    _ = stream.write(json.dumps({"type": "compacted"}) + "\n")
                self.fixture.world.locations[pane]["agent_status"] = "idle"
                active = progress_history.active_dispatch(row.id, self.run_dir)
                assert isinstance(active, progress_history.ActiveDispatch)
                notices = len(self.agent.notices)
                self.assertEqual(self.finish(name), turn_end.TurnOutcome.NO_DONE)
                self.assertEqual(self.agent.notices[notices:], [f"{name}: phase 8: no DONE; pass {active.instance_id}"])
                self.assertEqual(progress_history.active_dispatch(row.id, self.run_dir), active)
                self.assertEqual(self.reply.read_text(), "Full report from the prior turn.\n")
                self.assertIn(pane, self.fixture.world.locations)
                self.assertEqual(staff.by_id(row.id), row)

    def assert_detached_dispatch(self, initial_done: bool, *, closed: bool = False) -> None:
        opened = self.opened()
        self.agent.without_done = not initial_done
        path, record = self.detached_record(opened.row, "verification")
        with ExitStack() as processes:
            supervisors: list[subprocess.Popen[str]] = []

            def start_run() -> None:
                supervisor = processes.enter_context(subprocess.Popen(
                    [sys.executable, "-c", "import sys; sys.stdin.read()", "detached_runs.py", "supervise", str(path)],
                    stdin=subprocess.PIPE, text=True))
                record.update(status="running", pid=supervisor.pid)
                atomic_file.write_text_atomic(path, json.dumps(record))
                supervisors.append(supervisor)

            self.agent.during_prompt = start_run
            self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
            active = progress_history.active_dispatch(opened.row.id, self.run_dir)
            self.assertIsInstance(self.receipt(), worker.DispatchWaitingForRuns)
            self.assertEqual(self.finish(), turn_end.TurnOutcome.WAITING_ON_RUNS)
            self.assertEqual(progress_history.active_dispatch(opened.row.id, self.run_dir), active)
            if closed:
                _ = self.fixture.world.stop(opened.row, self.fixture.world)
            delivery = "sent: holder received the result" if closed else f"sent: worker {opened.row.name} at {opened.pane}"
            record.update(status="finished", delivery=delivery)
            atomic_file.write_text_atomic(path, json.dumps(record))
            if not closed:
                self.agent.append(opened.pane, "user", "Detached run: verification\nRun finished: private verification")
                self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
                self.agent.without_done = False
                self.agent.complete(opened.pane)
                self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
            else:
                self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
            supervisor, = supervisors
            assert supervisor.stdin is not None
            supervisor.stdin.close()
            _ = supervisor.wait(timeout=LauncherHangGuard("private supervisor exit").remaining())
        self.assertIn("DONE ", self.reply.read_text())
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])

    def test_done_with_a_live_run_waits_for_delivery_and_a_later_done_reply(self) -> None:
        self.assert_detached_dispatch(True)

    def test_a_reply_without_done_with_a_live_run_does_not_return_no_done(self) -> None:
        self.assert_detached_dispatch(False)

    def test_a_closed_worker_after_a_run_result_ends_the_launcher_with_a_live_run(self) -> None:
        self.assert_detached_dispatch(True, closed=True)

    def test_done_after_a_delivered_result_without_a_reply_returns_no_done_after_one_quiet_wait(self) -> None:
        atomic_file.write_text_atomic(self.reply, "The previous full report.\n")
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        self.agent.append(pane, "user", "Detached run: private-check\nRun finished: private verification")
        self.assertEqual(self.finish(), turn_end.TurnOutcome.WAITING_ON_REPLY)
        self.assertEqual(self.reply.read_text(), "The previous full report.\n")
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertEqual(self.agent.waited, [])
        self.assertEqual(self.reply.read_text(), "The previous full report.\n")
        self.assertIsInstance(progress_history.active_dispatch(self.agent.row(pane).id, self.run_dir), progress_history.ActiveDispatch)

    def test_a_delivered_result_can_start_work_after_a_done_state_read(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        self.agent.append(pane, "user", "Detached run: private-check\nRun finished: private verification")
        self.fixture.world.locations[pane]["agent_status"] = "working"
        self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
        self.agent.complete(pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertIn("DONE ", self.reply.read_text())

    def test_a_file_change_starts_another_wait_before_no_done(self) -> None:
        atomic_file.write_text_atomic(self.reply, "The previous full report.\n")
        previous = self.reply.read_text()
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        for run_id in ("first-check", "second-check"):
            self.agent.append(pane, "user", f"Detached run: {run_id}\nRun finished: private verification")
            self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertEqual(self.agent.waited, [])
        self.assertEqual(self.reply.read_text(), previous)
        self.assertNotIn("no DONE", "\n".join(self.agent.notices))

    def test_done_with_a_result_absent_from_the_transcript_completes_after_its_run_finishes(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        row = self.agent.row(pane)
        path, record = self.detached_record(row, "private-check")
        record["delivery"] = f"sent: worker {row.name} at {pane}"
        record["delivered_at"] = time.time()
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.agent.complete(pane)
        receipt = self.receipt()
        self.assertIsInstance(receipt, worker.DispatchComplete)
        assert isinstance(receipt, worker.DispatchComplete)
        self.assertEqual(receipt.anomalies, ("run private-check finished, but its result is not in the transcript",))
        self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.agent.notices[-1].count(receipt.anomalies[0]), 1)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertIn("DONE ", self.reply.read_text())

    def test_a_reply_without_done_with_a_finished_run_absent_from_the_transcript_returns_no_done(self) -> None:
        atomic_file.write_text_atomic(self.reply, "The previous report.\n")
        self.agent.without_done = True
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        row = self.agent.row(pane)
        path, record = self.detached_record(row, "private-check")
        record["delivery"] = f"sent: worker {row.name} at {pane}"
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
        self.assertEqual(self.reply.read_text(), "The previous report.\n")
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertEqual(self.agent.waited, [])

    def test_a_reply_after_the_state_read_completes_the_dispatch(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        dispatch_id = line.split("DONE ")[-1].split()[0].removesuffix(".")
        self.agent.append(pane, "user", "Detached run: private-check\nRun finished: private verification")
        final = "The result is verified.\nDONE " + dispatch_id
        observe = worker_panes.observe_worker
        def observe_then_reply(parent: staff.StaffId, selected: staff.StaffRow,
                               record: staff.StaffSnapshot) -> worker_panes.WorkerPane | worker_panes.WorkerNotObserved:
            observed = observe(parent, selected, record)
            self.agent.append(pane, "assistant", final)
            return observed
        row = self.agent.row(pane)
        with patch.object(worker_panes, "observe_worker", side_effect=observe_then_reply):
            event = turn_end.WorkerEvent(str(row.current_session), reply=final)
            self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.reply.read_text().strip(), final)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertEqual(self.agent.waited, [])

    def detached_record(self, row: staff.StaffRow, run_id: str) -> tuple[Path, detached_runs.RunRecord]:
        directory = self.run_dir / worker_lifecycle.DETACHED_RUNS_DIR
        directory.mkdir(exist_ok=True)
        record: detached_runs.RunRecord = {"run_id": run_id, "status": "finished", "worker": row.name,
            "staff_id": row.id, "session_dir": str(self.run_dir), "label": "private verification", "command": [],
            "log": str(directory / f"{run_id}.log"), "started_at": time.time(), "pid": 0,
            "director_dir": str(self.run_dir), "done_message_file": ""}
        return directory / f"{run_id}.json", record

    def test_stage_two_accepts_a_run_result_delivered_during_stage_one(self) -> None:
        self.unfinished_turn("review")
        pane, line = self.agent.submitted[-1]
        row = self.agent.row(pane)
        dispatch_id = line.split("DONE ")[-1].split()[0].removesuffix(".")
        path, record = self.detached_record(row, "stage-one-check")
        record["delivery"] = f"sent: worker {row.name} at {pane}"
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.agent.append(pane, "user", "Detached run: stage-one-check\nRun finished: private verification")
        second_id = dispatch_id + "-2"
        second_line = "Stage 2: " + line.replace(dispatch_id, second_id)
        run = self.run_dir
        delivery = pass_recorder.dispatch_delivery(worker_panes.worker_pass(row, run))
        progress_history.save_dispatch_delivery(run, dispatch_id,
            replace(delivery, line=second_line, completion_id=second_id))
        self.agent.append(pane, "user", second_line)
        self.agent.append(pane, "assistant", f"DONE {dispatch_id}")
        self.assertEqual(self.receipt(), worker.DispatchSubmitted(f"DONE {dispatch_id}"))
        state = worker_panes.observe_worker(self.fixture.parent.id, row, staff.snapshot())
        assert isinstance(state, worker_panes.OpenWorker)
        self.assertIsInstance(worker_panes.can_close(state), worker_panes.Kept)
        self.assertIsInstance(worker_panes.close(self.fixture.parent.id, staff.WorkerName.parse(row.name)), worker_panes.Kept)
        self.assertIn(pane, self.fixture.world.locations)
        final = "The second review is complete.\nDONE " + second_id
        self.agent.append(pane, "assistant", final)
        self.assertEqual(worker.dispatch_receipt(row, second_line, second_id, run=self.run_dir), worker.DispatchComplete(final))
        second_prompt = f"End your final reply with DONE {second_id}."
        stale_done = f"DONE {dispatch_id}"
        receipt = worker.transcript_receipt(
            [f"assistant:\n{stale_done}", f"user:\n{second_prompt}", f"assistant:\n{stale_done}"],
            worker.DispatchPrompt(dispatch_id, include_stage_two=True), worker.DoneReply(dispatch_id))
        self.assertEqual(receipt.receipt, worker.DispatchSubmitted(stale_done))

    def test_ended_pass_waits_for_the_saved_second_stage_reply_before_close(self) -> None:
        for status in ("interrupted", "canceled"):
            for stopped in (False, True):
                with self.subTest(status=status, stopped=stopped):
                    name = f"review-{status}-{int(stopped)}"
                    self.reply = self.run_dir / f"{name}.md"
                    self.assertIs(worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(name), "review",
                        self.prompt, session_dir=self.run_dir, slot=name, phase="8", kind="review",
                        launcher_command=["assign.sh"], reply=self.reply), worker.DispatchSubmission.SUBMITTED)
                    pane, line = self.agent.submitted[-1]
                    row = self.agent.row(pane)
                    passed = worker_panes.worker_pass(row, self.run_dir)
                    instance = str(passed["instance_id"])
                    second_id = worker_panes.stage_two_completion_id(instance)
                    second_line = "Stage 2: " + line.replace(instance, second_id)
                    delivery = pass_recorder.dispatch_delivery(passed)
                    progress_history.save_dispatch_delivery(self.run_dir, instance,
                        replace(delivery, line=second_line, completion_id=second_id))
                    if status == "canceled":
                        self.assertEqual(progress_history.cancel_worker_passes(self.run_dir, row.id), 1)
                    else:
                        state = self.run_dir / "progress_history_state.json"
                        fields = cast(dict[str, object], json.loads(state.read_text()))
                        cast(dict[str, dict[str, object]], fields["pass"])[name]["status"] = status
                        atomic_file.write_text_atomic(state, json.dumps(fields))
                    self.agent.append(pane, "user", second_line)
                    self.agent.append(pane, "assistant", f"DONE {instance}")
                    self.fixture.world.locations[pane]["agent_status"] = "idle"
                    parent = self.fixture.parent.id
                    worker_name = staff.WorkerName.parse(name)
                    self.assertEqual(detached_runs.live_runs(self.run_dir / worker_lifecycle.DETACHED_RUNS_DIR,
                                                             name, identity=row.id), [])
                    membership = staff.snapshot()
                    if status == "canceled" and not stopped:
                        worker_panes.close_canceled_worker(self.run_dir, row.id, instance)
                        self.assertNotIn(pane, self.fixture.world.locations)
                        self.assertEqual(staff.snapshot().by_id(row.id), row)
                        self.assertFalse(self.reply.exists())
                    if status == "interrupted":
                        self.assertIsInstance(worker_panes.close(parent, worker_name), worker_panes.Kept)
                    final = f"The second stage is complete.\nDONE {second_id}"
                    self.agent.append(pane, "assistant", final)
                    if stopped:
                        worker.save_reply(self.reply, f"DONE {instance}")
                        self.assertTrue(self.fixture.world.stop(row, self.fixture.world))
                        self.assertIsInstance(worker_panes.observe_worker(parent, row, staff.snapshot()),
                                              worker_panes.StoppedWorker)
                        if status == "interrupted":
                            with redirect_stderr(io.StringIO()):
                                self.assertIs(worker.end(parent, worker_name), worker.ExitCode.KEPT)
                            self.assertEqual(staff.snapshot().by_id(row.id), row)
                            self.assertIn(pane, self.fixture.world.locations)
                    if status == "interrupted":
                        self.assertIsInstance(worker_panes.close(parent, worker_name), worker_panes.Kept)
                        self.assertEqual(self.finish(name), turn_end.TurnOutcome.LATE_DONE)
                    else:
                        self.assertIsInstance(progress_history.hook_completion(
                            worker_panes.worker_pass(row, self.run_dir)), progress_history.NoHookCompletion)
                        with (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
                              patch("scripts.production.staff_sources.require_worker_stopped"),
                              redirect_stderr(io.StringIO())):
                            with patch.object(worker, "save_reply", side_effect=OSError("reply storage failed")):
                                self.assertIs(worker.end(parent, worker_name), worker.ExitCode.KEPT)
                            self.assertEqual(staff.snapshot().by_id(row.id), row)
                            self.agent.notice_failures = 1
                            self.assertIs(worker.end(parent, worker_name), worker.ExitCode.KEPT)
                            self.assertEqual(staff.snapshot().by_id(row.id), row)
                            self.assertIs(worker.end(parent, worker_name), worker.ExitCode.OK)
                        self.assertIsNone(staff.by_id(row.id))
                    saved = progress_history.hook_completion(worker_panes.worker_pass(row, self.run_dir, record=membership))
                    assert isinstance(saved, progress_history.HookCompletion)
                    self.assertEqual(saved.outcome, turn_end.TurnOutcome.LATE_DONE)
                    self.assertTrue(saved.finished)
                    self.assertEqual(Path(saved.reply_file).read_text(), final + "\n")
                    self.assertNotIn(pane, self.fixture.world.locations)
                    if status == "interrupted":
                        with self.subTest(sequence="completed hook, repeated end"):
                            notices, closes = list(self.agent.notices), list(self.fixture.world.closed)
                            with (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
                                  patch("scripts.production.staff_sources.require_worker_stopped")):
                                self.assertIs(worker.end(parent, worker_name), worker.ExitCode.OK)
                                self.assertIs(worker.end(parent, worker_name), worker.ExitCode.OK)
                            self.assertEqual(self.agent.notices, notices)
                            self.assertEqual(self.fixture.world.closed, closes)

    def test_only_a_live_run_or_a_result_sent_to_this_worker_can_hold_its_receipt(self) -> None:
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        pane, line = self.agent.submitted[-1]
        row = self.agent.row(pane)
        dispatch_id = line.split("DONE ")[-1].split()[0].removesuffix(".")
        path, record = self.detached_record(row, "private-check")
        for status, delivery, pending in (
            ("finished", "nothing delivered: exit 1 with a done message", False),
            ("finished", "finished: worker retired", False),
            ("finished", "nothing delivered: the supervisor is gone and a newer run answers", False),
            ("finished", "sent to director private-director", False),
            ("finished", "sent: holder received the result", False),
            ("running", "", False),
            ("finished", f"sent: worker {row.name} at {pane}", True),
            ("finished", "already delivered: private-check", True),
        ):
            with self.subTest(status=status, delivery=delivery):
                record["status"] = cast(Literal["running", "finished"], status)
                record["delivery"] = delivery
                record["delivered_at"] = time.time()
                atomic_file.write_text_atomic(path, json.dumps(record))
                self.agent.complete(pane)
                receipt = worker.dispatch_receipt(row, line, dispatch_id, run=self.run_dir)
                if status == "running":
                    assert isinstance(receipt, worker.DispatchWaitingForRuns)
                    self.assertEqual(receipt.reap_required, frozenset({"private-check"}))
                    continue
                self.assertIsInstance(receipt, worker.DispatchComplete)
                assert isinstance(receipt, worker.DispatchComplete)
                self.assertEqual(bool(receipt.anomalies), pending)

    def test_failed_delivery_preserves_its_receipt_and_never_retypes(self) -> None:
        self.agent.error = "agent_prompt_stalled"
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        pane = self.agent.submitted[-1][0]
        self.assertIn("agent_prompt_stalled", errors.getvalue())
        self.assertTrue(all(f"prompt delivered to pane {pane}" in line for line in errors.getvalue().splitlines()))
        self.assertEqual(len(self.agent.submitted), 1)
        self.agent.error = ""
        self.agent.finish_after = 1
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)

        for exited in (False, True):
            with self.subTest(exited=exited):
                with ExitStack() as cleanup:
                    case = set_up_fixture(cleanup.callback, WorkerCommandTests("runTest"))
                    config = case.fixture.root / "conf/direct.conf"
                    atomic_file.write_text_atomic(config, config.read_text().replace("WORKER_SETTLE_SECONDS=30", "WORKER_SETTLE_SECONDS=1"))
                    opened = case.opened()
                    def queued_without_delivery(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                        if arguments[0] == MISSING_HERDR_BIN and arguments[4:6] == ["prompt", opened.pane]:
                            if exited:
                                pid, _, _ = case.fixture.world.foreground.pop(opened.pane)
                                del case.fixture.world.processes[pid]
                                case.fixture.world.locations[opened.pane]["agent_status"] = "exited"
                            return subprocess.CompletedProcess(arguments, 0, "{}", "")
                        return case.agent.run(arguments, **options)
                    with (patch.object(subprocess, "run", side_effect=queued_without_delivery),
                          redirect_stderr(io.StringIO()) as errors):
                        code = worker.assign(case.fixture.parent.id, "impl", case.prompt, session_dir=case.run_dir,
                            slot="impl", phase="8", kind="impl", name="impl", launcher_command=["assign.sh"], reply=case.reply)
                    case.assertEqual(code, worker.ExitCode.UNCERTAIN)
                    case.assertEqual(case.agent.submitted, [])
                    case.assertIsInstance(case.receipt(), worker.DispatchNotSubmitted)
                    case.assertEqual(errors.getvalue(),
                        "anomaly: impl: dispatch prompt is not in the transcript; submission is unproved\n")
                    if exited:
                        case.assertIsInstance(worker_panes.observe_worker(case.fixture.parent.id, opened.row, staff.snapshot()),
                                              worker_panes.StoppedWorker)
                        case.fixture.world.locations[opened.pane]["agent_status"] = "idle"
                    case.assertIs(worker.assign(case.fixture.parent.id, "impl", case.prompt, session_dir=case.run_dir,
                        slot="impl", phase="8", kind="impl", name="impl", launcher_command=["assign.sh"], reply=case.reply),
                        worker.DispatchSubmission.SUBMITTED)
                    case.assertEqual(len(case.agent.submitted), 1)
                    case.assertEqual(case.agent.row(case.agent.submitted[-1][0]).current_session, opened.row.current_session)

        with self.subTest(delivery="delayed transcript"), ExitStack() as cleanup:
            case = set_up_fixture(cleanup.callback, WorkerCommandTests("runTest"))
            opened = case.opened()
            case.agent.defer_prompt_transcript = True
            config = case.fixture.root / "conf/direct.conf"
            atomic_file.write_text_atomic(config, config.read_text().replace("WORKER_SETTLE_SECONDS=30", "WORKER_SETTLE_SECONDS=1"))
            def record_prompt() -> None:
                pane, line = case.agent.submitted[-1]
                case.agent.append(pane, "user", line)
            delivery = Timer(0.1, record_prompt)
            case.agent.during_prompt = delivery.start
            try:
                with redirect_stderr(io.StringIO()) as errors:
                    case.assertIs(case.dispatch(), worker.DispatchSubmission.SUBMITTED)
                case.assertEqual(errors.getvalue(), "")
                case.assertEqual(len(case.agent.submitted), 1)
                case.assertEqual(case.agent.submitted[0][0], opened.pane)
                case.assertIsInstance(case.receipt(), worker.DispatchSubmitted)
            finally:
                delivery.join()

    def test_blocked_dispatch_returns_blocked(self) -> None:
        opened = self.opened()
        question = "Which output format must I use?\nJSON or Markdown?\n"
        cases = (("before", question), ("after", question),
                 ("unreadable", herdr_types.HerdrFailure("pane read failed\nterminal unavailable")))
        for stage, visible in cases:
            with self.subTest(stage=stage):
                self.agent.questions[opened.pane] = visible
                self.agent.error = "" if stage == "before" else "agent_blocked"
                self.fixture.world.locations[opened.pane]["agent_status"] = "idle" if stage == "after" else "blocked"
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.dispatch(), worker.ExitCode.BLOCKED)
                expected = visible if isinstance(visible, str) else "anomaly: impl: pane read failed terminal unavailable\n"
                self.assertEqual(errors.getvalue(), f"question: impl pane {opened.pane}\n" + expected)
                self.assertEqual(len(self.agent.submitted), 0 if stage == "before" else 1)
                self.assertIn(opened.pane, self.fixture.world.locations)
                self.assertEqual(staff.snapshot().by_id(opened.row.id), replace(opened.row, role=staff.KnownWorkerRole("impl")))
        self.assertIn(("pane", "read", opened.pane, "--source", "recent", "--lines", "40"), self.fixture.world.commands)

    def test_answer_continues_a_blocked_dispatch_and_refuses_another_parents_worker(self) -> None:
        opened = self.opened()
        self.agent.questions[opened.pane] = "Which output format must I use?"
        self.agent.error = "agent_blocked"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.dispatch(), worker.ExitCode.BLOCKED)
        active = progress_history.active_dispatch(opened.row.id, self.run_dir)
        self.assertIsInstance(active, progress_history.ActiveDispatch)
        answer_text = "Use JSON."
        for index, reported in enumerate((True, False), start=1):
            with self.subTest(reported=reported):
                if index == 2:
                    self.agent.append(opened.pane, "assistant", "Which output format must I use?")
                self.agent.questions[opened.pane] = "Which output format must I use?"
                self.fixture.world.locations[opened.pane]["agent_status"] = "blocked"
                if not reported:
                    _ = self.fixture.world.locations[opened.pane].pop("agent_session", None)
                    pid = self.fixture.world.foreground[opened.pane][0]
                    process = self.fixture.world.processes[pid]
                    self.fixture.world.processes[pid] = replace(process, identity=herdr.UnreportedResume(opened.row.current_session))
                self.assertEqual(self.command("answer", "impl", "--text", answer_text), (0, "done\n", ""))
                self.assertEqual(len(self.agent.answers), index)
                self.assertEqual(self.agent.answers[-1][0], opened.pane)
                self.assertIn(answer_text, self.agent.answers[-1][1])
                self.assertEqual(self.fixture.world.locations[opened.pane]["agent_status"], "working")
        answer_commands = [command for command in self.fixture.world.commands
                           if command[:2] in (("pane", "send-keys"), ("pane", "send-text"))]
        self.assertEqual(answer_commands, [("pane", "send-keys", opened.pane, "shift+left"),
            ("pane", "send-text", opened.pane, answer_text), ("pane", "send-keys", opened.pane, "enter"),
            ("pane", "send-keys", opened.pane, "enter")] * 2)
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.agent.complete(opened.pane)
        self.assertEqual(progress_history.active_dispatch(opened.row.id, self.run_dir), active)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())
        assert isinstance(active, progress_history.ActiveDispatch)
        self.assertIn("DONE " + active.instance_id, self.reply.read_text())
        self.assertIsInstance(progress_history.active_dispatch(opened.row.id, self.run_dir), progress_history.NoActiveDispatch)
        other = fixture_member("unit", name="other-unit", parent=self.fixture.parent.parent)
        foreign = fixture_member("worker", name="foreign", parent=other.id)
        self.fixture.world.add(foreign, "w2:p7", workspace="w2", tab="w2:t2")
        self.agent.questions["w2:p7"] = "Which output format must I use?"
        self.assertEqual(self.command("answer", foreign.name, "--text", "Use Markdown."),
                         (worker.ExitCode.REFUSED, "", "foreign: no worker staff row\n"))
        self.assertEqual(len(self.agent.answers), 2)

    def test_answer_refuses_without_an_accepted_pass(self) -> None:
        opened = self.opened()
        name = opened.row.name
        self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
        self.agent.questions[opened.pane] = "Which format?"
        with self.subTest(pass_state="no active pass"):
            with (patch.object(self.fixture.world, "prompt", wraps=self.fixture.world.prompt) as prompt,
                  patch.object(self.fixture.world, "answer_question", wraps=self.fixture.world.answer_question) as answer):
                code, output, error = self.command("answer", name, "--text", "Use JSON.")
                self.assertEqual((code, output), (worker.ExitCode.REFUSED, ""))
                self.assertIn("no active pass", error)
                self.assertIn("assign.sh", error)
                self.assertEqual(len(error.splitlines()), 1)
            prompt.assert_not_called()
            answer.assert_not_called()
            self.assertEqual(self.agent.answers, [])

        instance = progress_history.begin_dispatch(self.run_dir, slot=name, phase="8", role="impl",
            worker_name=name, worker_id=opened.row.id, launcher_command=["assign.sh"], prompt=self.prompt, kind="impl", lens="")
        for delivery in (None,
                         pass_recorder.DispatchDelivery("", "", ""),
                         pass_recorder.DispatchDelivery("fixture prompt", "", str(self.reply)),
                         pass_recorder.DispatchDelivery("fixture prompt", instance, str(self.reply))):
            if delivery is not None:
                progress_history.save_dispatch_delivery(self.run_dir, instance, delivery)
            for state in ("idle", "blocked"):
                with (self.subTest(delivery=delivery, state=state),
                      patch.object(self.fixture.world, "prompt", wraps=self.fixture.world.prompt) as prompt,
                      patch.object(self.fixture.world, "answer_question", wraps=self.fixture.world.answer_question) as answer):
                    self.fixture.world.locations[opened.pane]["agent_status"] = state
                    saved = (self.run_dir / "progress_history_state.json").read_bytes()
                    code, output, error = self.command("answer", name, "--text", "Use JSON.")
                    self.assertEqual((code, output), (worker.ExitCode.REFUSED, ""))
                    self.assertIn("assign.sh", error)
                    self.assertEqual(len(error.splitlines()), 1)
                    self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), saved)
                    prompt.assert_not_called()
                    answer.assert_not_called()
                    self.assertEqual(self.agent.answers, [])

    def test_answer_refuses_after_done_before_the_hook_updates_the_pass(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.agent.submitted[-1][0]
        row = self.agent.row(pane)
        self.agent.append(pane, "assistant", "A later assistant message.")
        self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.ActiveDispatch)
        self.fixture.world.locations[pane]["agent_status"] = "idle"
        with (patch.object(self.fixture.world, "prompt", wraps=self.fixture.world.prompt) as prompt,
              patch.object(self.fixture.world, "answer_question") as answer):
            code, output, error = self.command("answer", row.name, "--text", "Use JSON.")
        self.assertEqual((code, output), (worker.ExitCode.REFUSED, ""))
        self.assertIn("already has a DONE reply", error)
        prompt.assert_not_called()
        answer.assert_not_called()

        with self.subTest(receipt="early DONE after holder fallback"):
            instance = str(worker_panes.worker_pass(row, self.run_dir)["instance_id"])
            path, record = self.detached_record(row, "stopped-result")
            record.update(pane_dispatch_id=instance, stop_pass_requested=True, completion_pending=True,
                          finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                          delivery="sent: holder received the result", delivered_at=time.time())
            with detached_runs.record_lock(path):
                atomic_file.write_text_atomic(path, json.dumps(record))
            self.assertIsInstance(self.receipt(), worker.DispatchWaitingForRuns)
            self.agent.finish_after = 1
            run = self.fixture.world.run
            uncertain = False

            def accept_followup(*arguments: str, deadline: float = math.inf) -> str:
                result = run(*arguments, deadline=deadline)
                if arguments[:3] == ("agent", "prompt", pane):
                    self.agent.submitted.append((pane, arguments[3]))
                    if uncertain:
                        raise herdr_types.HerdrFailure("the prompt may have reached the worker")
                    self.agent.append(pane, "user", arguments[3])
                return result

            text = "Read the result of the stopped run and finish the pass."
            with patch.object(self.fixture.world, "run", side_effect=accept_followup):
                with self.subTest(sequence="holder fallback, answer, repeated answer before DONE"):
                    before = len(self.agent.submitted)
                    with patch.object(self.fixture.world, "prompt", side_effect=herdr.HerdrCommandNotStarted("not started")):
                        self.assertEqual(self.command("answer", row.name, "--text", text)[0], worker.ExitCode.UNCERTAIN)
                    self.assertEqual(self.command("answer", row.name, "--text", text), (0, "done\n", ""))
                    for state, code in (("working", worker.ExitCode.BUSY), ("unknown", worker.ExitCode.UNCERTAIN),
                                        ("idle", worker.ExitCode.BUSY)):
                        self.fixture.world.locations[pane]["agent_status"] = state
                        self.assertEqual(self.command("answer", row.name, "--text", text)[0], code)
                    self.assertEqual(len(self.agent.submitted), before + 1)
                with self.subTest(sequence="accepted answer, new worker question"):
                    self.agent.append(pane, "assistant", "Which result file must I read?")
                    self.assertEqual(self.command("answer", row.name, "--text", text), (0, "done\n", ""))
                    self.assertEqual(self.command("answer", row.name, "--text", text)[0], worker.ExitCode.BUSY)
                    self.assertEqual(len(self.agent.submitted), before + 2)
                with self.subTest(sequence="uncertain submission, retry"):
                    self.agent.append(pane, "assistant", "Can you confirm the result file?")
                    uncertain = True
                    self.assertEqual(self.command("answer", row.name, "--text", text)[0], worker.ExitCode.UNCERTAIN)
                    self.assertEqual(self.command("answer", row.name, "--text", text)[0], worker.ExitCode.BUSY)
                    self.assertEqual(len(self.agent.submitted), before + 3)
                    self.agent.append(pane, "user", self.agent.submitted[-1][1])
            target, submitted = self.agent.submitted[-1]
            self.assertEqual(target, pane)
            self.assertIn(text, submitted)
            self.assertIn(f"DONE {instance}", submitted)
            self.agent.final = "I reviewed the stopped result."
            self.agent.complete(pane)
            self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
            self.assertEqual(self.reply.read_text().strip(), self.agent.replies[-1])
            self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)

    def test_answer_refuses_during_detached_compaction(self) -> None:
        opened = self.opened()
        backend = staff.backend(opened.row)
        assert isinstance(backend, staff.CodexBackend)
        with worker_admission.transcript_source(opened.row, backend).open("a") as stream:
            _ = stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 43001, "cached_input_tokens": 0, "output_tokens": 0}}}}) + "\n")
        commands: list[list[str]] = []

        def queue(command: list[str], _lock: file_lock.ExclusiveLock, _log: Path) -> None:
            commands.append(command)

        with (patch.object(worker, "_spawn_compaction", side_effect=queue), redirect_stdout(io.StringIO())):
            self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        command, = commands
        wait = self.fixture.world.wait_turn_end
        self.agent.compact_proof = "compacted"
        refusal: list[tuple[int, str, str]] = []

        def answer_during_compaction(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
            self.agent.questions[pane] = "Which format?"
            refusal.append(self.command("answer", opened.row.name, "--text", "Use JSON."))
            return wait(pane, timeout_ms)

        with (patch.object(self.fixture.world, "wait_turn_end", side_effect=answer_during_compaction),
              patch.object(self.fixture.world, "prompt", wraps=self.fixture.world.prompt) as prompt,
              redirect_stderr(io.StringIO())):
            self.assertEqual(worker.main(command[2:]), worker.ExitCode.OK)
        prompt.assert_not_called()
        (code, output, error), = refusal
        self.assertEqual((code, output), (worker.ExitCode.BUSY, ""))
        self.assertIn("assign.sh", error)
        self.assertEqual(len(error.splitlines()), 1)
        self.assertEqual(self.agent.answers, [])
        self.assertEqual(len(self.agent.submitted), 1)

    def test_answer_uses_prompt_for_an_idle_worker(self) -> None:
        opened = self.opened()
        with self.subTest(answer="resubmit after compaction"):
            self.agent.finish_after = 1
            self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
            self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
            backend = staff.backend(opened.row)
            assert isinstance(backend, staff.CodexBackend)
            with worker_admission.transcript_source(opened.row, backend).open("a") as stream:
                _ = stream.write(json.dumps({"type": "compacted"}) + "\n")
            self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
            active = progress_history.active_dispatch(opened.row.id, self.run_dir)
            assert isinstance(active, progress_history.ActiveDispatch)
            status = self.run_dir / "resubmit.status"
            passed = worker_panes.worker_pass(opened.row, self.run_dir)
            delivery = pass_recorder.dispatch_delivery(passed)
            progress_history.save_dispatch_delivery(self.run_dir, active.instance_id,
                replace(delivery, actions=replace(delivery.actions, status_file=str(status))))
            atomic_file.write_text_atomic(status, "running\n")
            saved = (self.run_dir / "progress_history_state.json").read_bytes()
            run = self.fixture.world.run

            def accept_resubmit(*arguments: str, deadline: float = math.inf) -> str:
                result = run(*arguments, deadline=deadline)
                if arguments[:3] == ("agent", "prompt", opened.pane):
                    self.agent.submitted.append((opened.pane, arguments[3]))
                    self.agent.append(opened.pane, "user", arguments[3])
                return result

            text = "The assignment did not run after compaction. Read the assigned prompt and do the work."
            with patch.object(self.fixture.world, "run", side_effect=accept_resubmit):
                self.assertEqual(self.command("answer", "impl", "--text", text), (0, "done\n", ""))
            self.assertIn(text, self.agent.submitted[-1][1])
            self.assertIn(f"DONE {delivery.completion_id}", self.agent.submitted[-1][1])
            self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), saved)
            notices = len(self.agent.notices)
            self.agent.complete(opened.pane)
            self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
            self.assertEqual(len(self.agent.notices), notices + 1)
            self.assertIn(f"; pass {active.instance_id}", self.agent.notices[-1])
            self.assertIn(f"DONE {delivery.completion_id}", self.reply.read_text())
            self.assertEqual(status.read_text(), "done\n")

    def test_answer_keeps_prompt_for_a_blocked_claude_worker(self) -> None:
        _ = write_registry(self.fixture.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        opened = self.opened()
        self.agent.finish_after = 1
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.agent.questions[opened.pane] = "Which format?"
        self.fixture.world.locations[opened.pane]["agent_status"] = "blocked"
        with patch.object(self.fixture.world, "answer_question", wraps=self.fixture.world.answer_question) as answer:
            self.assertEqual(self.command("answer", "impl", "--text", "Use JSON."), (0, "done\n", ""))
        answer.assert_not_called()
        self.assertEqual(len(self.agent.answers), 1)
        self.assertIn("Use JSON.", self.agent.answers[0][1])
        self.assertIn("DONE ", self.agent.answers[0][1])

    def test_claude_without_a_transcript_receives_its_first_dispatch_prompt(self) -> None:
        _ = write_registry(self.fixture.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        opened = self.opened()
        backend = staff.backend(opened.row)
        self.assertIsInstance(backend, staff.ClaudeBackend)
        assert isinstance(backend, staff.ClaudeBackend)
        source = worker_admission.transcript_source(opened.row, backend)
        pid = self.fixture.world.foreground[opened.pane][0]
        with fake_unit_host.private_process_registry():
            with patch.dict(os.environ, {"FAKE_CLAUDE_PID": str(pid)}):
                record_fixture_claude_start(["--resume", opened.row.current_session])
            source.unlink()
            self.assertIsInstance(worker.dispatch_receipt(opened.row, "first prompt", "first-dispatch", run=self.run_dir),
                                  worker.DispatchNotSubmitted)
            self.assertFalse(source.exists())
            self.assertEqual(self.dispatch(), worker.ExitCode.OK)
            self.assertEqual(len(self.agent.submitted), 1)
            self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
            self.assertIn(self.agent.final, self.reply.read_text())
            self.assertTrue(source.exists())

    def test_first_codex_dispatch_keeps_its_identity_during_the_warmup_hook(self) -> None:
        world = self.fixture.world
        wait = world.wait_turn_end
        result = world.result
        native_process = world.native_process
        hook_pid = 998088
        hook_arguments = ["python3", str(Path(turn_end.__file__).resolve())]
        hook_parent = 0
        hook_input = self.fixture.root / "warmup-hook-stdin"
        os.mkfifo(hook_input)
        stdin = world.process_root / str(hook_pid) / "fd" / "0"
        stdin.parent.mkdir(parents=True)
        stdin.symlink_to(hook_input)

        def warmup_ended(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
            nonlocal hook_parent
            ended = wait(pane, timeout_ms)
            hook_parent = world.foreground[pane][0]
            fake_unit_host.process(os.environ, hook_pid, hook_parent, hook_arguments, {})
            return ended

        def fresh_codex(pid: int, kind: str, reported: str = "") -> herdr.NativeAgentProcess:
            if hook_parent and kind == "codex" and not reported:
                raise ValueError("Codex launch identity is unavailable")
            return native_process(pid, kind, reported)

        def with_hook(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            value = result(*arguments, deadline=deadline)
            if hook_parent and arguments[:2] == ("pane", "process-info") and arguments[-1] != "w1:p0":
                info = cast(dict[str, object], value["process_info"])
                foreground = cast(list[object], info["foreground_processes"])
                info["foreground_processes"] = [{"pid": hook_pid, "argv": hook_arguments}, *foreground]
            return value

        with (patch.object(world, "wait_turn_end", side_effect=warmup_ended),
              patch.object(world, "result", side_effect=with_hook),
              patch.object(herdr, "native_process", side_effect=fresh_codex)):
            self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertGreater(hook_parent, 0)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(len(world.launches), 1)
        self.assertEqual(len([entry for entry in world.commands if entry[:2] == ("agent", "wait")]), 1)

    def test_claude_delivery_returns_before_its_prompt_enters_the_transcript(self) -> None:
        _ = write_registry(self.fixture.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        self.agent.defer_prompt_transcript = True
        identities: list[staff.StaffId] = []
        threads: list[str] = []
        compaction_plan = worker._compaction_plan  # pyright: ignore[reportPrivateUsage]
        for turn, retry in enumerate(("dispatch", "finish-dispatch")):
            with self.subTest(turn=turn, retry=retry):
                self.prompt = self.run_dir / f"delayed-transcript-{turn}.md"
                atomic_file.write_text_atomic(self.prompt, "Complete the assigned work.\n")
                with patch.object(worker, "dispatch_receipt", wraps=worker.dispatch_receipt) as receipts:
                    self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
                self.assertEqual(receipts.call_count, 1)
                pane, line = self.agent.submitted[-1]
                row = self.agent.row(pane)
                identities.append(row.id)
                threads.append(str(row.current_session))
                backend = staff.backend(row)
                assert isinstance(backend, staff.ClaudeBackend)
                source = worker_admission.transcript_source(row, backend)
                self.assertNotIn(line, source.read_text() if source.exists() else "")
                self.assertIsInstance(worker.dispatch_receipt(row, line, "pending", run=self.run_dir),
                                      worker.DispatchNotSubmitted)
                self.assertFalse(self.reply.exists())
                self.assertEqual(self.agent.waited, [])
                passed = worker_panes.worker_pass(row, self.run_dir)
                instance = str(passed["instance_id"])
                delivery = progress_history.dispatch_delivery(passed)
                self.assertEqual(passed["status"], "active")
                self.assertEqual(passed["confirmed_delivery_ids"], [delivery.completion_id])
                self.assertIsInstance(worker.dispatch_receipt(row, line, delivery.completion_id, run=self.run_dir),
                                      worker.DispatchNotSubmitted)
                self.fixture.world.locations[pane]["agent_status"] = "idle"
                submitted = list(self.agent.submitted)
                with patch.object(worker, "_compaction_plan", wraps=compaction_plan) as compact:
                    if retry == "dispatch":
                        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
                    else:
                        command = worker.finish_dispatch_command(self.fixture.parent.id, row.id, self.run_dir,
                                                                 instance, delivery.completion_id)
                        self.assertEqual(worker.main(command[2:]), worker.ExitCode.OK)
                compact.assert_not_called()
                self.assertEqual(self.agent.submitted, submitted)
                self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["confirmed_delivery_ids"],
                                 [delivery.completion_id])
                self.agent.append(pane, "user", line)
                self.agent.complete(pane)
                self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
                self.assertIn(self.agent.final, self.reply.read_text())
                self.reply.unlink()
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(threads[0], threads[1])
        self.assertEqual(len(self.fixture.world.launches), 2)
        self.assertEqual(self.fixture.world.requests[-1][1][:2], ["--resume", threads[0]])

        with self.subTest(retry="stage ready"):
            self.agent.defer_prompt_transcript = False
            row = self.review_with_stage_one_reply("delayed-review", confirmed=True)
            passed = worker_panes.worker_pass(row, self.run_dir)
            instance = str(passed["instance_id"])
            second = worker_panes.stage_two_completion_id(instance)
            self.assertEqual(passed["confirmed_delivery_ids"], [instance])
            self.assertFalse(progress_history.dispatch_submission_confirmed(self.run_dir, instance, second))
            self.agent.defer_prompt_transcript = True
            before = len(self.agent.submitted)
            self.assertIs(self.finish(row.name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
            self.assertEqual(len(self.agent.submitted), before + 1)
            pane, line = self.agent.submitted[-1]
            passed = worker_panes.worker_pass(row, self.run_dir)
            self.assertEqual(passed["status"], "active")
            self.assertEqual(passed["confirmed_delivery_ids"], [instance, second])
            self.assertEqual(progress_history.dispatch_delivery(passed).completion_id, second)
            self.assertIsInstance(worker.dispatch_receipt(row, line, second, run=self.run_dir),
                                  worker.DispatchNotSubmitted)
            self.fixture.world.locations[pane]["agent_status"] = "idle"
            with patch.object(worker, "_compaction_plan", wraps=compaction_plan) as compact:
                self.assertIs(self.finish(row.name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
            compact.assert_not_called()
            self.assertEqual(len(self.agent.submitted), before + 1)
            self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["confirmed_delivery_ids"], [instance, second])
            self.agent.append(pane, "user", line)
            self.agent.complete(pane)
            self.assertIs(self.finish(row.name), turn_end.TurnOutcome.DONE)

    def test_codex_without_a_transcript_keeps_its_receipt_unknown(self) -> None:
        opened = self.opened()
        backend = staff.backend(opened.row)
        assert isinstance(backend, staff.CodexBackend)
        for source in (backend.codex_home / "sessions").rglob(f"*{opened.row.current_session}.jsonl"):
            source.unlink()
        self.assertIsInstance(worker.dispatch_receipt(opened.row, "first prompt", "first-dispatch", run=self.run_dir),
                              worker.DispatchEvidenceUnknown)
        self.assertEqual(self.agent.submitted, [])

    def test_working_worker_that_reaches_done_receives_one_prompt(self) -> None:
        for state in (herdr.AgentStatus.DONE, herdr.AgentStatus.BLOCKED):
            with self.subTest(state=state):
                name = "impl-" + state.value
                opened = self.opened(name)
                self.fixture.world.commands.clear()
                self.fixture.world.locations[opened.pane]["agent_status"] = "working"
                self.fixture.world.turn_end_status = state
                question = "Which output format must I use? JSON or Markdown?"
                if state == herdr.AgentStatus.BLOCKED:
                    self.agent.questions[opened.pane] = question
                before = len(self.agent.submitted)
                with redirect_stderr(io.StringIO()) as errors:
                    result = self.dispatch(name)
                waits = [command for command in self.fixture.world.commands if command[:2] == ("agent", "wait")]
                self.assertEqual(len(waits), 1)
                self.assertEqual(waits[0][waits[0].index("--timeout") + 1], "30000")
                if state == herdr.AgentStatus.BLOCKED:
                    self.assertEqual(result, worker.ExitCode.BLOCKED)
                    self.assertEqual(len(self.agent.submitted), before)
                    self.assertIn(f"question: {name} pane {opened.pane}", errors.getvalue())
                    self.assertIn(question, errors.getvalue())
                    self.assertNotIn("prompt delivered to pane", errors.getvalue())
                else:
                    self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                    self.assertEqual(len(self.agent.submitted), before + 1)
                    self.assertEqual(self.agent.submitted[-1][0], opened.pane)

    def test_working_worker_that_reaches_idle_receives_one_prompt(self) -> None:
        opened = self.opened()
        self.fixture.world.commands.clear()
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        self.fixture.world.turn_end_status = herdr.AgentStatus.IDLE
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        waits = [command for command in self.fixture.world.commands if command[:2] == ("agent", "wait")]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0][waits[0].index("--timeout") + 1], "30000")
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.submitted[0][0], opened.pane)

    def test_a_prompt_delivered_during_settle_keeps_its_follower(self) -> None:
        opened = self.opened()
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "after prompt"):
            _ = self.dispatch()
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(self.agent.waited, [])
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIsInstance(self.receipt(), worker.DispatchSubmitted)

        with self.subTest(cause="prompt enters the transcript during the wait while the worker stays working"):
            name = "accepted-during-settle"
            opened = self.opened(name)
            self.agent.defer_prompt_transcript = True
            self.agent.interrupt = "prompt"
            with self.assertRaisesRegex(OSError, "after prompt"):
                _ = self.dispatch(name)
            submitted = list(self.agent.submitted)
            pane, line = submitted[-1]
            self.fixture.world.locations[pane]["agent_status"] = "working"
            self.fixture.world.first_turn_timeout = True
            wait = self.fixture.world.wait_turn_end

            def accept_during_wait(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
                self.agent.append(pane, "user", line)
                return wait(pane, timeout_ms)

            with patch.object(self.fixture.world, "wait_turn_end", side_effect=accept_during_wait):
                self.assertIs(self.dispatch(name), worker.DispatchSubmission.SUBMITTED)
            self.assertEqual(self.fixture.world.locations[opened.pane]["agent_status"], "working")
            self.assertEqual(self.agent.submitted, submitted)

    def test_done_worker_can_take_a_fix_after_pass_recovery(self) -> None:
        for effort, hook_ran in (("high", True), ("low", True), ("high", False), ("low", False)):
            with self.subTest(fix_effort=effort, hook_ran=hook_ran):
                _ = write_registry(self.fixture.root / "conf/models.conf", {
                    "direct.codex": {"fix": f"gpt-test:{effort}"},
                }, update=True)
                submitted = len(self.agent.submitted)
                self.assertEqual(self.dispatch(), worker.ExitCode.OK)
                row = self.agent.row(self.agent.submitted[-1][0])
                self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
                if hook_ran:
                    self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
                launches = len(self.fixture.world.launches)
                self.assertEqual(worker.assign(self.fixture.parent.id, "fix", self.prompt, session_dir=self.run_dir,
                    slot="impl", phase="8", kind="fix", name="impl", launcher_command=["assign.sh"], reply=self.reply,
                    fix_cause="Repair the reported dispatch failure"), worker.ExitCode.OK)
                self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["kind"], "fix")
                self.assertEqual(len(self.agent.submitted), submitted + 2)
                if not hook_ran:
                    self.assertEqual(len(self.fixture.world.launches), launches + (effort == "low"))
                self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)

    def test_restore_keeps_a_working_worker_with_a_submitted_dispatch(self) -> None:
        opened = self.opened()
        dispatch = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.prompt, kind="impl", lens="")
        self.agent.append(opened.pane, "user", f"End your final reply with DONE {dispatch}.")
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        self.assertEqual(self.command("restore"), (0, "unit: workers unchanged\n", ""))
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches)), before)
        self.assertEqual(self.fixture.world.stops, [])
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(self.fixture.world.locations[opened.pane]["agent_status"], "working")

        with self.subTest(change="director session changes during resume"):
            self.check_restore_notice_after_director_session_change()

    def check_restore_notice_after_director_session_change(self) -> None:
        opened = self.opened("notice-worker")
        row = opened.row
        parent = self.fixture.parent
        moved = staff.StaffId(fixture_runtime_id("restore-new-director"))
        fixture_session(moved, staff.ClaudeBackend())
        dispatch = progress_history.begin_dispatch(self.run_dir, slot=row.name, phase="8", role="impl",
            worker_name=row.name, worker_id=row.id, launcher_command=["assign.sh"],
            prompt=self.prompt, kind="impl", lens="")
        line = f"Check the file. End your final reply with DONE {dispatch}."
        progress_history.save_dispatch_delivery(self.run_dir, dispatch,
            pass_recorder.DispatchDelivery(line, dispatch, str(self.reply)))
        self.agent.append(opened.pane, "user", line)

        def resume_and_move(_row: staff.StaffRow, _plan: worker.WorkerResume) -> worker.ExitCode:
            _ = staff.update_current_session(parent.current_session, moved)
            return worker.ExitCode.OK

        def send_notice(member: staff.StaffRow, director: staff.StaffRow, run: Path,
                        _hold: worker_panes.WorkerLockHold, *, record: staff.StaffSnapshot,
                        pane: worker.KeepCompletedPane) -> worker.CompletionRecovery:
            self.assertIs(pane, worker.KeepCompletedPane.KEEP)
            passed = worker_panes.worker_pass(member, run, record=record)
            turn_end.retry_completed_notice(member.name, director, run, passed)()
            return worker.CompletionRecovery.NO_PENDING_NOTICE

        atomic_file.write_text_atomic(self.reply, f"DONE {dispatch}\n")
        with (patch.object(worker, "_resume_launcher", side_effect=resume_and_move) as resume,
              patch.object(turn_end, "recover_completion", side_effect=send_notice),
              patch.object(send, "send", return_value=send.Result("sent", "notice received")) as notice):
            self.assertIs(worker.restore(parent.id, only=frozenset({row.id})), worker.ExitCode.OK)
        resume.assert_called_once()
        notice.assert_called_once()
        self.assertEqual(cast(send.Options, notice.call_args.args[0]).to, f"session:{moved}")

    def test_busy_worker_receives_no_prompt(self) -> None:
        opened = self.opened()
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        self.fixture.world.first_turn_timeout = True
        self.assertEqual(self.dispatch(), worker.ExitCode.BUSY)
        self.assertEqual(self.agent.submitted, [])

        with self.subTest(cause="busy assignment preserves the submitted pass"):
            self.fixture.world.first_turn_timeout = False
            self.agent.finish_after = 100
            self.assertIs(self.dispatch("submitted-worker"), worker.DispatchSubmission.SUBMITTED)
            pane = self.agent.submitted[-1][0]
            row = self.agent.row(pane)
            before = (self.run_dir / "progress_history_state.json").read_bytes()
            submitted = list(self.agent.submitted)
            self.prompt = self.run_dir / "next-prompt.md"
            atomic_file.write_text_atomic(self.prompt, "Check the next change.\n")
            self.fixture.world.first_turn_timeout = True
            self.assertEqual(self.dispatch("submitted-worker"), worker.ExitCode.BUSY)
            self.assertEqual(self.agent.submitted, submitted)
            self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), before)
            self.agent.complete(pane)
            self.assertEqual(self.finish("submitted-worker"), turn_end.TurnOutcome.DONE)
            self.assert_pass_completed(before)
            self.assertNotIn(pane, self.fixture.world.locations)
            self.assertEqual(staff.by_id(row.id), row)

        with self.subTest(cause="rename while direct dispatch takes the worker lock"):
            self.fixture.world.first_turn_timeout = False
            self.assertIs(self.dispatch("rename-source"), worker.DispatchSubmission.SUBMITTED)
            pane = self.agent.submitted[-1][0]
            source = self.agent.row(pane)
            replacement = self.opened("different-worker").row
            before = progress_history.active_dispatch(source.id, self.run_dir)
            submitted = list(self.agent.submitted)
            with self.rename_at_worker_lock(source, "renamed-source", replacement=replacement):
                result = worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(source.name), "impl",
                    self.prompt, session_dir=self.run_dir, slot=source.name, phase="8", kind="impl",
                    launcher_command=["another launcher"])
            self.assertIs(result, worker.ExitCode.BUSY)
            self.assertIsInstance(before, progress_history.ActiveDispatch)
            assert isinstance(before, progress_history.ActiveDispatch)
            self.assertEqual(worker_panes.worker_pass(source, self.run_dir)["instance_id"], before.instance_id)
            self.assertEqual(worker_panes.worker_pass(source, self.run_dir)["status"], "active")
            self.assertEqual(self.agent.submitted, submitted)

    def test_dispatch_lock_blocks_a_competing_dispatch_and_role_change(self) -> None:
        opened = self.opened()
        results: list[object] = []
        def concurrent() -> None:
            results.append(self.dispatch())
            results.append(worker_panes.start_or_resume(self.fixture.parent.id, staff.WorkerName.parse("impl"), "review"))
        self.agent.during_prompt = concurrent
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(results[0], worker.ExitCode.BUSY)
        self.assertIsInstance(results[1], worker_panes.StartRefused)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIsNotNone(staff.by_id(opened.row.id))

    def test_transcript_failure_reports_whether_the_prompt_was_delivered(self) -> None:
        config = self.fixture.root / "conf/direct.conf"
        atomic_file.write_text_atomic(config, config.read_text().replace("WORKER_SETTLE_SECONDS=30", "WORKER_SETTLE_SECONDS=1"))
        for delivered in (False, True):
            with self.subTest(delivered=delivered):
                name = "impl2" if delivered else "impl"
                opened = self.opened(name)
                backend = staff.backend(opened.row)
                assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
                path = worker_admission.transcript_source(opened.row, backend)

                def tear_transcript() -> None:
                    atomic_file.write_text_atomic(path, '{"torn":')

                if delivered:
                    self.agent.during_prompt = tear_transcript
                    self.agent.finish_after = 1
                else:
                    tear_transcript()
                with redirect_stderr(io.StringIO()) as errors:
                    result = self.dispatch(name)
                if delivered:
                    self.assertEqual(result, worker.ExitCode.UNCERTAIN)
                    self.assertEqual(errors.getvalue(),
                        f"anomaly: {name}: worker transcript has incomplete records; submission is unproved\n")
                else:
                    self.assertEqual(result, worker.ExitCode.UNCERTAIN)
                    self.assertEqual(errors.getvalue(),
                        f"anomaly: {name}: worker transcript has incomplete records; no prompt sent\n")
                if delivered:
                    self.assertEqual([submission[0] for submission in self.agent.submitted], [opened.pane])
                else:
                    self.assertEqual(self.agent.submitted, [])

    def test_older_incomplete_record_keeps_the_valid_completion_receipt(self) -> None:
        opened = self.opened()
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "interrupted after prompt"):
            _ = self.dispatch()
        backend = staff.backend(opened.row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        path = worker_admission.transcript_source(opened.row, backend)
        atomic_file.write_text_atomic(path, '{"torn":\n' + path.read_text())
        self.agent.complete(opened.pane)
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())
        self.assertEqual(len(self.agent.submitted), 1)

    def test_stopped_or_closed_worker_without_done_preserves_the_report(self) -> None:
        for closed in (False, True):
            with self.subTest(closed=closed):
                name = "impl2" if closed else "impl"
                opened = self.opened(name)
                atomic_file.write_text_atomic(self.reply, "The previous full report.\n")
                self.agent.finish_after = 1
                def exit_after_prompt(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                    result = self.agent.run(args, **kwargs)
                    if args[0] == MISSING_HERDR_BIN and args[4] == "prompt":
                        del self.fixture.world.foreground[opened.pane]
                        self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
                        if closed:
                            self.fixture.world.remove_leaf(opened.pane)
                            del self.fixture.world.locations[opened.pane]
                    return result
                with patch.object(subprocess, "run", side_effect=exit_after_prompt):
                    code = self.dispatch(name)
                self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
                self.assertEqual(self.reply.read_text(), "The previous full report.\n")
                self.assertEqual(self.agent.waited, [])
                self.assertEqual(len(self.agent.submitted), 2 if closed else 1)

    def test_unpublished_start_closes_its_pane_and_records_no_pass(self) -> None:
        self.fixture.world.outcome = herdr.PromptNotSubmitted("startup hook failed")
        self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(len(self.fixture.world.launches), 1)
        self.assertEqual(json.loads((self.run_dir / "progress_history_state.json").read_text())["pass"], {})

    def test_failed_dispatch_preparation_ends_only_a_new_pane(self) -> None:
        failures = ("recorder", "dispatch id", "delivery", "receipt", "wait", "observation", "busy", "compact", "target", "incomplete")
        for failure in failures:
            for existing in (False, True, "closed"):
                with self.subTest(failure=failure, existing=existing):
                    with ExitStack() as cleanup:
                        case = set_up_fixture(cleanup.callback, WorkerCommandTests("runTest"))
                        if existing:
                            opened = case.opened()
                            if existing == "closed":
                                case.assertTrue(case.fixture.world.close_terminal_pane(opened.pane, opened.terminal))
                        before = staff.snapshot().rows, set(case.fixture.world.locations)
                        closed = len(case.fixture.world.closed)
                        with ExitStack() as patches, redirect_stderr(io.StringIO()) as errors:
                            if failure == "incomplete":
                                start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                                def incomplete(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                                    result = start(*arguments, **options)
                                    assert isinstance(result, worker_panes.OpenWorker)
                                    return worker_panes.IncompleteStart(result.row, result.pane, "TEST startup failed", result.created_pane)
                                _ = patches.enter_context(patch.object(worker_panes, "start_or_resume", side_effect=incomplete))
                            elif failure in ("target", "compact"):
                                if failure == "compact":
                                    _ = patches.enter_context(patch.object(worker, "_compact_before_dispatch", side_effect=OSError("TEST compact failed")))
                                check = cast(Callable[..., bool], herdr.check_target)
                                def refuse(*arguments: object, **options: object) -> bool:
                                    return False if options.get("action") == "keys" and options.get("text") else check(*arguments, **options)
                                _ = patches.enter_context(patch.object(herdr, "check_target", side_effect=refuse))
                            elif failure == "recorder":
                                _ = patches.enter_context(patch.object(progress_history, "begin_dispatch", side_effect=ValueError("TEST recorder failed")))
                            elif failure == "delivery":
                                _ = patches.enter_context(patch.object(progress_history, "save_dispatch_delivery", side_effect=OSError("TEST delivery failed")))
                            elif failure == "receipt":
                                _ = patches.enter_context(patch.object(worker, "dispatch_receipt", return_value=worker.DispatchEvidenceUnknown("TEST receipt failed")))
                            elif failure in ("wait", "observation"):
                                start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                                def working(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                                    result = start(*arguments, **options)
                                    if failure == "wait":
                                        _ = patches.enter_context(patch.object(case.fixture.world, "wait_turn_end", side_effect=OSError("TEST wait failed")))
                                    return replace(result, status=herdr.AgentStatus.WORKING) if isinstance(result, worker_panes.OpenWorker) else result
                                _ = patches.enter_context(patch.object(worker_panes, "start_or_resume", side_effect=working))
                                if failure == "observation":
                                    _ = patches.enter_context(patch.object(worker_panes, "observe_worker", return_value=worker_panes.ClosedWorker(case.fixture.parent)))
                            elif failure == "busy":
                                start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                                def busy(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                                    result = start(*arguments, **options)
                                    return replace(result, status=herdr.AgentStatus.UNKNOWN) if isinstance(result, worker_panes.OpenWorker) else result
                                _ = patches.enter_context(patch.object(worker_panes, "start_or_resume", side_effect=busy))
                            if failure in ("recorder", "delivery"):
                                with case.assertRaisesRegex((OSError, ValueError), "TEST .* failed"):
                                    case.assertIsInstance(case.dispatch(), worker.ExitCode)
                            else:
                                code = case.dispatch(dispatch_id="TEST-other-pass" if failure == "dispatch id" else "")
                                case.assertEqual(code, worker.ExitCode.REFUSED if failure in ("dispatch id", "compact") else
                                                 worker.ExitCode.BUSY if failure == "busy" else worker.ExitCode.UNCERTAIN)
                        case.assertEqual(case.agent.submitted, [])
                        if failure == "compact":
                            case.assertIn("TEST compact failed; no prompt sent", errors.getvalue())
                            case.assertNotIn("dispatch target is unproved", errors.getvalue())
                        if existing:
                            case.assertEqual(staff.snapshot().rows, before[0])
                            case.assertEqual(set(case.fixture.world.locations), before[1])
                            case.assertEqual(len(case.fixture.world.closed), closed + (existing == "closed"))
                        else:
                            case.assertEqual(staff.find(kind="worker", parent=case.fixture.parent.id), [])
                            case.assertEqual(len(case.fixture.world.closed), 1)
                        case.assertNotIn("cleanup failed", errors.getvalue())

    def test_failed_dispatch_closes_its_new_pane_with_a_named_target(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                with ExitStack() as cleanup:
                    case = set_up_fixture(cleanup.callback, WorkerCommandTests("runTest"))
                    _ = write_registry(case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
                    rows: list[staff.StaffRow] = []
                    targets: list[str] = []
                    close = case.fixture.world.close_tab_confirmed
                    def refuse_empty_target(pane: str, *, target: str = "", terminal: str = "") -> bool:
                        if pane in case.fixture.world.locations:
                            targets.append(target)
                            if not target:
                                raise herdr_types.HerdrFailure("TEST close needs a named target")
                        return close(pane, target=target, terminal=terminal)
                    def fail(*_arguments: object, **_options: object) -> str:
                        row, = staff.find(kind="worker", parent=case.fixture.parent.id)
                        rows.append(row)
                        raise ValueError("TEST recorder failed")
                    with (patch.object(progress_history, "begin_dispatch", side_effect=fail),
                          patch.object(case.fixture.world, "close_tab_confirmed", side_effect=refuse_empty_target),
                          redirect_stderr(io.StringIO()) as errors):
                        with case.assertRaisesRegex(ValueError, "TEST recorder failed"):
                            _ = case.dispatch()
                    if family == "claude":
                        path = case.fixture.root / "pane-records/agents.json"
                        entries = cast(dict[str, object], json.loads(path.read_text())) if path.exists() else {}
                        case.assertEqual(entries, {})
                    row, = rows
                    case.assertEqual(targets, [str(row.current_session)])
                    case.assertIsNone(staff.by_id(row.id))
                    case.assertEqual(len(case.fixture.world.closed), 1)
                    case.assertEqual(case.agent.submitted, [])
                    case.assertNotIn("cleanup failed", errors.getvalue())

    def test_dispatch_keeps_a_replacement_terminal_after_preparation_fails(self) -> None:
        def replace_terminal(*_arguments: object, **_options: object) -> str:
            row, = staff.find(kind="worker", parent=self.fixture.parent.id)
            state = worker_panes.observe_worker(self.fixture.parent.id, row, staff.snapshot())
            assert isinstance(state, worker_panes.OpenWorker)
            self.fixture.world.locations[state.pane]["terminal_id"] = "TEST-replacement"
            raise ValueError("TEST recorder failed")
        with (patch.object(progress_history, "begin_dispatch", side_effect=replace_terminal),
              redirect_stderr(io.StringIO()) as errors):
            with self.assertRaisesRegex(ValueError, "TEST recorder failed"):
                _ = self.dispatch()
        self.assertEqual(self.fixture.world.closed, [])
        self.assertEqual(len(staff.find(kind="worker", parent=self.fixture.parent.id)), 1)
        self.assertEqual(errors.getvalue().count("anomaly:"), 1)
        self.assertIn("terminal identity changed", errors.getvalue())

    def test_dispatch_uses_saved_start_ownership_without_a_pane_read(self) -> None:
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                with ExitStack() as cleanup:
                    case = set_up_fixture(cleanup.callback, WorkerCommandTests("runTest"))
                    start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                    arm = worker.arm_launch_cleanup
                    def launch(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                        result = start(*arguments, **options)
                        if incomplete:
                            assert isinstance(result, worker_panes.OpenWorker)
                            return worker_panes.IncompleteStart(result.row, result.pane, "TEST startup failed", result.created_pane)
                        return result
                    def arm_without_read(cleanup: ExitStack, before: frozenset[staff.StaffId],
                                         result: worker_panes.WorkerStart) -> None:
                        with patch.object(worker, "_client", side_effect=AssertionError("no ownership pane read")):
                            arm(cleanup, before, result)
                    with (patch.object(worker_panes, "start_or_resume", side_effect=launch),
                          patch.object(worker, "arm_launch_cleanup", side_effect=arm_without_read),
                          redirect_stderr(io.StringIO()) as errors):
                        case.assertEqual(case.dispatch(), worker.ExitCode.UNCERTAIN if incomplete else worker.DispatchSubmission.SUBMITTED)
                    case.assertEqual(len(case.agent.submitted), 0 if incomplete else 1)
                    case.assertEqual(len(case.fixture.world.closed), int(incomplete))
                    if incomplete:
                        case.assertIn("TEST startup failed", errors.getvalue())

    def test_failed_first_assignment_ends_only_its_new_team_panes(self) -> None:
        for failure in ("selection", "dispatch"):
            with self.subTest(failure=failure):
                with ExitStack() as cleanup:
                    case = set_up_fixture(cleanup.callback, WorkerCommandTests("runTest"))
                    with ExitStack() as patches, redirect_stderr(io.StringIO()) as errors:
                        if failure == "selection":
                            _ = patches.enter_context(patch.object(worker, "select_worker", return_value=worker_panes.StartRefused(
                                worker_panes.StartRefusal.BUSY_ROLE_CHANGE, "TEST selection failed")))
                        else:
                            _ = patches.enter_context(patch.object(worker, "dispatch", return_value=worker.ExitCode.REFUSED))
                        code = worker.assign(case.fixture.parent.id, "impl", case.prompt,
                            session_dir=case.run_dir, slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
                    case.assertEqual(code, worker.ExitCode.BUSY if failure == "selection" else worker.ExitCode.REFUSED)
                    case.assertEqual(staff.find(kind="worker", parent=case.fixture.parent.id), [])
                    case.assertEqual(len(case.fixture.world.closed), 3)
                    case.assertNotIn("unit-impl", [destination.label for destination in case.fixture.world.launches
                                                  if isinstance(destination, herdr.SplitPane)])
                    case.assertEqual(case.agent.submitted, [])
                    case.assertNotIn("cleanup failed", errors.getvalue())

    def test_failed_restore_before_submission_ends_only_its_new_pane(self) -> None:
        for existing, incomplete, refused in ((False, False, False), (True, False, False),
                                               (False, True, False), (False, False, True)):
            with self.subTest(existing=existing, incomplete=incomplete, refused=refused):
                with ExitStack() as cleanup:
                    case = set_up_fixture(cleanup.callback, WorkerCommandTests("runTest"))
                    opened = case.opened()
                    _ = progress_history.begin_dispatch(case.run_dir, slot="impl", phase="8", role="impl",
                        worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
                        prompt=case.prompt, kind="impl", lens="")
                    if not existing:
                        case.assertIsInstance(worker_panes.close(case.fixture.parent.id, staff.WorkerName.parse(opened.row.name)),
                                              worker_panes.Closed)
                    closed = len(case.fixture.world.closed)
                    director = herdr.TabSession("w1:p0", "unit", "w1", str(case.fixture.parent.current_session))
                    start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                    def launch(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                        result = start(*arguments, **options)
                        if incomplete:
                            assert isinstance(result, worker_panes.OpenWorker)
                            return worker_panes.IncompleteStart(result.row, result.pane, "TEST resume read failed", result.created_pane)
                        return result
                    check = cast(Callable[..., bool], herdr.check_target)
                    def target(*arguments: object, **options: object) -> bool:
                        return False if refused and options.get("action") == "keys" else check(*arguments, **options)
                    opens_pane = incomplete or refused
                    step = (worker.UnsubmittedAssignment("TEST-pass", ("assign.sh",)) if opens_pane else
                            worker.DispatchEvidenceUnknown("TEST resume read failed"))
                    with (patch.object(worker_panes, "start_or_resume", side_effect=launch) as start_worker,
                          patch.object(worker, "resume_step", return_value=step),
                          patch.object(herdr, "check_target", side_effect=target),
                          patch.object(case.fixture.world, "tab_sessions", return_value=[director]),
                          redirect_stderr(io.StringIO()) as errors):
                        code = worker.restore(case.fixture.parent.id)
                    case.assertEqual(code, worker.ExitCode.UNCERTAIN)
                    case.assertIn("would send keys to an unproved target" if refused else "TEST resume read failed", errors.getvalue())
                    case.assertEqual(case.agent.submitted, [])
                    case.assertEqual(start_worker.call_count, int(opens_pane))
                    case.assertEqual(staff.by_id(opened.row.id), opened.row)
                    case.assertEqual(len(case.fixture.world.closed), closed + int(opens_pane))

    def test_publication_failure_reports_that_its_new_pane_was_closed(self) -> None:
        with (patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("TEST admission refused")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertIn("pane w1:p1 closed", errors.getvalue())
        self.assertNotIn("retained", errors.getvalue())
        self.assertEqual(self.fixture.world.closed, [("w1:p1", "terminal-w1:p1")])
        self.assertEqual(self.agent.submitted, [])

    def test_publication_failure_closes_the_backend_without_delivery(self) -> None:
        publish = staff.ensure_worker
        def failed(identity: staff.StaffId, name: str, parent: staff.StaffId) -> staff.StaffRow:
            del identity, name, parent
            raise staff.StaffRefused("publication unavailable")
        with patch.object(staff, "ensure_worker", side_effect=failed):
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertIs(staff.ensure_worker, publish)
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(len(self.fixture.world.launches), 1)
        self.assertEqual(staff.snapshot().find(kind="worker", parent=self.fixture.parent.id), [])
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.fixture.world.launches), 2)
        self.assertEqual(len(self.agent.submitted), 1)

    def test_admission_failure_has_the_exact_worker_refusal(self) -> None:
        parent = self.fixture.worker("parent-worker")
        with redirect_stderr(io.StringIO()) as errors:
            code = worker.dispatch(parent.id, staff.WorkerName.parse("child"), "impl", self.prompt,
                session_dir=self.run_dir, slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
        self.assertEqual(code, worker.ExitCode.REFUSED)
        self.assertEqual(errors.getvalue(), "staff: child: not opened: only a unit or showrunner can admit a worker\n")
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(staff.snapshot().find(kind="worker", parent=parent.id), [])

    def test_unknown_process_evidence_and_structural_fault_never_deliver(self) -> None:
        opened = self.opened()
        self.fixture.world.unknown[opened.pane] = "native reader unavailable"
        before = self.fixture.staff_state()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertEqual(errors.getvalue().count("native reader unavailable"), 1)
        self.fixture.world.unknown.clear()
        self.fixture.world.locations[opened.pane]["terminal_id"] = ""
        self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(self.fixture.staff_state(), before)

    def test_start_refusal_retains_the_active_role_and_worker(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "impl", completed=True)
        before = self.fixture.staff_state()
        with (patch("scripts.agents.detached_runs.live_runs", return_value=[object()]),
              redirect_stderr(io.StringIO()) as errors):
            code = self.dispatch(role="review")
        self.assertEqual(int(code), 2)
        self.assertEqual(errors.getvalue(), "role change blocked by active work: a detached run is live\n")
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(self.fixture.staff_state(), before)

    def test_dispatch_follows_the_current_native_session_after_replacement(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.assertNotEqual(row.id, row.current_session)
        self.fixture.world.add(row, "w1:p9", live=False)
        opened = self.opened()
        self.fixture.world.remove_leaf(opened.pane)
        location = self.fixture.world.locations.pop(opened.pane)
        self.fixture.world.locations["w2:p9"] = {**location, "pane_id": "w2:p9", "tab_id": "w2:t2", "workspace_id": "w2"}
        self.fixture.world.layouts["w2:t2"] = {"type": "pane", "pane_id": "w2:p9"}
        self.fixture.world.foreground["w2:p9"] = self.fixture.world.foreground.pop(opened.pane)
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(self.agent.submitted[0][0], "w2:p9")
        self.assertEqual(self.agent.row("w2:p9").current_session, row.current_session)
        self.assertEqual(self.agent.row("w2:p9").id, row.id)

    def test_selector_uses_role_number_order_and_reuses_a_stopped_worker(self) -> None:
        second = self.opened("impl2")
        first = self.opened("impl")
        chosen = worker.select_worker(self.fixture.parent.id, "impl", staff.snapshot())
        self.assertEqual(chosen, staff.WorkerName.parse("impl"))
        other = self.opened("fix", "fix").row
        path, run = self.detached_record(first.row, "selector-check")
        run.update(status="running", pid=101)
        for work in ("live detached run", "legacy detached run", "active pass", "older pass",
                     "older pass in another run", "damaged own run", "damaged other worker",
                     "damaged other unit", "missing supervised record"):
            with self.subTest(work=work), ExitStack() as cleanup:
                supervisors: dict[Path, frozenset[int]] = {}
                damaged = path
                if work == "missing supervised record":
                    supervisors[path.resolve()] = frozenset({101})
                elif work in ("live detached run", "legacy detached run"):
                    fields: dict[str, object] = dict(run)
                    if work == "legacy detached run":
                        del fields["staff_id"]
                    atomic_file.write_text_atomic(path, json.dumps(fields))
                    _ = cleanup.callback(path.unlink)
                    supervisors[path.resolve()] = frozenset({101})
                elif work.startswith("damaged"):
                    if work == "damaged other unit":
                        foreign = fixture_member("unit", name="TEST-foreign", parent=self.fixture.parent.parent)
                        foreign_run = self.fixture.root / "foreign-run"
                        _ = staff.set_run(foreign.id, staff.OpenRun(foreign_run))
                        damaged = foreign_run / worker_lifecycle.DETACHED_RUNS_DIR / "damaged.json"
                    else:
                        owner = first.row if work == "damaged own run" else other
                        damaged = path.with_name(f"damaged@{owner.id}.json")
                    atomic_file.write_text_atomic(damaged, "{")
                    _ = cleanup.callback(damaged.unlink)
                    supervisors[damaged.resolve()] = frozenset({101})
                else:
                    instance = progress_history.begin_dispatch(self.run_dir, slot=first.row.name, phase="8", role="impl",
                        worker_name=first.row.name, worker_id=first.row.id, launcher_command=["assign.sh"],
                        prompt=self.prompt, kind="impl", lens="")
                    progress_history.save_dispatch_delivery(self.run_dir, instance,
                        progress_history.DispatchDelivery(f"Read the prompt. End with DONE {instance}.",
                            instance, str(self.reply)))
                    _ = cleanup.callback(progress_history.complete_dispatch, self.run_dir, first.row.name,
                                         instance, status="error")
                    if work.startswith("older pass"):
                        newer_run = self.run_dir
                        if work.endswith("another run"):
                            newer_run = self.fixture.root / "private" / "newer-pass"
                            saved = cast(dict[str, object], json.loads((self.run_dir / unit_runs.STATE_FILE).read_text()))
                            saved.update({"pass": {}, "history_file": str(newer_run / "history.jsonl")})
                            atomic_file.write_text_atomic(newer_run / unit_runs.STATE_FILE, json.dumps(saved))
                        newer = progress_history.begin_dispatch(newer_run, slot="newer-slot", phase="8", role="impl",
                            worker_name=first.row.name, worker_id=first.row.id, launcher_command=["assign.sh"],
                            prompt=self.prompt, kind="impl", lens="")
                        progress_history.complete_dispatch(newer_run, "newer-slot", newer)
                record = staff.snapshot()
                only_first = replace(record, rows=tuple(row for row in record.rows if row.id != second.row.id))
                expected = "impl" if work in ("damaged other worker", "damaged other unit") else "impl2"
                folder_held = work in ("damaged own run", "missing supervised record")
                with (patch.object(detached_run_process, "supervisor_records", return_value=supervisors) as supervised,
                      patch.object(worker_lifecycle, "read_json_object", wraps=worker_lifecycle.read_json_object) as reads,
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(worker.select_worker(self.fixture.parent.id, "impl", record),
                                     staff.WorkerName.parse("impl3" if folder_held else expected))
                    self.assertEqual(worker.select_worker(self.fixture.parent.id, "impl", only_first),
                                     staff.WorkerName.parse(expected))
                    if work in ("damaged other worker", "damaged other unit"):
                        self.assertTrue(all(call.args != (damaged,) for call in reads.call_args_list))
                        self.assertEqual(errors.getvalue(), "")
                    if work in ("active pass", "older pass", "older pass in another run"):
                        for unknown in (False, True):
                            processes = {first.pane: "native reader unavailable"} if unknown else {}
                            for scope in ((), ("--all-parents",)):
                                with (self.subTest(unknown=unknown, scope=scope),
                                      patch.dict(self.fixture.world.unknown, processes)):
                                    code, cells, output = self.text_command("list", *scope)
                                    self.assertEqual(code, 0, output)
                                    shown = next(row[5] for row in cells if row[1] == first.row.name)
                                    if unknown and not scope:
                                        self.assertTrue(shown.startswith("busy ("), shown)
                                        self.assertIn("native reader unavailable", shown)
                                    else:
                                        self.assertEqual(shown, "busy")
                                    code, listing, output = self.command("list", "--json", *scope)
                                    self.assertEqual(code, 0, output)
                                    rows = cast(list[dict[str, object]], json.loads(listing))
                                    selected = next(row for row in rows if row["id"] == first.row.id)
                                    self.assertEqual(selected["work"], "busy")
                                    if unknown and not scope:
                                        self.assertEqual(selected["state"], "ProcessUnknownWorker")
                    if folder_held:
                        self.assertEqual(len(errors.getvalue().splitlines()), 1)
                        self.assertIn(str(damaged), errors.getvalue())
                        supervised.return_value = {}
                        self.assertEqual(worker.select_worker(self.fixture.parent.id, "impl", record),
                                         staff.WorkerName.parse("impl"))
        self.fixture.world.locations[first.pane]["agent_status"] = "working"
        _ = self.fixture.world.stop(second.row, self.fixture.world)
        self.fixture.world.reads.clear()
        chosen = worker.select_worker(self.fixture.parent.id, "impl", staff.snapshot())
        self.assertEqual(chosen, staff.WorkerName.parse("impl2"))
        self.assertEqual(self.fixture.world.reads.count(("pane", "list")), 1)
        launches = len(self.fixture.world.launches)
        self.assertEqual(self.dispatch("impl2"), worker.ExitCode.OK)
        self.assertEqual(len(self.fixture.world.launches), launches + 1)
        self.assertEqual(self.agent.submitted[0][0], second.pane)
        self.assertEqual(self.agent.row(self.agent.submitted[0][0]).current_session, second.row.current_session)

    def test_all_busy_assignment_names_each_worker_and_refuses(self) -> None:
        for name in ("impl", "impl2"):
            opened = self.opened(name)
            self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        with redirect_stderr(io.StringIO()) as errors:
            code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
        self.assertEqual(code, worker.ExitCode.BUSY)
        self.assertIn("impl, impl2", errors.getvalue())
        self.assertEqual(self.agent.submitted, [])

    def test_named_busy_worker_refuses_a_second_prompt_before_compaction(self) -> None:
        status = self.run_dir / "impl2_status"
        original = self.prompt
        following = self.run_dir / "next-prompt.md"
        atomic_file.write_text_atomic(following, "Check the next change.\n")

        def assign_named(prompt: Path) -> tuple[int, str, str]:
            return self.command("assign", "impl", str(prompt), "--session-dir", str(self.run_dir),
                "--slot", "impl2", "--phase", "8", "--kind", "assign", "--name", "impl2", "--reply", str(self.reply),
                "--launcher-command", json.dumps(["assign.sh", "--to", "impl2", "--prompt", str(prompt)]))

        self.agent.finish_after = 100
        with patch.dict(os.environ, {progress_history.ASSIGN_STATUS_FILE_ENV: str(status)}):
            code, output, errors = assign_named(original)
            self.assertEqual((code, output), (0, "submitted\n"), errors)
            (pane, _), = self.agent.submitted
            row = self.agent.row(pane)
            before = progress_history.active_dispatch(row.id, self.run_dir)
            self.assertIsInstance(before, progress_history.ActiveDispatch)
            state = (self.run_dir / unit_runs.STATE_FILE).read_bytes()
            self.fixture.world.locations[pane]["agent_status"] = "idle"
            backend = staff.backend(row)
            assert isinstance(backend, staff.CodexBackend)
            with (patch.object(worker, "_compaction_plan", return_value=worker.CompactionRequired(backend, 0)) as plan,
                  patch.object(worker, "_spawn_compaction") as spawn,
                  patch.object(worker, "_agent_command") as input_command):
                code, output, errors = assign_named(following)

        self.assertEqual((code, output), (worker.ExitCode.BUSY, ""), errors)
        plan.assert_not_called()
        spawn.assert_not_called()
        input_command.assert_not_called()
        self.assertEqual(self.agent.compactions, [])
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(status.read_text(), "running\n")
        self.assertEqual(progress_history.active_dispatch(row.id, self.run_dir), before)
        self.assertEqual((self.run_dir / unit_runs.STATE_FILE).read_bytes(), state)

        self.agent.complete(pane)
        self.assertEqual(self.finish(row.name), turn_end.TurnOutcome.DONE)
        atomic_file.write_text_atomic(status, "running\n")
        with patch.dict(os.environ, {progress_history.ASSIGN_STATUS_FILE_ENV: str(status)}):
            code, output, errors = assign_named(following)
        self.assertEqual((code, output), (worker.ExitCode.BUSY, ""), errors)
        self.assertIn("the assignment status is running", errors)
        self.assertEqual(len(self.agent.submitted), 1)

        with self.subTest(caller="direct dispatch with a run in another held folder"):
            held = self.run_dir.with_name("held-run")
            atomic_file.write_text_atomic(status, "done\n")
            atomic_file.write_text_atomic(held / unit_runs.STATE_FILE, json.dumps({
                "owner_staff_id": self.fixture.parent.id, "open_phases": [], "pass": {}}))
            state_path = self.run_dir / unit_runs.STATE_FILE
            state_before = state_path.read_bytes()
            with (patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(held.parent)}),
                  pane_fixtures.live_detached_run(held, row), redirect_stderr(io.StringIO()) as diagnostics,
                  patch.object(worker, "_agent_command") as input_command):
                result = worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(row.name), "impl", following,
                    session_dir=self.run_dir, slot=row.name, phase="8", kind="impl", launcher_command=["direct"])
            self.assertIs(result, worker.ExitCode.BUSY)
            self.assertIn("a detached run is live", diagnostics.getvalue())
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(len(self.agent.submitted), 1)
            input_command.assert_not_called()

    def test_assignment_recovers_saved_reply_before_checking_running_status(self) -> None:
        self.check_assignment_recovers_done(saved_reply=True)

    def test_assignment_recovers_done_without_a_saved_reply(self) -> None:
        self.check_assignment_recovers_done(saved_reply=False)

    def check_assignment_recovers_done(self, *, saved_reply: bool) -> None:
        opened = self.opened()
        status = self.run_dir / "impl_status"
        following = self.run_dir / "next-prompt.md"
        atomic_file.write_text_atomic(following, "Check the next change.\n")
        command = ("--session-dir", str(self.run_dir), "--slot", "impl", "--phase", "8", "--kind", "assign",
                   "--launcher-command", json.dumps(["assign.sh"]))

        self.agent.finish_after = 100
        with patch.dict(os.environ, {progress_history.ASSIGN_STATUS_FILE_ENV: str(status)}):
            self.assertEqual(self.command("assign", "impl", str(self.prompt), *command,
                                          "--name", "impl", "--reply", str(self.reply)), (0, "submitted\n", ""))
            passed = worker_panes.worker_pass(opened.row, self.run_dir)
            self.agent.complete(opened.pane)
            if saved_reply:
                worker.save_reply(self.reply, self.agent.replies[-1])
            self.assertEqual(self.reply.exists(), saved_reply)
            self.assertEqual(status.read_text(), "running\n")
            self.assertEqual(worker_panes.worker_pass(opened.row, self.run_dir)["status"], "active")

            begin_dispatch = cast(Callable[..., str], progress_history.begin_dispatch)

            def begin_after_recovery(*args: object, **options: object) -> str:
                self.assertEqual(status.read_text(), "done\n")
                ended = worker_panes.worker_pass(opened.row, self.run_dir)
                self.assertEqual((ended["instance_id"], ended["status"]), (passed["instance_id"], "completed"))
                return begin_dispatch(*args, **options)

            with patch.object(progress_history, "begin_dispatch", side_effect=begin_after_recovery) as begin:
                code, output, errors = self.command("assign", "impl", str(following), *command,
                    "--reply", str(self.run_dir / "next-reply.md"))
            self.assertEqual((code, output), (0, "submitted\n"), errors)
            begin.assert_called_once()

        self.assertEqual([pane for pane, _ in self.agent.submitted], [opened.pane, opened.pane])
        self.assertEqual(status.read_text(), "running\n")
        self.assertEqual(self.reply.read_text().rstrip(), self.agent.replies[0])
        # A new assignment leaves the old result to the notice retry.
        self.assertEqual(self.agent.notices, [])
        _ = detached_runs.reap_orphans(str(self.run_dir))
        self.assertEqual(len(self.agent.notices), 1)
        self.assertIn(str(passed["instance_id"]), self.agent.notices[0])
        finished = [event["status"] for event in assign.recorded_passes(self.run_dir)
                    if event.get("event_type") == "pass_finished" and event.get("pass_instance_id") == passed["instance_id"]]
        self.assertEqual(finished, ["completed"])
        next_pass = worker_panes.worker_pass(opened.row, self.run_dir)
        self.assertNotEqual(next_pass["instance_id"], passed["instance_id"])
        self.assertEqual(next_pass["status"], "active")

    def test_unsubmitted_assignment_retry_refuses_a_live_detached_run(self) -> None:
        opened = self.opened()
        name = opened.row.name
        launcher = ["assign.sh", "--to", name]
        instance = progress_history.begin_dispatch(self.run_dir, slot=name, phase="8", role="impl",
            worker_name=name, worker_id=opened.row.id, launcher_command=launcher,
            prompt=self.prompt, kind="assign", lens="")
        progress_history.save_dispatch_delivery(self.run_dir, instance,
            progress_history.DispatchDelivery(f"Read the unchanged prompt file {json.dumps(str(self.prompt))}. "
                + f"End your final reply with this line alone: DONE {instance}", instance, str(self.reply)))
        command = ("assign", "impl", str(self.prompt), "--name", name, "--slot", name,
                   "--phase", "8", "--kind", "assign", "--session-dir", str(self.run_dir),
                   "--launcher-command", json.dumps(launcher))
        passed = worker_panes.worker_pass(opened.row, self.run_dir)
        with (pane_fixtures.live_detached_run(self.run_dir, opened.row),
              patch.object(worker, "_agent_command") as input_command):
            code, output, errors = self.command(*command)
        self.assertEqual((code, output), (worker.ExitCode.BUSY, ""), errors)
        self.assertIn("a detached run is live", errors)
        input_command.assert_not_called()
        self.assertEqual(worker_panes.worker_pass(opened.row, self.run_dir), passed)

        self.assertEqual(self.command(*command), (0, "submitted\n", ""))
        self.assertEqual(worker_panes.worker_pass(opened.row, self.run_dir)["confirmed_delivery_ids"], [instance])
        self.assertEqual(len(self.agent.submitted), 1)

    def test_unnamed_assignment_keeps_the_busy_workers_pass(self) -> None:
        for stage_one_done in (False, True):
            with self.subTest(stage_one_done=stage_one_done):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    case.check_unnamed_assignment_keeps_the_busy_workers_pass(stage_one_done)
                finally:
                    _ = case.doCleanups()

    def check_unnamed_assignment_keeps_the_busy_workers_pass(self, stage_one_done: bool) -> None:
        def assign(launcher: str, *, early_ready: Path | worker.SingleStageReview = worker.SINGLE_STAGE_REVIEW
                   ) -> worker.DispatchSubmission | worker.ExitCode:
            return worker.assign(self.fixture.parent.id, "review", self.prompt, session_dir=self.run_dir,
                slot="review", phase="8", kind="review", launcher_command=["assign.sh", launcher], reply=self.reply,
                early_ready=early_ready)

        def passes() -> dict[str, dict[str, object]]:
            return cast(dict[str, dict[str, dict[str, object]]],
                        json.loads((self.run_dir / "progress_history_state.json").read_text()))["pass"]

        self.agent.finish_after = 100
        self.assertEqual(assign("adversary", early_ready=self.run_dir / "stage-two.ready" if stage_one_done
                                else worker.SINGLE_STAGE_REVIEW), worker.ExitCode.OK)
        busy = self.agent.submitted[0][0]
        if stage_one_done:
            self.agent.complete(busy)
            self.assertFalse(self.reply.exists())
        else:
            self.fixture.world.locations[busy]["agent_status"] = "working"
        _ = self.opened("review2", "review")
        held = passes()["review"]
        self.assertEqual(assign("rerun"), worker.ExitCode.OK)
        self.assertNotEqual(self.agent.submitted[1][0], busy)
        self.assertEqual(passes()["review"], held)
        members = {row.name: row.id for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)}
        self.assertEqual((held["worker_id"], held["status"]), (members["review"], "active"))
        self.assertEqual((passes()["review2"]["worker_id"], passes()["review2"]["status"]), (members["review2"], "active"))

        self.fixture.world.locations[busy]["agent_status"] = "idle"
        backend = staff.backend(self.agent.row(busy))
        assert isinstance(backend, staff.CodexBackend)
        for newer_completed in (False, True):
            with self.subTest(newer_completed=newer_completed):
                if newer_completed:
                    row = self.agent.row(busy)
                    instance = progress_history.begin_dispatch(self.run_dir, slot="newer-slot", phase="8",
                        role="review", worker_name=row.name, worker_id=row.id, launcher_command=["assign.sh"],
                        prompt=self.prompt, kind="review", lens="")
                    progress_history.complete_dispatch(self.run_dir, "newer-slot", instance)
                with (patch.object(worker, "select_worker", return_value=staff.WorkerName.parse("review")),
                      patch.object(worker, "_compaction_plan", return_value=worker.CompactionRequired(backend, 0)) as plan,
                      patch.object(worker, "_spawn_compaction") as spawn,
                      patch.object(worker_panes, "parent_run_index", wraps=worker_panes.parent_run_index) as discover,
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(assign("competing launcher"), worker.ExitCode.BUSY)
                self.assertIn("workers unavailable: review", errors.getvalue())
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(discover.call_count, 1)
                plan.assert_not_called()
                spawn.assert_not_called()
                self.assertEqual(len(self.agent.submitted), 2)
                self.assertEqual(passes()["review"], held)

    def test_add_uses_the_lowest_free_name(self) -> None:
        _ = self.opened("impl")
        _ = self.opened("impl3")
        added = worker.add(self.fixture.parent.id, "impl")
        self.assertIsInstance(added, worker_panes.OpenWorker)
        assert isinstance(added, worker_panes.OpenWorker)
        self.assertEqual(added.row.name, "impl2")

    def test_first_standard_assignment_creates_the_default_team(self) -> None:
        for role, ready_hooks in (("impl", False), ("test", True)):
            with self.subTest(role=role, ready_hooks=ready_hooks):
                with ExitStack() as cleanup:
                    case = set_up_fixture(cleanup.callback, WorkerCommandTests("runTest"))
                    case.agent.finish_after = 1
                    assignment_thread = current_thread()
                    hook_acquired = Event()
                    release_hook = Event()
                    pending: dict[str, Future[turn_end.TurnOutcome]] = {}
                    acquire = worker_panes.worker_lock
                    bind = worker_panes.WorkerLockHold.bind
                    guard = LauncherHangGuard("first assignment ready hooks")
                    with ThreadPoolExecutor(max_workers=4) as hooks:
                        def queue_ready_hook(hold: worker_panes.WorkerLockHold, row: staff.StaffRow, *,
                                             deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> None:
                            bind(hold, row, deadline=deadline)
                            if current_thread() is assignment_thread and ready_hooks and row.name not in pending:
                                pending[row.name] = hooks.submit(turn_end.handle,
                                    turn_end.WorkerEvent(str(row.current_session), reply="ready"))

                        @contextmanager
                        def queued_ready_hooks(parent: staff.StaffId, name: staff.WorkerName, *,
                                               blocking: bool = False) -> Generator[worker_panes.WorkerLockHold | worker_panes.Kept]:
                            with acquire(parent, name, blocking=blocking) as hold:
                                if current_thread() is not assignment_thread and name.value == role:
                                    hook_acquired.set()
                                    case.assertTrue(release_hook.wait(guard.remaining()))
                                yield hold
                            if (current_thread() is assignment_thread and ready_hooks
                                    and isinstance(hold, worker_panes.WorkerLockHold) and name.value in pending):
                                if name.value == role:
                                    case.assertTrue(hook_acquired.wait(guard.remaining()))
                                else:
                                    case.assertIs(pending[name.value].result(timeout=guard.remaining()), turn_end.TurnOutcome.IGNORED)

                        def prompt_during_ready_hook() -> None:
                            case.assertEqual(set(pending), {"impl", "fix", "review", "test"})
                            case.assertFalse(pending[role].done())
                            case.assertFalse(hook_acquired.is_set())

                        if ready_hooks:
                            case.agent.during_prompt = prompt_during_ready_hook
                        try:
                            with (patch.object(worker_panes.WorkerLockHold, "bind", queue_ready_hook),
                                  patch.object(worker_panes, "worker_lock", queued_ready_hooks)):
                                code = worker.assign(case.fixture.parent.id, cast(staff.WorkerRole, role), case.prompt,
                                    session_dir=case.run_dir, slot=role, phase="8", kind=role,
                                    launcher_command=["assign.sh"], reply=case.reply)
                        finally:
                            release_hook.set()
                        for future in pending.values():
                            case.assertIs(future.result(timeout=guard.remaining()), turn_end.TurnOutcome.IGNORED)
                    case.assertEqual(code, worker.ExitCode.OK)
                    case.assertEqual({row.name for row in staff.snapshot().find(kind="worker", parent=case.fixture.parent.id)},
                                     {"impl", "fix", "review", "test"})
                    case.assertEqual(len(case.agent.submitted), 1)
                    case.assertEqual(case.agent.row(case.agent.submitted[0][0]).name, role)
                    if ready_hooks:
                        case.assertEqual(set(pending), {"impl", "fix", "review", "test"})

    def test_assignment_continues_after_a_default_split_interruption(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex", live=False)
        self.fixture.world.interrupt = "split"
        with redirect_stderr(io.StringIO()) as errors:
            result = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(result, worker.ExitCode.OK)
        self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())
        self.assertIn("fix", errors.getvalue())
        self.assertEqual({row.name for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)},
                         {"impl", "review", "test"})
        self.assertIsInstance(self.receipt("impl"), worker.DispatchComplete)
        splits = len(self.fixture.world.locations)
        code = worker.assign(self.fixture.parent.id, "fix", self.prompt, session_dir=self.run_dir,
            slot="fix", phase="8", kind="fix", launcher_command=["assign.sh"], reply=self.reply,
            name="fix", fix_cause="Resume the interrupted startup")
        self.assertEqual(code, worker.ExitCode.OK)
        self.assertEqual({row.name for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)},
                         {"impl", "fix", "review", "test"})
        self.assertEqual(len(self.fixture.world.locations), splits)
        self.assertEqual(self.finish("fix"), turn_end.TurnOutcome.DONE)
        self.assertEqual(len(self.fixture.world.locations), splits - 1)
        self.assertEqual(len(self.agent.submitted), 2)

    def test_named_json_add_reconciles_a_lost_acknowledgment(self) -> None:
        code, output, errors = self.command("add", "astra-designer", "--name", "astra-designer-held1", "--json",
                                            "--session-dir", str(self.run_dir))
        self.assertEqual(code, 0, errors)
        first = cast(dict[str, object], json.loads(output))
        launches = len(self.fixture.world.launches)
        code, output, errors = self.command("add", "astra-designer", "--name", "astra-designer-held1", "--json")
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), first)
        self.assertEqual(len(self.fixture.world.launches), launches)
        self.assertEqual(self.agent.submitted, [])

    def test_cli_accepts_json_launcher_arguments_and_reply_alias(self) -> None:
        code, output, errors = self.command("assign", "impl", str(self.prompt),
            "--session-dir", str(self.run_dir), "--slot", "impl", "--phase", "8", "--kind", "impl",
            "--to", "impl", "--reply-file", str(self.reply),
            "--launcher-command", json.dumps(["assign.sh", "--prompt", str(self.prompt)]))
        self.assertEqual((code, output), (0, "submitted\n"), errors)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())

    def test_cli_model_override_uses_the_selected_account_catalog(self) -> None:
        _ = write_registry(self.fixture.root / "conf/models.conf", {
            "assignments": {"direct": "claude"}, "direct.claude": {"impl": "sonnet:high"},
        })
        environment = claude_catalog_environment(self.fixture.root / "caller-account", {
            "opus": "claude-opus-caller-fixture", "sonnet": "claude-sonnet-caller-fixture",
        })
        selected = self.fixture.root / "override-account"
        _ = claude_catalog_environment(selected, {
            "opus": "claude-opus-override-fixture", "sonnet": "claude-sonnet-role-fixture",
        })
        for index, (model, expected) in enumerate((
            ("opus", "claude-opus-override-fixture"),
            ("claude-opus-saved-fixture", "claude-opus-saved-fixture"),
        )):
            name = f"override-{index}"
            with (self.subTest(model=model), patch.dict(os.environ, environment),
                  patch.object(accounts, "account_for_unscoped_launch", return_value=accounts.NamedAccount("claude2", selected)),
                  patch.object(herdr, "launch_prepared_agent", side_effect=self.fixture.world.launch_through_herdr)):
                code, output, errors = self.command("assign", "impl", str(self.prompt),
                    "--session-dir", str(self.run_dir), "--slot", "impl", "--phase", "8", "--kind", "impl",
                    "--name", name, "--reply", str(self.reply), "--model", model,
                    "--launcher-command", json.dumps(["assign.sh", "--model", model]))
                self.assertEqual((code, output), (0, "submitted\n"), errors)
                pane = self.agent.submitted[-1][0]
                self.assertEqual(self.agent.row(pane).name, name)
                arguments = self.fixture.world.foreground[pane][2]
                self.assertEqual(arguments[arguments.index("--model") + 1], expected)
                self.assertEqual(self.finish(name), turn_end.TurnOutcome.DONE)

    def test_new_review_prompt_does_not_reuse_a_completed_predecessor(self) -> None:
        for already_completed in (False, True):
            with self.subTest(already_completed=already_completed):
                name = f"review-{int(already_completed)}"
                prompt = self.run_dir / f"{name}.md"
                following = self.run_dir / f"{name}-next.md"
                worker.save_reply(prompt, "Review the requested files.")
                worker.save_reply(following, prompt.read_text())
                command = ["assign.sh", "review"]
                reply = self.run_dir / f"{name}-reply.md"
                self.agent.finish_after = 1

                def assign(path: Path) -> worker.DispatchSubmission | worker.ExitCode:
                    return worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(name), "review", path,
                        session_dir=self.run_dir, slot=name, phase="8", kind="assign",
                        launcher_command=command, reply=reply)

                self.assertIs(assign(prompt), worker.DispatchSubmission.SUBMITTED)
                pane = self.agent.submitted[-1][0]
                row = self.agent.row(pane)
                old = str(worker_panes.worker_pass(row, self.run_dir)["instance_id"])
                self.agent.complete(pane)
                if already_completed:
                    self.assertEqual(self.finish(name), turn_end.TurnOutcome.DONE)
                before = len(self.agent.submitted)
                self.assertIs(assign(following), worker.DispatchSubmission.SUBMITTED)
                self.assertEqual(len(self.agent.submitted), before + 1)
                new_pane, line = self.agent.submitted[-1]
                current = worker_panes.worker_pass(self.agent.row(new_pane), self.run_dir)
                instance = str(current["instance_id"])
                self.assertNotEqual(instance, old)
                self.assertEqual(current["prompt_file"], str(following))
                self.assertIn(f"DONE {instance}", line)

    def test_dispatch_refuses_to_replace_a_pass_that_took_the_selected_slot(self) -> None:
        self.agent.finish_after = 1
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.agent.submitted[-1][0]
        row = self.agent.row(pane)
        self.agent.complete(pane)
        previous = worker_panes.worker_pass(row, self.run_dir)
        competitor = self.run_dir / "competitor.md"
        worker.save_reply(competitor, "Run a different assignment.")
        self.prompt = self.run_dir / "requested.md"
        worker.save_reply(self.prompt, "Run the requested assignment.")
        launched_agent = worker.launched_agent
        recorded: list[str] = []

        def publish_competitor(current: staff.StaffRow) -> progress_history.CalledAgent:
            recorded.append(progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
                worker_name=row.name, worker_id=row.id, launcher_command=["different-launcher"], prompt=competitor,
                kind="impl", lens=""))
            return launched_agent(current)

        with (patch.object(worker, "launched_agent", side_effect=publish_competitor),
              redirect_stderr(io.StringIO()) as errors):
            self.assertIs(self.dispatch(), worker.ExitCode.BUSY)
        self.assertIn("Slot impl or phase 8 changed during dispatch", errors.getvalue())
        current = worker_panes.worker_pass(row, self.run_dir)
        instance, = recorded
        self.assertEqual(current["instance_id"], instance)
        self.assertNotEqual(instance, previous["instance_id"])
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["prompt_file"], str(competitor))
        self.assertEqual(len(self.agent.submitted), 1)

    def test_stage_one_receipt_protects_status_from_a_delayed_launcher_failure(self) -> None:
        parent = self.fixture.parent.id
        name = staff.WorkerName.parse("review")
        ready = self.run_dir / "final.ready"
        status = self.run_dir / "review_status"
        self.agent.error = "agent_prompt_stalled"
        with (patch.dict(os.environ, {progress_history.ASSIGN_STATUS_FILE_ENV: str(status)}),
              redirect_stderr(io.StringIO())):
            result = worker.dispatch(parent, name, "review", self.prompt, session_dir=self.run_dir,
                slot=name.value, phase="8", kind="assign", launcher_command=["assign.sh"],
                early_ready=ready, reply=self.reply)
        self.assertIs(result, worker.ExitCode.UNCERTAIN)
        (pane, _), = self.agent.submitted
        row = self.agent.row(pane)
        passed = worker_panes.worker_pass(row, self.run_dir)
        instance = str(passed["instance_id"])
        self.assertNotIn("confirmed_delivery_ids", passed)

        self.agent.error = ""
        self.agent.complete(pane)
        atomic_file.write_text_atomic(ready, "ready\n")
        self.assertEqual(self.finish(name.value, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
        self.assertEqual(len(self.agent.submitted), 2)

        recorded = assign.record_worker_launch_exit(
            self.run_dir, UUID(instance), progress_history.AssignmentLaunchExit.FAILED,
        )
        self.assertEqual(recorded, progress_history.RecordedAssignmentLaunch(name.value, failure_recorded=False))
        self.assertEqual(status.read_text(), "running\n")
        passed = worker_panes.worker_pass(row, self.run_dir)
        self.assertEqual(passed["confirmed_delivery_ids"], [instance, worker_panes.stage_two_completion_id(instance)])

    def review_with_stage_one_reply(self, name: str, *, confirmed: bool) -> staff.StaffRow:
        ready = self.run_dir / f"{name}.ready"
        status = self.run_dir / f"{name}_status"
        self.agent.finish_after = 1
        self.agent.error = "" if confirmed else "agent_prompt_stalled"
        with (patch.dict(os.environ, {progress_history.ASSIGN_STATUS_FILE_ENV: str(status)}),
              redirect_stderr(io.StringIO())):
            result = worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(name), "review", self.prompt,
                session_dir=self.run_dir, slot=name, phase="8", kind="assign", launcher_command=["assign.sh"],
                early_ready=ready, reply=self.run_dir / f"{name}_reply.md")
        self.agent.error = ""
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED if confirmed else worker.ExitCode.UNCERTAIN)
        pane, _ = self.agent.submitted[-1]
        self.agent.complete(pane)
        atomic_file.write_text_atomic(ready, "ready\n")
        row = self.agent.row(pane)
        passed = worker_panes.worker_pass(row, self.run_dir)
        self.assertEqual(passed.get("confirmed_delivery_ids", []), [passed["instance_id"]] if confirmed else [])
        return row

    def test_early_review_advances_when_confirmation_status_write_fails(self) -> None:
        for confirmed, renamed in ((False, False), (True, False), (True, True)):
            with self.subTest(confirmed=confirmed, renamed=renamed):
                name = f"review-status-{int(confirmed)}-{int(renamed)}" if renamed else f"review-status-{int(confirmed)}"
                row = self.review_with_stage_one_reply(name, confirmed=confirmed)
                status = self.run_dir / f"{name}_status"
                atomic_file.write_text_atomic(status, "error\n")
                submitted = len(self.agent.submitted)
                write = atomic_file.write_text_atomic
                status_attempts = 0

                def fail_first_status_write(path: Path, text: str) -> None:
                    nonlocal status_attempts
                    if path == status and text == "running\n":
                        status_attempts += 1
                        if status_attempts == 1:
                            raise OSError("status write failed")
                    write(path, text)

                rename = self.rename_at_worker_lock(row, "renamed-review") if renamed else nullcontext()
                with (rename, patch.object(progress_history, "write_text_atomic", side_effect=fail_first_status_write),
                      redirect_stderr(io.StringIO()) as errors):
                    result = self.finish(name, turn_end.EventKind.STAGE_READY)

                self.assertEqual(result, turn_end.TurnOutcome.SUBMITTED)
                self.assertEqual(status_attempts, 2)
                self.assertIn(f"Unable to restore running status in {status}. status write failed", errors.getvalue())
                self.assertEqual(status.read_text(), "running\n")
                self.assertEqual(len(self.agent.submitted), submitted + 1)
                passed = worker_panes.worker_pass(row, self.run_dir)
                instance = str(passed["instance_id"])
                second = worker_panes.stage_two_completion_id(instance)
                self.assertEqual(passed["confirmed_delivery_ids"], [instance, second])
                self.assertEqual(progress_history.dispatch_delivery(passed).completion_id, second)

    def test_early_review_keeps_stage_one_when_confirmation_save_fails(self) -> None:
        row = self.review_with_stage_one_reply("review-recorder", confirmed=False)
        before = worker_panes.worker_pass(row, self.run_dir)
        submitted = len(self.agent.submitted)

        def fail_state_write(path: Path, _text: str) -> None:
            self.assertEqual(path, self.run_dir / unit_runs.STATE_FILE)
            raise OSError("confirmation save failed")

        with (patch.object(pass_recorder, "write_text_atomic", side_effect=fail_state_write) as writes,
              self.assertRaisesRegex(OSError, "confirmation save failed")):
            _ = self.finish(row.name, turn_end.EventKind.STAGE_READY)

        self.assertEqual(writes.call_count, 1)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir), before)
        self.assertEqual(len(self.agent.submitted), submitted)

    def test_early_review_uses_one_pass_and_two_transcript_receipts(self) -> None:
        ready = self.run_dir / "final.ready"
        atomic_file.write_text_atomic(self.reply, "The previous full report.\n")
        result = worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse("review"), "review", self.prompt,
            session_dir=self.run_dir, slot="review", phase="8", kind="review", launcher_command=["review.sh"],
            early_ready=ready, reply=self.reply)
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(self.finish("review"), turn_end.TurnOutcome.WAITING_ON_STAGE_TWO)
        self.assertEqual(len(self.agent.submitted), 1)
        atomic_file.write_text_atomic(ready, "ready\n")
        self.assertEqual(self.finish("review", turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
        first = self.agent.submitted[0][1].split("DONE ")[-1].split()[0].removesuffix(".")
        second = self.agent.submitted[1][1].split("DONE ")[-1].split()[0].removesuffix(".")
        self.assertEqual(second, first + "-2")
        row = self.agent.row(self.agent.submitted[1][0])
        passed = worker_panes.worker_pass(row, self.run_dir)
        self.assertEqual(passed["confirmed_delivery_ids"], [first, second])
        self.assertEqual(self.reply.read_text(), "The previous full report.\n")
        self.assertEqual(self.finish("review"), turn_end.TurnOutcome.DONE)
        self.assertTrue(self.reply.read_text().endswith("DONE " + second + "\n"))
        self.assertEqual(self.finish("review"), turn_end.TurnOutcome.IGNORED)
        self.assertEqual(len(self.agent.submitted), 2)

    def test_unnamed_retry_after_launcher_loss_reuses_the_busy_submitted_worker(self) -> None:
        args = ("assign", "impl", str(self.prompt), "--session-dir", str(self.run_dir),
            "--slot", "impl", "--phase", "8", "--kind", "impl", "--reply", str(self.reply),
            "--launcher-command", json.dumps(["assign.sh", "--prompt", str(self.prompt)]))
        self.agent.interrupt = "prompt"
        code, output, errors = self.command(*args)
        self.assertEqual((code, output), (8, ""))
        self.assertIn("interrupted after prompt", errors)
        submitted_pane = self.agent.submitted[0][0]
        self.fixture.world.locations[submitted_pane]["agent_status"] = "working"
        idle = self.opened("impl2")
        before = self.fixture.staff_state(), len(self.fixture.world.launches), self.fixture.world.locations.copy()
        pass_state = (self.run_dir / "progress_history_state.json").read_bytes()
        self.agent.finish_after = 1
        code, output, errors = self.command(*args)
        self.assertEqual((code, output, errors), (0, "submitted\n", ""))
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.assertNotEqual(submitted_pane, idle.pane)
        expected_panes = {pane: location for pane, location in before[2].items() if pane != submitted_pane}
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches),
                          self.fixture.world.locations.copy()), before)
        confirmed_state = self.assert_pass_confirmed(pass_state)
        original_prompt = self.prompt.read_text()
        instance = str(worker_panes.worker_pass(self.agent.row(submitted_pane), self.run_dir)["instance_id"])
        for status in ("working", "idle"):
            with self.subTest(prompt="changed content", status=status):
                self.fixture.world.locations[submitted_pane]["agent_status"] = status
                atomic_file.write_text_atomic(self.prompt, original_prompt + "Read the changed work order.\n")
                code, output, errors = self.command(*args)
                self.assertEqual((code, output), (worker.ExitCode.BUSY, ""))
                self.assertIn(f"active pass {instance}", errors)
                self.assertIn("no prompt sent", errors)
                self.assertEqual(len(self.agent.submitted), 1)
                self.assertEqual((self.run_dir / unit_runs.STATE_FILE).read_bytes(), confirmed_state)
                atomic_file.write_text_atomic(self.prompt, original_prompt)
        for changed_at in ("before lookup", "after lookup"):
            with self.subTest(prompt="changed active content and launcher", changed_at=changed_at):
                self.fixture.world.locations[submitted_pane]["agent_status"] = "idle"
                changed_args = (*args[:-1], json.dumps(["assign.sh", "--prompt", str(self.prompt), "--reply-file", str(self.reply)]))
                read_dispatch = progress_history.select_dispatch

                def change_after_lookup(session_dir: Path, *, slot: str, phase: str, role: staff.WorkerRole,
                                        prompt: Path, launcher_command: list[str],
                                        work: progress_history.DispatchWork | progress_history.DispatchWorkAtUse,
                                        target: staff.WorkerName | progress_history.DispatchTargetAtUse,
                                        ) -> pass_recorder.DispatchSelection | pass_recorder.ActiveDispatchPromptConflict:
                    result = read_dispatch(session_dir, slot=slot, phase=phase, role=role,
                                           prompt=prompt, launcher_command=launcher_command, work=work, target=target)
                    atomic_file.write_text_atomic(self.prompt, original_prompt + "Read the changed work order.\n")
                    return result

                if changed_at == "before lookup":
                    atomic_file.write_text_atomic(self.prompt, original_prompt + "Read the changed work order.\n")
                    code, output, errors = self.command(*changed_args)
                else:
                    with patch.object(progress_history, "select_dispatch", side_effect=change_after_lookup):
                        code, output, errors = self.command(*args, "--name", self.agent.row(submitted_pane).name)
                self.assertEqual((code, output), (worker.ExitCode.BUSY, ""))
                self.assertIn(f"active pass {instance}", errors)
                self.assertIn("no prompt sent", errors)
                self.assertEqual(len(self.agent.submitted), 1)
                self.assertEqual((self.run_dir / unit_runs.STATE_FILE).read_bytes(), confirmed_state)
                atomic_file.write_text_atomic(self.prompt, original_prompt)
        self.agent.complete(submitted_pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.fixture.world.locations, expected_panes)
        self.assert_pass_completed(confirmed_state)
        self.assertIn(self.agent.final, self.reply.read_text())

    def test_unnamed_retry_after_completion_returns_the_recorded_reply(self) -> None:
        args = ("assign", "impl", str(self.prompt), "--session-dir", str(self.run_dir),
            "--slot", "impl", "--phase", "8", "--kind", "impl", "--reply", str(self.reply),
            "--launcher-command", json.dumps(["assign.sh", "--prompt", str(self.prompt)]))
        self.assertEqual(self.command(*args), (0, "submitted\n", ""))
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        reply = self.reply.read_bytes()
        self.reply.unlink()
        state = (self.run_dir / "progress_history_state.json").read_bytes()
        before = self.fixture.staff_state(), len(self.fixture.world.launches), self.fixture.world.locations.copy()
        self.assertEqual(self.command(*args), (0, "submitted\n", ""))
        self.assertEqual(self.reply.read_bytes(), reply)
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches), self.fixture.world.locations.copy()), before)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), state)
        with self.subTest(prompt="changed content"):
            previous = worker_panes.worker_pass(self.agent.row(self.agent.submitted[0][0]), self.run_dir)
            atomic_file.write_text_atomic(self.prompt, "Read the changed work order and implement it.\n")
            self.assertEqual(self.command(*args), (0, "submitted\n", ""))
            self.assertEqual(len(self.agent.submitted), 2)
            current = worker_panes.worker_pass(self.agent.row(self.agent.submitted[-1][0]), self.run_dir)
            self.assertNotEqual(current["instance_id"], previous["instance_id"])
            self.assertIn(f"DONE {current['instance_id']}", self.agent.submitted[-1][1])
            self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)

    def test_role_changes_read_completion_before_recording_the_new_pass(self) -> None:
        _ = write_registry(self.fixture.root / "conf/models.conf", {
            "design_roles.claude": {"designer": "sonnet:high"},
            "design_roles.codex": {"designer": "gpt-test:low"},
        }, update=True)
        for role in ("review", "astra-designer"):
            with self.subTest(role=role):
                self.assertEqual(self.dispatch(), worker.ExitCode.OK)
                with patch.object(sys, "argv", ["pass_record.py", "finish-pass", "--session-dir", str(self.run_dir),
                                               "--status", "completed"]):
                    pass_recorder.main()
                before_roles: list[object] = []
                launch = cast(Callable[..., herdr.AgentLaunched], self.fixture.world.launch)
                def changing(*args: object, **kwargs: object) -> herdr.AgentLaunched:
                    state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
                    passes = cast(dict[str, dict[str, object]], state["pass"])
                    before_roles.append(passes["impl"]["kind"])
                    return launch(*args, **kwargs)
                with patch.object(herdr, "launch_prepared_agent", side_effect=changing):
                    self.assertEqual(self.dispatch(role=role), worker.ExitCode.OK)
                self.assertEqual(before_roles, ["impl"])
                with patch.object(sys, "argv", ["pass_record.py", "finish-pass", "--session-dir", str(self.run_dir),
                                               "--status", "completed"]):
                    pass_recorder.main()

    def test_dispatch_role_survives_a_new_phase_with_an_empty_pass_map(self) -> None:
        self.assertEqual(self.dispatch(role="review"), worker.ExitCode.OK)
        row = self.agent.row(self.agent.submitted[-1][0])
        for arguments in (["finish-pass", "--status", "completed"],
                          ["finish-phase", "--status", "completed"],
                          ["start-phase", "--phase-id", "9", "--phase-title", "Next phase"]):
            recorder = pass_recorder if arguments[0] == "finish-pass" else progress_history
            with patch.object(sys, "argv", [Path(recorder.__file__).name, *arguments, "--session-dir", str(self.run_dir)]):
                recorder.main()
        state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
        self.assertEqual(state["pass"], {})
        self.assertEqual(worker_panes.dispatch_role(row), "review")

    def test_dispatch_role_reads_the_workers_first_open_pass(self) -> None:
        opened = self.opened()
        dispatch_id = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="review",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.prompt, kind="review", lens="")
        passed = worker_panes.worker_pass(opened.row, self.run_dir)
        self.assertEqual((passed["instance_id"], passed["status"]), (dispatch_id, "active"))
        current = staff.by_id(opened.row.id)
        assert current is not None
        self.assertEqual(worker_panes.dispatch_role(current), "review")

    def test_private_run_restore_uses_finished_dispatch_history(self) -> None:
        self.assertEqual(self.command("restore"), (0, f"{self.fixture.parent.name}: workers unchanged\n", ""))
        self.assertEqual(self.dispatch(role="review"), worker.ExitCode.OK)
        row = self.agent.row(self.agent.submitted[-1][0])
        with patch.object(sys, "argv", ["pass_record.py", "finish-pass", "--status", "completed",
                                      "--session-dir", str(self.run_dir)]):
            pass_recorder.main()
        dispatch_id = str(worker_panes.worker_pass(row, self.run_dir)["instance_id"])
        private_root = self.fixture.root / "private"
        private_root.mkdir()
        self.run_dir = self.run_dir.rename(private_root / "held-run")
        _ = self.fixture.world.stop(row, self.fixture.world)
        before = len(self.fixture.world.launches)
        with patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(private_root)}):
            code, output, errors = self.command("restore")
        self.assertEqual((code, output, errors), (0,
            f"{self.fixture.parent.name}: workers restored\n"
            + f"{row.name}: phase 8; dispatch {dispatch_id}; reply {self.reply}\n", ""))
        self.assertEqual(staff.snapshot().by_id(row.id), row)
        self.assertEqual(len(self.fixture.world.launches), before)
        self.assertNotIn(self.agent.submitted[-1][0], self.fixture.world.foreground)

    def test_herdr_list_shares_history_and_staff_reads_across_workers(self) -> None:
        expected: dict[str, str] = {}
        for name, role in (("impl", "impl"), ("review", "review"), ("test", "test")):
            opened = self.opened(name, role=cast(staff.WorkerRole, role))
            self.fixture.dispatch(opened.row, cast(staff.WorkerRole, role), completed=True)
            _ = staff.set_worker_role(opened.row.id, staff.KnownWorkerRole(cast(staff.WorkerRole, role)))
            # Preserve each finished pass in its own private run.
            saved = self.fixture.root / ("history-" + name)
            saved.mkdir()
            atomic_file.write_text_atomic(saved / "progress_history_state.json",
                (self.run_dir / "progress_history_state.json").read_text())
            expected[name] = role
        started = time.perf_counter()
        enumerated: list[Path] = []
        iterdir = Path.iterdir
        def read_directory(path: Path) -> Iterator[Path]:
            if path == self.fixture.root:
                enumerated.append(path)
            return iterdir(path)
        with patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshots, \
                patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as local_snapshots, \
                patch.object(Path, "iterdir", read_directory), \
                patch.object(agents_log, "assignments", wraps=agents_log.assignments) as roles:
            code, output, errors = self.command("list", "--json")
        elapsed = time.perf_counter() - started
        self.assertEqual(code, 0, errors)
        rows = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual({str(row["name"]): row["role"] for row in rows}, expected)
        self.assertEqual(snapshots.call_count, 1)
        self.assertEqual(local_snapshots.call_count, 0)
        self.assertEqual(enumerated, [self.fixture.root])
        self.assertEqual(roles.call_count, 0)
        print(f"herdr list with three workers: {elapsed:.6f} seconds")

    def test_assign_shares_role_history_reads_before_selecting_an_unknown_worker(self) -> None:
        for index, name in enumerate(("impl", "fix", "review", "test"), start=1):
            row = self.fixture.worker(name)
            pane = f"w1:p{index}"
            self.fixture.world.add(row, pane)
            self.fixture.world.unknown[pane] = "private process evidence is unavailable"
        enumerated: list[Path] = []
        iterdir = Path.iterdir
        def read_directory(path: Path) -> Iterator[Path]:
            if path == self.fixture.root:
                enumerated.append(path)
            return iterdir(path)
        with patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as snapshots, \
                patch.object(agents_log, "assignments", wraps=agents_log.assignments) as roles, \
                patch.object(Path, "iterdir", read_directory), redirect_stderr(io.StringIO()):
            result = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
        self.assertEqual(result, worker.ExitCode.UNCERTAIN)
        self.assertEqual(snapshots.call_count, 0)
        self.assertEqual(roles.call_count, 0)
        self.assertEqual(enumerated, [self.fixture.root])
        self.assertEqual(self.agent.submitted, [])

    def text_command(self, *args: str) -> tuple[int, list[list[str]], str]:
        code, output, errors = self.command(*args)
        lines = output.splitlines()
        self.assertEqual(re.split(r" {2,}", lines[0].strip()),
                         ["#", "Name", "Account", "Activity", "Pane", "State", "Waiting on"])
        return code, [re.split(r" {2,}", line.strip()) for line in lines[1:]], errors

    def recorded_list_fixture(self) -> tuple[staff.StaffRow, staff.StaffRow, list[Path]]:
        first = self.fixture.worker("same-name")
        manager = fixture_member("showrunner", name="TEST-other-manager")
        second = fixture_worker("same-name", manager.id)
        _ = self.enterContext(patch.object(detached_run_process, "runs_command", return_value=True))
        paths: list[Path] = []
        for row, owner, status, family in (
                (first, self.fixture.parent, "active", "codex"),
                (second, manager, "completed", "claude")):
            run = self.fixture.root / ("recorded-" + owner.name)
            path = run / unit_runs.STATE_FILE
            atomic_file.write_text_atomic(path, json.dumps({"owner_staff_id": owner.id, "pass": {
                row.name: {"worker_id": row.id, "worker_name": row.name, "kind": "review", "status": status,
                           "activity": "Check the saved work.", "started_at": 10, "called_agent": {"family": family},
                           "launcher_pid": 101, "launcher_command": ["bash", "assign.sh"]}}}))
            paths.append(path)
            _ = staff.set_worker_role(row.id, staff.KnownWorkerRole("review"))
            _ = staff.set_home(row.id, staff.SessionHome(Path.home() / (".codex2" if family == "codex" else ".claude2")))
        atomic_file.write_text_atomic(accounts.groups_file(), json.dumps({"units": "claude2"}))
        atomic_file.write_text_atomic(accounts.accounts_config_file(), json.dumps({"workers": "codex2"}))
        return first, second, paths

    def test_list_all_uses_staff_role_and_home_without_account_sources(self) -> None:
        first, second, paths = self.recorded_list_fixture()
        for member, role, folder in ((first, "fix", ".codex3"), (second, "test", ".claude4")):
            _ = staff.set_worker_role(member.id, staff.KnownWorkerRole(cast(staff.WorkerRole, role)))
            _ = staff.set_home(member.id, staff.SessionHome(self.fixture.root / folder))
        for path in paths:
            path.unlink()
        history = self.run_dir / "history.jsonl"
        atomic_file.write_text_atomic(history, "Unreadable history.\n")
        original_open = cast(Callable[..., object], Path.open)
        def unreadable(path: Path, *args: object, **kwargs: object) -> object:
            if path == history:
                raise PermissionError("TEST history is unreadable")
            return original_open(path, *args, **kwargs)
        with (patch.object(accounts, "read_role_map", side_effect=AssertionError("no role map")),
              patch.object(accounts, "roles_history_file", side_effect=AssertionError("no account history")),
              patch.object(Path, "open", autospec=True, side_effect=unreadable)):
            code, output, errors = self.command("list", "all", "--json", "--all-parents")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        for member, role, label in ((first, "fix", "codex3"), (second, "test", "claude4")):
            self.assertEqual(rows[member.id]["role"], role)
            self.assertEqual(rows[member.id]["home"], str(self.fixture.root / ("." + label)))
            self.assertEqual(rows[member.id]["account"], label)

    def test_list_all_groups_pane_workers_by_manager_from_records(self) -> None:
        first, second, _paths = self.recorded_list_fixture()
        code, output, errors = self.command("list", "all", "--json", "--all-parents")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual(set(rows), {first.id, second.id})
        self.assertEqual([rows[row.id]["manager"] for row in (first, second)], [self.fixture.parent.name, "TEST-other-manager"])
        self.assertEqual([rows[row.id]["state"] for row in (first, second)], ["active", "completed"])
        self.assertEqual([rows[row.id]["work"] for row in (first, second)], ["busy", "idle"])
        self.assertEqual([rows[row.id]["account"] for row in (first, second)], ["codex2", "claude2"])
        for row, label in ((first, "codex2"), (second, "claude2")):
            self.assertEqual(rows[row.id]["role"], "review")
            self.assertEqual(rows[row.id]["activity"], "Check the saved work.")
            self.assertEqual(rows[row.id]["home"], str(Path.home() / ("." + label)))
        self.assertEqual(self.text_command("list", "all", "--all-parents"),
                         (0, [["1", "same-name", "codex2", "review", "-", "busy", "-"],
                              ["2", "same-name", "claude2", "review", "-", "idle", "-"]], ""))
        for hold, state in ((staff.Paused("TEST", "TEST", "Hold the worker."), "paused"),
                            (staff.Exited("TEST", "TEST", "Exit the worker."), "exited")):
            with self.subTest(state=state):
                _ = staff.set_hold(first.id, hold)
                self.assertEqual(self.text_command("list", "all", "--all-parents"),
                                 (0, [["1", "same-name", "codex2", "review", "-", state, "-"],
                                      ["2", "same-name", "claude2", "review", "-", "idle", "-"]], ""))
        _ = staff.set_hold(first.id, staff.Active())
        record = staff.local_snapshot()
        cases: tuple[tuple[worker_panes.WorkerDispatchState | OSError | ValueError, str, str], ...] = (
            (worker_panes.ReleasedWorkerPass.NO_PROMPT_DELIVERED, "no prompt was delivered", "idle"),
            (worker_panes.ReleasedWorkerPass.STALE_ACTIVE, "the launcher ended without finishing the pass", "idle"),
            (worker_panes.ReleasedWorkerPass.ENDED, "the pass ended", "idle"),
            (worker_panes.DispatchHoldsWorker("a detached run is live"), "active", "busy"),
            (worker_panes.CompletedWorkerPass("TEST-dispatch"), "completed", "idle"),
            (OSError("TEST state unreadable\nsecond line"), "active", "busy"),
            (ValueError("TEST state invalid\nsecond line"), "active", "busy"),
        )
        for result, state, work in cases:
            with self.subTest(result=result):
                with (patch.object(worker_panes, "worker_dispatch_state",
                                   side_effect=[result, worker_panes.ReleasedWorkerPass.ENDED]) as classified,
                      redirect_stderr(io.StringIO()) as anomalies):
                    listing = {str(row["id"]): row for row in worker.list_all_workers(record)}
                self.assertEqual((listing[first.id]["state"], listing[first.id]["work"]), (state, work))
                self.assertEqual((listing[second.id]["state"], listing[second.id]["work"]), ("completed", "idle"))
                self.assertEqual([call.args[0] for call in classified.call_args_list],
                                 [record.by_id(first.id), record.by_id(second.id)])
                self.assertEqual(anomalies.getvalue().splitlines(),
                    [f"anomaly: {first.name}: {' '.join(str(result).splitlines())}"]
                    if isinstance(result, (OSError, ValueError)) else [])
        parent = record.parent_of(first)
        assert parent is not None
        for machine, expected in (
                ("natedev", [["1", "same-name", "codex2", "review", "-", "busy", "-"],
                             ["2", "same-name", "claude2", "review", "-", "idle", "-"]]),
                ("mac", [["1", "same-name", "codex2 (mac)", "review", "-", "busy", "-"],
                         ["2", "same-name", "claude2 (—)", "review", "-", "idle", "-"]])):
            with self.subTest(machine=machine):
                current = staff.by_id(parent.id)
                assert current is not None
                _ = staff.set_worktree(parent.id, staff.PresentWorktree(machine, self.fixture.root / parent.name), expected=current.worktree)
                self.assertEqual(self.text_command("list", "all", "--all-parents"), (0, expected, ""))

    def test_list_all_keeps_a_refused_pass_over_an_equal_time_start_event(self) -> None:
        first, _second, paths = self.recorded_list_fixture()
        state = cast(dict[str, object], json.loads(paths[0].read_text()))
        slots = cast(dict[str, dict[str, object]], state["pass"])
        slots[first.name]["status"] = "refused"
        history = self.fixture.root / "refused-history.jsonl"
        state["history_file"] = str(history)
        atomic_file.write_text_atomic(paths[0], json.dumps(state))
        atomic_file.write_text_atomic(history, json.dumps({
            "event_type": "pass_started", "worker_id": first.id, "worker_name": first.name,
            "pass_kind": "review", "pass_activity": "Start the work.", "timestamp_epoch": 10,
            "called_agent": {"family": "codex"}}) + "\n")
        code, output, errors = self.command("list", "all", "--json", "--all-parents")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual((rows[first.id]["state"], rows[first.id]["work"], rows[first.id]["activity"]),
                         ("refused", "unknown", "Check the saved work."))

    def test_list_all_keeps_valid_history_events_beside_an_incomplete_line(self) -> None:
        first, second, paths = self.recorded_list_fixture()
        history = self.fixture.root / "saved-history.jsonl"
        for path in paths[:2]:
            state = cast(dict[str, object], json.loads(path.read_text()))
            state["history_file"] = str(history)
            state["pass"] = {}
            atomic_file.write_text_atomic(path, json.dumps(state))
        events = [{"event_type": "pass_finished", "worker_id": row.id, "worker_name": row.name,
                   "pass_kind": role, "status": "completed", "pass_activity": activity,
                   "timestamp_epoch": stamp, "called_agent": {"family": family}}
                  for row, role, activity, stamp, family in (
                      (first, "impl", "Earlier work.", 1, "codex"),
                      (first, "fix", "First saved activity.", 20, "codex"),
                      (second, "review", "Second saved activity.", 30, "claude"))]
        bad = '{"event_type":"pass_finished","worker_id":"' + str(first.id) + '",broken}\n'
        atomic_file.write_text_atomic(history, json.dumps(events[0]) + "\n" + bad
            + "".join(json.dumps(event) + "\n" for event in events[1:]) + bad)
        original_open = Path.open
        with (patch.object(json, "loads", wraps=json.loads) as decoded,
              patch.object(Path, "open", autospec=True, side_effect=original_open) as opened):
            code, output, errors = self.command("list", "all", "--json", "--all-parents")
        self.assertEqual(code, 0, errors)
        self.assertEqual(sum(isinstance(cast(object, call.args[0]), bytes) for call in decoded.call_args_list), 3)
        self.assertEqual(sum(cast(tuple[object, ...], call.args)[0] == history for call in opened.call_args_list), 1)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual([rows[row.id]["run_path"] for row in (first, second)],
                         [str(path.parent) for path in paths])
        self.assertEqual([rows[row.id]["state"] for row in (first, second)], ["completed", "completed"])
        self.assertEqual([rows[row.id]["role"] for row in (first, second)], ["review", "review"])
        self.assertEqual([rows[row.id]["activity"] for row in (first, second)],
                         ["First saved activity.", "Second saved activity."])

    def test_list_all_resolves_a_fixed_manager_id_after_its_session_changes(self) -> None:
        first, _second, paths = self.recorded_list_fixture()
        state = cast(dict[str, object], json.loads(paths[0].read_text()))
        self.assertEqual(state["owner_staff_id"], self.fixture.parent.id)
        state["main_agent"] = {}
        state["pass"] = {}
        history = self.fixture.root / "legacy-history.jsonl"
        state["history_file"] = str(history)
        atomic_file.write_text_atomic(paths[0], json.dumps(state))
        atomic_file.write_text_atomic(history, json.dumps({
            "event_type": "pass_finished", "worker_id": first.id, "pass_kind": "fix",
            "pass_activity": "Repair the saved work.", "status": "completed", "timestamp_epoch": 20,
            "called_agent": {"family": "codex"}}) + "\n")
        _ = staff.update_current_session(self.fixture.parent.current_session, staff.StaffId(fixture_runtime_id("TEST-new-manager-session")))
        code, output, errors = self.command("list", "all", "--json", "--all-parents")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual((rows[first.id]["state"], rows[first.id]["role"], rows[first.id]["activity"]),
                         ("completed", "review", "Repair the saved work."))

    def test_list_all_bounds_history_without_a_uuid_creation_time(self) -> None:
        first, _second, paths = self.recorded_list_fixture()
        absent = fixture_worker("absent-worker", self.fixture.parent.id,
                                runtime_id="00000000-0000-4000-8000-000000000001")
        ancient = self.fixture.worker("old-history-worker")
        other = self.fixture.worker("other-history-worker")
        state = cast(dict[str, object], json.loads(paths[0].read_text()))
        history = self.fixture.root / "bounded-history.jsonl"
        state["history_file"] = str(history)
        state["pass"] = {}
        atomic_file.write_text_atomic(paths[0], json.dumps(state))
        other_history = self.fixture.root / "newer-history.jsonl"
        other_run = self.fixture.root / "newer-run"
        atomic_file.write_text_atomic(other_run / unit_runs.STATE_FILE, json.dumps({
            "owner_staff_id": self.fixture.parent.id, "history_file": str(other_history), "pass": {}}))
        atomic_file.write_text_atomic(other_history, json.dumps({"event_type": "pass_started", "worker_id": other.id,
            "pass_kind": "review", "pass_activity": "Other work.", "timestamp_epoch": 200,
            "called_agent": {"family": "codex"}}) + "\n")
        old = {"event_type": "pass_finished", "worker_id": ancient.id, "pass_kind": "impl",
               "status": "completed", "timestamp_epoch": 1}
        latest = {**old, "worker_id": first.id, "pass_kind": "fix", "timestamp_epoch": 20, "pass_activity": "Latest work."}
        content = (json.dumps(old) + "\n" + (json.dumps({"event_type": "pass_finished", "timestamp_epoch": 5,
                   "worker_id": fixture_runtime_id("unrelated-worker"), "text": "Earlier progress." * 100}) + "\n")
                   * 200 + json.dumps(latest) + "\n").encode()
        positions: list[int] = []
        lengths: list[int] = []
        class HistoryReads(io.BytesIO):
            @override
            def read(self, size: int | None = -1) -> bytes:
                positions.append(self.tell())
                data = super().read(size)
                lengths.append(len(data))
                return data
        original = cast(Callable[..., object], Path.open)
        def opened(path: Path, *args: object, **kwargs: object) -> object:
            return HistoryReads(content) if path == history else original(path, *args, **kwargs)
        def check_lock(row: staff.StaffRow, *_args: object) -> worker_panes.CloseAllowed | worker_panes.Kept:
            return worker_panes.Kept("TEST dispatch lock") if row.id == other.id else worker_panes.CloseAllowed()
        with (patch.object(Path, "open", autospec=True, side_effect=opened),
              patch.object(worker_panes, "check_worker_lock", side_effect=check_lock),
              patch.object(json, "loads", wraps=json.loads) as decoded):
            code, output, errors = self.command("list", "all", "--json", "--all-parents")
        self.assertEqual(code, 0, errors)
        self.assertLessEqual(sum(lengths), 64 * 1024 + 1)
        self.assertEqual(sum(isinstance(cast(object, call.args[0]), bytes) for call in decoded.call_args_list), 2)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual((rows[first.id]["role"], rows[first.id]["activity"]), ("review", "Latest work."))
        self.assertEqual((rows[first.id]["work"], rows[first.id]["state"]), ("idle", "completed"))
        self.assertEqual(rows[ancient.id]["state"], "unknown")
        self.assertEqual(rows[absent.id]["state"], "unknown")
        self.assertEqual(rows[other.id]["state"], "active")
        self.assertEqual(rows[other.id]["activity"], "Other work.")
        self.assertTrue(positions)
        self.assertGreater(min(positions), 0)

    def test_list_all_needs_no_caller_staff_row(self) -> None:
        first, second, _ = self.recorded_list_fixture()
        for root in (None, *worker_panes.parent_run_roots()):
            with self.subTest(root=root), ExitStack() as cleanup:
                expected_errors: list[str] = []
                if root is not None:
                    run = root / "retired-owner"
                    owner = fixture_runtime_id("TEST-retired-owner")
                    path = run / unit_runs.STATE_FILE
                    atomic_file.write_text_atomic(path, json.dumps({"owner_staff_id": owner}))
                    _ = cleanup.callback(path.unlink)
                    expected_errors.append(f"anomaly: {run.resolve()}: run owner {owner}: unit staff row is unavailable")
                with patch.object(staff, "runtime_id", side_effect=AssertionError("no caller lookup")):
                    code, output, errors = self.command("list", "all", "--json", "--all-parents")
                self.assertEqual(code, 0, errors)
                rows = cast(list[dict[str, object]], json.loads(output))
                self.assertEqual(len(rows), 2)
                self.assertEqual({row["id"] for row in rows}, {first.id, second.id})
                self.assertEqual(errors.splitlines(), expected_errors)

    def test_list_all_reads_each_record_once_without_live_calls(self) -> None:
        _first, _second, paths = self.recorded_list_fixture()
        reads: list[Path] = []
        original = Path.read_text
        def read(path: Path, *args: object, **kwargs: object) -> str:
            reads.append(path)
            return original(path, *args, **kwargs)  # pyright: ignore[reportArgumentType]
        with (patch.object(Path, "read_text", side_effect=None, autospec=True) as reader,
              patch.object(staff, "snapshot", side_effect=AssertionError("use the local record")),
              patch.object(staff, "_found_backends", side_effect=AssertionError("no backend scan")),
              patch.object(sessions, "running_sessions", side_effect=AssertionError("no process scan")),
              patch.object(herdr, "Herdr", side_effect=AssertionError("no pane read")),
              patch.object(herdr, "codex_conversations", side_effect=AssertionError("no thread scan")),
              patch.object(subprocess, "run", side_effect=AssertionError("no live command"))):
            reader.side_effect = read
            code, output, errors = self.command("list", "all", "--json", "--all-parents")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(cast(list[object], json.loads(output))), 2)
        for path in (*paths, staff.record_path()):
            self.assertEqual(reads.count(path), 1, str(path))
        for path in (accounts.accounts_config_file(), accounts.groups_file()):
            self.assertEqual(reads.count(path), 0, str(path))


    def test_list_header_names_and_aligns_each_column(self) -> None:
        _ = self.opened("impl")
        _ = self.opened("reviewer", "review")
        code, output, errors = self.command("list")
        self.assertEqual(code, 0, errors)
        lines = output.splitlines()
        headers = ["#", "Name", "Account", "Activity", "Pane", "State", "Waiting on"]
        self.assertEqual(re.split(r" {2,}", lines[0].strip()), headers)
        starts = [lines[0].index(title) for title in headers]
        for line in lines[1:]:
            for offset in starts:
                self.assertNotEqual(line[offset], " ")

    def test_list_activity_uses_the_active_dispatch_kind_then_the_staff_role(self) -> None:
        opened = self.opened("impl")
        _ = staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("impl"))
        self.fixture.dispatch(opened.row, "review")
        code, rows, errors = self.text_command("list")
        self.assertEqual(code, 0, errors)
        self.assertEqual(rows[0][3], "review")
        for hold in (staff.Paused("TEST", "TEST", "Hold the worker."),
                     staff.Exited("TEST", "TEST", "Exit the worker.")):
            _ = staff.set_hold(opened.row.id, hold)
            for scope in ((), ("--all-parents",)):
                with self.subTest(hold=type(hold).__name__, scope=scope):
                    code, rows, errors = self.text_command("list", *scope)
                    self.assertEqual(code, 0, errors)
                    self.assertEqual(rows[0][3], "review")
        self.fixture.dispatch(opened.row, "review", completed=True)
        self.assertEqual(self.text_command("list")[1][0][3], "impl")

    def test_list_waiting_on_uses_live_detached_run_labels_only(self) -> None:
        opened = self.opened("impl")
        other = self.opened("test", "test")
        self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
        self.fixture.dispatch(opened.row, "impl")
        run = worker_panes.worker_run(opened.row, record=staff.snapshot())
        directory = run / worker_lifecycle.DETACHED_RUNS_DIR
        for run_id, name, status, pid, label in (
                ("live", opened.row.name, "running", 101, "TEST suite"),
                ("multiline", opened.row.name, "running", 101, "TEST build\ncontinued"),
                ("finished", opened.row.name, "finished", 101, "TEST finished"),
                ("dead", opened.row.name, "running", 102, "TEST dead"),
                ("other", "another-worker", "running", 101, "TEST other worker")):
            atomic_file.write_text_atomic(directory / (run_id + ".json"), json.dumps({
                "run_id": run_id, "worker": name,
                "staff_id": opened.row.id if name == opened.row.name else fixture_runtime_id("another-worker"),
                "status": status, "pid": pid, "label": label}))
        for alive, hold, expected, location, activity in (
                (True, "", "busy", "known", "idle"), (False, "", "idle", "known", "idle"),
                (True, "pause", "paused", "known", "idle"), (True, "exit", "exited", "known", "idle"),
                (True, "", "busy", "known", "starting"), (False, "", "busy", "known", "starting"),
                (True, "pause", "paused", "known", "starting"), (True, "exit", "exited", "known", "starting"),
                (True, "", "busy", "completed pass", "idle"), (True, "", "busy", "saved dispatch", "idle")):
            self.fixture.world.locations[opened.pane]["agent_status"] = activity
            if location == "completed pass":
                self.fixture.dispatch(opened.row, "impl", completed=True)
            if location == "saved dispatch":
                outside = self.fixture.root / "private" / "saved-dispatch"
                outside.mkdir(parents=True)
                atomic_file.write_text_atomic(outside / "progress_history_state.json",
                    (self.run_dir / "progress_history_state.json").read_text())
                self.run_dir = outside
                _ = staff.set_hold(opened.row.id, staff.Active())
                self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
                self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
                saved_directory = outside / worker_lifecycle.DETACHED_RUNS_DIR
                saved_directory.mkdir()
                for path in directory.glob("*.json"):
                    _ = path.replace(saved_directory / path.name)
                directory = saved_directory
            _ = staff.set_hold(opened.row.id, staff.Exited("TEST", "TEST", "Exit the worker.") if hold == "exit"
                               else staff.Paused("TEST", "TEST", "Hold the worker.") if hold == "pause" else staff.Active())
            def supervises(pid: int, path: Path) -> bool:
                del path
                return alive and pid == 101
            supervisors = {path.resolve(): frozenset({101}) for path in directory.glob("*.json")} if alive else {}
            with (patch.object(detached_run_process, "supervises", side_effect=supervises),
                  patch.object(detached_run_process, "supervisor_records", return_value=supervisors)):
                for scope in ((), ("--all-parents",)):
                    with (self.subTest(alive=alive, hold=hold, scope=scope, location=location, activity=activity),
                          patch.dict(os.environ, {"UNIT_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": ""}),
                          patch.object(worker_lifecycle, "read_json_object", wraps=worker_lifecycle.read_json_object) as reads):
                        code, rows, errors = self.text_command("list", *scope)
                        self.assertEqual(code, 0, errors)
                        self.assertEqual(len(rows), 2)
                        cells = {row[1]: row for row in rows}
                        self.assertEqual(cells[opened.row.name][-1], "TEST suite, TEST build continued" if alive else "-")
                        self.assertEqual(cells[other.row.name][-1], "-")
                        self.assertEqual(cells[opened.row.name][5], expected)
                        # Only records that a live supervisor names are read, each once.
                        for path in directory.glob("*.json"):
                            self.assertEqual(sum(call.args == (path,) for call in reads.call_args_list), int(alive), str(path))
                        if location == "completed pass":
                            code, output, errors = self.command("list", "--json", *scope)
                            self.assertEqual(code, 0, errors)
                            listing = cast(list[dict[str, object]], json.loads(output))
                            cell = next(row for row in listing if row["id"] == opened.row.id)
                            self.assertEqual(cell["work"], "busy")

    def test_list_all_sorts_open_panes_first_and_preserves_each_group_order(self) -> None:
        closed = [self.fixture.worker(name) for name in ("alpha", "gamma")]
        opened = [self.opened(name).row for name in ("beta", "zeta")]
        order = staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)
        expected = [row.name for group in (opened, closed) for row in order if row.id in {item.id for item in group}]
        code, rows, errors = self.text_command("list", "all")
        self.assertEqual(code, 0, errors)
        self.assertEqual([row[1] for row in rows], expected)
        self.assertEqual([row[4] for row in rows], ["open", "open", "closed", "closed"])

    def test_list_filters_open_closed_and_all_in_text_and_json(self) -> None:
        closed = self.fixture.worker("closed")
        opened = self.opened("opened").row
        for scope in ((), ("--all-parents",)):
            for selection, expected in (((), {opened.name}), (("open",), {opened.name}),
                                        (("closed",), {closed.name}), (("all",), {closed.name, opened.name})):
                with self.subTest(selection=selection, scope=scope):
                    code, rows, errors = self.text_command("list", *selection, *scope)
                    self.assertEqual(code, 0, errors)
                    self.assertEqual({row[1] for row in rows}, expected)
                    code, output, errors = self.command("list", *selection, "--json", *scope)
                    self.assertEqual(code, 0, errors)
                    records = cast(list[dict[str, object]], json.loads(output))
                    self.assertEqual({str(row["name"]) for row in records}, expected)
                    for row in records:
                        self.assertNotIn("waiting_on", row)
                        self.assertNotIn("list_activity", row)

    def test_text_list_numbers_rows_and_keeps_the_worker_fields(self) -> None:
        self.assertEqual(self.text_command("list"), (0, [], ""))
        self.assertEqual(self.dispatch("impl01"), worker.ExitCode.OK)
        self.assertEqual(self.finish("impl01"), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.text_command("list", "all"),
                         (0, [["1", "impl01", "-", "impl", "closed", "-", "-"]], ""))
        for number in range(2, 11):
            self.assertEqual(self.dispatch(f"impl{number:02}"), worker.ExitCode.OK)
            self.assertEqual(self.finish(f"impl{number:02}"), turn_end.TurnOutcome.DONE)
        code, output, errors = self.text_command("list", "all")
        self.assertEqual(code, 0, errors)
        members = staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)
        numbers = [str(number) for number in range(1, 11)]
        self.assertEqual(output, [[number, row.name, "-", "impl", "closed", "-", "-"]
                                 for number, row in zip(numbers, members, strict=True)])
        cases = (("idle", "idle"), ("working", "busy"), ("stopped", "exited"),
                 ("paused", "paused"), ("exited", "exited"), ("unknown", "unknown"))
        columns = [["-", "impl", "closed", "-", "-"] for _ in members]
        _ = staff.set_home(members[6].id, staff.SessionHome(self.fixture.root / ".codex2"))
        for index, (status, expected) in enumerate(cases):
            row = members[index]
            pane = f"w1:list{index}"
            self.fixture.world.add(row, pane, live=status != "stopped", family="codex")
            self.fixture.world.locations[pane]["agent_status"] = status if status in ("working", "unknown") else "idle"
            if status == "paused":
                _ = staff.set_hold(row.id, staff.Paused("TEST", "TEST", "Hold the worker."))
            elif status == "exited":
                _ = staff.set_hold(row.id, staff.Exited("TEST", "TEST", "Exit the worker."))
            if index == 0:
                _ = staff.set_home(row.id, staff.SessionHome(self.fixture.root / ".codex2"))
            columns[index] = ["codex2" if index == 0 else "codex", "impl", "open", expected, "-"]
        code, output, errors = self.text_command("list", "all")
        self.assertEqual(code, 0, errors)
        self.assertEqual(output, [[number, row.name, *column]
                                 for number, row, column in zip(numbers, members, columns, strict=True)])
        self.fixture.world.add(members[0], "w1:duplicate", family="codex")
        self.fixture.world.unknown["w1:list1"] = "TEST process is unreadable"
        code, output, errors = self.text_command("list", "all")
        self.assertEqual(code, 0, errors)
        rows = {cells[1]: cells[2:] for cells in output}
        self.assertEqual(rows[members[0].name], ["-", "impl", "unknown", "- (duplicate native identities; panes retained)", "-"])
        self.assertEqual(rows[members[1].name], ["-", "impl", "unknown", "- (TEST process is unreadable)", "-"])
        known: dict[str, object] = {"name": "known", "role": "impl", "account": "codex2", "pane": "open", "state": "idle"}
        unknown: dict[str, object] = {"name": "unknown", "role": {"state": "WorkerRoleUnavailable"},
                                      "account": {"state": "AccountUnavailable"}, "pane": "open", "state": "idle"}
        for fields, account, activity in (
                (unknown, "account unknown", "activity unknown"),
                ({key: value for key, value in known.items() if key != "account"}, "account unknown", "impl"),
                ({key: value for key, value in known.items() if key != "role"}, "codex2", "activity unknown")):
            with self.subTest(fields=fields), patch.object(worker, "list_workers", return_value=[known, fields]):
                self.assertEqual(self.text_command("list"),
                                 (0, [["1", "known", "codex2", "impl", "open", "idle", "-"],
                                      ["2", str(fields["name"]), account, activity, "open", "idle", "-"]], ""))
                code, output_json, errors = self.command("list", "--json")
                self.assertEqual(code, 0, errors)
                self.assertEqual(cast(list[dict[str, object]], json.loads(output_json)), [known, fields])
        code, json_before, errors = self.command("list", "all", "--json")
        self.assertEqual(code, 0, errors)
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine):
                parent = self.fixture.parent
                _ = staff.set_worktree(parent.id, staff.PresentWorktree(machine, self.fixture.root), expected=parent.worktree)
                code, output, errors = self.text_command("list", "all")
                self.assertEqual(code, 0, errors)
                account = "codex (mac)" if machine == "mac" else "codex"
                rows = {cells[1]: cells[2:] for cells in output}
                self.assertEqual(rows[members[2].name], [account, "impl", "open", "exited", "-"])
                code, output_json, errors = self.command("list", "all", "--json")
                self.assertEqual(code, 0, errors)
                self.assertEqual(json.loads(output_json), json.loads(json_before))

    def test_list_keeps_dispatch_role_separate_from_observation(self) -> None:
        opened = self.opened("impl2", "review")
        self.fixture.dispatch(opened.row, "review", completed=True)
        _ = staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("review"))
        code, output, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual(listing[0]["name"], "impl2")
        self.assertEqual(listing[0]["role"], "review")
        self.assertEqual(listing[0]["state"], "OpenWorker")
        self.assertEqual(listing[0]["activity"], "done")
        self.assertEqual({key: listing[0][key] for key in
                          ("dispatch_id", "run_path", "completion_state", "reply_path")},
                         {"dispatch_id": "dispatch-impl2", "run_path": str(self.fixture.root / "run"),
                          "completion_state": "completed", "reply_path": ""})

    def test_list_open_busy_worker_reports_work_account_and_home(self) -> None:
        row = self.fixture.worker("astra-designer", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        self.fixture.world.locations["w1:p1"]["agent_status"] = "working"
        self.fixture.dispatch(row, "astra-designer")
        home = self.fixture.root / ".codex2"
        _ = staff.set_worker_role(row.id, staff.KnownWorkerRole("astra-designer"))
        _ = staff.set_home(row.id, staff.SessionHome(home))
        with patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex2", home)) as selected:
            code, output, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["state"], "OpenWorker")
        self.assertEqual(listing[0]["role"], "astra-designer")
        self.assertEqual({key: listing[0].get(key) for key in ("work", "account", "home")},
                         {"work": "busy", "account": "codex2", "home": str(home)})
        selected.assert_not_called()

    def test_list_closed_worker_reports_work_account_and_home(self) -> None:
        row = self.fixture.worker("astra-reviewer", family="claude")
        self.fixture.dispatch(row, "astra-reviewer", completed=True)
        home = self.fixture.root / ".claude2"
        _ = staff.set_worker_role(row.id, staff.KnownWorkerRole("astra-reviewer"))
        _ = staff.set_home(row.id, staff.SessionHome(home))
        with patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude2", home)) as selected:
            code, output, errors = self.command("list", "all", "--json")
        self.assertEqual(code, 0, errors)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["state"], "ClosedWorker")
        self.assertEqual(listing[0]["role"], "astra-reviewer")
        self.assertEqual({key: listing[0].get(key) for key in ("work", "account", "home")},
                         {"work": "idle", "account": "claude2", "home": str(home)})
        selected.assert_not_called()

    def test_list_worker_without_backend_reports_account_unavailable(self) -> None:
        row = staff.ensure_worker(staff.native_id(fixture_runtime_id("missing-backend")),
                                  "missing-backend", self.fixture.parent.id)
        self.fixture.dispatch(row, "impl", completed=True)
        self.assertNotIn(row.id, staff.snapshot().backends)
        code, output, errors = self.command("list", "all", "--json")
        self.assertEqual(code, 0, errors)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["account"],
                         {"state": "AccountUnavailable", "reason": "worker home is unavailable"})
        self.assertNotIn("home", listing[0])
        self.assertEqual(listing[0]["role"],
                         {"state": "WorkerRoleUnavailable", "reason": "dispatch role is unavailable"})
        code, output, errors = self.text_command("list", "all")
        self.assertEqual(code, 0, errors)
        self.assertEqual(output[0][3], "activity unknown")
        self.assertEqual(output, [["1", "missing-backend", "-", "activity unknown", "closed", "-", "-"]])
        self.assertNotIn("{", str(output))

    def test_restore_retains_the_same_native_session_and_reruns_unchanged(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "impl", completed=True)
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        self.assertEqual(self.command("restore"), (0, f"{self.fixture.parent.name}: workers restored\n", ""))
        code, output, errors = self.command("restore")
        self.assertEqual((code, output, errors), (0, f"{self.fixture.parent.name}: workers unchanged\n", ""))
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches)), before)
        self.assertNotIn(opened.pane, self.fixture.world.locations)
        self.assertEqual(staff.snapshot().by_id(opened.row.id), opened.row)
        self.assertNotIn(opened.pane, self.fixture.world.foreground)

    def test_restore_uses_the_known_row_role_without_history(self) -> None:
        for family, name in (("codex", "impl"), ("claude", "claude-impl")):
            with self.subTest(family=family):
                _ = write_registry(self.fixture.root / "conf/models.conf", {
                    "assignments": {"direct": family}, "direct.claude": {"impl": "sonnet:high"},
                })
                opened = self.opened(name)
                _ = staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("impl"))
                if family == "claude":
                    source = worker_admission.transcript_source(opened.row, staff.ClaudeBackend())
                    atomic_file.write_text_atomic(source, json.dumps({"type": "assistant", "effort": "xhigh",
                        "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "Ready."}]}}) + "\n")
                _ = self.fixture.world.stop(opened.row, self.fixture.world)
                active = progress_history.ActiveDispatch("saved-pass", ("assign.sh",))
                launches = len(self.fixture.world.launches)
                with (patch.object(progress_history, "last_dispatch", return_value=progress_history.NoDispatchRecorded()),
                      patch.object(progress_history, "active_dispatch", return_value=active),
                      patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK)):
                    code, _output, errors = self.command("restore")
                self.assertEqual(code, 0, errors)
                self.assertEqual(len(self.fixture.world.launches), launches + 1)
                if family == "claude":
                    _, arguments, _ = self.fixture.world.requests[-1]
                    self.assertEqual(arguments[:2], ["--resume", opened.row.current_session])
                    self.assertEqual(arguments[arguments.index("--model") + 1], "claude-opus-5-5")
                    self.assertEqual(arguments[arguments.index("--effort") + 1], "xhigh")

    def test_restore_reports_the_unavailable_role_and_continues_other_workers(self) -> None:
        missing = self.opened("impl")
        retained = self.opened("review", "review")
        _ = self.fixture.world.stop(missing.row, self.fixture.world)
        _ = self.fixture.world.stop(retained.row, self.fixture.world)
        self.fixture.dispatch(retained.row, "review", completed=True)
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        self.assertEqual(self.command("restore"), (0, f"{self.fixture.parent.name}: workers restored\n", ""))
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches)), before)
        self.assertNotIn(retained.pane, self.fixture.world.foreground)
        self.assertNotIn(missing.pane, self.fixture.world.foreground)

    def test_end_closes_a_busy_worker_and_retires_its_row(self) -> None:
        for reused in (False, True):
            with self.subTest(reused=reused):
                opened = self.opened("reuse-target" if reused else "impl")
                self.fixture.world.locations[opened.pane]["agent_status"] = "working"
                self.fixture.completed_dispatch(opened.row, "impl")
                replacement = self.opened("other-worker") if reused else None
                if replacement is not None:
                    rename = self.rename_at_worker_lock(opened.row, "renamed-target", replacement=replacement.row)
                else:
                    rename = nullcontext()
                closed = list(self.fixture.world.closed)
                with (rename, patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
                      patch("scripts.production.staff_sources.require_worker_stopped")):
                    code, output, errors = self.command("end", opened.row.name)
                if replacement is not None:
                    self.assertEqual((code, output), (worker.ExitCode.KEPT, ""))
                    self.assertIn("the worker changed before end", errors)
                    self.assertEqual(staff.by_id(opened.row.id), replace(opened.row, name="renamed-target"))
                    self.assertEqual(staff.by_id(replacement.row.id), replace(replacement.row, name=opened.row.name))
                    self.assertEqual(self.fixture.world.closed, closed)
                    self.assertIn(opened.pane, self.fixture.world.locations)
                    self.assertIn(replacement.pane, self.fixture.world.locations)
                else:
                    self.assertEqual((code, output, errors), (0, "done\n", ""))
                    self.assertIsNone(staff.by_id(opened.row.id))
                    self.assertNotIn(opened.pane, self.fixture.world.locations)
                self.assertEqual(self.fixture.world.stops, [])

    def test_end_removes_an_idle_worker_with_an_interrupted_pass(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "impl")
        state = self.run_dir / "progress_history_state.json"
        atomic_file.write_text_atomic(state, state.read_text().replace('"active"', '"interrupted"'))
        self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()), \
             patch("scripts.production.staff_sources.require_worker_stopped"):
            self.assertEqual(worker.end(self.fixture.parent.id, staff.WorkerName.parse("impl")), worker.ExitCode.OK)
        self.assertIsNone(staff.by_id(opened.row.id))
        self.assertNotIn(opened.pane, self.fixture.world.locations)
        self.assertIn("w1:p0", self.fixture.world.locations)

    def test_end_removes_only_the_confirmed_closed_worker(self) -> None:
        opened = self.opened()
        self.fixture.completed_dispatch(opened.row, "impl")
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()), \
             patch("scripts.production.staff_sources.require_worker_stopped"):
            self.assertEqual(worker.end(self.fixture.parent.id, staff.WorkerName.parse("impl")), worker.ExitCode.OK)
        self.assertIsNone(staff.by_id(opened.row.id))
        self.assertNotIn(opened.pane, self.fixture.world.locations)
        self.assertIn("w1:p0", self.fixture.world.locations)
        self.assertEqual(worker.end(self.fixture.parent.id, staff.WorkerName.parse("impl")), worker.ExitCode.OK)

        for pending in ("finished and delivered", "legacy finish at the preceding second"):
            with self.subTest(pending=pending):
                self.check_end_with_delivered_result(pending)
        for failure in ("notice send fails", "role release fails"):
            with self.subTest(failure=failure):
                self.check_end_retries_completion(failure)
        with self.subTest(pending="delivered STOPPED notice after restore"):
            self.check_end_after_delivered_stop(late_done=False)
        with self.subTest(pending="verified late DONE after restore"):
            self.check_end_after_delivered_stop(late_done=True)

    def check_end_after_delivered_stop(self, *, late_done: bool) -> None:
        name = staff.WorkerName.parse("late-done-after-stop" if late_done else "restored-after-stop")
        parent = self.fixture.parent.id
        self.agent.finish_after = 1
        self.assertIs(self.dispatch(name.value), worker.DispatchSubmission.SUBMITTED)
        pane, _ = self.agent.submitted[-1]
        row = self.agent.row(pane)
        passed = worker_panes.worker_pass(row, self.run_dir)
        instance = str(passed["instance_id"])
        notice = progress_history.stop_dispatch(self.run_dir, str(passed["team_slot"]), instance,
                                               row, parent, run_id="stopped-run", reason="Stop the worker pass.")
        self.assertIsInstance(notice, progress_history.StoppedPassNotice)
        self.agent.complete(pane)
        run_id = f"{name.value}-result"
        path, record = self.detached_record(row, run_id)
        record.update(pane_dispatch_id=instance, delivery=f"sent: worker {row.name} at {pane}")
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.agent.append(pane, "user", f"Detached run: {run_id}")

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertIs(worker.restore(parent), worker.ExitCode.OK)
        self.assertNotIn(pane, self.fixture.world.locations)
        self.assertIsNotNone(staff.by_id(row.id))
        self.assertTrue(pass_notices.is_settled(self.run_dir, instance, "stopped"))
        completion = progress_history.hook_completion(worker_panes.worker_pass(row, self.run_dir))
        assert isinstance(completion, progress_history.HookCompletion)
        self.assertFalse(completion.finished)
        self.assertEqual(completion.outcome, "stopped")
        late_reply = self.reply.with_name(f"{self.reply.stem}_late_{instance}{self.reply.suffix}")
        self.assertFalse(late_reply.exists())
        final = f"I read the last result.\nDONE {instance}"
        if late_done:
            backend = staff.backend(row)
            assert isinstance(backend, staff.CodexBackend)
            transcript = worker_admission.transcript_source(row, backend)
            with transcript.open("a", encoding="utf-8") as stream:
                _ = stream.write(json.dumps({"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": {"type": "agent_message", "message": final}}) + "\n")
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()), \
             patch("scripts.production.staff_sources.require_worker_stopped"):
            self.assertIs(worker.end(parent, name), worker.ExitCode.OK)
        self.assertIsNone(staff.by_id(row.id))
        if late_done:
            self.assertEqual(late_reply.read_text(), final + "\n")

    def check_end_with_delivered_result(self, pending: str) -> None:
        legacy = pending == "legacy finish at the preceding second"
        name = "legacy-result" if legacy else "late-result"
        self.agent.finish_after = 1
        self.assertIs(self.dispatch(name), worker.DispatchSubmission.SUBMITTED)
        pane, _ = self.agent.submitted[-1]
        row = self.agent.row(pane)
        instance = str(worker_panes.worker_pass(row, self.run_dir)["instance_id"])
        path, record = self.detached_record(row, f"{name}-run")
        finished_at = datetime.now(timezone.utc)
        record.update(pane_dispatch_id=instance, completion_pending=True, exit_code=0,
                      finished_at=finished_at.isoformat(timespec="seconds" if legacy else "microseconds"))
        self.agent.complete(pane)
        done = self.agent.replies[-1]
        if legacy:
            backend = staff.backend(row)
            assert isinstance(backend, (staff.CodexBackend, staff.ClaudeBackend))
            transcript = worker_admission.transcript_source(row, backend)
            lines = transcript.read_text().splitlines()
            reply_record = cast(dict[str, object], json.loads(lines[-1]))
            reply_record["timestamp"] = (finished_at.replace(microsecond=0) + timedelta(seconds=1)).isoformat()
            lines[-1] = json.dumps(reply_record)
            atomic_file.write_text_atomic(transcript, "\n".join(lines) + "\n")
        self.agent.append(pane, "user", f"Detached run: {record['run_id']}\nRun finished: verification")
        self.agent.append(pane, "assistant", "I recorded that result earlier.")
        record.update(delivery=f"sent: worker {name} at {pane}", delivered_at=time.time())
        with detached_runs.record_lock(path):
            atomic_file.write_text_atomic(path, json.dumps(record))
        self.assertEqual(detached_runs.live_runs(path.parent, name, identity=row.id), [])
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()), \
             patch("scripts.production.staff_sources.require_worker_stopped"):
            self.assertEqual(worker.end(self.fixture.parent.id, staff.WorkerName.parse(name)), worker.ExitCode.OK)
        self.assertEqual(self.reply.read_text().rstrip(), done)
        self.assertIsNone(staff.by_id(row.id))
        self.assertNotIn(pane, self.fixture.world.locations)
        self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)

    def check_end_retries_completion(self, failure: str) -> None:
        role_failure = failure == "role release fails"
        parent = self.fixture.parent.id
        name = staff.WorkerName.parse("held-role" if role_failure else "pending-notice")
        role = "private-role" if role_failure else ""
        self.agent.finish_after = 1
        actions = progress_history.CompletionActions(held_role=role)
        self.assertIs(worker.dispatch(parent, name, "impl", self.prompt,
            session_dir=self.run_dir, slot="impl", phase="8", kind="impl",
            launcher_command=["assign.sh"], reply=self.reply, completion_actions=actions),
            worker.DispatchSubmission.SUBMITTED)
        pane, _ = self.agent.submitted[-1]
        row = self.agent.row(pane)
        self.agent.complete(pane)
        if not role_failure:
            self.agent.notice_failures = 1
        notices = len(self.agent.notices)
        with (patch.object(astra_roles, "release", side_effect=(OSError("role release failed"), None)) as release,
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch("scripts.production.staff_sources.require_worker_stopped"),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(worker.end(parent, name), worker.ExitCode.KEPT)
            self.assertIn("role release failed" if role_failure else "director message failed", errors.getvalue())
            self.assertIsNotNone(staff.by_id(row.id))
            self.assertEqual(pane in self.fixture.world.locations, not role_failure)
            passed = worker_panes.worker_pass(row, self.run_dir)
            self.assertEqual(passed["status"], "completed")
            completion = progress_history.hook_completion(passed)
            assert isinstance(completion, progress_history.HookCompletion)
            self.assertFalse(completion.finished)
            self.assertEqual(worker.end(parent, name), worker.ExitCode.OK)
            self.assertEqual(release.call_args_list, [call(role), call(role)] if role_failure else [])
        self.assertIsNone(staff.by_id(row.id))
        self.assertNotIn(pane, self.fixture.world.locations)
        self.assertEqual(sum(": done; reply " in notice for notice in self.agent.notices[notices:]), 1)

    def test_check_distinguishes_resumable_workers_from_unknown_evidence(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "impl", completed=True)
        opened = replace(opened, row=staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("impl")))
        self.assertEqual(self.command("check")[0], 0)
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        self.assertEqual(self.command("check")[0], 0)
        resumed = self.opened()
        self.fixture.world.unknown[resumed.pane] = "process evidence lost"
        code, output, errors = self.command("check")
        self.assertEqual(code, 1)
        self.assertIn("impl", output)
        self.assertIn("process evidence lost", errors)

    def test_native_identity_refuses_a_worker_caller_and_a_supplied_parent(self) -> None:
        opened = self.opened()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": str(opened.row.current_session)}):
            code, output, errors = self.command("add", "review")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("caller is not a unit or showrunner", errors)
        self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)), 1)

    def test_add_and_assign_refuse_in_no_worker_mode(self) -> None:
        from scripts.whoami import quota_state

        launches = len(self.fixture.world.launches)
        for arguments in (("add", "review"),
                          ("assign", "impl", str(self.prompt), "--session-dir", str(self.run_dir), "--slot", "impl",
                           "--phase", "8", "--kind", "impl", "--launcher-command", "[]")):
            with self.subTest(command=arguments[0]), patch.object(quota_state, "no_worker_mode", return_value=True):
                self.assertEqual(self.command(*arguments), (2, "", f"worker.py: {quota_state.NO_WORKER_START}\n"))
        self.assertEqual(len(self.fixture.world.launches), launches)
        self.assertEqual(self.agent.submitted, [])

    def test_showrunner_native_identity_selects_only_its_own_children(self) -> None:
        other = fixture_member("showrunner", name="other")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": str(other.current_session)}):
            self.assertEqual(self.command("list", "--json")[:2], (0, "[]\n"))
        self.assertEqual(self.agent.submitted, [])


    def test_launcher_recovery_reports_an_absent_director(self) -> None:
        opened = self.opened()
        with (patch.object(self.fixture.world, "tab_sessions", return_value=[]),
              patch.object(self.fixture.world, "prompt") as prompt):
            plan = worker.WorkerResume(worker.UnsubmittedAssignment("saved-pass", ("assign.sh",)))
            self.assertEqual(worker._resume_launcher(opened.row, plan),  # pyright: ignore[reportPrivateUsage]
                             worker.DispatchEvidenceUnknown("director session is unavailable"))
        prompt.assert_not_called()

    def test_publication_failure_reports_that_its_new_pane_close_failed(self) -> None:
        with (patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("TEST admission refused")),
              patch.object(self.fixture.world, "close_pane", return_value=False),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertIn("pane w1:p1 close failed", errors.getvalue())
        self.assertEqual(sum(line.startswith("anomaly:") for line in errors.getvalue().splitlines()), 1)
        self.assertIn("w1:p1", self.fixture.world.locations)
        self.assertEqual(self.agent.submitted, [])

    def test_list_all_resolves_the_legacy_unit_account_once_from_stored_history(self) -> None:
        _first, second, _paths = self.recorded_list_fixture()
        atomic_file.write_text_atomic(accounts.groups_file(), json.dumps({"units": "default"}))
        atomic_file.write_text_atomic(accounts.roles_history_file(), json.dumps({
            "role": "unit", "label": "default", "account_label": "claude2", "at": "2026-10-08T00:00:00Z"}) + "\n")
        with patch.object(accounts, "resolve_history_label", wraps=accounts.resolve_history_label) as resolved:
            code, output, errors = self.command("list", "all", "--json", "--all-parents")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual(rows[second.id]["account"], "claude2")
        self.assertEqual(rows[second.id]["home"], str(Path.home() / ".claude2"))
        resolved.assert_not_called()

    def test_list_all_reads_only_the_needed_default_account_history_once(self) -> None:
        _first, second, _paths = self.recorded_list_fixture()
        atomic_file.write_text_atomic(accounts.groups_file(), json.dumps({"units": "default", "showrunners": "default"}))
        history = accounts.roles_history_file()
        atomic_file.write_text_atomic(history, "".join(json.dumps(row) + "\n" for row in (
            {"role": "unit", "label": "default", "account_label": "claude2", "at": "2026-10-08T00:00:00Z"},
            {"group": "showrunners", "label": "default", "account_label": "claude3", "at": "2026-10-08T00:00:00Z"})))
        original = Path.read_text
        reads: list[Path] = []
        def read(path: Path, *args: object, **kwargs: object) -> str:
            reads.append(path)
            return original(path, *args, **kwargs)  # pyright: ignore[reportArgumentType]
        with (patch.object(Path, "read_text", autospec=True, side_effect=read),
              patch.object(accounts, "resolve_history_label", wraps=accounts.resolve_history_label) as resolve):
            code, output, errors = self.command("list", "all", "--json", "--all-parents")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual(rows[second.id]["account"], "claude2")
        self.assertEqual(reads.count(history), 0)
        resolve.assert_not_called()

    def test_list_scans_runs_once_and_prints_each_run_anomaly_once(self) -> None:
        start_new_command(self)
        rows = [self.fixture.worker(name) for name in ("impl", "review", "test")]
        for index, row in enumerate(rows, 1):
            self.fixture.world.add(row, f"w1:p{index}")
        runs = self.fixture.root
        for name in ("ownerless-a", "ownerless-b"):
            run = runs / name
            run.mkdir(parents=True)
            atomic_file.write_text_atomic(run / unit_runs.STATE_FILE, '{}')
            atomic_file.write_text_atomic(run / "fixture-impl_status", "running\n")
        with patch.object(unit_runs, "held_by_session", wraps=unit_runs.held_by_session) as scans:
            code, output, errors = self.command("list", "all", "--json")
        self.assertEqual(code, 0, errors)
        self.assertEqual(scans.call_count, 1)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual({item["name"] for item in listing}, {row.name for row in rows})
        self.assertEqual(errors.splitlines(), [
            f"anomaly: {runs / name}: this run has no owner because {unit_runs.STATE_FILE} names no staff row"
            for name in ("ownerless-a", "ownerless-b")])


if __name__ == "__main__":
    _ = unittest.main()
