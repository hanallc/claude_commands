from __future__ import annotations

from scripts.agents.test_support import write_machine_config
from scripts.lint.memory_test_support import copy_config_file_fixture, config_environment, write_config_fixture

from scripts.agents import remote
from scripts.herdr import herdr_types

import io
import fcntl
import json
import math
import os
import select
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Generator, Iterator, Mapping
from contextvars import Context
from contextlib import ExitStack, closing, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from enum import Enum
from functools import partial
from pathlib import Path
from threading import Barrier, Lock, local
from typing import Literal, Never, cast, final, override
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from scripts.accounts import accounts, moves, role_map, worker_names
from scripts.accounts.move_test_support import write_claude_move_credentials
from scripts.whoami import account, agent_accounts
from scripts.whoami.account_test_support import write_codex_auth
from scripts.agents import member_account, producer_creation, producer_transfer, restart, shutdown_record, stop_resume, stop_results
from scripts.lib import (agent_program, atomic_file, autocompact, conf, lock_holder, models_registry, operation_deadline,
                         process_start, process_stop, unit_runs)
from scripts.lib.transcript_test_support import compressed_copy
from scripts.herdr import worker_panes
from scripts.agents.stop_results import ClaudeStartState, StopResult, shutdown_state_root

from scripts.agents.stop_results import SourceReleased, SourceReleaseUnknown
from scripts.agents.launch_names import LaunchNames
from scripts.production import showrunners, staff, staff_sources, stall_watch, worker_admission
from scripts.lib.resume_message import ClaudeChildResume
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_worker, fixture_runtime_id, isolated_staff_file
from scripts.usage import agents_log
from scripts.herdr import herdr, pane_launch
from scripts.herdr.test_herdr import codex_foreground_cases
from scripts.agents import pane_record, shutdown
from scripts.message import codex_daemon, session_records, sessions
from scripts.message import sessions as runtime_sessions
from scripts.notify import pushover
from scripts.notify.push_texts import RenderedPush, render
from scripts.production import unit_lookup, test_worker
from scripts.direct import progress_history, step_record
from scripts.lib.test_models_registry import claude_catalog_environment, write_registry
from scripts.agents.test_pane_record_recovery import FakeClient, fixture_launch_transcript
from scripts.herdr.missing_binary import MISSING_HERDR_BIN

RUN_IN_PANE = herdr.Herdr.run_in_pane
ROLE_ACCOUNT = accounts.role_account
CHECK_MOVE_TARGET = accounts.check_move_target
READ_CODEX_CONVERSATIONS = herdr.codex_conversations
SAVE_PANE_RECORD = pane_record.save
WRITE_PANE_RECORD = pane_record.write
SEED_PANE_RECORDS = pane_record.seed
SEND_MOVE_PUSH = stop_resume._send_move_push  # pyright: ignore[reportPrivateUsage]


class ProducerAdmissionWorld:
    """A private producer pane with native process evidence."""

    def __init__(self, case: StopResumeTests, kind: Literal["claude", "codex"]) -> None:
        self.case: StopResumeTests = case
        self.kind: Literal["claude", "codex"] = kind
        self.identity: str = fixture_runtime_id("first-producer-" + kind)
        self.folder: Path = case.root / "producer-documents/dwight"
        self.home: Path = case.root / (".claude3" if kind == "claude" else ".codex3")
        self.arguments: list[list[str]] = []
        self.started_panes: list[str] = []
        self.openings: list[herdr.OpeningPrompt | herdr.NoOpeningPrompt] = []
        self.environment: dict[str, str] = {}
        self.active: bool = False
        self.pane: str = "w1:p3"
        self.folder.mkdir(parents=True)
        _ = (self.folder / "README.md").write_text("# dwight — strategist\n")

    def open_tab(self, name: str, space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
        self.pane = f"w1:p{3 + len(self.case.client.opened)}"
        self.case.client.opened.append((name, space, title, cwd, environment))
        self.environment = {**environment, "HERDR_SESSION": "agents", "HERDR_PANE_ID": self.pane}
        self.case.client.rows.append({"pane_id": self.pane, "terminal_id": "dwight-terminal-" + self.pane, "tab_id": "dwight-tab-" + self.pane,
                                      "label": title, "workspace_id": "w1"})
        return self.pane

    def start_agent(self, name: str, pane: str, kind: str, agent_arguments: list[str],
                    opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
        del name, timeout_ms
        self.openings.append(opening)
        if kind != self.kind or pane != self.pane:
            raise AssertionError("unexpected producer launch")
        if staff.initial_snapshot().rows:
            raise AssertionError("producer entered staff before its first launch")
        self.record_start(pane, agent_arguments)
        return herdr.PromptSubmissionNotRequired()

    def record_start(self, pane: str, agent_arguments: list[str]) -> None:
        self.arguments.append(list(agent_arguments))
        self.started_panes.append(pane)
        self.active = True
        self.case.launched.add(self.identity)
        self.case.client.native_ids[pane] = self.identity

    def process_info(self) -> dict[str, object]:
        return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 456,
            "foreground_processes": [{"pid": 456, "name": self.kind, "cwd": str(self.folder),
                                       "argv": [self.kind, *(self.arguments[-1] if self.arguments else [])]}]}}


@contextmanager
def producer_first_fixture(kind: Literal["claude", "codex"]) -> Generator[ProducerAdmissionWorld]:
    """Exercise first admission through fixture staff and a fake herdr."""
    case = StopResumeTests("runTest")
    try:
        case.setUp()
        world = ProducerAdmissionWorld(case, kind)
        staff_configuration = case.root / "configuration/staff.conf"
        _ = write_config_fixture(staff_configuration, f"PRODUCER_FOLDER_ROOT={world.folder.parent}\n")
        configuration = case.root / "configuration/models.conf"
        _ = write_registry(configuration, {"producers": {"dwight": kind + (":opus:high" if kind == "claude" else ":gpt-test:high")}}, update=True)
        result = case.client.result
        command = case.client.run
        program = herdr.run_program

        def pane_result(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments == ("pane", "process-info", "--pane", world.pane) and world.active:
                return world.process_info()
            return result(*arguments, deadline=deadline)

        def run(argv: list[str], timeout: float = herdr.PROGRAM_TIMEOUT_SECONDS, *,
                environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv == case.client.argv("pane", "process-info", "--pane", world.pane) and world.active:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"result": world.process_info()}), "")
            return program(argv, timeout, environment=environment)

        def pane_command(*arguments: str) -> str:
            if arguments == ("pane", "read", world.pane, "--source", "visible", "--format", "ansi"):
                return "────────\n❯ \n────────"
            return command(*arguments)

        def native(pid: int, family: str, reported: str | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> herdr.NativeAgentProcess:
            del reported
            if pid != 456 or family != world.kind or not world.active:
                raise ValueError("unexpected native producer process")
            return herdr.NativeAgentProcess(herdr.EnvironmentThread(world.identity), "generation-1", world.environment)

        def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
            return world.open_tab(case.client.session, space, title, cwd, environment)

        def run_in_pane(pane: str, command: list[str], *, terminal: str = "") -> None:
            del pane, terminal
            if len(shlex.join(command).encode()) > 4096:
                raise herdr_types.HerdrFailure("fixture shell command exceeds the terminal input limit")
            _, export, assignments = command[-1].rpartition("; export ")
            if export:
                for assignment in shlex.split(assignments):
                    key, _, value = assignment.partition("=")
                    world.environment[key] = value

        def activity(pane: str) -> herdr.PaneRunning | herdr.PaneAtShell:
            return (herdr.PaneRunning((world.kind,)) if pane == "w1:p2" or (pane == world.pane and world.active)
                    else herdr.PaneAtShell())

        def role_account(role: str) -> accounts.AccountSelection:
            return (accounts.NamedAccount("claude3", world.home) if role == "producer" else
                    accounts.CodexWorkersAccount("codex3", world.home))

        def conversation(identity: str, family: str = "", *, codex: herdr.CodexConversations | None = None
                         ) -> herdr.LiveConversation | herdr.StoppedConversation:
            del family, codex
            return (herdr.LiveConversation(world.kind, (456,)) if world.active and identity == world.identity
                    else herdr.StoppedConversation())

        def claude_home(pid: int) -> account.ConfiguredClaudeFolder:
            del pid
            return account.ConfiguredClaudeFolder(Path(world.environment["CLAUDE_CONFIG_DIR"]))

        def codex_home(pid: int) -> account.ProcessCodexHome:
            del pid
            return account.ProcessCodexHome(world.home)

        def claude_liveness(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown:
            if identity == world.identity and world.kind == "codex":
                return sessions.SessionNotLive()
            return case.observe(identity)

        with ExitStack() as scope:
            for boundary in (patch.object(case.client, "open_tab", side_effect=open_tab),
                    patch.dict(os.environ, {"MODELS_CONFIG_TIER_ONLY": "1"}),
                    patch.object(case.client, "run_in_pane", side_effect=run_in_pane),
                    patch.object(case.client, "start_agent", side_effect=world.start_agent),
                    patch.object(case.client, "pane_activity", side_effect=activity),
                    patch.object(case.client, "result", side_effect=pane_result),
                    patch.object(case.client, "run", side_effect=pane_command),
                    patch.object(herdr, "run_program", side_effect=run),
                    patch.object(herdr, "native_process", side_effect=native),
                    patch.object(accounts, "role_account", side_effect=role_account),
                    patch.object(herdr, "read_conversation", side_effect=conversation),
                    patch.object(sessions, "session_liveness", side_effect=claude_liveness),
                    patch.object(account, "process_config_dir", side_effect=claude_home),
                    patch.object(account, "process_codex_home", side_effect=codex_home)):
                _ = cast(object, scope.enter_context(boundary))
            yield world
    finally:
        case.doCleanups()


_REAL_CONFIRM_MOVE_START = stop_resume._confirm_move_start  # pyright: ignore[reportPrivateUsage]
_REAL_TELL = showrunners.tell


def confirmed_fixture_move(result: stop_resume.ClaudeUp, _requester: str, _why: str) -> stop_resume.ClaudeUp:
    return result


class ResumeClock(local):
    elapsed: float = 0.0


class WorkerResumeTests(unittest.TestCase):
    def test_resume_without_a_worker_record_returns_the_restored_pane(self) -> None:
        from scripts.herdr.test_worker_panes import live_detached_run

        case = test_worker.WorkerCommandTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        _ = write_registry(case.fixture.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        _ = copy_config_file_fixture(Path(__file__).resolve().parents[2] / "config/shutdown.conf",
                                     case.fixture.root / "conf/shutdown.conf")
        opened = case.opened()
        _ = progress_history.begin_dispatch(case.run_dir, slot=opened.row.name, phase="8", role="impl",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=case.prompt, kind="impl", lens="")
        world = case.fixture.world
        world.locations["w1:p0"]["agent_session"] = {"value": case.fixture.parent.current_session}
        _ = world.stop(opened.row, world)
        pane_record.save(world.session, {})

        def running(identity: str, pane: str, *, kind: str = "claude"
                    ) -> stop_resume.ClaudeUp | sessions.SessionNotLive:
            self.assertEqual((identity, kind), (opened.row.current_session, "claude"))
            return (stop_resume.ClaudeUp(identity, pane, "claude") if pane in world.foreground else
                    sessions.SessionNotLive())

        with (patch.object(stop_resume, "running_on_account", side_effect=running) as proof,
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              live_detached_run(case.run_dir, opened.row),
              redirect_stdout(io.StringIO())):
            result = stop_resume.resume(str(opened.row.id))
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.session_id, opened.row.current_session)
        self.assertEqual(result.pane_id, opened.pane)
        self.assertEqual(world.locations[result.pane_id]["terminal_id"], opened.terminal)
        self.assertEqual(world.closed, [])
        self.assertEqual(result.start_state, ClaudeStartState.STARTED)
        self.assertEqual(proof.call_args.args, (opened.row.current_session, result.pane_id))
        prompt = next(command[-1] for command in reversed(world.commands) if command[:2] == ("agent", "prompt"))
        self.assertIn(f"Detached run {opened.row.name} remains live.", prompt)
        self.assertIn(f"{case.run_dir}/detached_runs/{opened.row.name}.json", prompt)
        self.assertNotIn("ended with no result", prompt)


@final
class StopResumeTests(unittest.TestCase):
    def test_named_producer_stop_holds_until_a_confirmed_resume(self) -> None:
        row = fixture_member("producer", name="alpha", worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        identity = str(row.current_session)
        held = stop_resume.StoppedOnPurpose(row.name)
        with (patch.dict(os.environ, {"USER_ZONE": "UTC"}),
              patch.object(stop_resume, "_stop", return_value=stop_resume.StopFailed(identity, "fixture refused")) as stop,
              patch.object(stop_resume, "_resume_once", return_value=stop_resume.ResumeFailed(identity, "fixture refused")) as start):
            self.assertEqual(shutdown.main(["stop", row.name]), 1)
            self.assertEqual(shutdown_record.read(), [])
            self.assertIsInstance(staff.snapshot().rows_by_id[row.id].exited, staff.Active)
            stop.return_value = stop_resume.NotRunning(identity)
            self.assertEqual(shutdown.main(["stop", row.name]), 0)
            self.assertEqual(shutdown_record.read(), [row.id])
            self.assertEqual(stop_resume.resume_for_upkeep(row.id), held)
            start.assert_not_called()

            self.assertEqual(shutdown.main(["resume", row.name]), 1)
            self.assertEqual(stop_resume.resume_for_upkeep(row.id), held)
            start.return_value = stop_resume.ClaudeUp(identity, "fixture-pane", "claude3")
            self.assertEqual(shutdown.main(["resume", row.name]), 0)
            self.assertIsInstance(staff.snapshot().rows_by_id[row.id].exited, staff.Active)
            self.assertEqual(shutdown_record.read(), [])
            self.assertIsInstance(stop_resume.resume_for_upkeep(row.id), stop_resume.ClaudeUp)

            self.assertEqual(shutdown.main(["exit", row.name, "--why", "keep stopped"]), 0)
            self.assertEqual(shutdown_record.read(), [row.id])
            exited = staff.snapshot().rows_by_id[row.id].exited
            self.assertIsInstance(exited, staff.Exited)
            assert isinstance(exited, staff.Exited)
            self.assertEqual(exited.why, "keep stopped")
            self.assertEqual(stop_resume.resume_for_upkeep(row.id), held)

    def test_stop_selects_a_worker_by_its_unit_path(self) -> None:
        hana = fixture_member("showrunner", name="hana")
        assembly = fixture_member("unit", name="assembly", parent=hana.id)
        worker = fixture_member("worker", name="fix2", parent=assembly.id)
        _ = fixture_member("worker", name="fix2", parent=fixture_member("unit", name="cables", parent=hana.id).id)
        with (patch.object(stop_resume, "stop", return_value=stop_resume.NotRunning(worker.current_session)) as stop,
              patch.object(stop_resume, "stop_unit_workers", return_value=[]) as stop_workers):
            for path in ("assembly/fix2", "hana/assembly/fix2"):
                with self.subTest(path=path), redirect_stdout(io.StringIO()):
                    self.assertEqual(shutdown.main(["stop", path]), 0)
                    self.assertEqual(stop.call_args.args, (str(worker.id),))
            stop_workers.assert_not_called()
            with self.subTest(path="hana/assembly"), redirect_stdout(io.StringIO()):
                self.assertEqual(shutdown.main(["stop", "hana/assembly"]), 0)
                self.assertEqual(stop_workers.call_args.args[0], assembly.id)
                self.assertEqual(stop.call_args.args, (str(assembly.id),))
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(["stop", "--dry-run", "assembly/fix2"]), 0)
            self.assertEqual(output.getvalue(), "fix2: would stop\n")
            stop.reset_mock()
            other = fixture_member("unit", name="assembly", parent=fixture_member("showrunner", name="cad").id)
            _ = fixture_member("worker", name="fix2", parent=other.id)
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(shutdown.main(["stop", "assembly/fix2"]), 2)
            self.assertEqual(errors.getvalue(), "shutdown: StaffRefused: assembly/fix2: several staff rows match: "
                             + "cad/assembly/fix2, hana/assembly/fix2; use the full path or the staff id\n")
            stop.assert_not_called()

    def test_stop_gives_a_path_with_no_staff_row_to_the_session_stop(self) -> None:
        """A native session name can contain a slash, such as review/x."""
        with (patch.object(stop_resume, "stop", return_value=stop_resume.NotRunning("review/x")) as stop,
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["stop", "review/x"]), 0)
        self.assertEqual(stop.call_args.args, ("review/x",))

    def test_remote_producer_exit_stops_on_mac_and_saves_the_hold(self) -> None:
        row = fixture_member("producer", name="alpha", worktree=staff.PresentWorktree("mac", self.root))
        mac_state = self.root / "mac-shutdown"

        def remote_stop(arguments: list[str], stdin: str, limit: remote.RemoteCallLimit) -> tuple[int, str]:
            self.assertEqual(arguments, ["_unit-local"])
            self.assertEqual(limit["kind"], "while link alive")
            reply = io.StringIO()
            with (patch.object(shutdown, "local_machine", return_value="mac"),
                  patch.object(shutdown, "other_machine", return_value="natedev"),
                  patch.object(remote, "local_machine", return_value="mac"),
                  patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(mac_state)}),
                  patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(reply)):
                code = shutdown.unit_local_command()
            return code, reply.getvalue()

        def stop_on_mac(reference: str, _kind: str = "") -> stop_resume.Stopped:
            self.assertEqual(reference, row.id)
            self.assertEqual(remote.local_machine(), "mac")
            current = staff.snapshot().rows_by_id[row.id]
            self.assertIsInstance(current.paused, staff.Paused)
            self.assertIsInstance(current.exited, staff.Active)
            return stop_resume.Stopped(str(row.current_session), 123)

        with (patch.dict(os.environ, {"USER_ZONE": "UTC"}),
              patch.object(shutdown, "local_machine", return_value="natedev"),
              patch.object(shutdown, "other_machine", return_value="mac"),
              patch.object(shutdown, "run_remote", side_effect=remote_stop) as run,
              patch.object(stop_resume, "_stop", side_effect=stop_on_mac) as stop,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["exit", row.name, "--why", "maintenance"]), 0)
        run.assert_called_once()
        stop.assert_called_once()
        self.assertIn("alpha: exited", output.getvalue())
        current = staff.snapshot().rows_by_id[row.id]
        self.assertIsInstance(current.exited, staff.Exited)
        assert isinstance(current.exited, staff.Exited)
        self.assertEqual(current.exited.why, "maintenance")
        self.assertEqual(shutdown_record.read(), [])
        with patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(mac_state)}):
            self.assertEqual(shutdown_record.read(), [row.id])

    def test_producer_resume_uses_first_admission_after_a_family_change(self) -> None:
        class InterruptedResume(BaseException):
            """The command exits before its next step."""

        cases: tuple[tuple[Literal["claude", "codex"], Literal["claude", "codex"], str], ...] = (
            ("codex", "claude", "complete"), ("claude", "codex", "complete"),
            ("codex", "claude", "before ended write"),
            ("codex", "claude", "before close"), ("claude", "codex", "after close"),
            ("codex", "claude", "after launch"), ("codex", "claude", "stop refused"),
            ("claude", "claude", "same family"), ("codex", "claude", "registry changed after selection"),
        )
        for previous, configured, interruption in cases:
            with self.subTest(previous=previous, configured=configured, interruption=interruption), producer_first_fixture(previous) as world:
                case = world.case
                root = case.root
                homes = {"claude": root / ".claude5", "codex": root / ".codex3"}
                world.home = homes[previous]
                atomic_file.write_text_atomic(accounts.groups_file(), json.dumps({"producers": "claude5"}))
                atomic_file.write_text_atomic(worker_names.accounts_config_file(), json.dumps({"workers": "codex3"}))
                for label in ("claude5", "codex3"):
                    atomic_file.write_text_atomic(root / "notes" / f"{label}.md",
                                                  f"---\nlogin: {label}@example.invalid\n---\n")
                homes["codex"].mkdir()
                write_codex_auth(homes["codex"], "codex3@example.invalid")
                old_identity = world.identity
                new_identity = fixture_runtime_id("replacement-dwight-" + configured)
                original_close = case.client.close_tab_of

                def launch(name: str, pane: str, kind: Literal["claude", "codex"], agent_arguments: list[str],
                           opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                           timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
                    if world.arguments:
                        self.assertEqual((kind, pane), (configured, world.pane))
                        self.assertEqual(case.client.closed, [world.started_panes[0]])
                        self.assertEqual(staff.initial_snapshot().rows[0].current_session, old_identity)
                        if interruption == "before ended write":
                            self.assertTrue(pane_record.read_strict("agents")[world.started_panes[0]].get("ended", False))
                        world.kind = kind
                        world.home = homes[kind]
                        world.identity = new_identity
                        world.record_start(pane, agent_arguments)
                    else:
                        _ = world.start_agent(name, pane, kind, agent_arguments, opening, timeout_ms)
                    for row in case.client.rows:
                        if row["pane_id"] == pane:
                            row["agent_session"] = {"kind": "id", "value": world.identity}
                    if len(world.arguments) > 1 and interruption == "after launch":
                        raise InterruptedResume
                    return herdr.PromptSubmissionNotRequired()

                def signal_process(pid: int, number: int) -> None:
                    self.assertEqual((pid, number, world.identity), (456, signal.SIGTERM, old_identity))
                    world.active = False
                    case.launched.remove(old_identity)
                    _ = case.client.native_ids.pop(world.pane)
                    for row in case.client.rows:
                        if row["pane_id"] == world.pane:
                            _ = row.pop("agent_session")
                    if interruption == "before ended write":
                        raise InterruptedResume

                def conversation(identity: str, family: str = "", *, codex: herdr.CodexConversations | None = None
                                 ) -> herdr.LiveConversation | herdr.StoppedConversation:
                    del codex
                    if world.active and identity == world.identity:
                        self.assertIn(family, ("", world.kind))
                        return herdr.LiveConversation(world.kind, (456,))
                    return herdr.StoppedConversation()

                def close(pane: str) -> None:
                    self.assertFalse(world.active)
                    if interruption == "before close":
                        raise InterruptedResume
                    original_close(pane)
                    if interruption == "after close":
                        raise InterruptedResume

                def process_liveness(_pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
                    return sessions.LiveSessionProcess(456) if world.active else sessions.SessionNotLive()

                case.kill.side_effect = signal_process
                case.process.side_effect = process_liveness
                with (patch.object(accounts, "role_account", ROLE_ACCOUNT),
                      patch.object(pane_record, "save", SAVE_PANE_RECORD),
                      patch.object(pane_record, "write", WRITE_PANE_RECORD),
                      patch.object(pane_record, "seed", SEED_PANE_RECORDS),
                      patch.object(herdr, "read_conversation", side_effect=conversation),
                      patch.object(herdr, "check_native_target", return_value=True),
                      patch.object(case.client, "start_agent", side_effect=launch)):
                    first = stop_resume.resume("dwight")
                    self.assertIsInstance(first, stop_resume.ClaudeUp, first)
                    old_row = staff.initial_snapshot().rows[0]
                    old_pane = world.pane
                    registry = root / "configuration/models.conf"
                    if previous != configured:
                        setting = "claude:opus:high" if configured == "claude" else "codex:gpt-test:high"
                        _ = write_registry(registry, {"producers": {"dwight": setting}}, update=True)
                    if previous == configured:
                        repeated = stop_resume.resume("dwight")
                        self.assertIsInstance(repeated, stop_resume.ClaudeUp, repeated)
                        assert isinstance(repeated, stop_resume.ClaudeUp)
                        self.assertEqual(repeated.start_state, ClaudeStartState.ALREADY_RUNNING)
                        self.assertEqual((repeated.session_id, repeated.pane_id), (old_identity, old_pane))
                        self.assertEqual(staff.initial_snapshot().rows, (old_row,))
                        self.assertEqual((len(case.client.opened), case.client.closed), (1, []))
                        case.kill.assert_not_called()
                        self.assertEqual(world.started_panes, [old_pane])
                        self.assertFalse(stop_resume.producer_admission_record("dwight").exists())
                        continue
                    if interruption == "stop refused":
                        retained = pane_record.read_strict("agents")
                        self.assertFalse(retained[old_pane].get("ended", False))
                        with patch.object(herdr, "check_native_target", return_value=False):
                            refused = stop_resume.resume("dwight")
                        self.assertEqual(refused, stop_resume.ResumeFailed("dwight", "process identity changed; no signal sent"))
                        self.assertEqual(pane_record.read_strict("agents"), retained)
                        self.assertEqual(case.client.closed, [])
                        self.assertFalse(stop_resume.producer_admission_record("dwight").exists())
                        case.kill.assert_not_called()
                    elif interruption not in ("complete", "registry changed after selection"):
                        with (patch.object(case.client, "close_tab_of", side_effect=close),
                              self.assertRaises(InterruptedResume)):
                            _ = stop_resume.resume("dwight")
                        self.assertEqual(staff.initial_snapshot().rows, (old_row,))
                        self.assertNotIn(old_identity, case.launched)
                    if interruption == "before ended write":
                        binding = pane_record.read_strict("agents")[old_pane]
                        self.assertTrue(binding.get("producer_binding", False))
                        self.assertFalse(binding.get("ended", False))
                        self.assertEqual(case.client.closed, [])
                    if interruption == "after launch":
                        admission = stop_resume.producer_admission_record("dwight")
                        saved = admission.read_bytes()
                        self.assertEqual(case.launched, {new_identity})
                        with (patch.object(staff, "runtime_id", return_value=new_identity),
                              patch.dict(os.environ, {"HERDR_PANE_ID": world.pane}),
                              self.assertRaisesRegex(ValueError, "Run agents resume dwight first")):
                            _ = producer_transfer.transfer("dwight")
                        self.assertEqual(staff.initial_snapshot().rows, (old_row,))
                        self.assertEqual(admission.read_bytes(), saved)
                        self.assertEqual(case.launched, {new_identity})
                    if interruption == "registry changed after selection":
                        _ = write_registry(registry, {"compact_windows": {"producer": "300000"}}, update=True)
                        launch_settings = models_registry.launch_settings

                        def change_registry(section: str, name: str,
                                            family: Literal["claude", "codex", "assigned"] = "assigned", *,
                                            configuration: models_registry.LaunchModelConfiguration | None = None
                                            ) -> models_registry.LaunchSettings:
                            selected = launch_settings(section, name, family, configuration=configuration)
                            _ = write_registry(registry, {"producers": {name: "codex:gpt-test:high"},
                                                         "compact_windows": {"producer": "100000"}}, update=True)
                            return selected

                        with (patch.object(models_registry, "launch_settings", side_effect=change_registry),
                              patch.object(models_registry, "launch_snapshot",
                                           wraps=models_registry.launch_snapshot) as snapshot):
                            result = stop_resume.resume("dwight")
                        snapshot.assert_called_once()
                        self.assertIsInstance(result, stop_resume.ClaudeUp, result)
                        arguments = world.arguments[-1]
                        window = arguments.index("--autocompact")
                        self.assertEqual(arguments[window + 1], "300000")
                    else:
                        result = stop_resume.resume("dwight")
                    self.assertIsInstance(result, stop_resume.ClaudeUp, result)
                    assert isinstance(result, stop_resume.ClaudeUp)
                    self.assertEqual(result.start_state, ClaudeStartState.STARTED)
                    self.assertEqual((result.session_id, result.pane_id), (new_identity, world.pane))
                    self.assertNotEqual(world.pane, old_pane)
                    self.assertEqual(case.client.closed, [old_pane])
                    self.assertEqual(case.launched, {new_identity})
                    case.kill.assert_called_once_with(456, signal.SIGTERM)
                    self.assertEqual(len(case.client.opened), 2)
                    variable = "CLAUDE_CONFIG_DIR" if configured == "claude" else "CODEX_HOME"
                    self.assertEqual(case.client.opened[1][4][variable], str(homes[configured]))
                    self.assertEqual(len(world.arguments), 2)
                    for argument in ("--resume", "resume", old_identity):
                        self.assertNotIn(argument, world.arguments[1])
                    self.assertEqual(staff.initial_snapshot().rows, (replace(old_row,
                        current_session=staff.native_id(new_identity), home=staff.SessionHome(homes[configured])),))
                    publication = pane_record.read("agents")
                    self.assertNotIn(old_pane, publication)
                    self.assertEqual(publication[world.pane].get("staff_id"), old_row.id)
                    self.assertTrue(publication[world.pane].get("producer_binding", False))
                    self.assertFalse(stop_resume.producer_admission_record("dwight").exists())

    def test_producer_adoption_requires_the_proved_pane_owner(self) -> None:
        with producer_first_fixture("codex") as world, codex_foreground_cases(world.folder) as cases:
            client = world.case.client
            world.active = True
            world.environment = {"HERDR_SESSION": client.session, "HERDR_PANE_ID": world.pane,
                                 "CODEX_HOME": str(world.home)}
            client.rows.append({"pane_id": world.pane, "terminal_id": "native-terminal",
                                "workspace_id": "w1", "label": "producer", "session_id": world.identity})
            for case in cases:
                with (self.subTest(case=case.name),
                      patch.object(world, "process_info", return_value={"process_info": case.info})):
                    if case.owner_proved:
                        entry = stop_resume.producer_adoption_launch(world.identity, world.pane, client)
                        self.assertEqual((entry.get("kind"), entry["session_id"], entry.get("pid")),
                                         ("codex", world.identity, 456))
                    else:
                        with self.assertRaisesRegex(ValueError, "no unique native foreground process"):
                            _ = stop_resume.producer_adoption_launch(world.identity, world.pane, client)

    def test_producer_admission_refuses_an_incomplete_registry_before_reserving_a_pane(self) -> None:
        for missing in ("session registry", "creation record"):
            with self.subTest(missing=missing), producer_first_fixture("claude") as world:
                if missing == "session registry":
                    path = runtime_sessions.sessions_dir() / "unfinished.json"
                    _ = path.write_text("{")
                    reason = f"session registry unreadable: {path}"
                else:
                    machine = remote.this_machine()
                    registry = world.case.root / "configuration/models.conf"
                    _ = write_registry(registry, {"producer_creations": {"dwight": f"{machine}:{'1' * 32}"}}, update=True)
                    reason = f"the creation record is on {machine}"
                result = stop_resume.resume("dwight")
                self.assertIsInstance(result, stop_resume.ResumeFailed)
                assert isinstance(result, stop_resume.ResumeFailed)
                self.assertIn(reason, result.reason)
                self.assertEqual(world.case.client.opened, [])
                self.assertEqual(world.arguments, [])

    def test_producer_first_publication_uses_the_selected_launch_configuration(self) -> None:
        with producer_first_fixture("claude") as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"compact_windows": {"producer": "300000"}}, update=True)

            def launch(name: str, pane: str, kind: str, agent_arguments: list[str],
                       opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                       timeout_ms: int) -> herdr.PromptSubmissionNotRequired:
                outcome = world.start_agent(name, pane, kind, agent_arguments, opening, timeout_ms)
                _ = write_registry(registry, {"producers": {"dwight": "codex:gpt-test:high"},
                                             "compact_windows": {"producer": "100000"}}, update=True)
                return outcome

            with (patch.object(world.case.client, "start_agent", side_effect=launch),
                  patch.object(models_registry, "launch_snapshot", wraps=models_registry.launch_snapshot) as snapshot,
                  patch.object(producer_creation, "require_record", wraps=producer_creation.require_record) as marker):
                result = stop_resume.resume("dwight")
            snapshot.assert_called_once()
            marker.assert_called_once()
            self.assertIsInstance(result, stop_resume.ClaudeUp, result)
            arguments, = world.arguments
            self.assertEqual(arguments[arguments.index("--autocompact") + 1], "300000")
            publication = pane_record.read("agents")[world.pane]
            self.assertTrue(publication.get("producer_binding", False))
            self.assertEqual(staff.initial_snapshot().rows[0].current_session, world.identity)

    def test_configured_producer_first_start_and_live_resume_keep_one_native_conversation(self) -> None:
        for shown in (None, "old-producer-title"):
            with self.subTest(native_title=shown), producer_first_fixture("claude") as world:
                member = fixture_member("producer", name="dwight", runtime_id=world.identity)
                world.active = True
                world.environment = {"CLAUDE_CONFIG_DIR": str(world.home)}
                world.case.client.rows.append({"pane_id": world.pane, "terminal_id": "native-terminal",
                    "workspace_id": "w1", "tab_id": "native-tab", "label": shown})
                world.case.client.native_ids[world.pane] = world.identity
                self.assertEqual(stop_resume.producer_conflicts(member.name, world.case.client),
                                 [(world.identity, world.pane)])

        cases: tuple[tuple[Literal["claude", "codex"], bool, str, bool], ...] = (
            ("claude", False, "opus", False), ("codex", False, "gpt-test", False), ("claude", True, "opus", False),
            ("claude", False, "claude-opus-producer-fixture", False),
            ("claude", False, "opus", True))
        for kind, existing, expected_model, check_binding in cases:
            with self.subTest(kind=kind, existing=existing, model=expected_model), producer_first_fixture(kind) as world:
                if expected_model == "claude-opus-producer-fixture":
                    environment = claude_catalog_environment(world.case.root / "caller-claude",
                        {"opus": "claude-opus-caller-fixture"})
                    _ = claude_catalog_environment(world.home, {"opus": expected_model})
                    _ = cast(object, world.case.enterContext(patch.dict(os.environ, environment)))
                if existing:
                    world.case.client.rows[0]["label"] = "dwight"
                    world.case.tab_titles["w1:t9"] = "dwight"
                result = stop_resume.resume("dwight")
                self.assertIsInstance(result, stop_resume.ClaudeUp, result)
                record = staff.initial_snapshot()
                self.assertEqual(len(record.rows), 1)
                row = record.rows[0]
                self.assertEqual((row.kind, row.name, row.id, row.current_session, row.parent),
                    ("producer", "dwight", world.identity, world.identity, staff.STAFF_ROOT))
                self.assertEqual(row.home, staff.SessionHome(world.home))
                self.assertEqual(row.worktree, staff.PresentWorktree(remote.local_machine(), world.folder))
                self.assertEqual(row.doc, staff.LiveDoc(remote.local_machine(), world.folder))
                self.assertEqual(world.case.client.opened[0][1:4], ("producers", "dwight", str(world.folder)))
                self.assertNotIn("--resume", world.arguments[0])
                self.assertNotIn("resume", world.arguments[0])
                self.assertEqual(world.arguments[0][world.arguments[0].index("--model") + 1],
                                 expected_model)
                self.assertIn("high" if kind == "claude" else 'model_reasoning_effort="high"', world.arguments[0])
                publication = pane_record.read("agents")["w1:p3"]
                self.assertEqual(publication.get("staff_id"), row.id)
                self.assertTrue(publication.get("producer_binding", False))
                if check_binding:
                    self.assertEqual([tab["staff_id"]
                        for space in herdr.saved_layout("agents")["workspaces"] for tab in space["tabs"]],
                        [row.id])
                for reference in ("dwight", str(row.id)):
                    self.assertIsInstance(stop_resume.resume(reference), stop_resume.ClaudeUp)
                self.assertEqual(len(world.arguments), 1)
                self.assertEqual(world.case.client.closed, [])

        for location in ("registry", "codex-index", "codex-account-index", "codex-dual-source", "pane-label", "tab-label", "two-live"):
            kind: Literal["claude", "codex"] = "codex" if location.startswith("codex") else "claude"
            with self.subTest(location=location), producer_first_fixture(kind) as world:
                case = world.case
                world.active = True
                world.environment = {"HERDR_SESSION": "agents", "HERDR_PANE_ID": world.pane,
                                     "CLAUDE_CONFIG_DIR" if kind == "claude" else "CODEX_HOME": str(world.home)}
                case.client.rows.append({"pane_id": world.pane, "terminal_id": "manual-terminal",
                    "tab_id": "manual-tab", "workspace_id": "w1", "label": "manual",
                    "agent_session": {"kind": "id", "value": world.identity}})
                case.client.native_ids[world.pane] = world.identity
                if location in ("registry", "two-live"):
                    directory = case.root / "sessions"
                    directory.mkdir(exist_ok=True)
                    _ = (directory / "456.json").write_text(json.dumps({
                        "pid": 456, "sessionId": world.identity, "name": "dwight", "cwd": str(world.folder),
                        "messagingSocketPath": "", "updatedAt": 1, "procStart": "generation-1"}))
                    if location == "two-live":
                        second = fixture_runtime_id("second-live-dwight")
                        _ = (directory / "457.json").write_text(json.dumps({
                            "pid": 457, "sessionId": second, "name": "dwight", "cwd": str(world.folder),
                            "messagingSocketPath": "", "updatedAt": 1, "procStart": "generation-1"}))
                        case.client.rows.append({"pane_id": "w1:p4", "terminal_id": "second-terminal",
                            "tab_id": "second-tab", "workspace_id": "w1", "label": "dwight",
                            "agent_session": {"kind": "id", "value": second}})
                        original_result = case.client.result

                        def second_process(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
                            if arguments == ("pane", "process-info", "--pane", "w1:p4"):
                                return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 457,
                                    "foreground_processes": [{"pid": 457, "argv": ["claude"]}]}}
                            return original_result(*arguments, deadline=deadline)

                        _ = case.enterContext(patch.object(case.client, "result", side_effect=second_process))
                elif location in ("codex-index", "codex-account-index", "codex-dual-source"):
                    home = case.root / (".codex" if location == "codex-index" else ".codex7")
                    home.mkdir()
                    _ = (home / "session_index.jsonl").write_text(json.dumps({
                        "id": world.identity, "thread_name": "dwight"}) + "\n")
                    cast(Mock, herdr.codex_conversations).return_value = herdr.CodexConversations({world.identity: (456,)}, ())
                    if location == "codex-dual-source":
                        case.client.rows[-1]["label"] = "dwight"
                        _ = case.client.rows[-1].pop("agent_session")
                elif location == "pane-label":
                    case.client.rows[-1]["label"] = "dwight"
                else:
                    case.tab_titles["manual-tab"] = "dwight"
                if location in ("registry", "codex-index", "codex-account-index"):
                    self.assertEqual(stop_resume.producer_conflicts("dwight", case.client), [])
                if location in ("registry", "codex-index", "codex-account-index", "two-live"):
                    # The native launch record identifies the producer before staff admission.
                    case.client.rows[-2 if location == "two-live" else -1]["label"] = "dwight"
                entry = pane_launch.launch(world.pane, world.identity, str(world.folder), world.environment,
                    [kind, "-n", "dwight"], kind=kind)
                pane_record.write("agents", entry)
                if location == "two-live":
                    second = fixture_runtime_id("second-live-dwight")
                    entry = pane_launch.launch("w1:p4", second, str(world.folder), world.environment,
                        ["claude", "-n", "dwight"], kind="claude")
                    pane_record.write("agents", entry)
                    native_process = herdr.native_process
                    def second_native(pid: int, family: str, reported: str | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> herdr.NativeAgentProcess:
                        if pid == 457:
                            return herdr.NativeAgentProcess(herdr.EnvironmentThread(second), "generation-1", world.environment)
                        return native_process(pid, family, reported)
                    _ = case.enterContext(patch.object(herdr, "native_process", side_effect=second_native))
                result = stop_resume.resume("dwight")
                if location == "two-live":
                    self.assertIsInstance(result, stop_resume.ResumeFailed, result)
                    assert isinstance(result, stop_resume.ResumeFailed)
                    for identity, pane in ((world.identity, world.pane), (fixture_runtime_id("second-live-dwight"), "w1:p4")):
                        self.assertIn(identity, result.reason)
                        self.assertIn(pane, result.reason)
                    self.assertEqual(len(result.reason.splitlines()), 1)
                    self.assertEqual(staff.initial_snapshot().rows, ())
                else:
                    self.assertIsInstance(result, stop_resume.ClaudeUp, result)
                    assert isinstance(result, stop_resume.ClaudeUp)
                    self.assertEqual((result.session_id, result.pane_id, result.start_state),
                                     (world.identity, world.pane, ClaudeStartState.ALREADY_RUNNING))
                    self.assertEqual([(row.name, row.current_session, row.home) for row in staff.initial_snapshot().rows],
                                     [("dwight", world.identity, staff.SessionHome(world.home))])
                self.assertEqual(world.arguments, [])
                self.assertEqual(case.client.opened, [])
                self.assertEqual(list((case.root / "panes").glob(".producer-admission-*")), [])

        for reference, owned in (("dwight", True), ("old producer title", True), ("dwight", False)):
            with self.subTest(daemon_name=reference, owned=owned), producer_first_fixture("codex") as world:
                if owned:
                    member = fixture_member("producer", name="dwight", runtime_id=fixture_runtime_id("fixed-producer"))
                    member = staff.update_current_session(member.current_session, staff.native_id(world.identity))
                    self.assertNotEqual(member.id, member.current_session)
                read = codex_daemon.DaemonRead(
                    codex_daemon.CodexDaemon(world.home, world.home / "daemon.sock"),
                    [codex_daemon.DaemonSession(world.identity, str(world.folder), "idle")])
                with patch.object(codex_daemon, "read_daemons", return_value=[read]):
                    self.assertEqual(stop_resume.producer_conflicts(reference, world.case.client),
                                     [(world.identity, "outside herdr")] if owned and reference == "dwight" else [])

        for kind in ("claude", "codex"):
            with self.subTest(renamed_producer=kind), producer_first_fixture(kind) as world:
                member = staff.StaffRow(staff.StaffId("fixed-producer"), "producer", "renamed-producer", staff.StaffRoot(),
                                        current_session=staff.native_id(world.identity))
                directory = world.case.root / "sessions"
                directory.mkdir(exist_ok=True)
                _ = (directory / "456.json").write_text(json.dumps({
                    "pid": 456, "sessionId": world.identity, "name": "old-producer", "cwd": str(world.folder),
                    "messagingSocketPath": "", "updatedAt": 1, "procStart": "generation-1"}))
                world.home.mkdir(exist_ok=True)
                _ = (world.home / "session_index.jsonl").write_text(json.dumps({
                    "id": world.identity, "thread_name": "old-producer"}) + "\n")
                with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((member,))), \
                     patch.object(herdr, "process_environment", return_value={}), \
                     patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                         {world.identity: (456,)} if kind == "codex" else {}, ())):
                    self.assertEqual(stop_resume.producer_conflicts(member.name, world.case.client),
                                     [(world.identity, "outside herdr")])
                    self.assertEqual(stop_resume.producer_conflicts("old-producer", world.case.client), [])

    def test_reserved_producer_shell_retries_with_the_current_claude_group_account(self) -> None:
        with producer_first_fixture("claude") as world:
            with patch.object(world.case.client, "start_agent", side_effect=OSError("fixture launch failure")):
                self.assertIsInstance(stop_resume.resume("dwight"), stop_resume.ResumeFailed)
            self.assertEqual(staff.initial_snapshot().rows, ())
            self.assertEqual(world.environment["CLAUDE_CONFIG_DIR"], str(world.home))
            target = world.case.root / ".claude4"
            with patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude4", target)) as select:
                result = stop_resume.resume("dwight")
            self.assertIsInstance(result, stop_resume.ClaudeUp, result)
            assert isinstance(result, stop_resume.ClaudeUp)
            select.assert_called_once_with("producer")
            self.assertEqual((result.session_id, result.pane_id, result.account), (world.identity, "w1:p3", "claude4"))
            self.assertEqual(world.environment["CLAUDE_CONFIG_DIR"], str(target))
            self.assertEqual(staff.initial_snapshot().rows[0].home, staff.SessionHome(target))
            self.assertEqual(len(world.case.client.opened), 1)

    def test_interrupted_producer_admission_reuses_its_live_native_conversation(self) -> None:
        lost_stages = {"lost", "replaced", "unpublished", "foreign-recovery", "native-recovery"}
        first_ownership_stages = {"foreign-first", "native-first"}
        recovery_ownership_stages = {"foreign-recovery", "native-recovery"}
        ownership_stages = first_ownership_stages | recovery_ownership_stages
        resumed_stages = {"lost", "replaced"} | recovery_ownership_stages
        for stage in ("admission", "publication", "lost", "replaced", "unpublished", "named-before-retry", "unrecorded",
                      "foreign-first", "foreign-recovery", "native-first", "native-recovery", "broken-registry", "live-replacement"):
            with self.subTest(stage=stage), producer_first_fixture("claude") as world:
                if stage == "publication":
                    boundary = patch.object(pane_launch, "staff_admitted", side_effect=OSError("fixture publication failure"))
                elif stage in ("unpublished", "named-before-retry"):
                    boundary = patch.object(world.case.client, "start_agent", side_effect=OSError("fixture launch failure"))
                elif stage == "unrecorded" or stage in first_ownership_stages:
                    boundary = patch.object(stop_resume, "write_text_atomic", side_effect=OSError("fixture reservation failure"))
                else:
                    boundary = patch.object(staff, "ensure", side_effect=staff.StaffUnreadable("fixture admission failure"))
                with boundary:
                    self.assertIsInstance(stop_resume.resume("dwight"), stop_resume.ResumeFailed)
                self.assertEqual(len(staff.initial_snapshot().rows), 1 if stage == "publication" else 0)
                if stage == "named-before-retry":
                    registry = world.case.root / "sessions"
                    registry.mkdir(exist_ok=True)
                    _ = (registry / "123.json").write_text(json.dumps({
                        "pid": 123, "sessionId": fixture_runtime_id("manual-dwight"), "name": "dwight",
                        "messagingSocketPath": "", "updatedAt": 1, "procStart": "generation-1"}))
                if stage == "live-replacement":
                    _ = world.case.client.native_ids.pop(world.pane)
                    world.identity = fixture_runtime_id("replacement-dwight")
                    world.pane = "w1:p4"
                    world.environment["HERDR_PANE_ID"] = world.pane
                    world.case.client.native_ids[world.pane] = world.identity
                    world.case.client.rows.append({"pane_id": world.pane, "terminal_id": "replacement-terminal",
                        "tab_id": "replacement-tab", "workspace_id": "w1", "label": "dwight",
                        "agent_session": {"kind": "id", "value": world.identity}})
                    replacement = pane_launch.launch(world.pane, world.identity, str(world.folder), world.environment,
                        ["claude", "-n", "dwight"], kind="claude")
                    pane_record.write("agents", replacement)
                if stage in lost_stages:
                    world.active = False
                    _ = world.case.client.native_ids.pop(world.pane, None)
                    if stage == "replaced":
                        world.case.client.rows[-1]["terminal_id"] = "another-terminal"
                    else:
                        _ = world.case.client.rows.pop()
                foreign_pane = "w1:p9" if stage in recovery_ownership_stages else world.pane
                foreign = world.case.entry.copy()
                foreign["pane_id"] = foreign_pane
                if stage in ownership_stages:
                    if stage in recovery_ownership_stages:
                        world.case.client.rows.append({"pane_id": foreign_pane, "terminal_id": "foreign-terminal",
                            "tab_id": "foreign-tab", "label": "dwight", "workspace_id": "w1"})
                    if stage.startswith("foreign-"):
                        pane_record.write("agents", foreign)
                        foreign = pane_record.read_strict("agents")[foreign_pane]
                    else:
                        world.case.client.rows[-1]["agent_session"] = {"kind": "id", "value": cast(pane_launch.NativePaneLaunch, foreign)["session_id"]}
                if stage == "broken-registry":
                    receipts = {path.name: path.read_text() for path in (world.case.root / "panes").glob(".producer-admission-*")}
                    with patch.dict(os.environ):
                        _ = os.environ.pop("MODELS_LAUNCH_CONFIGURATION", None)
                        os.environ["MODELS_CONFIG_FILE"] = str(world.case.root / "absent-models.conf")
                        failed = stop_resume.resume("dwight")
                    self.assertIsInstance(failed, stop_resume.ResumeFailed, failed)
                    self.assertEqual(staff.initial_snapshot().rows, ())
                    self.assertEqual({path.name: path.read_text() for path in (world.case.root / "panes").glob(".producer-admission-*")}, receipts)
                result = stop_resume.resume("dwight")
                if stage == "named-before-retry":
                    self.assertIsInstance(result, stop_resume.ClaudeUp, result)
                    assert isinstance(result, stop_resume.ClaudeUp)
                    self.assertEqual(result.start_state, ClaudeStartState.STARTED)
                    self.assertEqual(len(world.case.client.opened), 1)
                    self.assertEqual(world.case.client.closed, [])
                if stage in ownership_stages:
                    self.assertNotIn(foreign_pane, world.started_panes)
                    if stage.startswith("foreign-"):
                        self.assertEqual(pane_record.read_strict("agents")[foreign_pane], foreign)
                    else:
                        self.assertEqual(world.case.client.rows[-2]["agent_session"], {"kind": "id", "value": cast(pane_launch.NativePaneLaunch, foreign)["session_id"]})
                self.assertIsInstance(result, stop_resume.ClaudeUp, result)
                if stage == "live-replacement":
                    assert isinstance(result, stop_resume.ClaudeUp)
                    self.assertEqual((result.session_id, result.pane_id, result.start_state),
                                     (world.identity, world.pane, ClaudeStartState.ALREADY_RUNNING))
                self.assertEqual(staff.initial_snapshot().rows[0].current_session, world.identity)
                self.assertEqual(len(staff.initial_snapshot().rows), 1)
                self.assertEqual(len(world.arguments), 2 if stage in resumed_stages else 1)
                self.assertEqual(len(world.case.client.opened), 2 if stage in lost_stages | first_ownership_stages | {"unrecorded"} else 1)
                if stage == "unrecorded":
                    self.assertEqual([pane["pane_id"] for pane in world.case.client.rows
                                      if pane["pane_id"] in world.started_panes or pane["label"] == "dwight"], [world.pane])
                self.assertTrue(pane_record.read("agents")[world.pane].get("producer_binding", False))
                self.assertEqual(list((world.case.root / "panes").glob(".producer-admission-*")), [])
                if stage in resumed_stages:
                    self.assertIn("--resume", world.arguments[-1])
                    self.assertEqual(world.arguments[-1][world.arguments[-1].index("--resume") + 1], world.identity)
                    self.assertNotIn("w1:p3", pane_record.read("agents"))
                elif stage == "unpublished":
                    self.assertNotIn("--resume", world.arguments[-1])

    def test_stop_unit_workers_rerun_after_pane_stop(self) -> None:
        parent = staff.StaffId("holder")
        rows = [staff.StaffRow(staff.StaffId(f"row-{i}"), "worker", f"worker-{i}", staff.ReportsTo(parent),
                              current_session=staff.StaffId(f"native-{i}")) for i in range(5)]
        states: list[worker_panes.WorkerPane] = [
            worker_panes.OpenWorker(rows[0], "p0", "t0", herdr.AgentStatus.WORKING),
            worker_panes.StoppedWorker(rows[1], "p1", "t1"), worker_panes.ClosedWorker(rows[2]),
            worker_panes.ProcessUnknownWorker(rows[3], "p3", "t3", "unreadable process"),
            worker_panes.UncertainWorker(rows[4], "duplicate terminal")]
        record = staff.StaffSnapshot(tuple(rows))
        def finish(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
            del client
            self.assertEqual(row, rows[0])
            self.assertEqual(deadline, math.inf)
            states[0] = worker_panes.StoppedWorker(rows[0], "p0", "t0")
            return True
        def observed(_parent: staff.StaffId, _record: staff.StaffSnapshot) -> list[worker_panes.WorkerPane]:
            return list(states)
        with (patch.object(worker_panes, "observe", side_effect=observed),
              patch.object(restart, "stop_row", side_effect=finish) as stop):
            expected = ["worker-3: unreadable process", "worker-4: duplicate terminal"]
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(stop_resume.stop_unit_workers(parent, record), expected)
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(stop_resume.stop_unit_workers(parent, record), expected)
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(record.rows, tuple(rows))

    def test_unresolved_worker_has_one_report_line_and_keeps_stop_diagnostics(self) -> None:
        parent = staff.StaffId("holder")
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(parent),
                             current_session=staff.StaffId("native-worker"))
        def stop(_row: staff.StaffRow, _client: herdr.Herdr, *, deadline: float) -> bool:
            del deadline
            print("warning: recovered shutdown configuration", file=sys.stderr)
            print("impl: restart anomaly: no shell proof", file=sys.stderr)
            return False
        with (patch.object(worker_panes, "observe", return_value=[worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)]),
              patch.object(restart, "stop_row", side_effect=stop), redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(stop_resume.stop_unit_workers(parent, staff.StaffSnapshot((row,))), ["impl: not proved stopped"])
        self.assertEqual(errors.getvalue().splitlines(), ["warning: recovered shutdown configuration",
            "impl: restart anomaly: no shell proof"])

    def test_worker_stop_proves_the_retained_pane_without_a_director_tab(self) -> None:
        parent = staff.StaffId("holder")
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(parent),
                             current_session=staff.StaffId("native-worker"))
        before = worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)
        for pane, terminal, activity, conversation, success in (
                ("", "", herdr.PaneAtShell(), herdr.StoppedConversation(), True),
                ("", "", herdr.PaneAtShell(), herdr.LiveConversation("claude", (123,)), False),
                ("pane", "terminal", herdr.PaneAtShell(), herdr.StoppedConversation(), True),
                ("other-pane", "terminal", herdr.PaneAtShell(), herdr.StoppedConversation(), False),
                ("pane", "other-terminal", herdr.PaneAtShell(), herdr.StoppedConversation(), False),
                ("pane", "terminal", herdr.PaneRunning(("busy",)), herdr.StoppedConversation(), False),
                ("pane", "terminal", herdr.PaneRunning(("codex", "python")), herdr.StoppedConversation(), False),
                ("pane", "terminal", herdr.PaneAtShell(), herdr.LiveConversation("claude", (123,)), False)):
            with (self.subTest(pane=pane, terminal=terminal, activity=activity),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(worker_panes, "observe", side_effect=[[before], [worker_panes.ClosedWorker(row)]]),
                  patch.object(self.client, "panes", return_value=[{"pane_id": pane, "terminal_id": terminal}] if pane else []),
                  patch.object(self.client, "tab_sessions", return_value=[]),
                  patch.object(self.client, "pane_activity", return_value=activity),
                  patch.object(herdr, "read_conversation", return_value=conversation),
                  patch.object(stop_resume, "_stop", return_value=stop_resume.Stopped("native-worker", 123))):
                result = stop_resume.stop(row.id)
                self.assertEqual(isinstance(result, stop_resume.Stopped), success)
                if isinstance(activity, herdr.PaneRunning):
                    self.assertIsInstance(result, stop_resume.StopFailed)
                    assert isinstance(result, stop_resume.StopFailed)
                    self.assertIn("pane pane", result.reason)
                    self.assertIn(f"foreground processes: {', '.join(activity.commands)}", result.reason)

    def test_worker_stop_with_a_closed_pane_requires_a_complete_registry(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId("native-worker"))
        before = worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)
        for cause in ("missing folder", "unreadable record"):
            registry = self.root / cause
            unreadable = registry
            if cause == "unreadable record":
                registry.mkdir()
                unreadable = registry / "unfinished.json"
                _ = unreadable.write_text("{")
            with (self.subTest(cause=cause),
                  patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(registry)}),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(worker_panes, "observe", return_value=[before]),
                  patch.object(self.client, "panes", return_value=[]),
                  patch.object(runtime_sessions, "session_liveness", side_effect=session_records.session_liveness),
                  patch.object(stop_resume, "_stop", return_value=stop_resume.Stopped(row.current_session, 123)),
                  patch.object(stop_resume, "_retire_prompt") as retired):
                result = stop_resume.stop(row.id, "claude")
            self.assertIsInstance(result, stop_resume.StopUnproved)
            assert isinstance(result, stop_resume.StopUnproved)
            self.assertIn(f"session registry unreadable: {unreadable}", result.reason)
            retired.assert_not_called()
        self.kill.assert_not_called()

    def test_worker_stop_rejects_an_unreadable_retained_pane(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId("native-worker"))
        before = worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker_panes, "observe", return_value=[before]),
              patch.object(self.client, "panes", side_effect=herdr_types.HerdrFailure("unreadable")),
              patch.object(stop_resume, "_stop", return_value=stop_resume.Stopped("native-worker", 123))):
            result = stop_resume.stop(row.id)
        self.assertIsInstance(result, stop_resume.StopFailed)

    def test_recorded_codex_worker_checks_both_stop_steps_without_the_claude_registry(self) -> None:
        identity = self.entry["session_id"]
        home = self.root / ".codex"
        (home / "sessions").mkdir(parents=True, exist_ok=True)
        (home / "sessions" / f"rollout-{identity}.jsonl").touch()
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId(identity), home=staff.SessionHome(home))
        snapshot = staff.StaffSnapshot((row,), running_sessions=frozenset())
        self.assertEqual(snapshot.backends.get(row.id), staff.CodexBackend(home))
        atomic_file.write_text_atomic(pane_record.record_path("agents"), "{}\n")
        with (patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.root / "missing-sessions")}),
              patch.object(staff, "initial_snapshot", return_value=snapshot),
              patch.object(worker_panes, "observe", return_value=[
                  worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)]),
              patch.object(self.client, "panes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", wraps=session_records.session_liveness) as claude,
              patch.object(herdr, "read_conversation", wraps=herdr.read_conversation) as native):
            self.assertIsNone(pane_record.publication_for_session(identity))
            for reference in (row.id, row.name):
                with self.subTest(reference=reference):
                    native.reset_mock()
                    self.assertEqual(stop_resume.stop(reference), stop_resume.NotRunning(identity))
                    self.assertEqual([seen.args for seen in native.call_args_list],
                                     [(identity, "codex"), (identity, "codex")])
        claude.assert_not_called()
        self.kill.assert_not_called()

    def test_stopped_codex_worker_with_unknown_thread_cannot_prove_absence(self) -> None:
        identity = self.entry["session_id"]
        home = self.root / ".codex"
        (home / "sessions").mkdir(parents=True, exist_ok=True)
        (home / "sessions" / f"rollout-{identity}.jsonl").touch()
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId(identity), home=staff.SessionHome(home))
        snapshot = staff.StaffSnapshot((row,), running_sessions=frozenset())
        self.assertEqual(snapshot.backends.get(row.id), staff.CodexBackend(home))
        atomic_file.write_text_atomic(pane_record.record_path("agents"), "{}\n")
        with (patch.object(staff, "initial_snapshot", return_value=snapshot),
              patch.object(worker_panes, "observe", return_value=[
                  worker_panes.StoppedWorker(row, "pane", "terminal")]),
              patch.object(self.client, "panes", return_value=[]),
              patch.object(stop_resume, "_retire_prompt") as retired):
            result = stop_resume.stop(row.id, codex=herdr.CodexConversations({}, ("sdlc (Codex thread unknown)",)))
        self.assertIsInstance(result, stop_resume.StopUnproved)
        assert isinstance(result, stop_resume.StopUnproved)
        self.assertIn("Codex thread unknown", result.reason)
        retired.assert_not_called()
        self.kill.assert_not_called()

    def test_stopped_codex_worker_needs_no_launch_or_signal_on_a_repeated_stop(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId(identity))
        self.client.rows[0]["agent_session"] = None
        atomic_file.write_text_atomic(pane_record.record_path("agents"), "{}\n")
        observed = worker_panes.StoppedWorker(row, "w1:p2", "w1:p2-terminal")
        results: list[StopResult] = []
        stop = stop_resume.stop
        def finish(session: str, *, deadline: float) -> StopResult:
            result = stop(session, deadline=deadline)
            results.append(result)
            return result
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker_panes, "observe", return_value=[observed]),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())),
              patch.object(stop_resume, "stop", side_effect=finish),
              patch.object(stop_resume, "_stop", wraps=stop_resume._stop) as native_stop,  # pyright: ignore[reportPrivateUsage]
              patch.object(stop_resume, "_worker_stopped", wraps=stop_resume._worker_stopped) as proof,  # pyright: ignore[reportPrivateUsage]
              redirect_stderr(io.StringIO()) as errors):
            self.assertIsNone(pane_record.publication_for_session(identity))
            self.assertTrue(restart.stop_row(row, self.client))
        self.assertEqual(results, [stop_resume.NotRunning(identity)])
        native_stop.assert_not_called()
        self.assertEqual(proof.call_count, 1)
        self.kill.assert_not_called()
        self.assertEqual(errors.getvalue(), "")

    def test_closed_worker_still_signals_its_live_native_conversation(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId(identity))
        self.client.rows.clear()
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker_panes, "observe", return_value=[worker_panes.ClosedWorker(row)]),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (123,)}, ())),
              patch.object(stop_resume, "_stop", wraps=stop_resume._stop) as native_stop):  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(restart.stop_row(row, self.client))
        native_stop.assert_called_once_with(identity, "")
        self.kill.assert_called_once_with(123, signal.SIGTERM)

    def test_closed_worker_needs_no_retained_shell_proof(self) -> None:
        row = staff.StaffRow(staff.StaffId("closed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId("native-worker"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker_panes, "observe", return_value=[worker_panes.ClosedWorker(row)]),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning("native-worker"))):
            self.assertEqual(stop_resume.stop(row.id), stop_resume.NotRunning("native-worker"))

    def test_shutdown_uses_committed_values_and_keeps_the_recovery_warning(self) -> None:
        config = self.root / "committed-config"
        path = config / "shutdown.conf"
        atomic_file.write_text_atomic(path, "# range: 5e-324..\nSTOP_GRACE_SECONDS=.2\n# range: 5e-324..\nKILL_GRACE_SECONDS=.3\n")
        for args in (("init", "-q"), ("add", "shutdown.conf"),
                     ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "settings")):
            _ = subprocess.run(["git", "-C", str(config), *args], capture_output=True, check=True)
        atomic_file.write_text_atomic(path, "# range: 5e-324..\nSTOP_GRACE_SECONDS=bad\n# range: 5e-324..\nKILL_GRACE_SECONDS=.3\n")
        self.process.side_effect = self.alive_until_kill
        self.live.return_value = sessions.LiveSessionProcess(123)
        with patch.dict(os.environ, {"CONF_DIR": str(config)}), redirect_stderr(io.StringIO()) as warnings:
            result = stop_resume.stop(self.entry["session_id"])
        self.assertIsInstance(result, stop_resume.Stopped)
        self.assertIn("STOP_GRACE_SECONDS='bad'", warnings.getvalue())
        self.assertAlmostEqual(self.clock.elapsed, .2)

    def test_worker_stops_share_the_outer_deadline_concurrently(self) -> None:
        parent = staff.StaffId("holder")
        rows = tuple(staff.StaffRow(staff.StaffId(f"row-{i}"), "worker", f"worker-{i}", staff.ReportsTo(parent),
                                   current_session=staff.StaffId(f"native-{i}")) for i in range(3))
        states = [worker_panes.OpenWorker(row, row.name, row.name, herdr.AgentStatus.WORKING) for row in rows]
        config = self.root / "config"
        atomic_file.write_text_atomic(config / "shutdown.conf", "# range: 5e-324..\nSTOP_GRACE_SECONDS=.25\n# range: 5e-324..\nKILL_GRACE_SECONDS=.5\n")
        joined = Barrier(3)
        def stop(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
            del row, client
            self.assertEqual(deadline, outer_deadline)
            _ = joined.wait(timeout=5)
            return True
        outer_deadline = time.monotonic() + 100
        with (operation_deadline.within(outer_deadline),
              patch.dict(os.environ, {"CONF_DIR": str(config)}),
              patch.object(worker_panes, "observe", return_value=states),
              patch.object(restart, "stop_row", side_effect=stop)):
            self.assertEqual(stop_resume.stop_unit_workers(parent, staff.StaffSnapshot(rows)), [])

    def test_slow_reads_preserve_each_concurrent_signal_grace(self) -> None:
        parent = staff.StaffId("holder")
        rows = tuple(staff.StaffRow(staff.StaffId(f"row-{i}"), "worker", f"worker-{i}", staff.ReportsTo(parent),
                                   current_session=staff.StaffId(f"native-{i}")) for i in range(3))
        states = [worker_panes.OpenWorker(row, row.name, row.name, herdr.AgentStatus.WORKING) for row in rows]
        for stop_grace, kill_grace in ((.25, .5), (.5, 1)):
            joined, lock = Barrier(3), Lock()
            elapsed: list[float] = []
            self.kill.reset_mock()
            self.clock.elapsed = 0
            config = self.root / "config"
            atomic_file.write_text_atomic(config / "shutdown.conf",
                f"# range: 5e-324..\nSTOP_GRACE_SECONDS={stop_grace}\n# range: 5e-324..\nKILL_GRACE_SECONDS={kill_grace}\n")
            def read(identity: str, _kind: str = "") -> herdr.LiveConversation:
                self.advance(10)
                _ = joined.wait(timeout=5)
                return herdr.LiveConversation("claude",  (100 + int(identity.rsplit("-", 1)[1]),))
            def native(pid: int, _kind: str) -> herdr.NativeAgentProcess:
                return herdr.NativeAgentProcess(herdr.EnvironmentThread(str(rows[pid - 100].current_session)), "started", {})
            def send(pid: int, number: int) -> None:
                operation_deadline.check()
                self.kill(pid, number)
            def stopped(session: str, kind: str = "") -> StopResult:
                result = stop_resume._stop(session, kind)  # pyright: ignore[reportPrivateUsage]
                with lock:
                    elapsed.append(self.clock.elapsed)
                return result
            with (self.subTest(stop_grace=stop_grace, kill_grace=kill_grace),
                  patch.dict(os.environ, {"CONF_DIR": str(config)}),
                  patch.object(time, "monotonic", lambda: self.clock.elapsed),
                  patch.object(staff, "row_operation", return_value=nullcontext()),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)),
                  patch.object(worker_panes, "observe", return_value=states),
                  patch.object(self.client, "panes", return_value=[]),
                  patch.object(stop_resume, "_stop_recorded", side_effect=stopped),
                  patch.object(stop_resume, "_same_process", return_value=True),
                  patch.object(stop_resume, "_signal_process", side_effect=send),
                  patch.object(herdr, "read_conversation", side_effect=read),
                  patch.object(herdr, "native_process", side_effect=native),
                  patch.object(herdr, "check_native_target", return_value=True)):
                self.assertEqual(len(stop_resume.stop_unit_workers(parent, staff.StaffSnapshot(rows))), 3)
            self.assertEqual(self.kill.call_count, 6)
            self.assertEqual(len(elapsed), 3)
            for duration in elapsed:
                self.assertAlmostEqual(duration, 10 + stop_grace + kill_grace)

    def test_unknown_stop_reason_prints_once_through_each_caller(self) -> None:
        parent = staff.StaffId("holder")
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(parent),
                             current_session=staff.StaffId("native-worker"))
        for batch in (False, True):
            with (self.subTest(batch=batch),
                  patch.object(staff, "row_operation", return_value=nullcontext()),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(self.client, "panes", return_value=[]),
                  patch.object(stop_resume, "_record_launches"),
                  patch.object(worker_panes, "observe", return_value=[worker_panes.OpenWorker(row, "pane", "terminal", herdr.AgentStatus.WORKING)]),
                  patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation("native evidence unavailable")),
                  redirect_stderr(io.StringIO()) as errors):
                if batch:
                    self.assertEqual(stop_resume.stop_unit_workers(parent, staff.StaffSnapshot((row,))), ["impl: not proved stopped"])
                else:
                    self.assertFalse(restart.stop_row(row, self.client))
            self.assertEqual(errors.getvalue().splitlines(), ["impl: restart anomaly: native evidence unavailable"])

    def test_stop_uses_the_runtime_from_an_ended_launch_with_a_fixed_staff_id(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-agent"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity),
                             home=staff.SessionHome(self.root / ".claude3"))
        for kind in ("claude", "codex"):
            entry = self.entry.copy()
            entry.update(session_id=str(row.id), kind=kind, ended=True)
            pane_record.write("agents", entry)
            unknown = ("accounts (Codex thread unknown)",) if kind == "claude" else ()
            with (self.subTest(kind=kind),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
                  patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, unknown)),
                  patch.object(herdr, "read_conversation", wraps=herdr.read_conversation) as read):
                self.assertEqual(stop_resume.stop(row.id), stop_resume.NotRunning(identity))
                read.assert_called_once_with(identity, kind)
        self.kill.assert_not_called()

    def test_stop_without_a_publication_still_finds_a_live_codex_session(self) -> None:
        identity = self.entry["session_id"]
        self.process.side_effect = self.alive_until_kill
        with (patch.object(pane_record, "publication_for_session", return_value=None),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (123,)}, ("unrelated unknown",))),
              patch.object(herdr, "read_conversation", wraps=herdr.read_conversation) as read):
            self.assertEqual(stop_resume.stop(identity), stop_resume.Stopped(identity, 123))
        read.assert_called_once_with(identity, "")

    def test_launch_read_error_reports_once_and_reads_both_runtimes(self) -> None:
        identity = self.entry["session_id"]
        with (patch.object(pane_record, "publication_for_session", side_effect=OSError("record unreadable")),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()) as read,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(stop_resume.stop(identity), stop_resume.NotRunning(identity))
        read.assert_called_once_with(identity, "")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("record unreadable", errors.getvalue())

    def test_unusable_shutdown_values_send_no_signal_or_start(self) -> None:
        config = self.root / "config"
        for key in ("STOP_GRACE_SECONDS", "KILL_GRACE_SECONDS", "START_SECONDS", "STAY_SECONDS"):
            values = {name: "1" for name in ("STOP_GRACE_SECONDS", "KILL_GRACE_SECONDS", "START_SECONDS", "STAY_SECONDS")}
            values[key] = "0"
            atomic_file.write_text_atomic(config / "shutdown.conf", "\n".join(f"# range: 5e-324..\n{name}={value}" for name, value in values.items()))
            with (self.subTest(key=key), patch.dict(os.environ, {"CONF_DIR": str(config)}),
                  patch.object(stop_resume, "_signal_process") as send,
                  patch.object(stop_resume, "_start_session") as start):
                result = stop_resume.stop(self.entry["session_id"]) if "GRACE" in key else stop_resume.resume(self.entry["session_id"])
            self.assertIsInstance(result, (stop_resume.StopFailed, stop_resume.ResumeFailed))
            self.assertIn(key, str(result))
            self.assertIn(str(config / "shutdown.conf"), str(result))
            send.assert_not_called()
            start.assert_not_called()

    def test_tab_lookup_keeps_the_conversation_tab_when_staff_is_unreadable(self) -> None:
        identity = self.entry["session_id"]
        for error in (OSError("staff unreadable"), ValueError("staff invalid")):
            with (self.subTest(error=error),
                  patch.object(staff, "initial_snapshot", side_effect=error),
                  redirect_stderr(io.StringIO()) as output):
                tab = stop_resume._tab_of(identity)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(tab, herdr.TabSession("w1:p2", "alpha", "w1", identity))
            self.assertEqual(output.getvalue().splitlines(), [f"launch names: staff is unavailable: {error}; saved names retained"])

        with self.subTest(staff="unrelated removed member"):
            record = staff.StaffSnapshot(())
            observed = self.entry.copy()
            observed.update(pane_id="w1:p9", staff_id="retired-member")
            unrelated = pane_launch.retained_launch(observed, record)
            pane_launch.save(pane_record.record_path("agents"), {"w1:p2": self.entry, "w1:p9": unrelated}, record=record)
            self.client.rows.append({"pane_id": "w1:p9", "terminal_id": "retired-terminal", "label": "unrelated",
                                    "workspace_id": "w1", "agent_session": {"kind": "none"}})
            with patch.object(staff, "initial_snapshot", return_value=record):
                tab = stop_resume._tab_of(identity)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(tab, herdr.TabSession("w1:p2", "alpha", "w1", identity))

        with self.subTest(binding="newer retained shell"):
            row = staff.StaffRow(staff.StaffId(identity), "unit", "alpha", staff.STAFF_ROOT,
                current_session=staff.StaffId(identity),
                worktree=staff.PresentWorktree(remote.local_machine(), self.root),
                unit_type=staff.UnitDetails("direct"),
                home=staff.SessionHome(self.root / ".claude3"))
            record = staff.StaffSnapshot((row,))
            old = self.entry.copy()
            old["written_at"] = "2026-01-01T00:00:00+00:00"
            newer = old.copy()
            newer.update(pane_id="w1:p4", written_at="2026-02-01T00:00:00+00:00")
            self.client.rows.append({**self.client.rows[0], "pane_id": "w1:p4", "tab_id": "w1:t10",
                                     "terminal_id": "newer-terminal"})
            pane_record.save("agents", {"w1:p2": old, "w1:p4": newer}, record=record)
            with (patch.object(staff, "initial_snapshot", return_value=record),
                  patch.object(stop_resume, "_start_session", return_value=sessions.LiveSessionProcess(456)) as start,
                  patch.object(stop_resume, "_watch_resume")):
                result = stop_resume.resume(row.name)
            self.assertIsInstance(result, stop_resume.ClaudeUp)
            assert isinstance(result, stop_resume.ClaudeUp)
            self.assertEqual(result.pane_id, "w1:p4")
            self.assertEqual(start.call_count, 1)
            self.assertEqual(cast(herdr.TabSession, start.call_args.args[0]).pane_id, "w1:p4")
            self.assertEqual(set(pane_record.read("agents")), {"w1:p2", "w1:p4"})

    def test_up_lines_name_the_agent_from_the_account(self) -> None:
        for label, agent in (("codex1", "Codex"), ("claude1", "Claude")):
            with self.subTest(account=label):
                started = stop_results.ClaudeUp("worker", "w1:p2", label, elapsed_seconds=2.5)
                running = stop_results.ClaudeUp("worker", "w1:p2", label,
                                              start_state=ClaudeStartState.ALREADY_RUNNING)
                self.assertEqual(stop_results.claude_up_line(started),
                                 f"worker: {agent} is up on {label} in w1:p2 (2.5 s)")
                self.assertEqual(stop_results.claude_up_line(running),
                                 f"worker: unchanged; {agent} runs on {label} in w1:p2")

        for state, text in ((ClaudeStartState.ALREADY_RUNNING, "unchanged; Claude runs on claude3"),
                            (ClaudeStartState.STARTED, "Claude is up on claude3 (2.5 s)")):
            with self.subTest(pane="", state=state):
                result = stop_results.ClaudeUp("worker", "", "claude3", elapsed_seconds=2.5, start_state=state)
                self.assertEqual(stop_results.claude_up_line(result), f"worker: {text}")


    def test_unnumbered_accounts_name_no_agent_in_either_result_state(self) -> None:
        for label in ("default", "unknown account", "~/.codex"):
            for state, text in ((ClaudeStartState.STARTED, f"up on {label} in w1:p2 (2.5 s)"),
                                (ClaudeStartState.ALREADY_RUNNING, f"unchanged; runs on {label} in w1:p2")):
                with self.subTest(account=label, state=state):
                    result = stop_results.ClaudeUp("worker", "w1:p2", label, elapsed_seconds=2.5, start_state=state)
                    self.assertEqual(stop_results.claude_up_line(result), f"worker: {text}")

    def test_resume_uses_the_group_account_unless_the_target_is_explicit(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        for kind in ("unit", "showrunner", "producer", "missing"):
            for target in (None, "claude3"):
                with self.subTest(kind=kind, target=target):
                    pane_record.save("agents", {self.entry["pane_id"]: self.entry})
                    row = staff.StaffRow(identity, "producer" if kind == "missing" else kind, "alpha",
                                         staff.STAFF_ROOT, current_session=identity, worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct") if kind == "unit" else staff.NotUnit())
                    expected = target or "claude4"
                    rows = () if kind == "missing" else (row,)
                    with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)),
                          patch.object(accounts, "role_account", return_value=accounts.NamedAccount(
                              "claude4", self.root / ".claude4")) as selection,
                          patch.object(stop_resume, "_start_session", return_value=sessions.LiveSessionProcess(456)) as start):
                        result = stop_resume.resume(str(identity), to=target)
                    self.assertIsInstance(result, stop_resume.ClaudeUp)
                    assert isinstance(result, stop_resume.ClaudeUp)
                    self.assertEqual(result.account, expected)
                    self.assertEqual(start.call_args.args[2], expected)
                    entry = cast(pane_record.PaneLaunch, start.call_args.args[1])
                    self.assertEqual(entry["environment"]["CLAUDE_CONFIG_DIR"], str(self.root / f".{expected}"))
                    if kind == "missing":
                        self.assertEqual(str(cast(staff.SessionHome, pane_launch.config_home(entry)).path), str(self.root / f".{expected}"))
                    else:
                        self.assertNotIn("config_dir", entry)
                    if target is None:
                        role = kind if kind in ("unit", "showrunner", "producer") else "unit"
                        selection.assert_called_once_with(role)
                    else:
                        selection.assert_not_called()

    def test_resume_without_a_staff_row_on_the_same_account_reports_no_move(self) -> None:
        result = stop_resume.resume(self.entry["session_id"], why="maintenance")
        self.assertEqual(result, stop_resume.ClaudeUp(self.entry["session_id"], "w1:p2", "claude3"))
        self.assertEqual(len(self.client.prompts), 1)
        text = self.client.prompts[0][1].text
        self.assertIn("You were stopped and resumed because maintenance.", text)
        self.assertNotIn("You were moved", text)
        self.start.assert_called_once()

    def saved_codex_name(self, name: str = "saved-worker") -> str:
        identity = "019b3a40-4250-7000-8000-000000000103"
        home = self.root / ".codex2"
        atomic_file.write_text_atomic(home / "session_index.jsonl", json.dumps({
            "id": identity, "thread_name": name, "updated_at": "2026-11-06T00:00:00Z"}) + "\n")
        atomic_file.write_text_atomic(home / "sessions" / f"rollout-{identity}.jsonl", json.dumps({
            "type": "session_meta", "payload": {"id": identity, "cwd": str(self.root / "project")}}) + "\n")
        for label in ("codex1", "codex2"):
            with closing(sqlite3.connect(":memory:")) as database:
                _ = database.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
                if label == "codex2":
                    _ = database.execute("INSERT INTO threads VALUES (?)", (identity,))
                database.commit()
                atomic_file.write_bytes_atomic(self.root / f".{label}" / "state_5.sqlite", database.serialize())
        pane_record.save("agents", {})
        self.client.rows.clear()
        return identity

    def test_resume_rejects_codex_names_with_duplicate_or_distinct_index_entries(self) -> None:
        identity = self.saved_codex_name()
        path = self.root / ".codex2/session_index.jsonl"
        first = path.read_text()

        def resume_selected(target: str, **_: object) -> stop_resume.ResumeResult:
            return (stop_resume.ClaudeUp(identity, "pane", "codex1") if target == identity
                    else stop_resume.SessionNotFound(target))

        for content in (first, first + first,
                first + json.dumps({"id": "another-id", "thread_name": "saved-worker"}) + "\n",
                first + json.dumps({"id": fixture_runtime_id("another-thread"), "thread_name": "saved-worker"}) + "\n"):
            with self.subTest(content=content):
                atomic_file.write_text_atomic(path, content)
                with (patch.dict(os.environ, {"CODEX_HOME": str(path.parent)}),
                      patch.object(stop_resume, "_resume_once", side_effect=resume_selected) as once,
                      patch.object(herdr, "launch_prepared_agent") as launch):
                    result = stop_resume.resume("saved-worker")
                    self.assertEqual(result,
                                     stop_resume.ResumeFailed("saved-worker", "unknown name"))
                    self.assertIsInstance(result, stop_resume.ResumeFailed)
                    assert isinstance(result, stop_resume.ResumeFailed)
                    self.assertEqual(result.reason, "unknown name")
                    self.assertNotIn(identity, result.reason)
                    self.assertNotIn("another-id", result.reason)
                    once.assert_called_once()
                    self.assertEqual(once.call_args.args[0], "saved-worker")
                    self.assertEqual(stop_resume.resume("SAVED-WORKER"), stop_resume.ResumeFailed("SAVED-WORKER", "unknown name"))
                launch.assert_not_called()
                self.start.assert_not_called()
                self.assertNotIn(identity, self.launched)

    def test_raw_codex_id_wins_over_another_threads_name(self) -> None:
        identity = self.saved_codex_name()
        path = self.root / ".codex2/session_index.jsonl"
        write = json.dumps({"id": "another-id", "thread_name": identity}) + "\n"
        atomic_file.write_text_atomic(path, path.read_text() + write)
        with (patch.dict(os.environ, {"CODEX_HOME": str(path.parent)}),
              patch.object(stop_resume, "_resume_once", return_value=stop_resume.SessionNotFound(identity)) as once):
            _ = stop_resume.resume(identity)
        self.assertEqual(once.call_args.args[0], identity)

    def test_claude_uuid_is_not_replaced_by_a_codex_thread_name(self) -> None:
        identity = self.entry["session_id"]
        other = self.saved_codex_name(identity)
        self.assertNotEqual(identity, other)
        with (patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex2")}),
              patch.object(stop_resume, "_resume_once", return_value=stop_resume.SessionNotFound(identity)) as once):
            _ = stop_resume.resume(identity)
        self.assertEqual(once.call_args.args[0], identity)
        with patch.object(agent_accounts, "codex_home", side_effect=AssertionError("an id needs no index")):
            self.assertEqual(stop_resume._codex_name_ids(identity), [identity])  # pyright: ignore[reportPrivateUsage]

    def test_uuid_does_not_match_another_conversations_pane_label(self) -> None:
        identity = self.entry["session_id"]
        pane_record.save("agents", {})
        self.client.rows[0].update(label=identity, agent_session={"kind": "id", "value": "another-conversation"})
        tab = stop_resume._tab_of(identity, names=LaunchNames(staff.StaffSnapshot(())))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(tab, stop_resume.SessionNotFound(identity))

    def resume_codex_staff(self, observed: herdr.LiveConversation | herdr.UnknownConversation) -> tuple[stop_resume.ResumeResult, Mock, Mock, Mock, str]:
        identity = self.entry["session_id"]
        _ = write_registry(self.root / "configuration/models.conf", {"producers": {"alpha": "codex:gpt-test:high"}}, update=True)
        row = staff.StaffRow(staff.StaffId("codex-holder"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity), home=staff.SessionHome(self.root / ".codex2"))
        self.entry["kind"] = "codex"
        self.entry["config_dir"] = str(self.root / ".codex2")
        self.entry["environment"] = {"CODEX_HOME": str(cast(staff.SessionHome, pane_launch.config_home(self.entry)).path)}
        pane_record.save("agents", {"w1:p2": self.entry})
        def record_home(identity: staff.StaffId, home: staff.SessionHome) -> staff.StaffRow:
            nonlocal row
            self.assertEqual(identity, row.id)
            row = replace(row, home=home)
            return row
        with (patch.object(staff, "initial_snapshot", side_effect=lambda: staff.StaffSnapshot((row,))),
              patch.object(staff, "snapshot", side_effect=lambda: staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", side_effect=lambda: staff.StaffSnapshot((row,))),
              patch.object(staff, "set_home", side_effect=record_home),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount(
                  "codex1", self.root / ".codex1")),
              patch.object(agent_accounts, "codex_home_role_label", return_value="codex7") as source_label,
              patch.object(herdr, "read_conversation", side_effect=[herdr.StoppedConversation(), observed]),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(self.root / ".codex1")),
              patch.object(accounts, "codex_home_label", return_value="codex1"),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                  self.root, {"CODEX_HOME": str(self.root / ".codex1")})),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p2", herdr.PromptSubmissionNotRequired())) as launch,
              patch.object(self.client, "prompt") as prompt,
              redirect_stderr(io.StringIO()) as errors):
            result = stop_resume.resume("alpha", to="codex1")
        return result, launch, prompt, source_label, errors.getvalue()

    def test_codex_staff_resume_uses_native_prompt_delivery(self) -> None:
        result, launch, prompt, _label, _errors = self.resume_codex_staff(herdr.LiveConversation("codex", (456,)))
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIsInstance(cast(object, launch.call_args.args[5]), herdr.NoOpeningPrompt)
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.kwargs["target"], self.entry["session_id"])

    def test_codex_resume_message_uses_the_source_login_label(self) -> None:
        _result, _launch, prompt, label, _errors = self.resume_codex_staff(herdr.LiveConversation("codex", (456,)))
        label.assert_called_once_with(self.root / ".codex2")
        self.assertIn("codex7", cast(str, prompt.call_args.args[1]))

    def test_codex_resume_observes_the_started_process_generation(self) -> None:
        _result, _launch, _prompt, _label, errors = self.resume_codex_staff(herdr.LiveConversation("codex", (456,)))
        self.detach.assert_called_once()
        arguments = cast(list[str], self.detach.call_args.args[0])
        self.assertEqual(arguments[arguments.index("--pid") + 1], "456")
        self.assertEqual(arguments[arguments.index("--generation") + 1], "generation-1")
        self.assertEqual(errors, "")

    def test_codex_resume_reports_one_anomaly_when_the_started_pid_is_unreadable(self) -> None:
        result, _launch, _prompt, _label, errors = self.resume_codex_staff(herdr.UnknownConversation("pid unreadable"))
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.detach.assert_not_called()
        self.assertEqual(len(errors.splitlines()), 2)
        self.assertIn("anomaly: pid unreadable; not blocking", errors)
        self.assertIn("observer anomaly:", errors)

    def test_resume_codex_id_uses_the_workers_account_and_the_rollout_folder(self) -> None:
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                identity = self.saved_codex_name()
                source = self.root / ".codex2"
                if compressed:
                    _ = compressed_copy(source / "sessions" / f"rollout-{identity}.jsonl")
                for target in (None, "codex3"):
                    expected = target or "codex1"
                    with (self.subTest(target=target), patch.dict(os.environ, {"CODEX_HOME": str(source)}),
                          patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount(
                              "codex1", self.root / ".codex1")) as role,
                          patch.object(self.client, "open_tab", return_value="w1:p2"),
                          patch.object(self.client, "prompt") as prompt,
                          patch.object(self.client, "tab_sessions", return_value=[herdr.TabSession("w1:p2", "saved-worker", "w1", "")]),
                          patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                              self.root / "project", {"CODEX_HOME": str(self.root / f".{expected}")})) as prepare,
                          patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                              "w1:p2", herdr.PromptSubmissionNotRequired())) as launch):
                        result = stop_resume.resume(identity, to=target)
                        self.assertIsInstance(result, stop_resume.ClaudeUp)
                        assert isinstance(result, stop_resume.ClaudeUp)
                        self.assertEqual((result.session_id, result.account), (identity, expected))
                        self.assertEqual(prepare.call_args.args[1], str(self.root / "project"))
                        self.assertEqual(prepare.call_args.args[2]["CODEX_HOME"], str(self.root / f".{expected}"))
                        self.assertEqual(launch.call_args.args[3], "codex")
                        self.assertIsInstance(cast(object, launch.call_args.args[5]), herdr.NoOpeningPrompt)
                        prompt.assert_not_called()
                        self.assertEqual(launch.call_args.args[4][-2:], ["resume", identity])
                        if target is None:
                            role.assert_called_once_with("workers")
                        else:
                            role.assert_not_called()

    def test_unknown_codex_process_does_not_block_a_stopped_thread_without_a_pane(self) -> None:
        identity = self.saved_codex_name()
        unknown = herdr.CodexConversations({}, ("other-session (Codex thread unknown)",))
        with (patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex2")}),
              patch.object(herdr, "codex_conversations", side_effect=[unknown, herdr.CodexConversations({identity: (456,)}, ())]),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", self.root / ".codex1")),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(self.root / ".codex1")),
              patch.object(accounts, "codex_home_label", return_value="codex1"),
              patch.object(self.client, "open_tab", return_value="w1:p2") as opened,
              patch.object(self.client, "tab_sessions", return_value=[herdr.TabSession("w1:p2", "saved-worker", "w1", "")]),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                  self.root / "project", {"CODEX_HOME": str(self.root / ".codex1")})),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p2", herdr.PromptSubmissionNotRequired())) as launch,
              redirect_stderr(io.StringIO()) as errors):
            result = stop_resume.resume(identity)
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.start_state, ClaudeStartState.STARTED)
        opened.assert_called_once()
        launch.assert_called_once()
        self.assertEqual(sum(line.startswith("anomaly:") for line in errors.getvalue().splitlines()), 1)
        self.assertIn("other-session (Codex thread unknown)", errors.getvalue())
        self.assertIn("not blocking", errors.getvalue())

    def test_unknown_codex_thread_in_a_running_pane_does_not_start_again(self) -> None:
        identity = self.saved_codex_name()
        labels: tuple[dict[str, str | None], ...] = ({"label": "saved-worker"}, {}, {"label": None})
        for label in labels:
            with self.subTest(label=label):
                self.client.rows[:] = [{"pane_id": "w1:p2", "workspace_id": "w1",
                                        "terminal_id": "worker-terminal", "session_id": identity, **label}]
                with (patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex2")}),
                      patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                          {}, ("other-session (Codex thread unknown)",))),
                      patch.object(self.client, "pane_activity", return_value=herdr.PaneRunning(("codex",))) as activity,
                      patch.object(self.client, "open_tab") as opened,
                      patch.object(herdr, "launch_prepared_agent") as launch,
                      redirect_stderr(io.StringIO()) as errors):
                    result = stop_resume.resume(identity, to="codex1")
                assert isinstance(result, stop_resume.ResumeFailed)
                self.assertEqual((result.session_id, result.reason), (identity,
                    "pane w1:p2: cannot prove the reported Codex owner"))
                activity.assert_not_called()
                opened.assert_not_called()
                launch.assert_not_called()
                self.detach.assert_not_called()
                self.assertEqual(sum(line.startswith("anomaly:") for line in errors.getvalue().splitlines()), 1)
                self.assertIn("other-session (Codex thread unknown)", errors.getvalue())

    def test_unknown_codex_resume_requires_the_proved_pane_conversation(self) -> None:
        identity = self.saved_codex_name()
        pane: herdr.PaneIdentity = {"pane_id": "w1:p2", "workspace_id": "w1", "session_id": identity}
        unknown = herdr.UnknownConversation("fixture conversation is unknown")
        with (codex_foreground_cases(self.root) as cases,
              patch.object(herdr, "read_conversation", return_value=unknown),
              patch.object(self.client, "pane_activity", return_value=herdr.PaneRunning(("codex",))),
              patch.object(self.client, "panes", return_value=[pane]),
              patch.object(self.client, "result") as report,
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.PaneSessionReport(identity), "1", {})),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(self.root / ".codex2")),
              patch.object(accounts, "codex_home_label", return_value="codex2"),
              redirect_stderr(io.StringIO())):
            for case in cases:
                with self.subTest(case=case.name):
                    report.return_value = {"process_info": case.info}
                    result = stop_resume.running_on_account(identity, "w1:p2", kind="codex")
                    if case.owner_proved:
                        assert isinstance(result, stop_resume.ClaudeUp)
                        self.assertEqual((result.session_id, result.pid, result.account), (identity, 456, "codex2"))
                    else:
                        self.assertIsInstance(result, stop_resume.ResumeFailed)

    def test_resume_of_a_running_codex_id_keeps_its_current_account(self) -> None:
        identity = self.saved_codex_name()
        with (patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex2")}),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (456,)}, ())),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(self.root / ".codex2")),
              patch.object(accounts, "codex_home_label", return_value="codex2"),
              patch.object(herdr, "launch_prepared_agent") as launch):
            result = stop_resume.resume(identity, to="codex1")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.start_state, ClaudeStartState.ALREADY_RUNNING)
        self.assertEqual(result.account, "codex2")
        launch.assert_not_called()

    def test_closed_showrunner_uses_its_role_account_before_its_first_launch(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "showrunner", "alpha", staff.STAFF_ROOT, current_session=identity, worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        self.client.rows.clear()
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude4", self.root / ".claude4")) as role,
              patch.object(self.client, "open_tab", return_value="w1:p3"),
              patch.object(self.client, "tab_sessions", return_value=[herdr.TabSession("w1:p3", "alpha", "w1", "")]),
              patch.object(stop_resume, "_start_session", return_value=sessions.LiveSessionProcess(456)) as start):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        role.assert_called_once_with("showrunner")
        self.assertEqual(start.call_args.args[1]["environment"]["CLAUDE_CONFIG_DIR"], str(self.root / ".claude4"))

    def test_closed_row_resumes_in_its_own_pane_or_refuses_with_no_new_tab(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "showrunner", "alpha", staff.STAFF_ROOT, current_session=identity, worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        refusal = herdr.OwnPaneNotAtShell("alpha", identity, "w1:p3", 30)
        cases: tuple[herdr.ExistingPane | herdr.OwnPaneNotAtShell, ...] = (herdr.ExistingPane("w1:p3", "own-terminal"), refusal)
        for own in cases:
            with self.subTest(own=type(own).__name__):
                self.client.rows.clear()
                with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                      patch.object(self.client, "saved_row_tab", side_effect=[own]) as saved,
                      patch.object(self.client, "open_tab") as opened,
                      patch.object(stop_resume, "_start_session", return_value=sessions.LiveSessionProcess(456)) as start):
                    result = stop_resume.resume("alpha")
                saved.assert_called_once()
                opened.assert_not_called()
                if isinstance(own, herdr.ExistingPane):
                    self.assertIsInstance(result, stop_resume.ClaudeUp)
                    self.assertEqual(cast(herdr.TabSession, start.call_args.args[0]).pane_id, "w1:p3")
                else:
                    self.assertEqual(result, stop_resume.ResumeFailed(identity, str(refusal)))
                    start.assert_not_called()

    def test_moved_showrunner_resumes_only_its_worker_children_with_native_dispatch_ids(self) -> None:
        fixture = StaffTestCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        pane_record.write("agents", self.entry)
        doc = fixture.root / "production.md"
        atomic_file.write_text_atomic(doc, f"# Production\n\n- **Merge branch:** `merge`\n"
            + f"- **Showrunner checkout:** `{fixture.root}`\n- **Log:** `log.md`\n\n## Units\n")
        owner = fixture_member("showrunner", name="owner", runtime_id=self.entry["session_id"],
                               doc=staff.LiveDoc(remote.local_machine(), doc))
        _ = staff.set_home(owner.id, staff.SessionHome(self.root / ".claude3"))
        unit = fixture_member("unit", name="unit", parent=owner.id)
        foreign = fixture_worker("foreign", unit.id)
        codex = fixture_worker("codex-own", owner.id, backend=staff.CodexBackend(fixture.root / "codex"))
        claude = fixture_worker("claude-own", owner.id)
        runs = fixture.root / "runs"
        run = runs / "unit-run"
        run.mkdir(parents=True)
        for child in (codex, claude):
            agents_log.append(child.current_session, "impl", None, datetime.now(timezone.utc))
        fixture.write_json(run / "progress_history_state.json", {
            "owner_staff_id": owner.id, "pass": {child.name: {
                "worker_id": child.id, "status": "active", "instance_id": f"dispatch-{child.name}",
                "launcher_command": ["assign.sh", "--to", child.name],
            } for child in (codex, claude, foreign)}})
        for child in (codex, claude):
            backend = staff.backend(child)
            assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
            text = f"End your final reply with DONE dispatch-{child.name}."
            event = ({"type": "user", "message": {"role": "user", "content": text}}
                     if isinstance(backend, staff.ClaudeBackend) else
                     {"type": "response_item", "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": text}]}})
            atomic_file.write_text_atomic(worker_admission.transcript_source(child, backend), json.dumps(event) + "\n")
        with (patch("scripts.lib.unit_runs.run_roots", return_value=(runs,)),
              patch.object(unit_runs, "held", return_value=[str(run)]),
              patch.dict(os.environ, {"UNIT_RUN_DIR": str(run)}),
              patch.object(staff, "initial_snapshot", return_value=staff.local_snapshot()),
              patch.object(stop_resume, "_start_session", return_value=sessions.LiveSessionProcess(456)) as start):
            _ = staff.set_run(owner.id, staff.OpenRun(run))
            self.assertEqual(unit_runs.director(run), owner.current_session)
            result = stop_resume.resume(owner.current_session, why="account move", to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        message = cast(str, start.call_args.args[3])
        self.assertNotIn(f"Worker {foreign.id}:", message)
        self.assertNotIn(f"dispatch-{foreign.name}", message)
        for child in (codex, claude):
            self.assertEqual(message.count(f"Worker {child.id}: busy dispatch dispatch-{child.name}."), 1)
        self.assertIn("moved from claude3 to claude4", message)
        self.assertNotIn("assign.sh --to", message)

    def test_resume_without_a_pane_record_starts_the_staff_row(self) -> None:
        for label in ("codex1", "codex2"):
            home = self.root / f".{label}"
            home.mkdir(exist_ok=True)
            login = label + "@example.invalid"
            write_codex_auth(home, login)
            atomic_file.write_text_atomic(self.root / "notes" / f"{label}.md", f"---\nlogin: {login}\n---\n")
        recorded_identity = self.entry["session_id"]
        launch_configuration = models_registry.launch_snapshot()
        cases: tuple[tuple[staff.Kind, str, str, str, str], ...] = (
            ("unit", "alpha", "claude", "claude3", recorded_identity),
            ("producer", "own-codex", "codex", "codex1", recorded_identity),
            ("producer", "alpha", "claude", "claude3", fixture_runtime_id("producer-without-claude-history")),
            ("producer", "own-codex", "codex", "codex1", fixture_runtime_id("producer-without-codex-history")))
        pane_record.save("agents", {})
        for row_kind, name, family, selected, identity in cases:
            if family == "codex" and identity == recorded_identity:
                fixture_launch_transcript(self.root, {**self.entry, "kind": "codex",
                    "config_dir": str(self.root / ".codex1")}, staff.StaffSnapshot(()))
            row = staff.StaffRow(staff.StaffId("fixed-row"), row_kind, name, staff.StaffRoot(),
                                 current_session=staff.StaffId(identity),
                                 unit_type=staff.UnitDetails("direct") if row_kind == "unit" else staff.NotUnit())
            record = staff.StaffSnapshot((row,))
            for target in (None, "codex2" if family == "codex" else "claude4"):
                for panes in (self.client.rows, []):
                    expected = target or selected
                    started = False

                    def start_staff(member: staff.StaffRow, membership: staff.StaffSnapshot, client: herdr.Herdr,
                                    *, target_account: str | accounts.AccountSelection,
                                    configuration: models_registry.LaunchModelConfiguration | None = None) -> str:
                        nonlocal started
                        self.assertEqual((member, membership, client, target_account),
                                         (row, record, self.client, target or ""))
                        self.assertEqual(configuration, launch_configuration if row_kind == "producer" else None)
                        started = True
                        return "w1:p2"

                    def claude_live(reference: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
                        self.assertEqual(reference, identity)
                        return sessions.LiveSessionProcess(456) if family == "claude" and started else sessions.SessionNotLive()

                    def codex_live(reference: str, kind: str = "") -> herdr.LiveConversation | herdr.StoppedConversation:
                        if identity != recorded_identity and not kind:
                            self.assertEqual(reference, identity)
                            self.assertFalse(started)
                            return herdr.StoppedConversation()
                        self.assertEqual((reference, kind), (identity, "codex"))
                        return herdr.LiveConversation("codex", (456,)) if started else herdr.StoppedConversation()

                    with (self.subTest(family=family, target=target, panes=bool(panes), transcript=identity == recorded_identity),
                          patch.object(staff, "initial_snapshot", return_value=record),
                          patch.object(self.client, "rows", panes),
                          patch.object(restart, "start_row", side_effect=start_staff) as start,
                          patch.object(sessions, "session_liveness", side_effect=claude_live),
                          patch.object(herdr, "read_conversation", side_effect=codex_live),
                          patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(
                              self.root / f".{expected}")),
                          patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(
                              self.root / f".{expected}"))):
                        result = stop_resume.resume(str(row.id), to=target)
                        self.assertEqual(result, stop_resume.ClaudeUp(identity, "w1:p2", expected))
                        assert isinstance(result, stop_resume.ClaudeUp)
                        self.assertEqual(result.start_state, ClaudeStartState.STARTED)
                        if row_kind == "producer":
                            start.assert_called_once_with(row, record, self.client, target_account=target or "",
                                                          configuration=launch_configuration)
                        else:
                            start.assert_called_once_with(row, record, self.client, target_account=target or "")

    def test_resume_without_a_staff_row_or_pane_record_finds_no_session(self) -> None:
        identity = self.entry["session_id"]
        pane_record.save("agents", {})
        result = stop_resume._resume_once(identity, why="test", names=LaunchNames(staff.StaffSnapshot(())))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(result, stop_resume.SessionNotFound(identity))

    def test_admitted_pane_run_with_no_start_fails_after_one_attempt(self) -> None:
        with patch.object(self.client, "run", self.start):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ResumeFailed("019b3a40-4250-7000-8000-000000000102", "never started"))
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.clock.elapsed, 20.0)
        self.assertEqual({invocation.args[2] for invocation in self.start.call_args_list}, {"w1:p2"})


    def test_batch_without_a_tab_reaches_the_shared_restore(self) -> None:
        up = stop_resume.ClaudeUp("saved", "new-pane", "claude4")
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning("saved")),
              patch.object(stop_resume, "resume", return_value=up) as resumed):
            results = stop_resume.move_sessions(["saved"], "claude4", why="limit")
        self.assertEqual(results, [up])
        resumed.assert_called_once_with("saved", why="limit", to="claude4", client=self.client)


    def test_stale_launch_metadata_does_not_prevent_resume(self) -> None:
        identity = self.entry["session_id"]
        self.entry.update(session_id="sid-other", flags_read=False, ended=True)
        pane_record.write("agents", self.entry)
        tab = herdr.TabSession("w1:p2", "alpha", "w1", identity)
        with (patch.object(stop_resume, "_tab_of", return_value=tab), redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume(identity)
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("launch record anomaly:", output.getvalue())
        self.start.assert_called_once()
        self.assertIn(identity, self.launched)


    def test_failed_stop_bookkeeping_reports_an_anomaly_and_keeps_the_stopped_result(self) -> None:
        result = stop_resume.Stopped(self.entry["session_id"], 123)
        with (patch.object(stop_resume, "_stop", return_value=result),
              patch.object(pane_record, "end", side_effect=OSError("fixture write failed")),
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(stop_resume.stop("alpha"), result)
        self.assertEqual(output.getvalue().splitlines(),
                         [f"{result.session_id}: stop note anomaly: fixture write failed"])


    def test_staff_home_changes_only_after_the_target_pane_starts(self) -> None:
        row = staff.ensure("producer", name="TEST-account-home", runtime_id=self.entry["session_id"],
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        self.tab_titles["w1:t9"] = row.name
        row = pane_launch.staff_admitted(row)
        running_home = self.root / ".claude3"
        target_home = self.root / ".claude4"
        self.assertEqual(row.home, staff.SessionHome(running_home))
        starts: list[str] = []
        launch_configuration = models_registry.launch_snapshot()

        def started(tab: herdr.TabSession, entry: pane_launch.CurrentLaunch, label: str,
                    _message: str, *,
                    configuration: models_registry.LaunchModelConfiguration) -> sessions.LiveSessionProcess:
            self.assertEqual(configuration, launch_configuration)
            current = staff.by_id(row.id)
            assert current is not None
            self.assertEqual(current.home, staff.SessionHome(running_home))
            self.assertNotIn("config_dir", entry)
            self.assertNotIn("config_dir", pane_record.read("agents")[tab.pane_id])
            self.assertEqual(entry["environment"]["CLAUDE_CONFIG_DIR"], str(target_home))
            replacement = pane_record.launch(tab.pane_id, entry["session_id"], entry["cwd"], entry["environment"],
                                            ["claude", *pane_record.resume_arguments(entry)])
            pane_record.write("agents", replacement)
            starts.append(label)
            return sessions.LiveSessionProcess(456)

        with patch.object(stop_resume, "_start_session", side_effect=started):
            result = stop_resume.resume(row.id, to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(starts, ["claude4"])
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current.home, staff.SessionHome(target_home))
        saved = pane_record.read("agents")["w1:p2"]
        self.assertNotIn("config_dir", saved)
        self.assertNotIn("CLAUDE_CONFIG_DIR", saved["environment"])

        running_home = target_home
        target_home = self.root / ".claude3"
        for failed in (True, False):
            with self.subTest(destination="claude3", failed=failed):
                starts.clear()
                before = pane_record.record_path("agents").read_bytes()
                with patch.object(stop_resume, "_start_session",
                                  side_effect=OSError("fixture target start failed") if failed else started):
                    result = stop_resume.resume(row.id, to="claude3")
                current = staff.by_id(row.id)
                assert current is not None
                if failed:
                    self.assertIsInstance(result, stop_resume.ResumeFailed)
                    self.assertEqual(current.home, staff.SessionHome(running_home))
                    self.assertEqual(pane_record.record_path("agents").read_bytes(), before)
                    self.assertEqual(starts, [])
                else:
                    self.assertIsInstance(result, stop_resume.ClaudeUp)
                    self.assertEqual(starts, ["claude3"])
                    self.assertEqual(current.home, staff.SessionHome(target_home))

    def test_account_change_keeps_pane_data_until_the_start_publication(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity), home=staff.SessionHome(self.root / ".claude3"))
        retained = pane_launch.retained_launch(self.entry, staff.StaffSnapshot((row,)))
        pane_record.save("agents", {"w1:p2": retained}, record=staff.StaffSnapshot((row,)))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,)))):
            result = stop_resume.resume("alpha", to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        saved = pane_record.read("agents")["w1:p2"]
        self.assertEqual(saved, retained)
        self.assertEqual(saved.get("staff_id"), row.id)
        self.assertNotIn("CLAUDE_CONFIG_DIR", saved["environment"])
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertIn("CLAUDE_CODE_SESSION_ID=" + identity, command)

    def test_own_pane_resume_saves_pane_data_with_the_fixed_staff_reference(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        retained = pane_launch.retained_launch(self.entry, staff.StaffSnapshot((row,)))
        pane_record.save("agents", {"w1:p2": retained}, record=staff.StaffSnapshot((row,)))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha")
        self.assertEqual(pane_record.read("agents")["w1:p2"], retained)
        launched = cast(pane_launch.CurrentLaunch, execute.call_args.args[1])
        self.assertEqual(launched["session_id"], identity)
        self.assertEqual(launched["environment"]["CLAUDE_CODE_SESSION_ID"], identity)

    def test_resume_after_a_rename_keeps_the_fixed_staff_reference(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "renamed-alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        self.tab_titles["w1:t9"] = row.name
        retained = pane_launch.retained_launch(self.entry, staff.StaffSnapshot((row,)))
        pane_record.save("agents", {"w1:p2": retained}, record=staff.StaffSnapshot((row,)))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume(row.name)
        self.assertEqual(pane_record.read("agents")["w1:p2"].get("staff_id"), row.id)
        execute.assert_called_once()
        launched = cast(pane_launch.CurrentLaunch, execute.call_args.args[1])
        self.assertEqual(launched["session_id"], identity)
        self.assertEqual(launched["name"], row.name)

    def test_failed_account_bookkeeping_reports_an_anomaly_and_resumes(self) -> None:
        with (patch.object(pane_record, "write", side_effect=OSError("fixture")),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha", to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("account record anomaly: fixture", output.getvalue())
        self.start.assert_called_once()


    def test_missing_prompt_confirmation_reports_an_anomaly_and_keeps_the_live_result(self) -> None:
        with (patch.object(self.client, "submit_opening_prompt", return_value=herdr.PromptNotSubmitted("fixture")),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("prompt anomaly: fixture", output.getvalue())
        self.start.assert_called_once()


    def test_missing_stay_generation_reports_an_anomaly_and_keeps_the_live_result(self) -> None:
        with (patch.object(sessions, "process_start", return_value=None), redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("observer anomaly: process generation is unavailable", output.getvalue())
        self.start.assert_called_once()
        self.detach.assert_not_called()


    def test_failed_stay_launch_reports_an_anomaly_and_keeps_the_live_result(self) -> None:
        self.detach.side_effect = OSError("fixture")
        with redirect_stderr(io.StringIO()) as output:
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("observer anomaly: fixture", output.getvalue())
        self.start.assert_called_once()


    def test_failed_title_change_reports_an_anomaly_and_resumes(self) -> None:
        with (patch.object(self.client, "retitle", side_effect=herdr_types.HerdrFailure("fixture")),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertIn("title anomaly: fixture", output.getvalue())
        self.start.assert_called_once()


    def test_enveloped_resume_launch_paths_need_no_source_record(self) -> None:
        from scripts.lib import script_prompts

        with patch.object(script_prompts, "record", side_effect=AssertionError("recorded resume")):
            result = stop_resume.resume("alpha")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertTrue(self.client.prompts[0][1].text.startswith('<system-reminder source="script">'))


    def test_resume_does_not_type_into_a_pane_with_other_work(self) -> None:
        self.client.process_info["foreground_process_group_id"] = 998043
        pane_result = self.client.result

        def observe(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] == ("pane", "process-info"):
                return {"process_info": self.client.process_info}
            return pane_result(*arguments, deadline=deadline)

        with (patch.object(self.client, "result", side_effect=observe),
              patch.object(time, "monotonic", lambda: self.clock.elapsed)):
            result = stop_resume.resume("alpha")
        self.assertEqual(result, stop_resume.ResumeFailed(
            self.entry["session_id"], "would send keys to a pane with other live work instead of the selected shell"))
        self.assertEqual(self.clock.elapsed, herdr.PROGRAM_TIMEOUT_SECONDS)
        self.start.assert_not_called()
        self.assertEqual(self.client.prompts, [])


    def test_resume_names_use_a_fresh_snapshot_under_the_row_lock(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(staff.StaffId("accounts"), "unit", "accounts", staff.StaffRoot(),
                             current_session=identity, worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        for reference in (row.name, row.id, identity):
            self.launched.clear()
            self.client.prompts.clear()
            with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))) as read:
                result = stop_resume.resume(reference)
            self.assertIsInstance(result, stop_resume.ClaudeUp)
            self.assertEqual(read.call_count, 2)
            self.assertIn("resumed by name", self.client.prompts[0][1].text)
            self.assertTrue(self.client.prompts[0][1].text.startswith('<system-reminder source="script">'))
            self.assertNotIn("the herdr server restarted", self.client.prompts[0][1].text)

    def test_unknown_ambiguous_and_remote_names_refuse(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(staff.StaffId("accounts"), "unit", "accounts", staff.StaffRoot(),
                             current_session=identity, unit_type=staff.UnitDetails("direct"))
        other = staff.StaffRow(staff.StaffId("other"), "unit", "accounts", staff.StaffRoot(),
                               current_session=staff.StaffId("other-session"), unit_type=staff.UnitDetails("direct"))
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row, other))):
            for reference, text in (("absent", "unknown name"), ("accounts", "ambiguous name")):
                result = stop_resume.resume(reference)
                assert isinstance(result, stop_resume.ResumeFailed)
                self.assertIn(text, result.reason)
                if reference == "accounts":
                    self.assertIn("accounts", result.reason)
                    self.assertIn("other", result.reason)
                else:
                    self.assertEqual(result.reason, "unknown name")
        remote_row = staff.StaffRow(row.id, row.kind, row.name, row.parent,
            current_session=identity, worktree=staff.PresentWorktree("other-machine", self.root), unit_type=row.unit_type)
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((remote_row,))):
            result = stop_resume.resume("accounts")
        self.assertEqual(result, stop_resume.ResumeFailed("accounts", "session is on other-machine"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_codex_record_uses_workers_home_without_claude_trust(self) -> None:
        _ = write_registry(self.root / "configuration/models.conf", {"producers": {"alpha": "codex:gpt-test:high"}}, update=True)
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("saved-codex-owner"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        home = self.root / ".codex2"
        workers = self.root / ".codex1"
        self.entry.update(kind="codex", config_dir=str(home), model="")
        self.entry["environment"] = {"CLAUDE_CONFIG_DIR": str(home)}
        pane_record.write("agents", self.entry)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", workers)) as role,
              patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha")
        trust.assert_not_called()
        role.assert_called_once_with("workers")
        entry = cast(pane_launch.CurrentLaunch, execute.call_args.args[1])
        self.assertNotIn("config_dir", entry)
        self.assertEqual(entry["environment"]["CODEX_HOME"], str(workers))

    def test_codex_own_pane_exec_publishes_the_target_home(self) -> None:
        source, target = self.root / ".codex1", self.root / ".codex2"
        row = fixture_member("producer", name="own-codex", runtime_id=self.entry["session_id"])
        _ = staff.set_home(row.id, staff.SessionHome(source))
        entry = self.entry.copy()
        entry.update(kind="codex", config_dir=str(source), environment={"CODEX_HOME": str(target)})
        tab = herdr.TabSession("w1:p2", row.name, "w1", row.current_session)
        with (patch.object(os, "chdir"), patch.object(os, "execvpe", side_effect=SystemExit)):
            with self.assertRaises(SystemExit):
                stop_resume._exec_in_own_pane(tab, entry, "codex2", "")  # pyright: ignore[reportPrivateUsage]
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current.home, staff.SessionHome(target))

    def test_codex_own_pane_exec_has_no_prompt_and_uses_workers_home(self) -> None:
        _ = write_registry(self.root / "configuration/models.conf", {"producers": {"alpha": "codex:gpt-test:high"}}, update=True)
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("saved-codex-owner"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        home = self.root / ".codex2"
        workers = self.root / ".codex1"
        self.entry.update(kind="codex", config_dir=str(home), model="")
        self.entry["environment"] = {"CLAUDE_CONFIG_DIR": str(home)}
        pane_record.write("agents", self.entry)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", workers)) as role,
              patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.object(stop_resume, "resume_message", return_value="Continue this conversation.") as message,
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(os, "chdir"), patch.object(os, "execvpe", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha")
        trust.assert_not_called()
        role.assert_called_once_with("workers")
        message.assert_called_once()
        arguments = cast(list[str], execute.call_args.args[1])
        self.assertEqual(arguments[arguments.index("resume"):arguments.index("resume") + 2], ["resume", identity])
        self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-test")
        self.assertIn('model_reasoning_effort="high"', arguments)
        self.assertEqual(arguments[0], str(self.root / "bin" / "codex"))
        environment = cast(dict[str, str], execute.call_args.args[2])
        self.assertEqual(environment["CODEX_HOME"], str(workers))
        self.assertNotIn("Continue this conversation.", arguments)
        self.assertNotIn(str(stop_resume.SCRIPTS / "accounts/accounts.py"), arguments)

    def test_saved_codex_resume_uses_explicit_account(self) -> None:
        _ = write_registry(self.root / "configuration/models.conf", {"producers": {"alpha": "codex:gpt-test:high"}}, update=True)
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("saved-codex-owner"), "producer", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        home = self.root / ".codex2"
        target = self.root / ".codex3"
        self.entry.update(kind="codex", config_dir=str(home), model="")
        self.entry["environment"] = {"CLAUDE_CONFIG_DIR": str(home)}
        pane_record.write("agents", self.entry)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "role_account") as role,
              patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha", to="codex3")
        role.assert_not_called()
        trust.assert_not_called()
        entry = cast(pane_launch.CurrentLaunch, execute.call_args.args[1])
        self.assertNotIn("config_dir", entry)
        self.assertEqual(entry["environment"]["CODEX_HOME"], str(target))

    def test_own_empty_pane_executes_the_saved_launch(self) -> None:
        self.entry["ended"] = True
        pane_record.write("agents", self.entry)
        with (patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(os, "chdir") as directory,
              patch.object(os, "execvpe", side_effect=SystemExit(0)) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume("alpha")
        directory.assert_called_once_with(str(self.root))
        arguments = cast(list[str], execute.call_args.args[1])
        self.assertEqual(arguments[0], str(self.root / "bin" / "claude"))
        self.assertIn("resumed by name", arguments[-1])
        environment = cast(dict[str, str], execute.call_args.args[2])
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(cast(staff.SessionHome, pane_launch.config_home(self.entry)).path))
        self.assertTrue(pane_record.read("agents")["w1:p2"].get("ended", False))
        self.start.assert_not_called()
        observed = pane_record.launch("w1:p2", self.entry["session_id"], str(self.root),
            self.entry["environment"], ["claude", "--resume", self.entry["session_id"]])
        pane_record.write("agents", observed)
        self.assertFalse(pane_record.read("agents")["w1:p2"].get("ended", False))

        families: tuple[Literal["claude", "codex"], ...] = ("claude", "codex")
        for family in families:
            with self.subTest(family=family, stale_contract=True):
                row = fixture_member("producer", name="row-name", runtime_id=self.entry["session_id"])
                _ = write_registry(self.root / "configuration/models.conf", {"producers": {
                    row.name: "codex:gpt-test:high" if family == "codex" else "claude:opus:max"}}, update=True)
                variable = "CODEX_HOME" if family == "codex" else "CLAUDE_CONFIG_DIR"
                source = self.root / (".codex1" if family == "codex" else ".claude3")
                target = self.root / (".codex2" if family == "codex" else ".claude4")
                _ = staff.set_home(row.id, staff.SessionHome(source))
                retained = self.entry.copy()
                retained.update(staff_id=row.id, kind=family, environment={variable: str(source)}, config_dir=str(source))
                pane_record.save("agents", {"w1:p2": retained})
                retained = pane_record.read("agents")["w1:p2"]
                contract_path = pane_record.record_directory() / f".producer-start-{row.id}"
                atomic_file.write_text_atomic(contract_path, json.dumps({
                    "member": row.id, "session": row.current_session, "kind": family,
                    "folder": str(self.root), "home": str(source), "attempt": "killed-start"}))
                contract_bytes = contract_path.read_bytes()
                self.launched.clear()
                self.tab_titles["w1:t9"] = row.name
                executed = self.root / f"{family}-exec.json"
                requests: list[pane_launch.ProducerLaunchRequest] = []
                launch_scope = pane_launch.producer_launch_request

                @contextmanager
                def observe_request(request: pane_launch.ProducerLaunchRequest) -> Generator[None]:
                    requests.append(request)
                    with launch_scope(request):
                        yield

                def execute_launch(program: str, arguments: list[str], environment: dict[str, str]) -> Never:
                    request = requests[-1]
                    atomic_file.write_text_atomic(executed, json.dumps({"program": program, "environment": environment,
                        "request": {"member": request.member, "session": request.session, "kind": request.kind,
                                    "folder": str(request.folder), "home": str(request.home)}}))
                    if family == "claude":
                        replacement = pane_record.launch("w1:p2", row.current_session, str(self.root), environment,
                                                         arguments, kind="claude")
                        replacement.update(pid=456, proc_start="generation-1")
                        with patch.dict(os.environ, environment, clear=True):
                            Context().run(pane_record.write, "agents", replacement)
                    os._exit(0)
                def conversation(identity: str, kind: str = "") -> herdr.LiveConversation | herdr.StoppedConversation:
                    del kind
                    return (herdr.LiveConversation(pane_launch.agent_kind(retained), (456,)) if identity in self.launched
                            else herdr.StoppedConversation())
                claude_registry = (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive())
                                   if family == "codex" else nullcontext())
                with (patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}), patch.object(os, "chdir"),
                      patch.object(pane_launch, "producer_launch_request", side_effect=observe_request),
                      claude_registry,
                      patch.object(herdr, "read_conversation", side_effect=conversation),
                      patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                          herdr.EnvironmentThread(row.current_session), "generation-1",
                          {"HERDR_PANE_ID": "w1:p2", "HERDR_SESSION": "agents", variable: str(target)})),
                      patch.object(account, "process_config_dir", return_value=account.ConfiguredClaudeFolder(target)),
                      patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(target))):
                    child = os.fork()
                    if child == 0:
                        try:
                            with patch.object(os, "execvpe", side_effect=execute_launch):
                                _ = stop_resume.resume(row.name, to=target.name.removeprefix("."))
                        finally:
                            os._exit(1)
                    self.assertEqual(os.waitpid(child, 0), (child, 0))
                    invocation = cast(dict[str, object], json.loads(executed.read_text()))
                    self.assertEqual(invocation["program"], str(self.root / "bin" / family))
                    self.assertEqual(cast(dict[str, str], invocation["environment"])[variable], str(target))
                    self.assertEqual(json.loads(cast(dict[str, str], invocation["environment"])[
                        pane_launch.PRODUCER_LAUNCH_REQUEST_ENV + row.id.encode().hex()]), invocation["request"])
                    self.assertEqual(contract_path.read_bytes(), contract_bytes)
                    if family == "codex":
                        self.assertEqual(pane_record.read("agents"), {"w1:p2": retained})
                        self.assertEqual(cast(dict[str, str], invocation["request"])["home"], str(target))
                        self.assertEqual(invocation["request"], {
                            "member": row.id, "session": row.current_session, "kind": family,
                            "folder": str(self.root), "home": str(target)})
                    self.launched.add(row.current_session)
                    with patch.object(os, "execvpe", side_effect=AssertionError("the live session must not execute again")) as execute:
                        second = stop_resume.resume(row.name)
                    execute.assert_not_called()
                self.assertIsInstance(second, stop_resume.ClaudeUp)
                assert isinstance(second, stop_resume.ClaudeUp)
                self.assertEqual((second.session_id, second.pane_id), (row.current_session, "w1:p2"))
                self.assertEqual(list(pane_record.read("agents")), ["w1:p2"])
                if family == "claude":
                    self.assertTrue(pane_record.read("agents")["w1:p2"].get("producer_binding", False))
                else:
                    self.assertEqual(pane_record.read("agents"), {"w1:p2": retained})
                current = staff.by_id(row.id)
                assert current is not None
                self.assertEqual(current.home, staff.SessionHome(target))

    def test_unknown_name_lists_only_close_local_units_and_showrunners(self) -> None:
        from scripts.agents import remote

        machine = remote.local_machine()
        rows = (
            staff.StaffRow(staff.StaffId("accounts"), "unit", "accounts", staff.StaffRoot(),
                current_session=staff.StaffId("accounts-session"),
                worktree=staff.PresentWorktree(machine, self.root), unit_type=staff.UnitDetails("direct")),
            staff.StaffRow(staff.StaffId("team-accounts"), "showrunner", "team-accounts", staff.StaffRoot(),
                current_session=staff.StaffId("team-session"), doc=staff.LiveDoc(machine, self.root / "plan.md")),
            staff.StaffRow(staff.StaffId("account-worker"), "worker", "accounts-worker", staff.StaffRoot(),
                current_session=staff.StaffId("worker-session"), worktree=staff.PresentWorktree(machine, self.root)),
            staff.StaffRow(staff.StaffId("remote-accounts"), "unit", "accounts-remote", staff.StaffRoot(),
                current_session=staff.StaffId("remote-session"), worktree=staff.PresentWorktree("other-machine", self.root), unit_type=staff.UnitDetails("direct")),
            staff.StaffRow(staff.StaffId("unrelated"), "unit", "herdr", staff.StaffRoot(),
                current_session=staff.StaffId("herdr-session"), worktree=staff.PresentWorktree(machine, self.root), unit_type=staff.UnitDetails("direct")),
        )
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)) as read:
            self.assertEqual(stop_resume.resume("acc"), stop_resume.ResumeFailed(
                "acc", "unknown name; candidates: accounts, team-accounts"))
        self.assertEqual(read.call_count, 2)
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)):
            self.assertEqual(stop_resume.resume("absent"), stop_resume.ResumeFailed("absent", "unknown name"))
        self.start.assert_not_called()

    def test_own_pane_exec_failure_keeps_the_stop_state_and_records_failure(self) -> None:
        self.entry["ended"] = True
        pane_record.write("agents", self.entry)
        with (patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(os, "chdir"),
              patch.object(os, "execvpe", side_effect=OSError("exec refused")),
              patch.object(pane_launch, "finish_restore", wraps=pane_launch.finish_restore) as finish,
              patch.object(LaunchNames, "resume_proof") as proof):
            self.assertEqual(stop_resume.resume("alpha"), stop_resume.ResumeFailed(
                self.entry["session_id"], "exec refused"))
        proof.assert_not_called()
        self.assertTrue(finish.called)
        for invocation in finish.call_args_list:
            self.assertIsInstance(cast(object, invocation.args[3]), pane_launch.FailedResume)
        self.assertTrue(pane_record.read("agents")["w1:p2"].get("ended", False))
        self.start.assert_not_called()
        self.assertEqual(self.client.closed, [])

    def test_resume_of_a_live_target_is_an_idempotent_success(self) -> None:
        identity = self.entry["session_id"]
        self.launched.add(identity)
        self.client.native_ids["w1:p2"] = identity
        for target, registry in (("alpha", "configured"), (identity, "absent")):
            with self.subTest(target=target, registry=registry), patch.dict(os.environ):
                if registry == "absent":
                    _ = os.environ.pop("MODELS_LAUNCH_CONFIGURATION", None)
                    os.environ["MODELS_CONFIG_FILE"] = str(self.root / "absent-models.conf")
                self.assertEqual(stop_resume.resume(target, to="claude4"), stop_resume.ClaudeUp(
                    identity, "w1:p2", "claude4"))
        self.start.assert_not_called()
        self.kill.assert_not_called()
        self.detach.assert_not_called()

        row = fixture_member("producer", name="alpha", runtime_id=identity)
        source = self.root / ".claude3"
        target = self.root / ".claude4"
        for unknown in (False, True):
            with self.subTest(unknown_account=unknown):
                _ = staff.set_home(row.id, staff.SessionHome(source))
                native = herdr.NativeAgentProcess(herdr.EnvironmentThread(identity), "generation-1",
                    {"HERDR_PANE_ID": "w1:p2", "HERDR_SESSION": "agents", "CLAUDE_CONFIG_DIR": str(target)})
                with (patch.object(staff, "set_home", side_effect=staff.StaffUnreadable("fixture home write failed")),
                      patch.object(herdr, "native_process", return_value=native)):
                    observed = pane_record.launch("w1:p2", identity, str(self.root),
                        {"CLAUDE_CONFIG_DIR": str(target)}, ["claude", "--resume", identity])
                    observed.update(pid=456, proc_start="generation-1")
                    pane_record.write("agents", observed)
                saved = staff.by_id(row.id)
                assert saved is not None
                self.assertEqual(saved.home, staff.SessionHome(source))
                setting = (account.ProcessEnvironmentUnread("fixture account is unreadable") if unknown else
                           account.ConfiguredClaudeFolder(target))
                with (patch.object(herdr, "native_process", return_value=native),
                      patch.object(account, "process_config_dir", return_value=setting)):
                    resumed = stop_resume.resume("alpha")
                self.assertIsInstance(resumed, stop_resume.ClaudeUp)
                saved = staff.by_id(row.id)
                assert saved is not None
                self.assertEqual(saved.home, staff.SessionHome(source if unknown else target))
                self.start.assert_not_called()


    def test_running_codex_account_uses_its_live_process_home(self) -> None:
        from scripts.accounts import accounts
        from scripts.whoami import account

        identity = self.entry["session_id"]
        home = self.root / ".codex4"
        with (patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", (987654,))),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(home)) as read,
              patch.object(accounts, "codex_home_label", return_value="codex4")):
            result = stop_resume.running_on_account(identity, "w1:p2", kind="codex")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.account, "codex4")
        read.assert_called_once_with(987654)

    def test_resume_by_name_of_a_running_session_reports_success_without_a_launch(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(staff.StaffId("accounts"), "unit", "accounts", staff.StaffRoot(),
                             current_session=identity, worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        self.client.native_ids["w1:p2"] = identity
        self.live.return_value = sessions.LiveSessionProcess(123)
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["resume", "accounts"]), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            f"{identity}: unchanged; Claude runs on claude3 in w1:p2"])
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_resume_arguments_use_the_current_row_name_after_a_rename_and_clear(self) -> None:
        fixed = staff.StaffId(self.entry["session_id"])
        current = staff.StaffId("019b3a40-4250-7000-8000-000000000103")
        owner = staff.StaffRow(fixed, "unit", "row-name", staff.StaffRoot(), current_session=current, worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        fixture_launch_transcript(self.root, {**self.entry, "session_id": str(current)}, staff.StaffSnapshot(()))
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((owner,))):
            arguments = stop_resume.resume_arguments(self.entry)
        self.assertEqual(arguments[arguments.index("--resume") + 1], current)
        self.assertEqual(arguments[arguments.index("-n") + 1], "row-name")
        self.assertEqual(arguments[arguments.index("--remote-control") + 1], "row-name")
        self.assertEqual(self.entry["name"], "alpha")

        runner = fixture_member("showrunner", name="resume-runner")
        unit = fixture_member("unit", name="resume-unit", parent=runner.id,
                              worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        worker = fixture_worker("resume-worker", unit.id)
        _ = staff.set_worker_role(worker.id, staff.KnownWorkerRole("impl"))
        home = self.root / ".claude3"
        _ = staff.set_home(worker.id, staff.SessionHome(home))
        transcript = home / "projects/resume-worker" / f"{worker.current_session}.jsonl"
        tab = herdr.TabSession("w1:p2", worker.name, "w1", worker.current_session)

        for family, saved_model, turn_model, turn_effort, expected_model, expected_effort in (
            ("claude", "sonnet", "opus", "", "opus", "high"),
            ("claude", "sonnet", "opus", "xhigh", "opus", "xhigh"),
            ("claude", "opus", "", "", "opus", "high"),
            ("claude", "", "", "", "sonnet", "high"),
            ("codex", "sonnet", "opus", "xhigh", "opus", "xhigh"),
            ("codex", "opus", "", "", "opus", ""),
        ):
            with self.subTest(family=family, saved_model=saved_model, turn_model=turn_model, turn_effort=turn_effort):
                _ = write_registry(self.root / "configuration/models.conf", {
                    "assignments": {"direct": family},
                    "direct.claude": {"impl": "sonnet:high" if family == "claude" else None},
                }, update=True)
                turn: dict[str, object] = {"type": "assistant", "message": {"model": turn_model}}
                if turn_effort:
                    turn["effort"] = turn_effort
                atomic_file.write_text_atomic(transcript, json.dumps(turn) + "\n" if turn_model else "")
                entry: pane_launch.CurrentLaunch = {**self.entry, "staff_id": worker.id,
                    "session_id": worker.current_session, "model": saved_model}
                self.launched.clear()
                _ = stop_resume._start_session(tab, entry, "claude3", "")  # pyright: ignore[reportPrivateUsage]
                arguments = self._typed(cast(tuple[str, ...], self.start.call_args.args))
                self.assertEqual(arguments[arguments.index("--resume") + 1], worker.current_session)
                model_flag = arguments.index("--model")
                self.assertEqual(arguments[model_flag:model_flag + 2], ("--model", expected_model))
                if expected_effort:
                    self.assertEqual(arguments[model_flag + 2:model_flag + 4], ("--effort", expected_effort))
                else:
                    self.assertNotIn("--effort", arguments)

    def test_a_moved_or_relaunched_member_gets_its_role_compact_window(self) -> None:
        _ = write_registry(self.root / "configuration/models.conf", {"compact_windows": {
            "producer": "300000", "showrunner": "250k", "worker": "200000"}}, update=True)
        fixed = staff.StaffId(self.entry["session_id"])
        worktree = staff.PresentWorktree(remote.local_machine(), self.root)

        def resumed(kind: staff.Kind) -> list[str]:
            owner = staff.StaffRow(fixed, kind, "row-name", staff.StaffRoot(), current_session=fixed, worktree=worktree,
                                   unit_type=staff.NotUnit() if kind == "producer" else staff.UnitDetails("direct"))
            with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((owner,))):
                return stop_resume.resume_arguments(self.entry)

        with patch.dict(os.environ, {"HOME": str(self.root)}):
            cases: tuple[tuple[staff.Kind, list[str]], ...] = (
                ("producer", ["--autocompact", "300000"]), ("showrunner", ["--autocompact", "250k"]),
                ("worker", ["--autocompact", "200000"]), ("unit", []))
            for kind, expected in cases:
                with self.subTest(kind=kind):
                    arguments = resumed(kind)
                    window = arguments.index("--autocompact") if "--autocompact" in arguments else len(arguments)
                    self.assertEqual(arguments[window:window + 2], expected)
                    self.assertEqual(arguments.count("--autocompact"), len(expected) // 2)
            _ = write_registry(self.root / "configuration/models.conf", {"compact_windows": {"unit": "150k"}}, update=True)
            for recorded, expected_window in (("", "150k"), ("400k", "400k")):
                with self.subTest(recorded=recorded):
                    if recorded:
                        autocompact.record(str(fixed), recorded)
                    arguments = resumed("unit")
                    self.assertEqual((arguments.count("--autocompact"), arguments[arguments.index("--autocompact") + 1]),
                                     (1, expected_window))

    def test_resume_reuses_the_open_shell_with_the_fixed_row_id(self) -> None:
        fixed = staff.StaffId(self.entry["session_id"])
        current = staff.StaffId("019b3a40-4250-7000-8000-000000000103")
        row = staff.StaffRow(fixed, "producer", "row-name", staff.StaffRoot(), current_session=current)
        foreign = staff.StaffRow(staff.StaffId("foreign-owner"), "producer", "another-production", staff.STAFF_ROOT,
                                 current_session=staff.StaffId("foreign-native"))
        retained = {**self.client.rows[0], "agent_session": {"kind": "none"}}
        self.publish_new_launch = True

        class ResumeCaller(Enum):
            OTHER_PANE = "another pane"
            TARGET_PANE = "the bound target pane"

        cases: list[tuple[str, bool, tuple[str, ...], bool, bool, bool, Literal["claude", "codex"], bool, ResumeCaller]] = [
                 (workspace, shared, commands, refuse_close, metadata, cross, "claude", False, ResumeCaller.OTHER_PANE)
                 for workspace, shared, commands, refuse_close, metadata, cross in (
                ("producers", False, (), False, False, False),  # Control: reuse the valid producer tab.
                ("morty", False, (), False, False, False),
                ("producers", True, (), False, False, False),
                ("morty", False, ("editor",), False, True, False),
                ("morty", False, ("background-job",), False, True, False),
                ("morty", False, (), True, True, False),
                ("morty", False, (), False, False, True))]
        cases.extend(("morty", False, ("editor",), False, True, False, family, True, ResumeCaller.OTHER_PANE)
                     for family in ("claude", "codex"))
        cases.append(("producers", False, ("python3",), False, False, False, "claude", False, ResumeCaller.TARGET_PANE))
        cases.append(("producers", False, (), False, False, False, "claude", False, ResumeCaller.OTHER_PANE))
        renamed_tab_case = len(cases) - 1
        cases.append(("producers", True, (), False, False, False, "claude", False, ResumeCaller.OTHER_PANE))
        for case, (workspace, shared, commands, refuse_close, metadata, cross, family, stale, caller) in enumerate(cases):
            shell = not commands
            exiting = case == len(cases) - 1
            with self.subTest(workspace=workspace, shared=shared, commands=commands, refuse_close=refuse_close,
                              family=family, stale=stale, caller=caller):
                _ = write_registry(self.root / "configuration/models.conf", {"producers": {
                    row.name: "codex:gpt-test:high" if family == "codex" else "claude:opus:max"}}, update=True)
                foreign_source = workspace == "morty" and shell and not cross
                foreign_live = foreign_source
                operation_record = staff.StaffSnapshot((row, foreign)) if foreign_live else staff.StaffSnapshot((row,))
                variable = "CODEX_HOME" if family == "codex" else "CLAUDE_CONFIG_DIR"
                source_home = self.root / (".codex1" if family == "codex" else ".claude3")
                target_home = self.root / (".codex2" if family == "codex" else ".claude4" if stale else ".claude3")
                saved = self.entry.copy()
                saved.update(kind=family, environment={variable: str(source_home)}, config_dir=str(source_home))
                self.client.rows[:] = [dict(retained)]
                if workspace == "producers" and not shared and shell:
                    herdr._write_layout("agents", herdr.SavedLayout(at=time.time(), workspaces=[  # pyright: ignore[reportPrivateUsage]
                        herdr.SavedSpace(name="producers", seen=time.time(), tabs=[herdr.SavedMemberTab(
                            staff_id=row.id,
                            seen=time.time())])]))
                if metadata:
                    self.client.rows[0]["agent_session"] = {"kind": "id", "value": current}
                self.client.closed.clear()
                self.client.native_ids.clear()
                self.launched.clear()
                self.start.reset_mock()
                fixture_launch_transcript(self.root, {**saved, "session_id": str(current)}, staff.StaffSnapshot(()))
                pane_record.save("agents", {self.entry["pane_id"]: saved}, record=staff.StaffSnapshot((row,)))
                if stale:
                    atomic_file.write_text_atomic(pane_record.record_directory() / f".producer-start-{row.id}", json.dumps({
                        "member": row.id, "session": current, "kind": family,
                        "folder": str(self.root), "home": str(source_home), "attempt": "killed-start"}))
                source = FakeClient([dict(retained)])
                source.session = "legacy"
                if cross:
                    pane_record.save("legacy", {self.entry["pane_id"]: self.entry})
                    unrelated = self.entry.copy()
                    unrelated.update(session_id="other-session", name="other")
                    pane_record.save("agents", {self.entry["pane_id"]: unrelated})
                def client_for(session: str) -> herdr.Herdr:
                    return source if session == "legacy" else self.client
                def close_source(pane: str) -> bool:
                    source.close_tab_of(pane)
                    return True
                self.tab_titles["w1:t9"] = "another-producer" if case == renamed_tab_case else row.name
                if shared:
                    self.client.rows.append({"pane_id": "w1:p3", "tab_id": "w1:t9", "label": "other work",
                                             "workspace_id": "w1", "agent_session": {"kind": "none"}})
                source_reads = 0
                def layout(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
                    nonlocal source_reads
                    if arguments == ("pane", "process-info", "--pane", "w1:p2") and exiting:
                        source_reads += 1
                        if source_reads == 1:
                            return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 458,
                                "foreground_processes": [{"pid": 458, "name": "claude", "argv": ["claude"]}]}}
                    if arguments == ("pane", "process-info", "--pane", "w1:p2") and foreign_live:
                        return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 457,
                            "foreground_processes": [{"pid": 457, "argv": ["claude"]}]}}
                    if arguments[:2] == ("workspace", "list"):
                        return {"workspaces": [{"workspace_id": "w1", "label": workspace},
                                                {"workspace_id": "w2", "label": "producers"}]}
                    if arguments == ("pane", "process-info", "--pane", "w2:p1") and current in self.launched:
                        return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 456,
                            "foreground_processes": [{"pid": 456, "name": family, "argv": [family, "resume", current]}]}}
                    return self.pane_result(*arguments, deadline=deadline)
                def run(*arguments: str) -> str:
                    if arguments[:2] == ("pane", "read"):
                        return ""
                    if arguments[:2] == ("agent", "prompt"):
                        self.assertEqual(self.client.native_ids[arguments[2]], current)
                        return ""
                    return self.pane_run(*arguments)
                def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
                    self.assertEqual((space, title, cwd), ("producers", row.name, str(self.root)))
                    self.assertIn(variable, environment)
                    self.client.rows.append({"pane_id": "w2:p1", "tab_id": "w2:t1", "label": title,
                                             "terminal_id": "w2:p1-terminal",
                                             "workspace_id": "w2", "agent_session": {"kind": "none"}})
                    self.tab_titles["w2:t1"] = title
                    return "w2:p1"
                def activity(pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
                    return herdr.PaneRunning(commands) if pane == "w1:p2" and commands else herdr.PaneAtShell()
                def start(*arguments: str) -> str:
                    nonlocal foreign_live
                    foreign_live = False
                    self.assertEqual(self.client.closed, [])
                    self.assertEqual(arguments[:2], ("pane", "run"))
                    return ""
                self.start.side_effect = start
                def native(pid: int, kind: str) -> herdr.NativeAgentProcess:
                    self.assertEqual((pid, kind), (456, family))
                    return herdr.NativeAgentProcess(herdr.EnvironmentThread(current), "generation-1",
                        {"HERDR_PANE_ID": "w2:p1", "HERDR_SESSION": "agents", variable: str(target_home)})
                def observe_native(pid: int, kind: str, reported: str = "") -> herdr.NativeAgentProcess:
                    del reported
                    if pid == 458:
                        raise herdr.NativeRegistryUnavailable("pid 458: native registry identity is unavailable")
                    if pid == 457:
                        return herdr.NativeAgentProcess(herdr.EnvironmentThread(str(foreign.current_session)), "generation-1", {})
                    return native(pid, kind)
                def launch(session: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                           prepared: herdr.PreparedAgentEnvironment, kind: Literal["claude", "codex"], arguments: list[str],
                           opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout: int, *, agent: str, client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> herdr.AgentLaunched:
                    nonlocal foreign_live
                    foreign_live = False
                    _ = client
                    del opening, timeout
                    self.assertIsInstance(destination, herdr.ExistingPane)
                    assert isinstance(destination, herdr.ExistingPane)
                    self.assertEqual((session, kind, agent), ("agents", family, row.name))
                    self.assertEqual(arguments[arguments.index("resume") + 1], current)
                    replacement = pane_record.launch(destination.pane, current, str(prepared.directory), prepared.env,
                                                     [kind, *arguments], kind=kind)
                    replacement.update(pid=456, proc_start="generation-1")
                    pane_record.write(session, replacement)
                    self.launched.add(current)
                    self.client.native_ids[destination.pane] = current
                    return herdr.AgentLaunched(destination.pane, herdr.PromptSubmissionNotRequired())
                def conversation(identity: str, kind: str = "") -> herdr.LiveConversation | herdr.StoppedConversation:
                    del kind
                    return (herdr.LiveConversation(pane_launch.agent_kind(saved), (456,)) if identity in self.launched
                            else herdr.StoppedConversation())
                close = (patch.object(self.client, "close_tab_confirmed", return_value=False)
                         if refuse_close else nullcontext())
                claude_registry = (patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive())
                                   if family == "codex" else nullcontext())
                with (herdr.pane_evidence_run() if exiting else nullcontext(),
                      patch.object(staff, "initial_snapshot", return_value=operation_record),
                      patch.object(herdr, "Herdr", side_effect=client_for),
                      patch.object(source, "close_tab_confirmed", side_effect=close_source),
                      patch.object(herdr, "native_process", side_effect=observe_native), close, claude_registry,
                      patch.object(herdr, "read_conversation", side_effect=conversation),
                      patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(target_home)),
                      patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                          self.root, {variable: str(target_home)})),
                      patch.object(herdr, "launch_prepared_agent", side_effect=launch) as codex_start,
                      patch.object(self.client, "run", side_effect=run),
                      patch.object(self.client, "result", side_effect=layout),
                      patch.object(self.client, "pane_activity", side_effect=activity),
                      patch.object(self.client, "open_tab", side_effect=open_tab) as opened,
                      patch.dict(os.environ, {"HERDR_SESSION": "agents", "HERDR_PANE_ID": self.entry["pane_id"]}
                                 if caller is ResumeCaller.TARGET_PANE else {}),
                      patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
                    if caller is ResumeCaller.TARGET_PANE:
                        with self.assertRaises(SystemExit):
                            _ = stop_resume._resume_once(str(current), why="recovery",  # pyright: ignore[reportPrivateUsage]
                                                         names=LaunchNames(operation_record))
                        execute.assert_called_once()
                        target = cast(herdr.TabSession, execute.call_args.args[0])
                        resumed = cast(pane_launch.CurrentLaunch, execute.call_args.args[1])
                        self.assertEqual((target.pane_id, target.session_id), (self.entry["pane_id"], current))
                        self.assertEqual(resumed["session_id"], current)
                        opened.assert_not_called()
                        self.start.assert_not_called()
                        codex_start.assert_not_called()
                        self.assertEqual(self.client.closed, [])
                        publication = pane_record.read("agents")[self.entry["pane_id"]]
                        self.assertEqual(publication.get("staff_id"), fixed)
                        continue
                    result = stop_resume._resume_once(str(current), why="recovery",  # pyright: ignore[reportPrivateUsage]
                                                     to=target_home.name.removeprefix(".") if stale else None,
                                                     names=LaunchNames(operation_record))
                    if not shell or refuse_close:
                        second = stop_resume.resume(row.name)
                        self.assertIsInstance(second, stop_resume.ClaudeUp)
                        assert isinstance(second, stop_resume.ClaudeUp)
                        self.assertEqual((second.session_id, second.pane_id), (current, "w2:p1"))
                        if family == "claude":
                            self.start.assert_called_once()
                        else:
                            codex_start.assert_called_once()
                            self.start.assert_not_called()
                        self.assertEqual([pane["pane_id"] for pane in self.client.rows], ["w1:p2", "w2:p1"])
                self.assertIsInstance(result, stop_resume.ClaudeUp)
                assert isinstance(result, stop_resume.ClaudeUp)
                valid = workspace == "producers" and not shared and shell
                expected = self.entry["pane_id"] if valid else "w2:p1"
                self.assertEqual((result.session_id, result.pane_id), (current, expected))
                if family == "claude":
                    command = self._typed(self.start.call_args.args)
                    self.assertEqual(command[2], expected)
                    self.assertEqual(command[command.index("--resume") + 1], current)
                if valid:
                    opened.assert_not_called()
                else:
                    opened.assert_called_once()
                self.assertEqual(self.client.closed, [self.entry["pane_id"]] if not valid and shell and not refuse_close and not cross and not foreign_source else [])
                self.assertEqual(list(pane_record.read("agents")),
                                 [self.entry["pane_id"], expected] if cross or not shell else [expected])
                if stale:
                    self.assertTrue(pane_record.read("agents")[expected].get("producer_binding", False))
                if cross:
                    self.assertEqual(source.closed, [self.entry["pane_id"]])
                    self.assertEqual(pane_record.read("legacy"), {})
                if shared:
                    self.assertEqual([pane["pane_id"] for pane in self.client.rows], ["w1:p3", "w2:p1"])

    def test_pane_command_unsets_conductor_variables_and_removes_saved_values(self) -> None:
        conductor = {"HERDR_TAB_ID": "w1:t9", "STAFF_ID": "conductor", "STAFF_OPERATION_FD": "81",
                     "PYTHONUNBUFFERED": "1", "REFUSAL_WATCH_INVENTORY": "inventory", "STAFF_ROW_FD_x": "82"}
        entry = self.entry.copy()
        entry["environment"] = {**entry["environment"], **conductor,
                                "STAFF_ROW_FD_saved": "83", "RESUME_TEST_VALUE": "kept"}
        with patch.dict(os.environ, conductor):
            command = stop_resume._pane_command(  # pyright: ignore[reportPrivateUsage]
                herdr.TabSession("w1:p2", "alpha", "w1", entry["session_id"]), entry, "claude3")
        environment = command[1:command.index("sh")]
        for key in (*conductor, "STAFF_ROW_FD_saved"):
            with self.subTest(key=key):
                self.assertEqual(environment[environment.index(key) - 1], "-u")
                self.assertFalse(any(argument.startswith(f"{key}=") for argument in environment))
        self.assertIn("RESUME_TEST_VALUE=kept", environment)

    def test_pane_command_trusts_the_codex_folder(self) -> None:
        folder = str(self.root / 'a.b "folder"')
        entry = pane_record.launch("w1:p2", "thread", folder, {"CODEX_HOME": "/account"}, ["codex"], kind="codex")
        command = stop_resume._pane_command(  # pyright: ignore[reportPrivateUsage]
            herdr.TabSession("w1:p2", "test", "w1", "thread"), entry, "workers")
        self.assertEqual(command[-4:], ["-c", 'projects={' + json.dumps(folder)
            + '={trust_level="trusted"}}', "resume", "thread"])

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch.dict(os.environ, {"STAFF_FILE": str(self.root / "staff.json")}):
            self.entry = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", str(self.root),
                {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3"), "SHOWRUNNER_UNIT": "alpha"},
                ["claude", "-n", "alpha", "--remote-control", "alpha", "--model", "sonnet"])
        self.client = FakeClient([{"pane_id": "w1:p2", "tab_id": "w1:t9", "label": "alpha", "workspace_id": "w1",
                                   "agent_session": {"kind": "id", "value": "019b3a40-4250-7000-8000-000000000102"}}])
        self.client_result = self.client.result
        self.tab_titles: dict[str, str] = {"w1:t9": "alpha"}
        for label in ("claude3", "claude4"):
            config = self.root / f".{label}"
            config.mkdir()
            _ = (config / ".claude.json").write_text(json.dumps({
                "projects": {str(self.root): {"hasTrustDialogAccepted": True}},
            }))
        self.live = Mock(return_value=sessions.SessionNotLive())
        self.process = Mock(return_value=sessions.LiveSessionProcess(123))
        self.kill = Mock()
        self.signal_guard = patch.object(stop_resume, "_signal_process", self.kill)
        self.wait = Mock()
        self.start = Mock(return_value="")
        self.seed = Mock(return_value=0)
        self.detach = Mock()
        self.confirm = Mock(side_effect=confirmed_fixture_move)
        self.real_detach = stop_resume._detach  # pyright: ignore[reportPrivateUsage]
        self.clock = ResumeClock()
        self.launched: set[str] = set()
        self.publish_new_launch = False

    @override
    def setUp(self) -> None:
        configuration = self.root / "configuration"
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            _ = write_config_fixture(configuration / source.name)
        write_machine_config(configuration)
        _ = write_config_fixture(configuration / "staff.conf", "PRODUCER_WORKSPACE=producers\n")
        _ = write_config_fixture(configuration / "shutdown.conf", "START_SECONDS=20\nSTOP_GRACE_SECONDS=2\n")
        _ = write_registry(configuration / "models.conf", {"producers": {
            "alpha": "claude:opus:max", "renamed-alpha": "claude:opus:max",
            "TEST-account-home": "claude:opus:max", "row-name": "claude:opus:max",
            "own-codex": "codex:gpt-test:high"}})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CONF_DIR": str(configuration), "MODELS_CONFIG_FILE": str(configuration / "models.conf")})))
        _ = self.enterContext(redirect_stderr(io.StringIO()))
        _ = self.enterContext(patch.object(accounts, "accounts_home", return_value=self.root))
        def resolved_program(tool: str, *, environment: Mapping[str, str]) -> str:
            self.assertTrue(environment)
            return str(self.root / "bin" / tool)
        _ = self.enterContext(patch.object(agent_program, "resolve_program", side_effect=resolved_program))
        _ = self.enterContext(patch.object(accounts, "role_account", return_value=accounts.NamedAccount(
            "claude3", self.root / ".claude3")))
        _ = self.enterContext(patch.object(accounts, "check_move_target", return_value=accounts.MoveTargetReady(
            accounts.NamedAccount("claude4", self.root / ".claude4"))))
        _ = self.enterContext(patch.object(account, "process_config_dir", side_effect=self.process_account))
        _ = self.enterContext(patch.object(herdr, "repository_root", return_value=self.root))
        _ = self.enterContext(patch.object(herdr, "repository_trust_key", return_value=self.root))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root),
            "CODEX_HOME": str(self.root / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / ".claude3"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "AGENT_NOTES_DIR": str(self.root / "notes"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CLAUDE_CODE_SESSION_ID": "", "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
        })))
        self.assertTrue(moves.store_file().is_relative_to(self.root))
        sessions.sessions_dir().mkdir(parents=True, exist_ok=True)
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = self.enterContext(patch.object(herdr, "shared_session", return_value="agents"))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.client))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        fixture_launch_transcript(self.root, self.entry, staff.StaffSnapshot(()))
        saved_records = pane_record.save
        def save(session: str, entries: dict[str, pane_record.PaneLaunch], *,
                 record: staff.StaffSnapshot | pane_launch.StaffSnapshotRead = pane_launch.StaffSnapshotRead.CURRENT) -> None:
            for entry in entries.values():
                fixture_launch_transcript(self.root, entry, record if isinstance(record, staff.StaffSnapshot) else staff.StaffSnapshot(()))
            saved_records(session, entries, record=record)
        _ = self.enterContext(patch.object(pane_record, "save", side_effect=save))
        publish_record = pane_record.write
        def write(session: str, entry: pane_record.PaneLaunch) -> None:
            source = entry.copy()
            if "session_id" in source:
                try:
                    _ = staff.native_id(source["session_id"])
                except staff.StaffRefused:
                    source["session_id"] = self.entry["session_id"]
            fixture_launch_transcript(self.root, source, staff.StaffSnapshot(()))
            publish_record(session, entry)
        _ = self.enterContext(patch.object(pane_record, "write", side_effect=write))
        pane_record.write("agents", self.entry)
        _ = self.enterContext(patch.object(stall_watch, "STATE_DIR",
                                         Path(self.enterContext(tempfile.TemporaryDirectory()))))
        _ = self.enterContext(patch.object(sessions, "session_liveness", self.observe))
        _ = self.enterContext(patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())))
        _ = self.enterContext(patch.object(sessions, "process_start", return_value="generation-1"))
        _ = self.enterContext(patch.object(session_records, "process_start", return_value="generation-1"))
        _ = self.enterContext(patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
            herdr.PaneSessionReport("019b3a40-4250-7000-8000-000000000102"), "generation-1", {})))
        _ = self.enterContext(patch.object(sessions, "process_liveness", self.process))
        _ = self.enterContext(patch.object(os, "kill", self.kill))
        # Stop tests supply signals; guard tests restore the shared function.
        _ = self.signal_guard.start()
        self.addCleanup(self.signal_guard.stop)
        _ = self.enterContext(patch.object(stop_resume, "Event", self.wait))
        cast(Mock, cast(Mock, self.wait.return_value).wait).side_effect = self.advance
        _ = self.enterContext(patch.object(self.client, "run", self.pane_run))
        _ = self.enterContext(patch.object(self.client, "run_in_pane", self.run_in_pane))
        _ = self.enterContext(patch.object(self.client, "result", self.pane_result))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HERDR_PROC": str(self.root / "proc")})))
        _ = self.enterContext(self.client.empty_shell(998042, self.root))
        from scripts.production.fake_unit_host import process
        process(os.environ, 123, 1, ["claude"], {}, cwd=self.root)
        _ = self.enterContext(patch.object(stop_resume, "monotonic", lambda: self.clock.elapsed))
        _ = self.enterContext(patch.object(time, "sleep", self.advance))
        _ = self.enterContext(patch.object(unit_runs, "held", return_value=[]))
        _ = self.enterContext(patch.object(pane_record, "seed", self.seed))
        _ = self.enterContext(patch.object(stop_resume, "_detach", self.detach))
        _ = self.enterContext(patch.object(stop_resume, "_confirm_move_start", self.confirm))
        _ = self.enterContext(patch.object(stop_resume, "_send_move_push"))
        _ = self.enterContext(patch.object(showrunners, "tell", return_value="fixture report"))


    def run_in_pane(self, pane: str, command: list[str], *, terminal: str = "") -> None:
        RUN_IN_PANE(self.client, pane, command, terminal=terminal)

    def process_account(self, _pid: int) -> account.ConfiguredClaudeFolder:
        return account.ConfiguredClaudeFolder(self.root / (".claude4" if self.launched else ".claude3"))

    def advance(self, seconds: float) -> None:
        self.clock.elapsed += seconds

    def observe(self, session: str, registry: sessions.RegistryRead | None = None
                ) -> sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown:
        if registry is not None:
            return session_records.session_liveness(session, registry=registry)
        return sessions.LiveSessionProcess(456) if session in self.launched else cast(
            sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown, self.live(session))

    def pane_result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        if arguments[:2] == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "w1", "label": "producers"}]}
        if arguments[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": row.get("tab_id", str(row["pane_id"]) + ":tab"),
                              "workspace_id": row["workspace_id"],
                              "label": self.tab_titles.get(
                                  str(row.get("tab_id", str(row["pane_id"]) + ":tab")), row["label"]),
                              "pane_count": 1} for row in self.client.rows]}
        return self.client_result(*arguments, deadline=deadline)

    def pane_run(self, *arguments: str) -> str:
        if arguments == ("session", "list", "--json"):
            return json.dumps({"sessions": [{"name": self.client.session, "running": True, "default": False,
                "session_dir": str(self.root), "socket_path": str(self.root / "herdr.sock")}]})
        if arguments[:2] in (("pane", "rename"), ("tab", "rename")):
            for row in self.client.rows:
                tab = str(row.get("tab_id", str(row["pane_id"]) + ":tab"))
                if arguments[:2] == ("pane", "rename") and arguments[2] == row["pane_id"]:
                    if tab not in self.tab_titles:
                        self.tab_titles[tab] = str(row["label"])
                    row["label"] = arguments[3]
                elif arguments[:2] == ("tab", "rename") and arguments[2] == tab:
                    self.tab_titles[tab] = arguments[3]
            return "{}"
        result = cast(str, self.start(*arguments))
        command = self._typed(arguments)
        identity = command[command.index("--resume") + 1]
        self.launched.add(identity)
        self.client.native_ids[arguments[2]] = identity
        pane = arguments[2]
        if pane not in pane_record.read("agents"):
            references = (identity, self.entry["session_id"]) if "resume" in command else (identity,)
            original = next((entry for entry in pane_record.read("agents").values()
                             if pane_launch.reference_id(entry) in references), None)
            if original is None:
                names = LaunchNames.read()
                original = next((entry for entry in pane_record.launches()
                                 if names.resume(entry)["session_id"] == identity), None)
            assert original is not None
            environment = dict(original["environment"])
            for item in command[:command.index("exec")]:
                if "=" in item:
                    key, value = item.split("=", 1)
                    environment[key] = value
            replacement = pane_record.launch(pane, identity, command[command.index("resume") + 1], environment,
                ["claude", *command[command.index("exec") + 2:]])
            if self.publish_new_launch:
                replacement.update(pid=456, proc_start="generation-1")
                pane_record.write("agents", replacement)
            else:
                entries = pane_record.read("agents")
                entries[pane] = replacement
                pane_record.save("agents", entries)
        return result

    @staticmethod
    def _typed(arguments: tuple[str, ...]) -> tuple[str, ...]:
        """What the pane's shell parses from the one line herdr types."""
        return (*arguments[:3], *shlex.split(arguments[3]))

    def add_beta(self) -> None:
        second = pane_record.launch("w1:p4", "sid-beta", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3")}, ["claude", "-n", "beta", "--model", "opus"])
        self.client.rows.append({"pane_id": "w1:p4", "terminal_id": "beta-terminal", "tab_id": "w1:t7", "label": "beta", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-beta"}})
        pane_record.write("agents", second)

    def alive_until_kill(self, _pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        return (sessions.LiveSessionProcess(123) if call(123, signal.SIGKILL) not in self.kill.call_args_list
                else sessions.SessionNotLive())

    def test_review_resume_keeps_the_lens_and_task_with_the_current_folder(self) -> None:
        run = str(self.root / "worker run")
        old, current = str(self.root / "old folder"), str(self.root / "staff folder")
        command = ["bash", str(stop_resume.SCRIPTS / "direct/review.sh"), "--lens", "contract",
                   "--phase", "4w", run, old, "old prompt.md", "review", "check the contract", "reviewing", "3"]
        seat = ClaudeChildResume(run_dir=run, seat="review2", role="review", staff_id="019b3a40-4250-7000-8000-000000000104",
                          turn_live=True, launcher_command=command)
        rebuilt = shlex.split(stop_resume.resume_worker_command(seat, current))
        self.assertEqual(rebuilt, ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh"),
                                  "--to", "review2", "--session-dir", run, "--cwd", current,
                                  "--prompt", str(Path(run) / "review2.resume-prompt.md"),
                                  "--lens", "contract", "--phase", "4w", "--role", "review", "--pass", "3"])
        self.assertEqual(seat["launcher_command"], command)

    def test_saved_review_resumes_without_retired_allocation_options(self) -> None:
        run, cwd = str(self.root / "worker run"), str(self.root / "working tree")
        task = [run, cwd, "old prompt.md", "review", "check this change", "reviewing", "2"]
        command = ["bash", str(stop_resume.SCRIPTS / "direct/review.sh"),
                   "--pool", "code", "--lease", "x/y/7", "--phase", "3p", "--lens", "contract",
                   "--slot", "review", *task]
        seat = ClaudeChildResume(run_dir=run, seat="review2", role="review", staff_id="019b3a40-4250-7000-8000-000000000104",
                          turn_live=True, launcher_command=command)
        expected = ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh"), "--to", "review2",
                    "--session-dir", run, "--cwd", cwd, "--prompt", str(Path(run) / "review2.resume-prompt.md"),
                    "--phase", "3p", "--lens", "contract", "--role", "review", "--pass", "2"]
        rebuilt = stop_resume.resume_worker_command(seat, cwd)
        self.assertEqual(shlex.split(rebuilt), expected)
        self.assertNotIn("--pool", shlex.split(rebuilt))
        self.assertNotIn("--lease", shlex.split(rebuilt))
        seat["launcher_command"] = shlex.split(rebuilt)
        self.assertEqual(stop_resume.resume_worker_command(seat, cwd), rebuilt)

    def test_positional_review_resume_keeps_the_pass_lens_and_delivery_file(self) -> None:
        run, cwd = str(self.root / "worker run"), str(self.root / "staff folder")
        ready = str(self.root / "final diff ready")
        for requested_lens in ("", "contract"):
            with self.subTest(requested_lens=requested_lens):
                command = ["bash", str(stop_resume.SCRIPTS / "direct/review.sh")]
                if requested_lens:
                    command.extend(("--lens", requested_lens))
                command.extend((run, "/old tree", "old prompt", "review", "check this change", "reviewing",
                                "3", "adversary", ready))
                seat = ClaudeChildResume(run_dir=run, seat="review2", role="review", staff_id="worker-id",
                                  turn_live=True, launcher_command=command)
                rebuilt = shlex.split(stop_resume.resume_worker_command(seat, cwd))
                options = dict(zip(rebuilt[2::2], rebuilt[3::2]))
                self.assertEqual(options["--pass"], "3")
                self.assertEqual(options["--lens"], requested_lens or "adversary")
                self.assertEqual(options["--early-ready"], ready)
                self.assertEqual(options["--cwd"], cwd)
                self.assertEqual(options["--prompt"], str(Path(run) / "review2.resume-prompt.md"))
                seat["launcher_command"] = rebuilt
                self.assertEqual(shlex.split(stop_resume.resume_worker_command(seat, cwd)), rebuilt)

    def test_saved_assignment_resume_keeps_task_options_and_repeated_images(self) -> None:
        run, cwd = str(self.root / "worker run"), str(self.root / "staff folder")
        task = ["--role", "review", "--model", "gpt-6.1", "--effort", "high",
                "--image", "/image one.png", "--image", "/image two.png", "--operation", "follow",
                "--held-role", "reviewer", "--held-design", "design-id", "--family", "codex",
                "--phase", "3t", "--lens", "contract", "--pass", "4", "--reply-file", "/summary file",
                "--early-ready", "/ready file", "--style-since", "abc", "--style-until", "def",
                "--diff", "/saved diff", "--log", "/saved log", "--id-file", "/saved id",
                "--from-session-dir", "/former run", "--fix-cause", "keep this cause", "--paths", "/saved paths"]
        for launcher in ("assign.sh", "assign.py"):
            with self.subTest(launcher=launcher):
                saved = ["bash" if launcher.endswith(".sh") else "python3",
                         str(stop_resume.SCRIPTS / "direct" / launcher), "--new-worker", "former-worker",
                         "--session-dir", "/old run", "--cwd", "/old tree", "--prompt", "/old prompt", *task]
                seat = ClaudeChildResume(run_dir=run, seat="review2", role="impl", staff_id="worker-id",
                                  turn_live=True, launcher_command=saved)
                rebuilt = shlex.split(stop_resume.resume_worker_command(seat, cwd))
                self.assertEqual(rebuilt, ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh"),
                                          "--to", "review2", "--session-dir", run, "--cwd", cwd,
                                          "--prompt", str(Path(run) / "review2.resume-prompt.md"), *task])
                self.assertEqual(seat["launcher_command"], saved)
                seat["launcher_command"] = rebuilt
                self.assertEqual(shlex.split(stop_resume.resume_worker_command(seat, cwd)), rebuilt)

    def test_writer_resume_uses_the_phase_folder_and_has_no_run_paths_fallback(self) -> None:
        run = str(self.root / "worker run")
        for phase in ("ad hoc", ""):
            with self.subTest(phase=phase):
                command = ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh"), "--role", "implement",
                           "--session-dir", run, "--cwd", "/old tree", "--prompt", "old prompt", "--to", "writer"]
                if phase:
                    command.extend(("--phase", phase))
                seat = ClaudeChildResume(run_dir=run, seat="writer", role="impl", staff_id="worker-id",
                                  turn_live=True, launcher_command=command)
                rebuilt = shlex.split(stop_resume.resume_worker_command(seat, "/current tree"))
                options = dict(zip(rebuilt[2::2], rebuilt[3::2]))
                self.assertEqual(options["--role"], "implement")
                if phase:
                    self.assertEqual(options["--paths"], str(Path(run) / "phases/adhoc/files"))
                else:
                    self.assertNotIn("--paths", options)

    def test_resume_without_a_launcher_uses_the_shared_last_dispatch(self) -> None:
        from scripts.direct import progress_history

        run = self.root / "worker run"
        seat = ClaudeChildResume(run_dir=str(run), seat="worker", role="impl",
                          staff_id="019b3a40-4250-7000-8000-000000000104", turn_live=True, launcher_command=[])
        for role in ("review", "test"):
            with self.subTest(role=role):
                seat["role"] = role
                missing = progress_history.DispatchDetailNotRecorded.UNRECORDED
                dispatch = progress_history.RecordedDispatch(
                    role, "3t", "contract" if role == "review" else missing,
                    missing if role == "review" else "/saved/paths.txt")
                with patch.object(progress_history, "last_dispatch", return_value=dispatch) as read:
                    command = shlex.split(stop_resume.resume_worker_command(seat, "/current/tree"))
                read.assert_called_once_with(staff.StaffId(seat["staff_id"]))
                self.assertEqual(command[:2], ["bash", str(stop_resume.SCRIPTS / "direct/assign.sh")])
                options = dict(zip(command[2::2], command[3::2]))
                self.assertEqual(options["--role"], seat["role"])
                self.assertEqual(options["--phase"], "3t")
                self.assertEqual(options["--cwd"], "/current/tree")
                self.assertEqual(options["--prompt"], str(run / "worker.resume-prompt.md"))
                if role == "review":
                    self.assertEqual(options["--lens"], "contract")
                    self.assertNotIn("--paths", options)
                else:
                    self.assertEqual(options["--paths"], "/saved/paths.txt")

    def test_recovery_opens_a_fresh_tab_for_a_missing_pane_then_uses_the_shared_resume(self) -> None:
        self.client.rows.clear()
        def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
            self.assertEqual((space, title, cwd), ("alpha", "alpha", str(self.root)))
            self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.root / ".claude3"))
            self.client.rows.append({"pane_id": "w2:p3", "terminal_id": "fresh-terminal", "label": title, "workspace_id": "w2",
                "agent_session": {"kind": "none"}})
            return "w2:p3"
        with patch.object(self.client, "open_tab", side_effect=open_tab) as opened:
            result = stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="watchdog recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual((result.session_id, result.pane_id, result.account), ("019b3a40-4250-7000-8000-000000000102", "w2:p3", "claude3"))
        opened.assert_called_once()
        self.assertIn("019b3a40-4250-7000-8000-000000000102", self.launched)
        self.assertEqual(cast(pane_launch.NativePaneLaunch, pane_record.read("agents")["w2:p3"])["session_id"], "019b3a40-4250-7000-8000-000000000102")
        self.assertEqual(self._typed(self.start.call_args.args)[2], "w2:p3")

    def test_recovery_uses_the_locked_snapshot_for_the_renamed_row_title_and_arguments(self) -> None:
        from scripts.production import session_staff

        fixed = staff.StaffId(self.entry["session_id"])
        current = staff.StaffId("019b3a40-4250-7000-8000-000000000103")
        row = staff.StaffRow(fixed, "showrunner", "row-name", staff.StaffRoot(), current_session=current,
                             home=staff.SessionHome(self.root / ".claude3"), worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        record = staff.StaffSnapshot((row,))
        _ = self.enterContext(patch.object(staff, "local_snapshot", return_value=record))
        retained = self.entry.copy()
        retained["session_id"] = current
        retained["staff_id"] = row.id
        fixture_launch_transcript(self.root, retained, record)
        pane_record.save("agents", {retained["pane_id"]: retained})
        self.client.rows.clear()
        opened: list[str] = []

        def open_tab(space: str, title: str, cwd: str, environment: dict[str, str]) -> str:
            _ = space, cwd, environment
            opened.append(title)
            self.client.rows.append({"pane_id": "w2:p3", "terminal_id": "fresh-terminal", "label": title, "workspace_id": "w2"})
            return "w2:p3"

        with (patch.object(staff, "initial_snapshot", return_value=record) as snapshot,
              patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(())),
              patch.object(self.client, "open_tab", side_effect=open_tab)):
            result = stop_resume.resume(str(fixed), why="recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(opened, ["row-name"])
        self.assertEqual(snapshot.call_count, 3)
        command = self._typed(self.start.call_args.args)
        self.assertEqual(command[command.index("-n") + 1], "row-name")
        self.assertEqual(command[command.index("--resume") + 1], current)
        self.assertEqual(pane_launch.current_launch(pane_record.read("agents")["w2:p3"], record)["name"], "row-name")

    def test_recovery_retitles_an_existing_pane_with_the_current_row_name(self) -> None:
        from scripts.production import session_staff

        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "showrunner", "row-name", staff.StaffRoot(), current_session=identity, worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))) as snapshot,
              patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(()))):
            result = stop_resume.resume(str(identity), why="recovery")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(self.client.rows[0]["label"], "row-name")
        command = self._typed(self.start.call_args.args)
        self.assertEqual(command[command.index("-n") + 1], "row-name")
        self.assertEqual(snapshot.call_count, 2)

    def test_recovery_reconciles_titles_and_preserves_unnamed_titles(self) -> None:
        from scripts.production import session_staff

        identity = staff.StaffId(self.entry["session_id"])
        cases = (
            ("stale-tab", "row-name", "row-name", "old-tab", 1),
            ("current-titles", "row-name", "row-name", "row-name", 2),
            ("unnamed", "", "manual-pane", "manual-tab", 1),
        )
        for case, name, pane_title, tab_title, attempts in cases:
            with self.subTest(case=case):
                retained = self.entry.copy()
                retained["name"] = name
                pane_record.save("agents", {retained["pane_id"]: retained})
                self.client.rows[0]["label"] = pane_title
                self.tab_titles["w1:t9"] = tab_title
                record = staff.StaffSnapshot((staff.StaffRow(
                    identity, "showrunner", name, staff.StaffRoot(), current_session=identity, worktree=staff.PresentWorktree(remote.local_machine(), self.root)),)) if name else staff.StaffSnapshot(())
                state = self.root / case
                with (patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}),
                      patch.object(staff, "initial_snapshot", return_value=record),
                      patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(())),
                      patch.object(self.client, "retitle", wraps=self.client.retitle) as retitle,
                      patch.object(self.client, "run", wraps=self.client.run) as commands,
                      patch.object(self.client, "order_tabs", return_value={}) as order,
                      patch.object(self.client, "save_layout", wraps=self.client.save_layout) as save):
                    for _ in range(attempts):
                        self.launched.clear()
                        result = stop_resume.resume(str(identity), why="recovery")
                        self.assertIsInstance(result, stop_resume.ClaudeUp)
                    renames = [invocation for invocation in commands.call_args_list
                               if invocation.args[:2] in (("pane", "rename"), ("tab", "rename"))]
                    if case == "stale-tab":
                        self.assertEqual(renames, [call("tab", "rename", "w1:t9", name)])
                        order.assert_called_once()
                        save.assert_called_once()
                    else:
                        self.assertEqual(renames, [])
                        order.assert_not_called()
                        save.assert_not_called()
                        self.assertFalse(herdr.layout_path("agents").exists())
                    self.assertEqual(self.client.rows[0]["label"], name or pane_title)
                    self.assertEqual(self.tab_titles["w1:t9"], name or tab_title)
                    if name:
                        self.assertEqual(retitle.call_count, attempts)
                    else:
                        retitle.assert_not_called()

    def test_stopped_unit_resume_refuses_an_unreadable_owner_report(self) -> None:
        parent = fixture_member("showrunner", name="unit-owner")
        row = fixture_member("unit", name="alpha", parent=parent.id, runtime_id=self.entry["session_id"],
                             worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        with patch.object(unit_lookup, "checked_unit", return_value=unit_lookup.UnknownUnit("pane list denied")):
            result = stop_resume.resume(str(row.id))
        self.assertEqual(result, stop_resume.ResumeFailed(row.current_session, "pane list denied"))
        self.start.assert_not_called()

    def test_stopped_unit_resumes_in_its_pane_with_a_detached_holder(self) -> None:
        from scripts.production.fake_unit_host import process

        parent = fixture_member("showrunner", name="unit-owner")
        row = fixture_member("unit", name="alpha", parent=parent.id, runtime_id=self.entry["session_id"],
                             worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        place = herdr.HerdrPlace("agents", self.entry["pane_id"])
        shell, supervisor = 998042, 998043
        process(os.environ, supervisor, 1, ["python3", "supervise"],
                {"HERDR_PANE_ID": place.pane}, cwd=self.root)
        detached = herdr.HerdrProcess(supervisor, place, {"CLAUDE_CODE_SESSION_ID": row.current_session},
                                     "python3", str(self.root),
                                     herdr.ProcessAncestry({supervisor: 1}, {shell: 34816, supervisor: 0}))
        shell_process = herdr.HerdrProcess(shell, place, {"CLAUDE_CODE_SESSION_ID": row.current_session},
                                          "zsh", str(self.root))
        with (patch.object(herdr, "live_sessions", return_value=[{"name": place.session}]),
              patch.object(herdr, "herdr_processes", return_value=[shell_process, detached])):
            result = stop_resume.resume(str(row.id), to="claude4")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual((result.pane_id, result.session_id, result.account), (place.pane, row.current_session, "claude4"))
        self.assertEqual(self.start.call_args.args[:3], ("pane", "run", place.pane))

    def test_stopped_unit_waits_only_for_non_agent_holders(self) -> None:
        parent = fixture_member("showrunner", name="unit-owner")
        row = fixture_member("unit", name="alpha", parent=parent.id, runtime_id=self.entry["session_id"],
                             worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        place = herdr.HerdrPlace("agents", self.entry["pane_id"])
        shell, holder = 998042, 998043
        ancestry = herdr.ProcessAncestry({holder: shell}, {shell: 34816, holder: 34816})
        shell_process = herdr.HerdrProcess(shell, place, {"CLAUDE_CODE_SESSION_ID": row.current_session},
                                          "zsh", str(self.root), ancestry)
        _ = write_config_fixture(self.root / "configuration/shutdown.conf", "HOLDER_EXIT_SECONDS=.25\n")
        for command, runs_agent, exits_after_wait, expected_wait in (
                ("node", True, False, 0.0),
                ("python3", False, False, 0.25),
                ("python3", False, True, 0.1),
        ):
            with self.subTest(command=command, exits_after_wait=exits_after_wait):
                self.clock.elapsed = 0
                began = time.monotonic()
                process = herdr.HerdrProcess(holder, place, {}, command, str(self.root), ancestry, runs_agent)
                before = [shell_process, process]
                after = [shell_process] if exits_after_wait else before

                def observe(**_options: object) -> list[herdr.HerdrProcess]:
                    return after if self.clock.elapsed else before

                def clock() -> float:
                    return began + self.clock.elapsed

                with (patch.object(herdr, "live_sessions", return_value=[{"name": place.session}]),
                      patch.object(herdr, "herdr_processes", side_effect=observe),
                      patch.object(time, "monotonic", new=clock),
                      patch.object(stop_resume, "monotonic", new=clock),
                      patch.object(unit_lookup, "checked_unit", wraps=unit_lookup.checked_unit) as check):
                    result = stop_resume.resume(str(row.id), to="claude4")
                self.assertAlmostEqual(self.clock.elapsed, expected_wait)
                if exits_after_wait:
                    self.assertIsInstance(result, stop_resume.ClaudeUp)
                    self.assertEqual(self.start.call_args.args[:3], ("pane", "run", place.pane))
                    self.assertEqual(check.call_count, 2)
                else:
                    self.assertEqual(result, stop_resume.ResumeFailed(row.current_session,
                        f"no foreground agent. Blocking processes are {command} (inside the shell tree)"))
                    self.start.assert_not_called()
                    self.assertEqual(check.call_count, 1 if runs_agent else 3)


    def test_recovery_fixture_rejects_an_unrecorded_resume_identity(self) -> None:
        with self.assertRaises(AssertionError):
            _ = self.pane_run("pane", "run", "w2:p3", "exec claude --resume unrecorded-session")
        self.assertNotIn("w2:p3", pane_record.read("agents"))

    def test_fixed_owner_resume_uses_current_pane_and_preserves_staff_and_retained_record(self) -> None:
        from scripts.production.staff_test_support import isolated_staff_file, isolated_source_readers, source_environment

        path = self.root / "staff.json"
        with (isolated_staff_file(path, {}),
              patch.dict(os.environ, source_environment(self.root / "sources")),
              isolated_source_readers()):
            sessions.sessions_dir().mkdir(parents=True, exist_ok=True)
            parent = staff.ensure("showrunner", name="demo", runtime_id="019b3a40-4250-7000-8000-000000000101")
            owner = staff.ensure("unit", name="alpha", parent=staff.ReportsTo(parent.id), runtime_id=self.entry["session_id"])
            owner = staff.set_home(owner.id, staff.SessionHome(self.root / ".claude3"))
            current = staff.native_id("019b3a40-4250-7000-8000-000000000103")
            _ = staff.update_current_session(owner.current_session, current)
            fixture_launch_transcript(self.root, {**self.entry, "session_id": str(current)}, staff.StaffSnapshot(()))
            folder = self.root / "staff-folder"
            _ = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), folder), expected=owner.worktree)
            self.client.rows[0]["agent_session"] = {"kind": "id", "value": current}
            pane_record.write("agents", self.entry)
            record = pane_record.record_path("agents")
            before = path.read_bytes(), record.read_bytes(), record.stat().st_mtime_ns
            result = stop_resume.resume(owner.id, why="fixture changed conversation")
            self.assertIsInstance(result, stop_resume.ClaudeUp)
            assert isinstance(result, stop_resume.ClaudeUp)
            self.assertEqual((result.session_id, result.pane_id, result.account), (current, "w1:p2", "claude3"))
            arguments = self._typed(self.start.call_args.args)
            self.assertEqual(arguments[arguments.index("--resume") + 1], current)
            self.assertNotIn(owner.id, arguments)
            self.assertEqual(self.launched, {current})
            self.assertIn(str(folder), arguments)
            self.assertEqual((path.read_bytes(), record.read_bytes(), record.stat().st_mtime_ns), before)

        from scripts.herdr.test_worker_panes import WorkerPaneTests
        from dataclasses import replace

        case = WorkerPaneTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        _ = case.enterContext(patch.object(pane_record, "RECORD_DIR", pane_launch.record_directory()))
        _ = copy_config_file_fixture(Path(__file__).resolve().parents[2] / "config/shutdown.conf",
                                     case.root / "conf/shutdown.conf")
        owner = case.worker("selected-native")
        foreign = replace(owner, id=staff.StaffId("foreign-native"),
                          current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000199"))
        case.world.add(owner, "w1:p7", label="misleading-current")
        case.world.add(foreign, "w1:p8", label=owner.name)
        case.world.locations["w1:p7"]["agent_session"] = {"value": "stale-report"}
        case.world.locations["w1:p8"]["agent_session"] = {"value": owner.current_session}
        case.world.record(owner, "w1:p8")
        entries = pane_record.read("agents")
        _ = entries.pop("w1:p7", None)
        pane_record.save("agents", entries)

        def native_liveness(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            for pid, process in case.world.processes.items():
                if process.current_identity == identity:
                    return sessions.LiveSessionProcess(pid)
            return sessions.SessionNotLive()

        with (self.subTest(record="reused pane"),
              patch.object(sessions, "session_liveness", side_effect=native_liveness)):
            result = stop_resume.resume(owner.id)
            self.assertIsInstance(result, stop_resume.ClaudeUp)
            assert isinstance(result, stop_resume.ClaudeUp)
            self.assertEqual((result.session_id, result.pane_id), (owner.current_session, "w1:p7"))
            self.assertEqual(case.world.launches, [])


    def test_stop_returns_as_soon_as_term_takes_effect(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        def exits_soon(_pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(123) if self.clock.elapsed < 0.1 else sessions.SessionNotLive()
        self.process.side_effect = exits_soon
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertLessEqual(self.clock.elapsed, 0.1)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def prompt_retirement_fixture(self, *, alerted: bool) -> tuple[Path, staff.StaffRow]:
        """Publish cancellation requests for the watcher with an isolated send.py recorder."""
        from scripts.production.staff_test_support import unit_membership

        row = unit_membership("production", "alpha")
        row = staff.update_current_session(row.current_session, staff.native_id(self.entry["session_id"]))
        row = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), self.root), expected=row.worktree)
        self.entry["environment"].update({"SHOWRUNNER_UNIT": "production", "SHOWRUNNER_UNIT_ID": "alpha"})
        pane_record.write("agents", self.entry)
        state = self.root / "stall-watch"
        state.mkdir(exist_ok=True)
        sender = self.root / "retire.py"
        log = self.root / "retire.json"
        _ = sender.write_text("import json, pathlib, sys\n"
                              + f"pathlib.Path({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        _ = self.enterContext(patch.object(stall_watch, "STATE_DIR", state))
        _ = self.enterContext(patch.object(stall_watch, "SEND", sender))
        prompt = stall_watch.PromptWait(session_id="019b3a40-4250-7000-8000-000000000102", created=0,
            key="stall-watch:prompt:production:alpha", since=0, alerted=alerted, tool="permission", retiring=False, told=60)
        stall_watch.save_prompt(stall_watch.watched_staff_unit("production", "alpha", row).prompt_path, prompt)
        return log, row

    def assert_prompt_cancellation_requested(self, log: Path, row: staff.StaffRow) -> None:
        self.assertFalse(log.exists(), "stop and move must never send")
        path = stall_watch.watched_staff_unit("production", "alpha", row).prompt_path
        self.assertTrue(path.exists())
        requests = list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json"))
        self.assertTrue(requests)
        for request in requests:
            record = cast(dict[str, object], json.loads(request.read_text()))
            self.assertEqual(set(record), {"session_id", "at"})
            self.assertEqual(record["session_id"], "019b3a40-4250-7000-8000-000000000102")
            self.assertIsInstance(record["at"], (float, int))

    def drain_prompt_cancellations(self) -> None:
        with (patch.object(showrunners, "load_settings", return_value={}),
              patch.object(showrunners, "registered_showrunners", return_value=showrunners.ShowrunnerDiscovery()),
              patch.object(stall_watch, "processes", return_value=[]),
              patch.object(stall_watch, "prompt_grace_seconds", return_value=120),
              patch.object(unit_lookup, "read_run_states", return_value=unit_lookup.RunStates({}))):
            _ = stall_watch.tick(100)

    def test_stop_requests_prompt_cancellation_before_returning_without_sending(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.assert_prompt_cancellation_requested(log, row)
        worker = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "impl", staff.ReportsTo(staff.StaffId("holder")),
                             current_session=staff.StaffId(self.entry["session_id"]))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((worker,))),
              patch.object(worker_panes, "observe", return_value=[worker_panes.StoppedWorker(worker, "w1:p2", "w1:p2-terminal")]),
              patch.object(stop_resume, "_stop", side_effect=AssertionError("a proved shell needs no native stop")),
              patch.object(stop_resume, "_retire_prompt", wraps=stop_resume._retire_prompt) as retire):  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(stop_resume.stop(worker.id), stop_resume.NotRunning(self.entry["session_id"]))
        retire.assert_called_once_with(self.entry["session_id"])
        self.assert_prompt_cancellation_requested(log, row)

    def test_move_requests_prompt_cancellation_before_resuming_without_sending(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=False)
        def start_after_request(*arguments: str) -> str:
            self.assertIn("exec", self._typed(arguments))
            self.assert_prompt_cancellation_requested(log, row)
            return ""
        self.start.side_effect = start_after_request
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.assert_prompt_cancellation_requested(log, row)

    def test_failed_prompt_retirement_stays_requested_until_the_watcher_confirms_it(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        sender = self.root / "retire.py"
        original = sender.read_text()
        _ = sender.write_text(original + "raise SystemExit(3)\n")
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.assert_prompt_cancellation_requested(log, row)
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.drain_prompt_cancellations()
        self.assertIn("stall-watch: retire:", errors.getvalue())
        path = stall_watch.watched_staff_unit("production", "alpha", row).prompt_path
        self.assertTrue(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")))
        _ = sender.write_text(original)
        self.drain_prompt_cancellations()
        self.assertFalse(path.exists())
        self.assertEqual(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")), [])
        self.assertEqual(json.loads(log.read_text()), ["retire", "--to", "user", "--from", "stall-watch",
                                                      "--key", "stall-watch:prompt:production:alpha"])

    def test_cancellation_key_errors_cannot_prevent_stop_bookkeeping_or_move_resume(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        errors = io.StringIO()
        with (patch.object(stall_watch, "retire_session_prompt", side_effect=KeyError("session_id")),
              redirect_stderr(errors)):
            self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertTrue(pane_record.read("agents")["w1:p2"].get("ended"))
        self.assertIn("019b3a40-4250-7000-8000-000000000102: prompt cancellation:", errors.getvalue())
        pane_record.write("agents", self.entry)
        self.live.return_value = sessions.SessionNotLive()
        self.process.side_effect = None
        self.process.return_value = sessions.LiveSessionProcess(456)
        with (patch.object(stall_watch, "retire_session_prompt", side_effect=KeyError("session_id")),
              redirect_stderr(errors)):
            self.assertIsInstance(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp)
        self.assertEqual(errors.getvalue().count("prompt cancellation:"), 2)

    def test_stop_and_move_never_take_a_held_watcher_lock(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        real_flock = fcntl.flock
        forbidden_attempts: list[tuple[int, int]] = []
        with (stall_watch.STATE_DIR / "lock").open("a+") as lock:
            real_flock(lock, fcntl.LOCK_EX)
            held = os.fstat(lock.fileno())
            watcher_identity = (held.st_dev, held.st_ino)

            def forbid_watcher_lock(file: int | io.IOBase, operation: int) -> None:
                descriptor = file if isinstance(file, int) else file.fileno()
                stat = os.fstat(descriptor)
                identity = (stat.st_dev, stat.st_ino)
                if identity == watcher_identity:
                    forbidden_attempts.append(identity)
                    raise AssertionError("watcher lock must not be acquired")
                real_flock(file, operation)

            with patch.object(fcntl, "flock", side_effect=forbid_watcher_lock):
                self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
                self.assertIsInstance(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp)
        self.assertEqual(forbidden_attempts, [], "stop and move must never attempt the watcher lock")
        self.assert_prompt_cancellation_requested(log, row)

    def test_a_delayed_cancellation_never_retires_a_resumed_sessions_new_prompt(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        path = stall_watch.watched_staff_unit("production", "alpha", row).prompt_path
        stall_watch.save_prompt(path, stall_watch.PromptWait(session_id="019b3a40-4250-7000-8000-000000000102", created=time.time() + 1,
            key="stall-watch:prompt:production:alpha", since=200, alerted=False, tool="new-request", retiring=False))
        self.drain_prompt_cancellations()
        self.assertFalse(log.exists())
        self.assertEqual(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")), [])
        saved = stall_watch.saved_prompt(path)
        assert saved is not None
        self.assertEqual((saved["since"], saved["retiring"]), (200, False))

    def test_a_malformed_cancellation_does_not_stop_valid_requests(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        bad = stall_watch.STATE_DIR / "aaa.prompt.cancel.json"
        _ = bad.write_text("{")
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.drain_prompt_cancellations()
        self.assertIn(str(bad), errors.getvalue())
        self.assertTrue(bad.exists())
        self.assertTrue(log.exists())
        self.assertFalse(stall_watch.watched_staff_unit("production", "alpha", row).prompt_path.exists())

    def test_a_mac_stop_publishes_cancellation_without_any_lookup(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        with (patch("platform.system", return_value="Darwin"),
              patch.object(pane_record, "launch_for_session", side_effect=AssertionError("no launch lookup")) as launch,
              patch.object(staff_sources, "unit_parent", side_effect=AssertionError("no remote lookup")) as parent,
              patch.object(showrunners, "socket_for", side_effect=AssertionError("no socket lookup")) as socket):
            self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        launch.assert_not_called()
        parent.assert_not_called()
        socket.assert_not_called()
        self.assert_prompt_cancellation_requested(log, row)

    def test_launch_deletion_after_stop_cannot_lose_the_old_alerts_cancellation(self) -> None:
        log, row = self.prompt_retirement_fixture(alerted=True)
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        # Launch deletion must preserve pending prompt cancellation.
        pane_record.record_path("agents").unlink()
        self.drain_prompt_cancellations()
        self.assertEqual(json.loads(log.read_text()), ["retire", "--to", "user", "--from", "stall-watch",
                                                      "--key", "stall-watch:prompt:production:alpha"])
        self.assertFalse(stall_watch.watched_staff_unit("production", "alpha", row).prompt_path.exists())
        self.assertEqual(list(stall_watch.STATE_DIR.glob("*.prompt.cancel.json")), [])

    def test_missing_pane_recovery_does_not_duplicate_a_live_session(self) -> None:
        self.client.rows.clear()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.client.native_ids["w1:p2"] = self.entry["session_id"]
        with patch.object(self.client, "open_tab") as opened:
            result = stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="recovery")
        self.assertEqual(result, stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "", "claude3", pid=123))
        opened.assert_not_called()
        self.start.assert_not_called()

    def test_seed_reads_only_the_selected_session_pane(self) -> None:
        self.add_beta()
        _ = stop_resume.stop("alpha")
        self.seed.assert_called_once()
        select = cast(Callable[[herdr.PaneIdentity], bool], self.seed.call_args.kwargs["select"])
        self.assertEqual([pane["pane_id"] for pane in self.client.panes() if select(pane)], ["w1:p2"])

    def test_move_resume_failure_is_pending_and_rerun_finishes_without_restopping(self) -> None:
        self.start.side_effect = herdr_types.HerdrFailure("registry temporarily unreadable")
        with patch.object(stop_resume, "_stop", return_value=stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123)):
            pending = stop_resume.move("alpha", "claude4", why="limit")
        self.assertIsInstance(pending, stop_resume.ResumePending)
        assert isinstance(pending, stop_resume.ResumePending)
        self.assertEqual(accounts.move_result_line(pending),
                         "019b3a40-4250-7000-8000-000000000102: stopped, resume pending on claude4: registry temporarily unreadable")
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
        self.assertEqual(entries[0]["move_id"], entries[1]["move_id"])
        remaining = moves.pending_move_requests()
        self.assertEqual(len(remaining), 1)
        failed, = remaining
        self.assertEqual(failed.request.target, "claude4")
        self.start.side_effect = None
        self.assertEqual(stop_resume.main(["retry"]), 0)
        self.assertEqual(moves.pending_move_requests(), ())
        self.start.reset_mock()
        self.seed.reset_mock()
        before = pane_record.record_path("agents").read_bytes()
        with patch.object(stop_resume, "_stop", side_effect=AssertionError("rerun stopped the destination")), \
                patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000102"}):
            self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                             stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.start.assert_not_called()
        self.seed.assert_not_called()
        self.confirm.assert_called_once()
        self.detach.assert_not_called()
        self.assertEqual(pane_record.record_path("agents").read_bytes(), before)


    def test_shutdown_resume_with_an_explicit_target_finishes_a_pending_move_and_reruns_with_one_line(self) -> None:
        for state in ("pending move", "paused account change"):
            with self.subTest(state=state), ExitStack() as scope:
                case = StopResumeTests("runTest")
                _ = scope.callback(case.doCleanups)
                case.setUp()
                if state == "pending move":
                    case.start.side_effect = herdr_types.HerdrFailure("cannot start yet")
                    self.assertIsInstance(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ResumePending)
                    case.start.side_effect = None
                else:
                    parent = fixture_member("showrunner", name="demo")
                    owner = fixture_member("unit", name="alpha", parent=parent.id,
                        runtime_id=case.entry["session_id"],
                        worktree=staff.PresentWorktree(remote.local_machine(), case.root))
                    _ = staff.set_home(owner.id, staff.SessionHome(case.root / ".claude3"))
                    _ = staff.pause(str(owner.id), "review")

                    def launch(pane: str, command: list[str], *, terminal: str = "") -> None:
                        case.run_in_pane(pane, command, terminal=terminal)
                        arguments = case._typed(case.start.call_args.args)
                        environment = dict(item.split("=", 1) for item in arguments[:arguments.index("exec")] if "=" in item)
                        publication = pane_record.launch(pane, owner.current_session, str(case.root), environment,
                            ["claude", *arguments[arguments.index("exec") + 2:]])
                        pane_record.write("agents", publication)

                    _ = scope.enter_context(patch.object(case.client, "run_in_pane", side_effect=launch))
                for iteration in range(2):
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        status = shutdown.main(["resume", "alpha", "--why", "finish move", "--to", "claude4"])
                    self.assertEqual(status, 0, errors.getvalue() + output.getvalue())
                    self.assertEqual(len(output.getvalue().splitlines()), 1)
                    self.assertIn("Claude is up on claude4" if iteration == 0 else "unchanged; Claude runs on claude4", output.getvalue())
                    self.assertNotIn("unchanged", errors.getvalue())
                    if iteration == 0:
                        self.assertNotIn("unchanged", output.getvalue())
                        if state == "pending move":
                            path = moves.latest_request_path(case.entry["session_id"])
                            self.assertTrue(path.exists())
                            with redirect_stdout(io.StringIO()) as shown:
                                self.assertEqual(stop_resume.main(["result"]), 0)
                            self.assertEqual(shown.getvalue(), "No sessions need a move.\n")
                            self.assertFalse(path.exists())
                    if state == "paused account change":
                        current = staff.by_current_session(case.entry["session_id"])
                        assert current is not None
                        self.assertEqual(current.home, staff.SessionHome(case.root / ".claude4"))
                        self.assertIsInstance(current.paused, staff.Active)

    def test_move_writes_target_trust_before_start(self) -> None:
        target = self.root / ".claude4/.claude.json"
        atomic_file.write_text_atomic(target, json.dumps({"projects": {}, "unrelated": "keep"}))
        def start_after_trust(*arguments: str) -> str:
            self.assertIsInstance(herdr.claude_trust(self.root, target), herdr.Trusted)
            command = self._typed(arguments)
            self.assertEqual(command[command.index("exec") + 1], "claude4")
            return ""
        self.start.side_effect = start_after_trust
        results = stop_resume.move_sessions(["alpha"], "claude4", why="usage limit")
        self.start.assert_called_once()
        result = results[0]
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        assert isinstance(result, stop_resume.ClaudeUp)
        self.assertEqual(result.trust_copied_from, "")
        line = accounts.move_result_line(result)
        self.assertNotIn("copied folder trust", line)
        self.assertEqual(json.loads(target.read_text())["unrelated"], "keep")


    def test_a_new_session_herdr_has_no_id_for_is_found_through_the_launches_read_first(self) -> None:
        self.client.rows[0]["agent_session"] = None
        pane_record.record_path("agents").unlink()
        member = fixture_member("producer", name="alpha", runtime_id=self.entry["session_id"],
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        _ = staff.set_home(member.id, staff.SessionHome(self.root / ".claude3"))
        self.client.process_info = {"shell_pid": 998042, "foreground_process_group_id": 123,
            "foreground_processes": [{"pid": 123, "argv": ["claude"]}]}
        def seed(_session: str, *, select: Callable[[herdr.PaneIdentity], bool]) -> int:
            self.assertEqual({pane["pane_id"] for pane in self.client.panes() if select(pane)}, {"w1:p2"})
            pane_record.write("agents", self.entry)
            self.client.process_info = {"shell_pid": 998042, "foreground_process_group_id": 998042,
                "foreground_processes": [{"pid": 998042, "argv": ["zsh"]}]}
            return 0
        unrelated = pane_launch.launch("w9:p9", fixture_runtime_id("unrelated-seed"), str(self.root), {}, ["claude", "-n", "unrelated"])
        pane_record.save("agents", {"w9:p9": unrelated})
        self.seed.side_effect = seed
        def read(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] == ("workspace", "list"):
                return {"workspaces": [{"workspace_id": "w1", "label": "producers"}]}
            return self.pane_result(*arguments, deadline=deadline)
        with patch.object(self.client, "result", side_effect=read):
            self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="limit"),
                             [stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4")])

    def test_launches_are_read_while_claude_runs_before_any_stop(self) -> None:
        order: list[str] = []
        def seed(_session: str, *, select: Callable[[herdr.PaneIdentity], bool]) -> int:
            self.assertEqual({pane["pane_id"] for pane in self.client.panes() if select(pane)}, {"w1:p2"})
            order.append("seed")
            return 0
        self.seed.side_effect = seed
        def stop_one(session: str) -> stop_resume.NotRunning:
            order.append("stop")
            return stop_resume.NotRunning(session)
        with patch.object(stop_resume, "_stop", side_effect=stop_one):
            _ = stop_resume.move_sessions(["019b3a40-4250-7000-8000-000000000102", "sid-beta"], "claude4", why="limit")
        self.assertEqual(order, ["seed", "stop", "stop"])
        self.launched.clear()
        self.seed.side_effect = herdr_types.HerdrFailure("no reply")
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))

    def test_proof_workspace_refuses_a_stop_outside_its_panes(self) -> None:
        self.signal_guard.stop()
        identity = self.entry["session_id"]
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.client.rows.append({"pane_id": "w2:p1", "workspace_id": "w2", "label": "proof-TEST"})
        def read(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] == ("pane", "process-info"):
                self.assertEqual(arguments[-1], "w2:p1")
                return {"process_info": {"foreground_processes": [{"pid": 456}]}}
            return self.client_result(*arguments, deadline=deadline)
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w2"}),
              patch.object(self.client, "result", side_effect=read)):
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertEqual(result.session_id, identity)
        self.assertIn("proof workspace w2", result.reason)
        self.assertIn("pid 123", result.reason)
        self.kill.assert_not_called()

    def test_proof_workspace_refuses_a_pid_in_no_pane(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.client.process_info = {"foreground_processes": [{"pid": 456}]}
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w1"}),
              patch.object(self.client, "result", side_effect=self.client_result)):
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("proof workspace w1", result.reason)
        self.assertIn("pid 123", result.reason)
        self.kill.assert_not_called()

    def test_proof_workspace_allows_a_pid_in_its_pane(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.client.process_info = {"foreground_processes": [{"pid": 123}]}
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w1"}),
              patch.object(self.client, "result", side_effect=self.client_result)):
            result = stop_resume.stop("alpha")
        self.assertEqual(result, stop_resume.Stopped(self.entry["session_id"], 123))
        self.kill.assert_called_once_with(123, signal.SIGTERM)

    def test_stop_without_a_proof_workspace_reads_no_process_info(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ):
            _ = os.environ.pop("SHUTDOWN_PROOF_WORKSPACE", None)
            _ = os.environ.pop("STAFF_FILE", None)
            _ = os.environ.pop("SHUTDOWN_STATE_DIR", None)
            with patch.object(self.client, "result", side_effect=self.client_result) as read:
                result = stop_resume.stop("alpha")
        self.assertEqual(result, stop_resume.Stopped(self.entry["session_id"], 123))
        self.assertFalse(any(item.args[:2] == ("pane", "process-info") for item in read.call_args_list))
        self.kill.assert_called_once_with(123, signal.SIGTERM)

    def test_proof_workspace_unset_refuses_staff_file_signals(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ):
            _ = os.environ.pop("SHUTDOWN_PROOF_WORKSPACE", None)
            _ = os.environ.pop("SHUTDOWN_STATE_DIR", None)
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("SHUTDOWN_PROOF_WORKSPACE", result.reason)
        self.kill.assert_not_called()

    def test_proof_workspace_unset_refuses_shutdown_state_signals(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ):
            _ = os.environ.pop("SHUTDOWN_PROOF_WORKSPACE", None)
            _ = os.environ.pop("STAFF_FILE", None)
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("SHUTDOWN_PROOF_WORKSPACE", result.reason)
        self.kill.assert_not_called()

    def test_proof_workspace_refuses_unreadable_pane_processes(self) -> None:
        self.signal_guard.stop()
        replies: tuple[dict[str, object] | Exception, ...] = (
            {}, {"process_info": {"foreground_processes": [{"pid": 123.0}]}},
            herdr_types.HerdrFailure("pane unavailable"), OSError("herdr unavailable"))
        for reply in replies:
            with self.subTest(reply=reply):
                self.live.return_value = sessions.LiveSessionProcess(123)
                self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
                def read(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
                    if arguments[:2] == ("pane", "process-info"):
                        if isinstance(reply, Exception):
                            raise reply
                        return reply
                    return self.client_result(*arguments, deadline=deadline)
                with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w1"}),
                      patch.object(self.client, "result", side_effect=read)):
                    result = stop_resume.stop("alpha")
                self.assertIsInstance(result, stop_resume.StopFailed)
                assert isinstance(result, stop_resume.StopFailed)
                self.assertIn("proof workspace w1", result.reason)
                self.kill.assert_not_called()

    def test_proof_workspace_checks_membership_again_before_sigkill(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.client.process_info = {"foreground_processes": [{"pid": 123}]}
        def move_pane(_pid: int, _number: int) -> None:
            self.client.rows[0]["workspace_id"] = "w2"
        self.kill.side_effect = move_pane
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w1"}),
              patch.object(self.client, "result", side_effect=self.client_result)):
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.kill.assert_called_once_with(123, signal.SIGTERM)

    def test_proof_workspace_does_not_guard_signal_zero(self) -> None:
        self.signal_guard.stop()
        with (patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": "w2"}),
              patch.object(herdr, "Herdr", side_effect=AssertionError("unexpected pane read"))):
            stop_resume._signal_process(123, 0)  # pyright: ignore[reportPrivateUsage]
        self.kill.assert_called_once_with(123, 0)

    def test_proof_workspace_refuses_an_empty_workspace(self) -> None:
        self.signal_guard.stop()
        self.live.return_value = sessions.LiveSessionProcess(123)
        with patch.dict(os.environ, {"SHUTDOWN_PROOF_WORKSPACE": ""}):
            result = stop_resume.stop("alpha")
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.kill.assert_not_called()

    def test_stop_sends_term_then_kill_to_the_same_claude_and_only_marks_its_launch_ended(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        def signal_delivered(_pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                self.live.return_value = sessions.SessionNotLive()
                self.process.return_value = sessions.SessionNotLive()
        self.kill.side_effect = signal_delivered
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        self.assertTrue(all(0 < invocation.args[0] <= 0.1
                            for invocation in cast(Mock, cast(Mock, self.wait.return_value).wait).call_args_list))
        launch_path = pane_record.record_path("agents")
        expected = cast(dict[str, dict[str, object]], json.loads(before[launch_path]))
        expected["w1:p2"]["ended"] = True
        self.assertEqual(json.loads(launch_path.read_bytes()), expected)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(set(after), set(before))
        self.assertEqual({path: content for path, content in after.items() if path != launch_path},
                         {path: content for path, content in before.items() if path != launch_path})
        self.start.assert_not_called()

    def test_stop_does_not_kill_an_exited_or_replaced_process(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_stop_never_reports_success_for_a_live_process_in_either_runtime(self) -> None:
        identity = self.entry["session_id"]
        for kind in ("claude", "codex"):
            with (self.subTest(kind=kind),
                  patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation(kind, (123,)))):
                self.kill.reset_mock()
                self.clock.elapsed = 0
                result = stop_resume.stop(identity)
                self.assertIsInstance(result, stop_resume.StopFailed)
                self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])

    def test_stop_refuses_launch_evidence_even_when_a_caller_says_live(self) -> None:
        identity = self.entry["session_id"]
        with (patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", (123,))),
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.LaunchArgument(identity), "generation-1", {}))):
            result = stop_resume.stop(identity)
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("launch argument does not prove", result.reason)
        self.kill.assert_not_called()

    def test_stop_unknown_never_reports_stopped_or_sends_a_signal(self) -> None:
        identity = self.entry["session_id"]
        for kind in ("Claude", "Codex"):
            with (self.subTest(kind=kind), redirect_stderr(io.StringIO()) as output,
                  patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation(
                      f"test-tab ({kind} conversation unknown)"))):
                result = stop_resume.stop(identity)
            self.assertIsInstance(result, stop_resume.StopFailed)
            self.kill.assert_not_called()
            self.assertEqual(output.getvalue(), "")
            assert isinstance(result, stop_resume.StopFailed)
            self.assertIn("test-tab", result.reason)

    def test_codex_release_keeps_an_unknown_native_process_unproved(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        unknown = "pid 998071 (Codex thread unknown)"
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, (unknown,))),
              redirect_stderr(io.StringIO()) as output):
            result = stop_resume.release_source(row, staff.CodexBackend(self.root / "codex"))
        self.assertIsInstance(result, SourceReleaseUnknown)
        assert isinstance(result, SourceReleaseUnknown)
        self.assertIn(unknown, result.reason)
        self.assertEqual(output.getvalue(), "")
        self.kill.assert_not_called()

    def test_source_release_confirms_the_native_process_exit(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        result = stop_resume.release_source(row, staff.ClaudeBackend())
        self.assertIsInstance(result, SourceReleased)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_source_release_keeps_unavailable_backend_evidence_unproved(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        with patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation("source identity unreadable")):
            result = stop_resume.release_source(row, staff.BACKEND_UNAVAILABLE)
        self.assertIsInstance(result, SourceReleaseUnknown)
        assert isinstance(result, SourceReleaseUnknown)
        self.assertIn("source identity unreadable", result.reason)
        self.kill.assert_not_called()

    def test_codex_release_refuses_a_hosted_thread_without_a_signal_target(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: ()}, ()))):
            result = stop_resume.release_source(row, staff.CodexBackend(self.root / "codex"))
        self.assertIsInstance(result, SourceReleaseUnknown)
        assert isinstance(result, SourceReleaseUnknown)
        self.assertIn("no unique signal target", result.reason)
        self.kill.assert_not_called()

    def test_claude_release_ignores_unrelated_unknown_codex_processes(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()) as claude,
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {}, ("pid 998071 (Codex thread unknown)",))) as codex):
            result = stop_resume.release_source(row, staff.ClaudeBackend())
        self.assertIsInstance(result, SourceReleased)
        claude.assert_called_once_with(identity)
        codex.assert_not_called()
        self.kill.assert_not_called()

    def test_codex_release_does_not_read_an_unreadable_claude_registry(self) -> None:
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "worker", "alpha", staff.STAFF_ROOT, current_session=identity)
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", side_effect=OSError("Claude registry unreadable")) as claude,
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ()))):
            result = stop_resume.release_source(row, staff.CodexBackend(self.root / "codex"))
        self.assertIsInstance(result, SourceReleased)
        claude.assert_not_called()
        self.kill.assert_not_called()

    def test_direct_stop_keeps_shared_unknown_evidence_failed_and_preserves_the_prompt(self) -> None:
        identity = self.entry["session_id"]
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        prompt = stall_watch.STATE_DIR / "selected.prompt.json"
        with prompt.open("w") as output:
            json.dump({"session_id": identity, "key": "selected-prompt"}, output)
        before = {path: path.read_bytes() for path in stall_watch.STATE_DIR.iterdir()}
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[]),
              patch.object(runtime_sessions, "session_liveness", return_value=runtime_sessions.SessionNotLive()),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {}, ("pid 998071 (Codex thread unknown)",)))):
            result = stop_resume.stop(identity)
        self.assertIsInstance(result, stop_resume.StopFailed)
        self.assertIsInstance(result, stop_resume.StopUnproved)
        self.assertEqual({path: path.read_bytes() for path in stall_watch.STATE_DIR.iterdir()}, before)
        self.kill.assert_not_called()

    def test_stop_proves_an_idle_restarted_codex_process_with_the_staff_current_session(self) -> None:
        identity = self.entry["session_id"]
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        self.client.rows.insert(0, {"pane_id": "w1:p1", "workspace_id": "w1", "label": "unnamed"})
        row = staff.StaffRow(staff.StaffId("stable-row"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        self.process.side_effect = self.alive_until_kill
        self.client.process_info = {"foreground_processes": [
            {"pid": 123, "argv": ["codex", "resume", identity], "cwd": str(self.root)}]}
        def native_process(pid: int, kind: str,
                           reported: str | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> herdr.NativeAgentProcess:
            del reported
            self.assertEqual((pid, kind), (123, "codex"))
            processes = herdr.json_items(self.client.process_info["foreground_processes"], "fixture foreground processes")
            if herdr.json_object(processes[0], "fixture Codex process")["argv"] != ["codex", "resume", identity]:
                raise ValueError(f"pid {pid}: Codex launch identity is unavailable")
            return herdr.NativeAgentProcess(herdr.PaneSessionReport(identity), "generation-1", {})
        def pane_activity(pane: str) -> herdr.PaneRunning:
            self.assertEqual(pane, "w1:p2")
            return herdr.PaneRunning(("codex",))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "pane_activity", side_effect=pane_activity),
              patch.object(self.client, "result", side_effect=self.client_result),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (123,)}, ())),
              patch.object(herdr, "native_process", side_effect=native_process) as native):
            self.assertEqual(stop_resume.stop("stable-row"), stop_resume.Stopped(identity, 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        self.assertEqual(native.call_args_list, [
            call(123, "codex", identity),
            call(123, "codex"),
            call(123, "codex"),
            call(123, "codex"),
        ])

    def test_stop_finds_an_unnamed_resumed_codex_pane(self) -> None:
        identity = self.entry["session_id"]
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        row = staff.StaffRow(staff.StaffId("stable-row"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        _ = self.client.rows[0].pop("agent_session")
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.client.process_info = {"foreground_processes": [
            {"pid": 123, "argv": ["codex", "resume", identity], "cwd": str(self.root)}]}
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "pane_activity", return_value=herdr.PaneRunning(("codex",))),
              patch.object(self.client, "result", side_effect=self.client_result),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({identity: (123,)}, ()))):
            self.assertEqual(stop_resume.stop("stable-row"), stop_resume.Stopped(identity, 123))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])

    def test_stop_skips_an_unnamed_pane_without_the_selected_codex_conversation(self) -> None:
        identity = self.entry["session_id"]
        self.entry["kind"] = "codex"
        pane_record.write("agents", self.entry)
        row = staff.StaffRow(staff.StaffId("stable-row"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        _ = self.client.rows[0].pop("agent_session")
        observations: list[tuple[dict[str, tuple[int, ...]], list[str]]] = [
            ({"other-thread": (987,)}, []), ({}, []), ({}, ["test-tab (Codex thread unknown)"])]
        for observed in observations:
            with (self.subTest(observed=observed),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                  patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(observed[0], tuple(observed[1])))):
                result = stop_resume.stop("stable-row")
                self.assertIsInstance(result, stop_resume.StopFailed if observed[1] else stop_resume.NotRunning)
                self.kill.assert_not_called()

    def test_registry_removal_does_not_resume_until_the_saved_process_exits(self) -> None:
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123),
                                 sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = self.alive_until_kill
        def start(*_command: str) -> str:
            self.assertGreater(self.process.call_count, 4, "registry deletion is not process exit")
            self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
            return ""
        self.start.side_effect = start
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))

    def test_stop_confirms_exit_after_kill(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = self.alive_until_kill
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("019b3a40-4250-7000-8000-000000000102", 123))
        self.assertGreater(self.process.call_count, 4)
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])

    def test_move_waits_for_exit_when_term_removes_the_registry_before_cleanup_finishes(self) -> None:
        identity = self.entry["session_id"]
        parent = fixture_member("showrunner", name="move-parent", doc=staff.LiveDoc(remote.local_machine(), self.root / "production.md"))
        row = fixture_member("unit", name="alpha", parent=parent.id, runtime_id=identity, worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        _ = staff.set_home(row.id, staff.SessionHome(self.root / ".claude3"))
        for exits, error in ((True, herdr.NativeRegistryUnavailable("pid 123: native registry identity is unavailable")),
                             (True, PermissionError("process read denied during exit")),
                             (False, herdr.NativeRegistryUnavailable("pid 123: native registry identity is unavailable"))):
            with self.subTest(exits=exits, error=error):
                self.clock.elapsed = 0
                self.launched.clear()
                self.kill.reset_mock()
                self.start.reset_mock()
                def registry(_session: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
                    return sessions.SessionNotLive() if self.kill.called else sessions.LiveSessionProcess(123)

                self.live.side_effect = registry

                def process(_pid: int, _started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
                    return sessions.SessionNotLive() if exits and self.clock.elapsed >= 3 else sessions.LiveSessionProcess(123)

                def native(_pid: int, _kind: str) -> herdr.NativeAgentProcess:
                    if self.kill.called:
                        raise error
                    return herdr.NativeAgentProcess(herdr.PaneSessionReport(identity), "generation-1", {})

                def start(*_command: str) -> str:
                    self.assertGreaterEqual(self.clock.elapsed, 3)
                    return ""

                self.process.side_effect = process
                self.start.side_effect = start
                with patch.object(herdr, "native_process", side_effect=native), redirect_stderr(io.StringIO()) as output:
                    result = stop_resume.move("alpha", "claude4", why="limit")
                self.kill.assert_called_once_with(123, signal.SIGTERM)
                if isinstance(error, herdr.NativeRegistryUnavailable):
                    self.assertEqual(output.getvalue(), "")
                else:
                    self.assertIn("stop identity anomaly after SIGTERM:", output.getvalue())
                if exits:
                    self.assertEqual(result, stop_resume.ClaudeUp(identity, "w1:p2", "claude4"))
                    self.start.assert_called_once()
                else:
                    self.assertEqual(result, stop_resume.StopFailed(identity, "pid 123 is still running after SIGTERM"))
                    self.start.assert_not_called()

    def test_a_process_still_alive_after_kill_fails_and_never_resumes(self) -> None:
        self.live.return_value = sessions.LiveSessionProcess(123)
        result = stop_resume.move("alpha", "claude4", why="limit")
        self.assertIsInstance(result, stop_resume.StopFailed)
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("123", result.reason)
        self.assertIn("SIGKILL", result.reason)
        self.start.assert_not_called()

    def test_self_move_hands_off_the_whole_batch_without_signalling(self) -> None:
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        alpha = fixture_member("producer", name="alpha", runtime_id=self.entry["session_id"])
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        self.add_beta()

        def confirm_start(argv: list[str], **_options: object) -> None:
            descriptor = os.dup(int(argv[argv.index("--ready-fd") + 1]))
            self.addCleanup(os.close, descriptor)
            _ = os.write(descriptor, b"1")

        for platform in ("linux", "darwin"):
            with (self.subTest(platform=platform),
                  patch.object(moves, "store_file", return_value=self.root / platform / "moves.jsonl"),
                  patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": self.entry["session_id"]}),
                  patch.object(stop_resume, "_detach", wraps=self.real_detach),
                  patch.object(models_registry, "launch_settings",
                               return_value=models_registry.LaunchSettings("claude", "opus", "high", "")),
                  patch.object(sys, "platform", platform),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "", "")) as run,
                  patch.object(subprocess, "Popen", side_effect=confirm_start) as popen):
                self.client.rows[0]["agent_session"] = None
                result = stop_resume.move_sessions(["beta", "alpha"], "claude4", why="limit", warn_seconds=3, cause="auto")
                self.assertEqual(moves.read_moves(), [])
                self.assertEqual(result, [stop_resume.MoveHandedOff("sid-beta"), stop_resume.MoveHandedOff("019b3a40-4250-7000-8000-000000000102")])
                invocation = run.call_args if platform == "linux" else popen.call_args
                self.assertIsNotNone(invocation)
                assert invocation is not None
                argv = cast(list[str], invocation.args[0])
                if platform == "linux":
                    self.assertEqual(argv[:5], ["systemd-run", "--user", "--collect", "--quiet", "--service-type=notify"])
                    self.assertIn("--property=UnsetEnvironment=CLAUDE_CODE_SESSION_ID", argv)
                    self.assertLess(argv.index("--notify-systemd"), argv.index("--"))
                else:
                    self.assertTrue(cast(bool, invocation.kwargs["start_new_session"]))
                    self.assertLess(argv.index("--ready-fd"), argv.index("--"))
                self.assertNotIn("CLAUDE_CODE_SESSION_ID", cast(dict[str, str], invocation.kwargs["env"]))
                self.assertEqual(cast(object, invocation.kwargs["stdin"]), subprocess.DEVNULL)
                self.assertIn("batch", argv)
                self.assertEqual(argv[argv.index("--cause") + 1], "auto")
                self.assertEqual(argv[argv.index("--target") + 1], "claude4")
                self.assertEqual(argv[argv.index("--") + 1:], ["sid-beta", alpha.id])
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_self_move_uses_the_current_session_of_a_saved_staff_reference(self) -> None:
        current = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(current))
        saved = self.entry.copy()
        saved["session_id"] = row.id
        pane_record.save("agents", {saved["pane_id"]: saved})
        self.client.rows[0]["agent_session"] = None
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": current}),
              patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,)))):
            result = stop_resume.move_sessions(["alpha"], "claude4", why="limit")
        self.assertEqual(result, [stop_resume.MoveHandedOff(current)])
        self.detach.assert_called_once()
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_detached_self_move_respects_a_newer_move_of_the_same_session(self) -> None:
        identity = self.entry["session_id"]
        initial = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for newer_runs_first, clock_reversed in ((True, False), (False, False), (True, True)):
            with self.subTest(newer_runs_first=newer_runs_first, clock_reversed=clock_reversed):
                self.detach.reset_mock()
                moves.store_file().unlink(missing_ok=True)
                log = self.root / f"ordered-move-{newer_runs_first}-{clock_reversed}.log"
                account_label = "claude2"
                now = Mock(return_value=initial)
                seed: moves.RequestedMove = {"kind": "requested", "tool": "claude", "at": initial.isoformat(),
                    "subject": "unrelated", "move_id": "earlier", "why": "earlier", "window": "unknown",
                    "from": "claude2", "to": "claude3", "scope": "session"}
                if clock_reversed:
                    moves.append_move(seed)

                def resumed(session: str, target: str, *_args: object) -> stop_resume.ClaudeUp:
                    nonlocal account_label
                    account_label = target
                    return stop_resume.ClaudeUp(session, "w1:p2", target)

                with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": identity}),
                      patch.object(stop_resume, "datetime", wraps=datetime, now=now),
                      patch.object(stop_resume, "_tab_of", return_value=herdr.TabSession("w1:p2", "alpha", "w1", identity)),
                      patch.object(stop_resume, "_record_launches"),
                      patch.object(stop_resume, "_load_batch_modules"),
                      patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
                      patch.object(stop_resume, "_resume_move", side_effect=resumed),
                      patch.object(stop_resume, "_send_move_push") as send):
                    self.assertEqual(stop_resume.move(identity, "claude4", why="first"), stop_resume.MoveHandedOff(identity))
                    self.detach.assert_called_once()
                    argv = cast(list[str], self.detach.call_args.args[0])
                    _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
                    stopped.assert_not_called()
                    if newer_runs_first:
                        now.return_value = initial + timedelta(seconds=-1 if clock_reversed else 1)
                        self.assertIsInstance(stop_resume.move(identity, "claude3", why="second"), stop_resume.ClaudeUp)
                        if clock_reversed:
                            moves.append_move(moves.ConfirmedMove(kind="confirmed", tool="claude", at=initial.isoformat(),
                                subject=seed["subject"], move_id=seed["move_id"]))
                        now.return_value = initial + timedelta(seconds=2)
                        self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
                        stopped.assert_called_once_with(identity)
                        self.assertEqual(log.read_text().count("not moved: a newer move ran"), 1)
                    else:
                        self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 0)
                        self.assertEqual(account_label, "claude4")
                        now.return_value = initial + timedelta(seconds=1)
                        self.assertIsInstance(stop_resume.move(identity, "claude3", why="second"), stop_resume.ClaudeUp)
                        self.assertEqual(stopped.call_args_list, [call(identity), call(identity)])
                    send.assert_not_called()
                self.assertEqual(account_label, "claude3")
                self.assertFalse(moves.latest_request_path(identity).exists())
                self.assertEqual([entry["to"] for entry in moves.read_moves()
                                  if entry["kind"] == "requested" and entry["move_id"] != seed["move_id"]],
                                 ["claude3"] if newer_runs_first else ["claude4", "claude3"])
                with self.assertRaises(FileNotFoundError):
                    _ = moves.read_batch_result(argv[argv.index("--request-id") + 1])

    def test_same_account_move_supersedes_an_older_handoff(self) -> None:
        from scripts.agents import inventory

        identity = self.entry["session_id"]
        account_label = "claude3"
        log = self.root / "older-move.log"

        def listing() -> list[inventory.ListedSession]:
            return [inventory.ListedSession(identity, 123, "generation-1", "alpha", "top-level",
                                            self.root / f".{account_label}", "", "", "")]

        def resumed(session: str, target: str, *_args: object) -> stop_resume.ClaudeUp:
            nonlocal account_label
            account_label = target
            return stop_resume.ClaudeUp(session, "w1:p2", target)

        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": identity}),
              patch.object(inventory, "listing", side_effect=listing),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              patch.object(stop_resume, "_resume_move", side_effect=resumed)):
            self.assertEqual(stop_resume.move(identity, "claude4", why="first"), stop_resume.MoveHandedOff(identity))
            argv = cast(list[str], self.detach.call_args.args[0])
            latest = moves.read_latest_request(identity)
            self.assertIsInstance(latest, moves.LatestMoveRequest)
            _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

            holder = ExitStack()
            _ = holder.enter_context(staff.slot_operation(moves.native_row_key(identity), "alpha", join=False))

            def operation_ends(_seconds: float) -> None:
                # The move by hand claims the busy row before it waits, so the older handoff cannot win.
                claim = moves.read_latest_request(identity)
                assert isinstance(claim, moves.LatestMoveRequest)
                self.assertNotEqual(claim, latest)
                self.assertEqual(claim.target, "claude3")
                holder.close()

            # alpha is on claude3, so the busy move skips the quota check, the same as on a free row.
            # Only the batch checks the target, before it resumes the stopped session.
            with (patch("scripts.agents.stop_resume.time.sleep", side_effect=operation_ends) as waited,
                  patch.object(accounts, "check_move_target", return_value=accounts.MoveTargetReady(
                      accounts.NamedAccount("claude3", self.root / ".claude3"))) as checked):
                moved = accounts.move("alpha", "claude3")
            waited.assert_called_once()
            checked.assert_called_once()
            # The fixture has no live process for alpha, so the move by hand resumes it on claude3.
            self.assertEqual(moved, accounts.MoveStarted(
                identity, "claude3", f"{identity}: Claude is up on claude3 in w1:p2 (0.0 s)\n", "", 0))

            self.assertEqual(accounts.move("alpha", "claude3"),
                             accounts.MoveStarted(identity, "claude3", "alpha: already on claude3\n", "", 0))
            self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
            stopped.assert_called_once_with(identity)
        self.assertEqual(account_label, "claude3")
        self.assertFalse(moves.latest_request_path(identity).exists())
        self.assertEqual(log.read_text().count("not moved: a newer move ran"), 1)


    def test_group_move_clears_an_older_handoff_for_a_member_on_target(self) -> None:
        from scripts.agents import inventory

        identity = self.entry["session_id"]
        row = fixture_member("showrunner", name="alpha", runtime_id=identity)
        member = accounts.LiveGroupMember(identity, row.name, "showrunners", self.root / ".claude3")
        group = accounts.GroupMoveMembers((accounts.LiveStaffMoveMember(row, member),), (), False, frozenset({identity}))
        live = stop_resume.ClaudeUp(identity, "w1:p2", "claude3")
        log = self.root / "older-move.log"
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": identity}),
              patch.object(inventory, "listing", return_value=[]),
              patch.object(inventory, "showrunner_registrations", return_value=inventory.ShowrunnerRegistrations(())),
              patch.object(accounts, "group_move_members", return_value=group),
              patch.object(accounts, "claude_account_dir", return_value=self.root / ".claude3"),
              patch.object(accounts, "set_group_account"),
              patch.object(accounts, "group_transition", return_value=accounts.NoGroupTransition()),
              patch.object(accounts, "current_move_account", return_value="claude3"),
              patch.object(stop_resume, "running_on_account", return_value=stop_resume.LiveMoveSource(live, 123, "test")),
              patch.object(member_account, "repair_home"),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              patch.object(stop_resume, "_resume_move", return_value=live) as resumed):
            self.assertEqual(stop_resume.move(row.id, "claude4", why="first"), stop_resume.MoveHandedOff(identity))
            argv = cast(list[str], self.detach.call_args.args[0])
            _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            self.assertIsInstance(accounts.move_group("showrunners", "claude3"), accounts.GroupMoveStarted)
            self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
            stopped.assert_not_called()
            resumed.assert_not_called()
        self.assertFalse(moves.latest_request_path(identity).exists())
        self.assertEqual(log.read_text().count("not moved: a newer move ran"), 1)

    def test_busy_member_gets_a_command_when_a_peer_handoff_is_superseded(self) -> None:
        identity = self.entry["session_id"]
        busy = fixture_member("producer", name="busy")
        log = self.root / "older-move.log"
        moved = stop_resume.ClaudeUp(identity, "w1:p2", "claude3")
        with (patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              patch.object(stop_resume, "_resume_move", return_value=moved) as resumed):
            with (staff.slot_operation(moves.native_row_key(busy.current_session), busy.name, join=False),
                  stop_resume.detached_moves()):
                a, other = stop_resume.move_sessions([busy.id, identity], "claude4", why="first")
            assert isinstance(a, moves.MoveWaitingForRow)
            self.assertIn("Run again:", accounts.move_result_line(a))
            self.assertEqual(a.command, moves.batch_move_command(busy.current_session, "claude4"))
            self.assertEqual(other, stop_resume.MoveHandedOff(identity))
            argv = cast(list[str], self.detach.call_args.args[0])
            self.assertEqual(stop_resume.move(identity, "claude3", why="second"), moved)
            self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
            stopped.assert_called_once_with(identity)
            self.assertEqual(resumed.call_args.args[:2], (identity, "claude3"))
        self.assertEqual(moves.pending_move_requests(), ())
        self.assertEqual(log.read_text().count("not moved: a newer move ran"), 1)

    def test_older_child_pane_read_failure_cannot_restore_a_completed_request(self) -> None:
        identity = self.entry["session_id"]
        log = self.root / "older-move.log"
        moved = stop_resume.ClaudeUp(identity, "w1:p2", "claude3")
        with (patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_send_move_push") as send,
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              patch.object(stop_resume, "_resume_move", return_value=moved)):
            with stop_resume.detached_moves():
                self.assertEqual(stop_resume.move(identity, "claude4", why="first"), stop_resume.MoveHandedOff(identity))
            argv = cast(list[str], self.detach.call_args.args[0])
            self.assertEqual(stop_resume.move(identity, "claude3", why="second"), moved)
            with patch.object(pane_record, "read_strict", side_effect=OSError("pane read failed")):
                self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
            stalled = argv[argv.index("--request-id") + 1]
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(stop_resume.main(["retry", stalled]), 0)
            self.assertEqual(output.getvalue().strip(), "No sessions need a move.")
            send.assert_not_called()
            stopped.assert_called_once_with(identity)
        self.assertEqual(moves.pending_move_requests(), ())

    def test_new_staff_membership_does_not_split_the_request_key(self) -> None:
        identity = self.entry["session_id"]
        log = self.root / "older-move.log"
        write = moves.write_latest_request

        def crash_after_write(native: str, request: moves.LatestMoveRequest, *, name: str = "") -> None:
            write(native, request, name=name)
            raise KeyboardInterrupt

        with (patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              patch.object(stop_resume, "_resume_move", return_value=stop_resume.ClaudeUp(identity, "pane", "claude4"))):
            with stop_resume.detached_moves():
                self.assertEqual(stop_resume.move(identity, "claude4", why="first"), stop_resume.MoveHandedOff(identity))
            argv = cast(list[str], self.detach.call_args.args[0])
            _ = fixture_member("showrunner", name="alpha")
            row = fixture_member("showrunner", name="alpha", runtime_id=identity)
            self.assertNotEqual(row.id, identity)
            self.assertEqual(row.current_session, identity)
            with patch.object(moves, "write_latest_request", side_effect=crash_after_write), self.assertRaises(KeyboardInterrupt):
                _ = stop_resume.move(row.id, "claude3", why="second")
            self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
            stopped.assert_not_called()
        self.assertIn("not moved: a newer move ran", log.read_text())
        request, = moves.pending_move_requests()
        self.assertEqual(request.member.reference, identity)
        self.assertEqual(request.request.target, "claude3")
        self.assertEqual(list(moves.latest_request_path(identity).parent.iterdir()), [moves.latest_request_path(identity)])

    def test_superseded_child_reports_no_command_while_a_newer_move_holds_the_lock(self) -> None:
        identity = self.entry["session_id"]
        log = self.root / "older-move.log"
        with (patch.object(stop_resume, "_load_batch_modules"), patch.object(stop_resume, "_stop") as stopped):
            with stop_resume.detached_moves():
                self.assertEqual(stop_resume.move(identity, "claude4", why="first"), stop_resume.MoveHandedOff(identity))
            argv = cast(list[str], self.detach.call_args.args[0])
            with staff.session_operation(identity, native_key=moves.native_row_key(identity)):
                moves.write_latest_request(identity, moves.LatestMoveRequest("b" * 32, "claude3"), name="alpha")
                self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
            stopped.assert_not_called()
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertIn("not moved: a newer move ran", log.read_text())
        self.assertNotIn("Run again", log.read_text())
        self.assertNotIn("batch --target", log.read_text())

    def test_retry_keeps_the_request_when_the_second_pane_read_fails(self) -> None:
        identity = self.entry["session_id"]
        path = moves.latest_request_path(identity)
        moves.write_latest_request(identity, moves.LatestMoveRequest("a" * 32, "claude4"), name="alpha")
        before = path.read_bytes()
        read = pane_record.read_strict
        panes = pane_record.record_path("agents")

        def read_damaged(owner: str) -> dict[str, pane_record.PaneLaunch]:
            _ = panes.write_text("{")
            return read(owner)

        with (patch.object(pane_record, "read_strict", side_effect=read_damaged) as second_read,
              patch.object(stop_resume, "_stop") as stopped, redirect_stderr(io.StringIO()) as error):
            self.assertEqual(stop_resume.main(["retry"]), 1)
        second_read.assert_called_once_with("agents")
        stopped.assert_not_called()
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        self.assertIn("could not check the pending move", error.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_retry_keeps_the_request_when_the_fresh_registry_process_start_is_unreadable(self) -> None:
        identity = fixture_runtime_id("appears-after-source-read")
        path = moves.latest_request_path(identity)
        moves.write_latest_request(identity, moves.LatestMoveRequest("a" * 32, "claude4"), name="late-session")
        before = path.read_bytes()
        pid = 994813

        def read_source(*_args: object, **_options: object) -> sessions.SessionNotLive:
            self.assertEqual(session_records.session_liveness(identity), sessions.SessionNotLive())
            _ = (sessions.sessions_dir() / f"{pid}.json").write_text(json.dumps({
                "sessionId": identity, "pid": pid, "name": "late-session", "messagingSocketPath": "",
                "procStart": "fixture-start", "updatedAt": 1,
            }))
            return sessions.SessionNotLive()

        def read_process(_pid: int, signum: int) -> None:
            self.assertEqual(signum, 0)

        with (patch.object(stop_resume, "running_on_account", side_effect=read_source),
              patch.object(session_records, "process_start", return_value=None),
              patch.object(os, "kill", side_effect=read_process),
              patch.object(stop_resume, "_stop") as stopped, redirect_stderr(io.StringIO()) as errors):
            result = stop_resume.main(["retry"])

        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result, 1)
        stopped.assert_not_called()
        self.assertEqual(errors.getvalue().splitlines(), [
            f"late-session: could not check the pending move: process identity unreadable for pid {pid}.",
        ])

    def test_legacy_result_and_retry_include_only_members_off_target(self) -> None:
        identity = self.entry["session_id"]
        self.add_beta()
        beta = pane_launch.reference_id(pane_record.read_strict("agents")["w1:p4"])
        legacy = moves.store_file().parent / "move-results" / f"{'a' * 32}.json"
        legacy.parent.mkdir(parents=True)
        report = json.dumps({"target": "claude4", "pending": [
            {"reference": identity, "name": "alpha"}, {"reference": beta, "name": "beta"}]})
        _ = legacy.write_text(report)
        self.launched.add(beta)
        advice = f"alpha: start a new move with {moves.batch_move_command(identity, 'claude4')}\n"
        with redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(stop_resume.main(["result"]), 0)
        self.assertEqual(shown.getvalue(), advice)
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              redirect_stdout(io.StringIO()) as retry):
            self.assertEqual(stop_resume.main(["retry"]), 0)
        self.assertEqual(retry.getvalue(), advice)
        stopped.assert_not_called()
        with redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(stop_resume.main(["result"]), 0)
        self.assertEqual(shown.getvalue(), advice)
        self.assertEqual(legacy.read_text(), report)
        self.assertEqual(moves.pending_move_requests(), ())

    def test_legacy_group_result_and_retry_preserve_the_group(self) -> None:
        identity = self.entry["session_id"]
        batch_id = "a" * 32
        legacy = moves.store_file().parent / "move-results" / f"{batch_id}.json"
        legacy.parent.mkdir(parents=True)
        report = json.dumps({"group": "showrunners", "target": "claude3",
                             "pending": [{"reference": identity, "name": "alpha"}]})
        _ = legacy.write_text(report)
        advice = f"alpha: start a new move with {moves.MOVE_COMMAND} batch --group showrunners -- {identity}\n"

        with (patch.object(accounts, "current_move_account", return_value="claude4"),
              patch.object(stop_resume, "_stop") as stopped):
            for command in ("result", "retry"):
                with self.subTest(command=command), redirect_stdout(io.StringIO()) as shown:
                    self.assertEqual(stop_resume.main([command, batch_id]), 0)
                    self.assertEqual(shown.getvalue(), advice)
        stopped.assert_not_called()
        self.assertEqual(legacy.read_text(), report)

    def test_retry_names_an_invalid_file_and_moves_the_valid_member(self) -> None:
        identity = self.entry["session_id"]
        moves.write_latest_request(identity, moves.LatestMoveRequest("a" * 32, "claude4"), name="alpha")
        invalid = moves.latest_request_path("invalid")
        _ = invalid.write_text("{")
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(stop_resume.main(["retry"]), 0)
        self.assertEqual([line for line in errors.getvalue().splitlines() if "Could not read move request" in line],
                         [f"Could not read move request {invalid.name}."])
        stopped.assert_called_once_with(identity)
        self.assertFalse(moves.latest_request_path(identity).exists())
        self.assertEqual(invalid.read_text(), "{")

    def test_retry_names_a_deeply_nested_file_and_moves_the_valid_member(self) -> None:
        identity = self.entry["session_id"]
        moves.write_latest_request(identity, moves.LatestMoveRequest("a" * 32, "claude4"), name="alpha")
        invalid = moves.latest_request_path(identity).with_name("native-" + "0" * 64)
        nested = "[" * 10_000 + "]" * 10_000
        _ = invalid.write_text(nested)
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(stop_resume.main(["retry"]), 0)
        self.assertEqual([line for line in errors.getvalue().splitlines() if "Could not read move request" in line],
                         [f"Could not read move request {invalid.name}."])
        stopped.assert_called_once_with(identity)
        self.assertFalse(moves.latest_request_path(identity).exists())
        self.assertEqual(invalid.read_text(), nested)

    def test_two_overlapping_retries_keep_one_request_when_the_first_fails(self) -> None:
        identity = self.entry["session_id"]
        moves.write_latest_request(identity, moves.LatestMoveRequest("a" * 32, "claude4"), name="alpha")
        retry_output = io.StringIO()
        overlap: list[int] = []

        def resumed(session: str, target: str, *_args: object) -> stop_resume.ResumeFailed | stop_resume.ClaudeUp:
            if not overlap:
                with redirect_stdout(retry_output):
                    overlap.append(stop_resume.main(["retry"]))
                return stop_resume.ResumeFailed(session, "fixture start failed")
            return stop_resume.ClaudeUp(session, "w1:p2", target)

        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              patch.object(stop_resume, "_resume_move", side_effect=resumed),
              redirect_stderr(io.StringIO())):
            self.assertEqual(stop_resume.main(["retry"]), 1)
            self.assertEqual(overlap, [1])
            self.assertIn("Run again:", retry_output.getvalue())
            saved, = moves.pending_move_requests()
            self.assertEqual(saved.member.reference, identity)
            self.assertNotEqual(saved.request.request_id, "a" * 32)
            self.assertEqual(list((moves.store_file().parent / "move-results").glob("*.json")), [])
            stopped.assert_called_once_with(identity)
            self.assertEqual(stop_resume.main(["retry"]), 0)
        self.assertEqual(moves.pending_move_requests(), ())

    def test_staff_read_before_lock_leaves_the_request_for_retry(self) -> None:
        row = fixture_member("showrunner", name="fixture-runner")
        record = staff.initial_snapshot()
        log = self.root / "move.log"
        result = stop_resume.ClaudeUp(row.current_session, "w1:p2", "claude4")
        with (patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(row.current_session)) as stopped,
              patch.object(stop_resume, "_resume_move", return_value=result)):
            with stop_resume.detached_moves():
                self.assertIsInstance(stop_resume.move(row.id, "claude4", why="move"), stop_resume.MoveHandedOff)
            argv = cast(list[str], self.detach.call_args.args[0])
            saved = moves.read_latest_request(row.current_session)
            with patch.object(staff, "initial_snapshot", side_effect=[record, staff.StaffUnreadable("failed")]):
                self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
            stopped.assert_not_called()
            self.assertEqual(moves.read_latest_request(row.current_session), saved)
            self.assertEqual(stop_resume.main(["retry"]), 0)
            stopped.assert_called_once_with(row.id)
        self.assertFalse(moves.latest_request_path(row.current_session).exists())

    def test_retry_removes_a_request_for_a_missing_session(self) -> None:
        missing = "missing-record"
        moves.write_latest_request(missing, moves.LatestMoveRequest("a" * 32, "claude4"))
        with (patch.object(stop_resume, "stop_target", return_value=stop_resume.SessionNotFound(missing)),
              patch.object(stop_resume, "_stop") as stopped, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(stop_resume.main(["retry"]), 0)
        self.assertIn("session that no longer exists", output.getvalue())
        stopped.assert_not_called()
        self.assertEqual(moves.pending_move_requests(), ())

    def test_latest_request_wins_in_either_child_execution_order(self) -> None:
        identity = self.entry["session_id"]
        for newer_runs_first in (False, True):
            with self.subTest(newer_runs_first=newer_runs_first):
                self.detach.reset_mock()
                moves.store_file().unlink(missing_ok=True)
                account = "claude2"
                log = self.root / f"handoff-order-{newer_runs_first}.log"

                def resumed(session: str, target: str, *_args: object) -> stop_resume.ClaudeUp:
                    nonlocal account
                    account = target
                    return stop_resume.ClaudeUp(session, "w1:p2", target)

                with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": identity}),
                      patch.object(stop_resume, "_load_batch_modules"),
                      patch.object(stop_resume, "_record_launches"),
                      patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
                      patch.object(stop_resume, "_resume_move", side_effect=resumed),
                      patch.object(stop_resume, "_send_move_push") as send):
                    self.assertEqual(stop_resume.move(identity, "claude4", why="first"), stop_resume.MoveHandedOff(identity))
                    self.assertEqual(stop_resume.move(identity, "claude3", why="second"), stop_resume.MoveHandedOff(identity))
                    older, newer = (cast(list[str], child.args[0]) for child in self.detach.call_args_list)
                    latest = moves.LatestMoveRequest(newer[newer.index("--request-id") + 1], "claude3")
                    self.assertEqual(moves.read_latest_request(identity), latest)
                    self.assertFalse(moves.store_file().exists())
                    _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
                    stopped.assert_not_called()
                    executions = ((newer, 0), (older, 1)) if newer_runs_first else ((older, 1), (newer, 0))
                    for argv, expected in executions:
                        self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), expected)
                        if argv is older and not newer_runs_first:
                            self.assertEqual(moves.read_latest_request(identity), latest)
                    stopped.assert_called_once_with(identity)
                    send.assert_not_called()
                self.assertEqual(account, "claude3")
                self.assertEqual(log.read_text().count("not moved: a newer move ran"), 1)
                self.assertFalse(moves.latest_request_path(identity).exists())
                self.assertEqual([row["kind"] for row in moves.read_journal()], ["requested", "confirmed"])
                self.assertEqual(list((moves.store_file().parent / "move-results").glob("*.json")), [])

    def test_move_handoff_does_not_read_or_change_a_corrupt_journal(self) -> None:
        self.add_beta()
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        before = (b'{"kind":"requested","at":"2026-01-01T00:00:00Z","tool":"claude",'
                  + b'"subject":"older","move_id":"older","from":"claude2","to":"claude3","why":"\xff","window":"unknown"}\n')
        _ = path.write_bytes(before)
        with (stop_resume.detached_moves(), patch.object(stop_resume, "_stop") as stopped,
              patch.object(stop_resume, "_send_move_push") as send):
            results = stop_resume.move_sessions([self.entry["session_id"], "sid-beta"], "claude4", why="move")
        self.detach.assert_called_once()
        stopped.assert_not_called()
        self.assertEqual(list((path.parent / "move-results").glob("*.json")), [])
        send.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(results, [stop_resume.MoveHandedOff(identity) for identity in (self.entry["session_id"], "sid-beta")])

    def test_retry_of_a_legacy_result_cannot_reverse_a_newer_move(self) -> None:
        identity = self.entry["session_id"]
        state = shutdown_state_root()
        state.mkdir()
        child_log = state / "first.log"
        request_ids: list[str] = []

        def resumed(session: str, target: str, *_args: object) -> stop_resume.ClaudeUp | stop_resume.ResumeFailed:
            latest = moves.read_latest_request(session)
            assert isinstance(latest, moves.LatestMoveRequest)
            self.assertEqual(latest.target, target)
            request_ids.append(latest.request_id)
            if resume.call_count == 1:
                return stop_resume.ResumeFailed(session, "fixture start failed")
            return stop_resume.ClaudeUp(session, "w1:p2", target)

        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": identity}),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_send_move_push") as send,
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
              patch.object(stop_resume, "_resume_move", side_effect=resumed) as resume):
            self.assertEqual(stop_resume.move(identity, "claude4", why="first"), stop_resume.MoveHandedOff(identity))
            argv = cast(list[str], self.detach.call_args.args[0])
            batch_id = argv[argv.index("--request-id") + 1]
            _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

            self.assertEqual(stop_resume.main(["batch", "--log", str(child_log), *argv[1:]]), 1)
            send.assert_called_once()
            self.assertIn("stop_resume.py retry", cast(RenderedPush, send.call_args.args[0]).values["action"])
            self.assertTrue(moves.latest_request_path(identity).exists())
            legacy = moves.store_file().parent / "move-results" / f"{batch_id}.json"
            legacy.parent.mkdir()
            before = json.dumps({"target": "claude4", "order_mark": None,
                                 "pending": [{"reference": identity, "name": "alpha"}]})
            _ = legacy.write_text(before)
            with redirect_stdout(io.StringIO()) as shown:
                self.assertEqual(stop_resume.main(["result", batch_id]), 0)
            self.assertIn("alpha", shown.getvalue())
            self.assertEqual(stop_resume.move(identity, "claude3", why="second"),
                             stop_resume.ClaudeUp(identity, "w1:p2", "claude3"))
            with redirect_stdout(io.StringIO()) as retry:
                self.assertEqual(stop_resume.main(["retry", batch_id]), 0)
            self.assertEqual([received.args[1] for received in resume.call_args_list], ["claude4", "claude3"])
            self.assertEqual(retry.getvalue().splitlines(), [
                f"alpha: start a new move with {moves.batch_move_command(identity, 'claude4')}",
            ])
            self.assertEqual(request_ids[0], argv[argv.index("--request-id") + 1])
            self.assertEqual(len(set(request_ids)), 2)
            self.assertEqual(legacy.read_text(), before)
            self.assertFalse(moves.latest_request_path(identity).exists())

    def test_fresh_group_command_moves_a_member_after_an_individual_move(self) -> None:
        identity = self.entry["session_id"]
        intent = moves.FollowGroupAccount("showrunners")
        log = self.root / "group-move.log"

        def resumed(session: str, target: str, *_args: object) -> stop_resume.ClaudeUp:
            return stop_resume.ClaudeUp(session, "w1:p2", target)

        with (patch.object(accounts, "current_move_account", return_value="claude4"),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
              patch.object(stop_resume, "_resume_move", side_effect=resumed) as resume):
            child_requests: list[str] = []
            for prior_move in (False, True):
                if prior_move:
                    self.assertIsInstance(stop_resume.move(identity, "claude3", why="individual"), stop_resume.ClaudeUp)
                with stop_resume.detached_moves():
                    self.assertEqual(stop_resume.move_sessions([identity], "claude4", why="group", intent=intent), [stop_resume.MoveHandedOff(identity)])
                argv = cast(list[str], self.detach.call_args.args[0])
                child_requests.append(argv[argv.index("--request-id") + 1])
                self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 0)
                self.assertFalse(moves.latest_request_path(identity).exists())
            self.assertNotEqual(child_requests[0], child_requests[1])
            self.assertEqual([received.args[1] for received in resume.call_args_list], ["claude4", "claude3", "claude4"])

    def test_attachment_and_native_move_refuse_when_the_other_holds_the_session(self) -> None:
        from scripts.production import add_unit
        from scripts.production.production_doc import Production, Refusal

        identity = self.entry["session_id"]
        owner = staff.StaffRow(staff.StaffId("runner"), "showrunner", "fixture", staff.StaffRoot(),
                               current_session=staff.StaffId("runner-session"))
        production = Production(self.root / "production.md", "fixture", "main", self.root,
                                self.root / "unit-log.md", ZoneInfo("UTC"), owner)
        request = add_unit.RequestedUnitLaunch(production=production, branch="fixture-unit", worktree=self.root,
            plan=add_unit.Standby(), port=add_unit.OmittedCell(), timeout=1.0, requested_name="unit", unit="unit",
            session=add_unit.ResumedSession(identity, self.root, add_unit.AttachRunningSession()))
        arguments = ["--production", "fixture", "unit", "--standby", "--attach", identity, "--cwd", str(self.root)]
        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        for move_first in (True, False):
            with self.subTest(move_first=move_first):
                self.kill.reset_mock()
                moves.store_file().unlink(missing_ok=True)
                competed: list[stop_resume.MoveResult] = []
                attachment_errors = io.StringIO()

                def attach_account(*_args: object, **_options: object) -> Never:
                    if not move_first:
                        competed.append(stop_resume.move(identity, "claude4", why="move"))
                    raise Refusal("fixture reached attachment account selection")

                def prove_signal_target(pid: int, kind: str, process: herdr.NativeAgentProcess) -> bool:
                    self.assertEqual((pid, kind, process.current_identity), (123, "claude", identity))
                    self.assertEqual(add_unit.main(arguments), 2)
                    return True

                self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
                with (redirect_stderr(attachment_errors),
                      patch.object(add_unit, "launch_request", return_value=request),
                      patch.object(add_unit, "account_for_launch", side_effect=attach_account) as select_account,
                      patch.object(stop_resume, "_record_launches"),
                      patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
                      patch.object(stop_resume, "stop_target", return_value=identity),
                      patch.object(stop_resume, "running_on_account", return_value=stop_resume.LiveMoveSource(
                          replace(up, account="claude3", pid=123), 123, "generation-1")),
                      patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (123,))),
                      patch.object(herdr, "check_native_target", side_effect=prove_signal_target if move_first else None,
                                   return_value=True),
                      patch.object(stop_resume, "_resume_move", return_value=up) as resumed):
                    if move_first:
                        self.assertEqual(stop_resume.move(identity, "claude4", why="move"), up)
                        self.assertIn("another operation holds this row", attachment_errors.getvalue())
                        select_account.assert_not_called()
                        self.kill.assert_called_once_with(123, signal.SIGTERM)
                        resumed.assert_called_once()
                    else:
                        self.assertEqual(add_unit.main(arguments), 2)
                        self.assertEqual(len(competed), 1)
                        self.assertIsInstance(competed[0], moves.MoveWaitingForRow)
                        self.kill.assert_not_called()
                        resumed.assert_not_called()
                        self.assertEqual(moves.read_moves(), [])

    def test_detached_batch_uses_the_same_moves_and_logs_each_member(self) -> None:
        log = self.root / "batch.log"

        def liveness(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            if identity == self.entry["session_id"] and not self.kill.called:
                return sessions.LiveSessionProcess(123)
            return sessions.SessionNotLive()

        self.live.side_effect = liveness
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000102"}), patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen:
            result = stop_resume.main(["batch", "--target", "claude4", "--why", "limit", "--cause", "auto", "--log", str(log), "alpha", "missing"])
        self.assertEqual(result, 1)
        self.assertEqual(len(log.read_text().splitlines()), 2)
        self.assertIn('"result": "ClaudeUp"', log.read_text())
        self.assertIn('"result": "SessionNotFound"', log.read_text())
        self.assertEqual([entry.get("cause") for entry in moves.read_moves() if entry["kind"] == "requested"], ["auto", "auto"])
        self.start.assert_called_once()
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        self.seed.assert_called_once()
        run.assert_not_called()
        popen.assert_not_called()

    def test_batch_restarts_once_when_module_preload_fails(self) -> None:
        for second_failure in (False, True):
            with self.subTest(second_failure=second_failure):
                log = self.root / "preload.log"
                log.unlink(missing_ok=True)
                args = ["batch", "--target", "claude4", "--log", str(log), "alpha", "beta"]
                preload = [ImportError("mixed modules"), AttributeError("missing symbol") if second_failure else None]
                results = [stop_resume.ClaudeUp("alpha", "w1:p2", "claude4"),
                           stop_resume.ClaudeUp("beta", "w1:p4", "claude4")]

                def restart_batch(executable: str, command: list[str]) -> Never:
                    self.assertEqual(executable, sys.executable)
                    self.assertEqual(command[:2], [sys.executable, stop_resume.__file__])
                    self.assertEqual(log.read_text(),
                                     "batch startup: module preload failed (ImportError); restarting in a new interpreter\n")
                    raise SystemExit(stop_resume.main(command[2:]))

                with (patch.object(stop_resume, "_load_batch_modules", side_effect=preload) as load_modules,
                      patch.object(os, "execv", side_effect=restart_batch) as execv,
                      patch.object(stop_resume, "move_sessions", return_value=results) as move_sessions):
                    if second_failure:
                        with self.assertRaisesRegex(AttributeError, "missing symbol"):
                            _ = stop_resume.main(args)
                        move_sessions.assert_not_called()
                    else:
                        with self.assertRaises(SystemExit) as exited:
                            _ = stop_resume.main(args)
                        self.assertEqual(exited.exception.code, 0)
                        self.assertEqual(move_sessions.call_args.args, (["alpha", "beta"], "claude4"))
                        entries = [cast(dict[str, object], json.loads(line)) for line in log.read_text().splitlines()[1:]]
                        self.assertEqual([entry["session_id"] for entry in entries], ["alpha", "beta"])
                    execv.assert_called_once()
                    self.assertEqual(load_modules.call_count, 2)
                self.assertEqual(log.read_text().count("restarting in a new interpreter"), 1)

    def check_batch_moves_across_main_change(self) -> None:
        """Run the batch in a fresh interpreter with fake sessions."""
        from importlib import import_module

        from scripts.production import session_staff, worker

        production = import_module("scripts.production")
        self.add_beta()
        identity = self.entry["session_id"]
        third = pane_record.launch("w1:p6", "sid-gamma", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3")}, ["claude", "-n", "gamma"])
        pane_record.write("agents", third)
        self.client.rows.append({"pane_id": "w1:p6", "terminal_id": "gamma-terminal", "label": "gamma",
                                 "workspace_id": "w1", "agent_session": {"kind": "id", "value": "sid-gamma"}})
        expected = [(identity, "w1:p2"), ("sid-beta", "w1:p4"), ("sid-gamma", "w1:p6")]
        members = [member for member, _pane in expected]
        checkout = self.root / "checkout/scripts/production"
        checkout.mkdir(parents=True)
        dependency = checkout / "session_staff.py"
        dependent = checkout / "worker.py"
        old_dependency = Path(session_staff.__file__).read_bytes()
        old_dependent = Path(worker.__file__).read_bytes()
        _ = dependency.write_bytes(old_dependency)
        _ = dependent.write_bytes(old_dependent)

        def publish_main(*_args: object) -> None:
            self.assertFalse(self.launched)
            _ = dependency.write_bytes(old_dependency + b"\nBatchRevision = 2\n")
            _ = dependent.write_bytes(old_dependent + b"\nfrom scripts.production.session_staff import BatchRevision\n")

        def stop_member(member: str) -> stop_resume.Stopped:
            return stop_resume.Stopped(member, 123)

        log = self.root / "batch.log"
        with (patch.object(production, "__path__", [str(checkout), str(Path(session_staff.__file__).parent)]),
              patch.object(stop_resume, "_stop", side_effect=stop_member),
              patch.object(stop_resume, "_warn_moves", side_effect=publish_main) as published):
            # The test fixtures import worker. Let the batch load it from the fixture checkout.
            del sys.modules[worker.__name__]
            delattr(production, "worker")
            status = stop_resume.main(["batch", "--target", "claude4", "--cause", "auto", "--warn-seconds", "1",
                                       "--log", str(log), *members])

        rows = [cast(dict[str, object], json.loads(line)) for line in log.read_text().splitlines()
                if line.startswith("{")]
        self.assertEqual([(row["result"], row["session_id"], row.get("pane_id"), row.get("account")) for row in rows],
                         [("ClaudeUp", member, pane, "claude4") for member, pane in expected])
        self.assertEqual(status, 0)
        self.assertEqual(self.launched, set(members))
        self.assertEqual(sys.modules[worker.__name__].__file__, str(dependent))
        published.assert_called_once()


    def test_reused_pid_generation_is_checked_before_each_signal(self) -> None:
        for changed_before_term in (True, False):
            with self.subTest(before_term=changed_before_term):
                self.kill.reset_mock()
                self.live.return_value = sessions.LiveSessionProcess(123)
                self.process.side_effect = ([sessions.SessionNotLive()] if changed_before_term else
                                           [sessions.LiveSessionProcess(123), sessions.SessionNotLive()])
                result = stop_resume.stop("alpha")
                self.assertIsInstance(result, stop_resume.NotRunning if changed_before_term else stop_resume.Stopped)
                self.assertEqual(self.kill.call_args_list, [] if changed_before_term else [call(123, signal.SIGTERM)])

    def test_absent_and_unknown_sessions_are_distinct_and_unsignalled(self) -> None:
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.live.return_value = sessions.SessionLivenessUnknown("registry unreadable")
        identity = "019b3a40-4250-7000-8000-000000000102"
        reason = f"{identity} is not proved live or stopped: 1 processes unknown, first: "\
            + f"{identity} (Claude session unknown: registry unreadable)"
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.StopUnproved(identity, reason))
        self.assertEqual(stop_resume.stop("elsewhere"), stop_resume.SessionNotFound("elsewhere"))
        self.assertEqual(stop_resume.resume("elsewhere", why="maintenance"), stop_resume.ResumeFailed("elsewhere", "unknown name"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_stop_without_a_target_keeps_a_denied_registry_unknown(self) -> None:
        registry = sessions.sessions_dir()
        iterdir = Path.iterdir

        def list_folder(path: Path) -> Iterator[Path]:
            if path == registry:
                raise PermissionError(path)
            return iterdir(path)

        with patch.object(Path, "iterdir", new=list_folder):
            self.assertEqual(stop_resume.stop("elsewhere"),
                             stop_resume.StopUnproved("elsewhere", "session registry unreadable"))
        self.kill.assert_not_called()

    def test_tab_labels_are_exact_and_unidentified_or_ambiguous_tabs_never_signal(self) -> None:
        self.assertEqual(stop_resume.stop("alph"), stop_resume.SessionNotFound("alph"))
        self.client.rows.append({"pane_id": "w1:p4", "label": "empty", "workspace_id": "w1"})
        self.assertEqual(stop_resume.stop("empty"), stop_resume.SessionNotFound("empty"))
        self.client.rows.append({"pane_id": "w1:p5", "label": "alpha", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-other"}})
        self.assertEqual(stop_resume.stop("alpha"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.NotRunning("019b3a40-4250-7000-8000-000000000102"))
        self.kill.assert_not_called()
        self.start.assert_not_called()

    def test_resume_leaves_a_live_session_on_its_account_without_starting(self) -> None:
        self.client.native_ids["w1:p2"] = self.entry["session_id"]
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))
        self.start.assert_not_called()

    def test_resume_proves_the_native_id_before_submitting_the_opening_work(self) -> None:
        def observe(pane: str, kind: str, timeout: float) -> str:
            self.assertEqual((pane, kind), ("w1:p2", "claude"))
            self.assertGreater(timeout, 0)
            self.assertEqual(self.client.prompts, [])
            self.start.assert_called_once()
            arguments = self._typed(cast(tuple[str, ...], self.start.call_args.args))
            self.assertNotIn("Continue where you stopped", " ".join(arguments))
            self.assertNotIn("STAFF_ID=", " ".join(arguments))
            return self.entry["session_id"]

        with patch.object(self.client, "native_session", side_effect=observe):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(len(self.client.prompts), 1)
        pane, opening = self.client.prompts[0]
        self.assertEqual(pane, "w1:p2")
        assert isinstance(opening.transcript, herdr.SessionTranscript)
        self.assertEqual(opening.transcript.session_id, self.entry["session_id"])
        self.assertIn("maintenance", opening.text)
        self.assertIn("Continue where you stopped", opening.text)


    def test_resume_identity_mismatch_or_missing_proof_dispatches_no_work(self) -> None:
        for proof in ("019b3a40-4250-7000-8000-000000000199", herdr_types.HerdrFailure("registry unavailable")):
            with self.subTest(proof=proof):
                self.launched.clear()
                self.client.prompts.clear()
                self.start.reset_mock()
                errors = io.StringIO()
                identity = staff.StaffId(self.entry["session_id"])
                row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
                with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
                      patch.object(self.client, "native_session", return_value=proof if isinstance(proof, str) else "",
                                   side_effect=None if isinstance(proof, str) else proof),
                      redirect_stderr(errors)):
                    result = stop_resume.resume("alpha", why="maintenance")
                self.assertIsInstance(result, stop_resume.ClaudeUp)
                self.assertEqual(self.client.prompts, [])
                self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())
                self.assertIn("prompt identity anomaly:", errors.getvalue())
                self.detach.assert_called()

    def test_resume_retries_identity_until_proved_and_submits_one_prompt(self) -> None:
        errors = io.StringIO()
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "native_session", side_effect=[
                  herdr_types.HerdrFailure("registry unavailable"), self.entry["session_id"]]) as observed,
              redirect_stderr(errors)):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(observed.call_count, 2)
        self.assertEqual(self.clock.elapsed, 0.5)
        self.assertEqual(len(self.client.prompts), 1)
        self.assertEqual(errors.getvalue(), "")

    def test_resume_retries_a_timed_out_identity_read_and_submits_one_prompt(self) -> None:
        errors = io.StringIO()
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "native_session", side_effect=[
                  subprocess.TimeoutExpired("herdr pane process-info", 0.1), self.entry["session_id"]]) as observed,
              redirect_stderr(errors)):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(observed.call_count, 2)
        self.assertEqual(self.clock.elapsed, 0.5)
        self.assertEqual(len(self.client.prompts), 1)
        self.assertEqual(errors.getvalue(), "")

    def test_resume_reports_identity_failure_once_at_the_deadline(self) -> None:
        errors = io.StringIO()
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "native_session", side_effect=herdr_types.HerdrFailure(
                  "registry unavailable")) as observed,
              redirect_stderr(errors)):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertGreater(observed.call_count, 1)
        self.assertEqual(self.clock.elapsed, conf.read("shutdown").number("START_SECONDS"))
        self.assertEqual(self.client.prompts, [])
        self.assertEqual(errors.getvalue().splitlines(), [
            f"{self.entry['session_id']}: prompt identity anomaly: registry unavailable"])
        self.detach.assert_called()

    def test_resume_prepares_the_transcript_cursor_before_launch(self) -> None:
        path = Path(str(cast(staff.SessionHome, pane_launch.config_home(self.entry)).path)) / "projects" / "fixture" / f"{self.entry['session_id']}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text('{"type":"assistant","message":{"content":"old context"}}\n')
        before = path.stat().st_size

        def start(*_command: str) -> str:
            with path.open("a") as stream:
                _ = stream.write(json.dumps({
                    "type": "user",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {"role": "user", "content": "launch-time work"},
                }) + "\n")
            return ""

        self.start.side_effect = start
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity, worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(len(self.client.prompts), 1)
        cursor = self.client.prompts[0][1].cursor
        assert isinstance(cursor, herdr.TranscriptCursor)
        self.assertEqual(cursor.offsets[path], before)
        self.assertEqual(len(cursor.read_turns()), 1)

    def test_resume_identity_mismatch_dispatches_no_work_without_retry(self) -> None:
        proof = "019b3a40-4250-7000-8000-000000000199"
        errors = io.StringIO()
        identity = staff.StaffId(self.entry["session_id"])
        row = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity,
                             home=staff.SessionHome(self.root / ".claude3"), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(self.client, "native_session", return_value=proof) as observed,
              redirect_stderr(errors)):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(self.client.prompts, [])
        self.assertEqual(errors.getvalue().splitlines(), [
            f"{identity}: prompt identity anomaly: observed {proof}"])
        observed.assert_called_once()
        self.assertEqual(self.clock.elapsed, 0)
        self.detach.assert_called()

    def test_resume_worker_uses_its_own_busy_pass_and_leaves_idle_workers_without_input(self) -> None:
        from scripts.direct import progress_history
        from scripts.production import worker

        parent = staff.StaffRow(staff.StaffId("director"), "unit", "director", staff.STAFF_ROOT,
                                current_session=staff.StaffId("native-director"), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "alpha", staff.ReportsTo(parent.id),
                             current_session=staff.StaffId(self.entry["session_id"]), worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        record = staff.StaffSnapshot((parent, row))
        for active in (progress_history.ActiveDispatch("own-dispatch", ("assign.sh",)), progress_history.NoActiveDispatch()):
            with (self.subTest(active=active),
                  patch.object(staff, "initial_snapshot", return_value=record),
                  patch.object(worker_panes, "worker_run", return_value=self.root) as run,
                  patch.object(progress_history, "active_dispatch", return_value=active) as read,
                  patch.object(worker, "resume_receipt", return_value=worker.DispatchSubmitted())):
                self.launched.clear()
                self.client.prompts.clear()
                result = stop_resume.resume(str(row.id), why="maintenance")
                self.assertIsInstance(result, stop_resume.ClaudeUp)
                if isinstance(active, progress_history.ActiveDispatch):
                    self.assertEqual(len(self.client.prompts), 1)
                    self.assertIn("Continue pass own-dispatch in this conversation", self.client.prompts[0][1].text)
                    self.assertNotIn("Worker ", self.client.prompts[0][1].text)
                else:
                    self.assertEqual(self.client.prompts, [])
                run.assert_called_once_with(row, record=record)
                read.assert_called_once_with(row.id, self.root)

    def test_named_resume_keeps_the_saved_assignment_when_the_worker_never_received_it(self) -> None:
        from scripts.direct import progress_history
        from scripts.production import worker

        parent = staff.StaffRow(staff.StaffId("director"), "unit", "director", staff.STAFF_ROOT,
                                current_session=staff.StaffId("native-director"), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "alpha", staff.ReportsTo(parent.id),
                             current_session=staff.StaffId(self.entry["session_id"]), worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        record = staff.StaffSnapshot((parent, row))
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(worker_panes, "worker_run", return_value=self.root),
              patch.object(progress_history, "active_dispatch", return_value=progress_history.ActiveDispatch(
                  "own-dispatch", ("bash", "assign.sh", "saved prompt.md"))),
              patch.object(worker, "resume_receipt", return_value=worker.DispatchNotSubmitted())):
            result = stop_resume.resume("alpha", why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        text = "\n".join(prompt.text for _pane, prompt in self.client.prompts)
        self.assertIn("bash assign.sh 'saved prompt.md'", text)
        self.assertNotIn("Continue pass", text)

    def test_resume_director_submits_an_opening_prompt_with_idle_pane_workers(self) -> None:
        from scripts.lib.resume_message import IdleWorker, PaneWorkerResume, ResumeWorkers
        from scripts.production import worker

        row = staff.StaffRow(staff.StaffId("director"), "unit", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(self.entry["session_id"]), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("merge"))
        workers = ResumeWorkers(panes=[PaneWorkerResume(staff.StaffId("idle-worker"), IdleWorker())])
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(worker, "resume_workers", return_value=workers)):
            result = stop_resume.resume(str(row.id), why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.assertEqual(len(self.client.prompts), 1)
        text = self.client.prompts[0][1].text
        self.assertIn("You were stopped and resumed because maintenance.", text)
        self.assertIn("for messages kept while you were down", text)
        self.assertIn("Worker idle-worker: idle", text)
        self.assertIn("/unit:merge", text)
        self.assertIn("~/.claude/commands/unit/merge.md", text)

    def test_no_worker_mode_resumes_the_director_alone_with_the_notice(self) -> None:
        from scripts.lib.resume_message import IdleWorker, PaneWorkerResume, ResumeWorkers
        from scripts.production import worker
        from scripts.whoami import quota_state

        row = staff.StaffRow(staff.StaffId("director"), "unit", "alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(self.entry["session_id"]), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        workers = ResumeWorkers(panes=[PaneWorkerResume(staff.StaffId("idle-worker"), IdleWorker())])
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(quota_state, "no_worker_mode", return_value=True),
              patch.object(quota_state, "no_worker_notice", return_value="Codex workers have no capacity. Send no assign."),
              patch.object(worker, "resume_workers", return_value=workers) as listed):
            result = stop_resume.resume(str(row.id), why="maintenance")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        listed.assert_not_called()
        self.assertEqual(len(self.client.prompts), 1)
        text = self.client.prompts[0][1].text
        self.assertIn("You were stopped and resumed because maintenance.", text)
        self.assertIn("Codex workers have no capacity. Send no assign.", text)
        self.assertNotIn("Worker idle-worker", text)

    def test_no_worker_mode_refuses_a_codex_worker_before_any_launch(self) -> None:
        from scripts.message import session_records as sessions
        from scripts.whoami import quota_state

        parent = staff.StaffRow(staff.StaffId("director"), "unit", "director", staff.STAFF_ROOT,
                                current_session=staff.StaffId("native-director"), worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        row = staff.StaffRow(staff.StaffId("fixed-worker"), "worker", "alpha", staff.ReportsTo(parent.id),
                             current_session=staff.StaffId(self.entry["session_id"]), worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        for kind, held in (("codex", True), ("claude", True), ("codex", False)):
            with (self.subTest(kind=kind, held=held),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((parent, row))),
                  patch.object(pane_launch, "saved_kind", return_value=kind),
                  patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
                  patch.object(quota_state, "no_worker_mode", return_value=held)):
                self.launched.clear()
                result = stop_resume.resume(str(row.id), why="maintenance")
                if kind == "codex" and held:
                    self.assertEqual(result, stop_resume.ResumeFailed(self.entry["session_id"],
                                                                      "no-worker mode keeps Codex workers stopped"))
                    self.assertEqual(self.launched, set())
                else:
                    self.assertNotEqual(result, stop_resume.ResumeFailed(self.entry["session_id"],
                                                                         "no-worker mode keeps Codex workers stopped"))

    def test_empty_resume_message_adds_no_argument_in_the_current_pane(self) -> None:
        tab = herdr.TabSession("w1:p2", "alpha", "w1", self.entry["session_id"])
        with (patch.object(os, "chdir"), patch.object(os, "execvpe", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                stop_resume._exec_in_own_pane(tab, self.entry, "claude3", "")  # pyright: ignore[reportPrivateUsage]
        self.assertNotIn("", cast(list[str], execute.call_args.args[1]))

    def test_resume_runs_account_exec_in_its_own_pane_with_one_message(self) -> None:
        self.entry["environment"].update(HERDR_PANE_ID="w9:p9", HERDR_WORKSPACE_ID="w9")
        pane_record.write("agents", self.entry)
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertEqual(result, stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))
        self.start.assert_called_once()
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[:4], ("pane", "run", "w1:p2", "env"))
        self.assertFalse(any(part.startswith("SHOWRUNNER_") for part in command))
        self.assertIn("HERDR_PANE_ID=w1:p2", command)
        self.assertEqual(command[4:6], ("-u", "HERDR_TAB_ID"))
        self.assertIn("HERDR_WORKSPACE_ID=w1", command)
        self.assertLess(self.clock.elapsed, 1.0)
        self.detach.assert_called_once()
        index = command.index("exec")
        self.assertTrue(command[index - 1].endswith("accounts/accounts.py"))
        self.assertEqual(command[index + 1:], ("claude3", "--resume", "019b3a40-4250-7000-8000-000000000102", "-n", "alpha",
            "--remote-control", "alpha", "--model", "sonnet", "--settings", '{"disableAgentView": true}'))
        self.assertEqual(len(self.client.prompts), 1)
        pane, opening = self.client.prompts[0]
        self.assertEqual(pane, "w1:p2")
        self.assertIn("stopped and resumed", opening.text)
        self.assertIn("maintenance", opening.text)
        self.assertIn("Continue where you stopped", opening.text)
        self.assertEqual(self.client.closed, [])
        self.assertEqual(self.client.started, [])


    def test_resume_launches_in_the_saved_move_destination(self) -> None:
        from scripts.worktree import worktrees

        old = self.root / "old checkout"
        new = self.root / "new checkout"
        (old / "crates").mkdir(parents=True)
        self.entry["cwd"] = str(old / "crates")
        pane_record.write("agents", self.entry)
        identity = worktrees.FolderIdentity.read(old)
        _ = old.rename(new)
        move = worktrees.WorktreeMove(old, new, "origin", "alpha", "test", worktrees.MoveOutput.LIVE_UNIT)
        journal = worktrees.MoveJournal(move, identity, identity, "", Path(".git"), (), worktrees.MoveStage.FINISHED)
        with patch.dict(os.environ, {"WORKTREES_STATE_DIR": str(self.root / "worktrees")}):
            worktrees.write_move(journal)
            result = stop_resume.resume("alpha", why="worktree move")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.start.assert_called_once()
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[command.index("resume") + 1], str(new / "crates"))

    def test_resume_uses_the_saved_folder_with_an_incomplete_move_record(self) -> None:
        from scripts.worktree import worktrees

        saved = self.root / "saved checkout"
        saved.mkdir()
        self.entry["cwd"] = str(saved)
        pane_record.write("agents", self.entry)
        with patch.dict(os.environ, {"WORKTREES_STATE_DIR": str(self.root / "worktrees")}):
            record = worktrees.move_file(saved)
            record.parent.mkdir(parents=True)
            _ = record.write_text("{}")
            result = stop_resume.resume("alpha", why="worktree move")
            _ = record.write_text(json.dumps({"old": str(saved)}))
            self.assertEqual(worktrees.moved_launch_folder(saved), saved)
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        self.start.assert_called_once()
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[command.index("resume") + 1], str(saved))

    def test_resume_returns_before_the_stay_window_and_detaches_its_generation(self) -> None:
        result = stop_resume.resume("alpha", why="maintenance")
        self.assertLess(self.clock.elapsed, conf.read("shutdown").number("STAY_SECONDS"))
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        arguments = cast(list[str], self.detach.call_args.args[0])
        self.assertEqual(arguments[:3], ["stay", "--session", "019b3a40-4250-7000-8000-000000000102"])
        self.assertEqual(arguments[arguments.index("--pid") + 1], "456")
        self.assertIn("generation-1", arguments)
        self.assertEqual(arguments[arguments.index("--why") + 1], "maintenance")
        self.start.assert_called_once()

    def run_stay_child(self, *, requester: str = "", why: str = "usage limit") -> tuple[int, str]:
        def exits_soon(_pid: int, _generation: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(456) if self.clock.elapsed < 0.6 else sessions.SessionNotLive()
        self.process.side_effect = exits_soon
        log = self.root / "stay.log"
        result = stop_resume.main(["stay", "--session", "019b3a40-4250-7000-8000-000000000102", "--pane", "w1:p2",
            "--account", "claude3", "--pid", "456", "--generation", "generation-1",
            "--requester", requester, "--why", why, "--log", str(log)])
        return result, log.read_text(encoding="utf-8")

    def test_detached_stay_check_alerts_on_early_exit_without_restarting(self) -> None:
        with patch.object(stop_resume, "_send_move_push") as push:
            result, log = self.run_stay_child()
        self.assertEqual(result, 1)
        self.assertIn("stopped 0.6 s", log)
        self.assertLess(self.clock.elapsed, conf.read("shutdown").number("STAY_SECONDS"))
        push.assert_called_once()
        self.assertTrue(cast(str, push.call_args.args[1]).startswith("resume-ended:"))
        self.start.assert_not_called()

    def test_early_exit_tells_showrunner_pushes_except_probe_and_enters_watchdog(self) -> None:
        from scripts.production import showrunners, staff_sources
        from scripts.accounts import unit_watchdog
        identity = staff.StaffId(self.entry["session_id"])
        owner = staff.StaffRow(identity, "unit", "alpha", staff.StaffRoot(), current_session=identity, unit_type=staff.UnitDetails("direct"))
        _ = self.enterContext(patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((owner,))))
        for probe in (False, True):
            for outcome in ("watchdog: alpha restarted on claude3",
                            "watchdog: alpha: folder no longer exists; restore dropped"):
                with self.subTest(probe=probe, outcome=outcome):
                    self.entry["environment"]["CLAUDE_PROBE_SESSION"] = "1" if probe else "0"
                    pane_record.write("agents", self.entry)
                    with (patch.object(staff_sources, "unit_parent", return_value=staff_sources.UnitProduction("demo", str(self.root / "production.md"))),
                          patch.object(showrunners, "tell", return_value="told demo") as tell,
                          patch.object(stop_resume, "_send_move_push") as push,
                          patch.object(unit_watchdog, "run", return_value=[outcome]) as recovery):
                        result, log = self.run_stay_child(requester="other")
                        self.assertEqual(result, 1)
                        tell.assert_called_once()
                        self.assertEqual(tell.call_args.args[0], "demo")
                        line = cast(str, tell.call_args.args[2])
                        self.assertEqual(line, f"alpha stopped 0.6 s after its move or resume to claude3; {outcome}")
                        self.assertIn(line, log)
                        self.assertEqual(push.call_count, 0 if probe else 1)
                        if not probe:
                            self.assertEqual(cast(RenderedPush, push.call_args.args[0]).values["cause"], line)
                        recovery.assert_called_once()
                    self.clock.elapsed = 0.0

    def test_named_probe_reports_rounded_time_account_retry_and_shutdown_sender(self) -> None:
        from scripts.production import showrunners
        self.entry["environment"]["CLAUDE_PROBE_SESSION"] = "1"
        pane_record.write("agents", self.entry)
        with (patch.object(showrunners, "tell", _REAL_TELL),
              patch.object(showrunners, "current_address", return_value="session:requester"),
              patch.object(showrunners, "send_to_address", return_value=showrunners.NoticeDelivery(("told requester",))) as send,
              patch.object(stop_resume, "_send_move_push") as push):
            result, log = self.run_stay_child(requester="requester")
        self.assertEqual(result, 1)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "session:requester")
        self.assertEqual(send.call_args.kwargs, {"recipient": "requester", "sender": "shutdown"})
        line = cast(str, send.call_args.args[2])
        self.assertEqual(line, "alpha stopped 0.6 s after its move or resume to claude3; resume it: "
                         + "agents resume alpha --to claude3 --why 'usage limit'")
        self.assertIn(line, log)
        push.assert_not_called()

    def test_unnamed_session_uses_its_id_in_alert_and_retry(self) -> None:
        from scripts.production import showrunners
        self.entry["name"] = ""
        self.client.rows[0]["label"] = None
        pane_record.write("agents", self.entry)
        with (patch.object(showrunners, "tell", return_value="told requester") as tell,
              patch.object(stop_resume, "_send_move_push") as push):
            result, log = self.run_stay_child(requester="requester")
        self.assertEqual(result, 1)
        line = cast(str, tell.call_args.args[2])
        self.assertEqual(line, "019b3a40-4250-7000-8000-000000000102 stopped 0.6 s after its move or resume to claude3; resume it: "
                         + "agents resume 019b3a40-4250-7000-8000-000000000102 --to claude3 --why 'usage limit'")
        self.assertIn(line, log)
        self.assertEqual(cast(RenderedPush, push.call_args.args[0]).values["cause"], line)

    def test_no_requester_or_unit_never_tells_showrunners_and_only_pushes_non_probes(self) -> None:
        from scripts.production import showrunners
        for probe in (False, True):
            with self.subTest(probe=probe):
                self.entry["environment"]["CLAUDE_PROBE_SESSION"] = "1" if probe else "0"
                pane_record.write("agents", self.entry)
                with (patch.object(showrunners, "tell") as tell,
                      patch.object(showrunners, "tell_others") as broadcast,
                      patch.object(stop_resume, "_send_move_push") as push):
                    result, log = self.run_stay_child()
                self.assertEqual(result, 1)
                self.assertIn("alpha stopped 0.6 s", log)
                tell.assert_not_called()
                broadcast.assert_not_called()
                self.assertEqual(push.call_count, 0 if probe else 1)
                self.clock.elapsed = 0.0

    def test_non_unit_early_exit_tells_only_the_requesting_showrunner(self) -> None:
        from scripts.production import showrunners
        requester = fixture_member("showrunner", name="requester",
            runtime_id="019b3a40-4250-7000-8000-000000000201")
        with (patch.object(staff, "runtime_id", return_value=requester.current_session),
              patch.object(stop_resume, "_detach", wraps=self.real_detach) as detach,
              patch.object(subprocess, "run"), patch.object(subprocess, "Popen"),
              patch.object(showrunners, "tell") as tell,
              patch.object(showrunners, "tell_others") as broadcast,
              patch.object(stop_resume, "_send_move_push") as push):
            _ = stop_resume.resume("alpha", why="maintenance")
            invocation = cast(list[str], (detach.call_args.args[0]))
            # Read the actual detached command, after _detach appends the requester.
            platform_call = cast(Mock, subprocess.Popen if sys.platform == "darwin" else subprocess.run)
            command = cast(list[str], platform_call.call_args.args[0])
            arguments = command[command.index("stay"):]
            self.assertEqual(invocation[0], "stay")
            self.assertIn("--requester", arguments)
            self.assertEqual(arguments[arguments.index("--requester") + 1], "requester")
            log = self.root / "stay.log"
            self.process.return_value = sessions.SessionNotLive()
            arguments[arguments.index("--log") + 1] = str(log)
            result = stop_resume.main(arguments)
            self.assertEqual(result, 1)
            tell.assert_called_once()
            self.assertEqual(tell.call_args.args[0], "requester")
            broadcast.assert_not_called()
            push.assert_called_once()

    def test_detached_move_keeps_the_requester_for_its_stay_observation(self) -> None:
        log = self.root / "requested-batch.log"
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-requester"}):
            result = stop_resume.main(["batch", "--requester", "requester", "--target", "claude4",
                                       "--why", "limit", "--log", str(log), "alpha"])
            self.assertEqual(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "")
        self.assertEqual(result, 0)
        self.confirm.assert_called_once_with(stop_resume.ClaudeUp(self.entry["session_id"], "w1:p2", "claude4"),
                                             "requester", "limit")
        self.detach.assert_not_called()

    def test_saved_entry_finds_an_unlabelled_pane_with_empty_or_stale_herdr_id(self) -> None:
        self.client.rows[0]["label"] = None
        original = self.entry.copy()
        identity = original["session_id"]
        owner = staff.StaffRow(staff.StaffId(fixture_runtime_id("fixed-shell-owner")), "unit", "alpha", staff.StaffRoot(),
                              current_session=staff.StaffId(identity), home=staff.SessionHome(self.root / ".claude3"),
                              worktree=staff.PresentWorktree(remote.local_machine(), self.root), unit_type=staff.UnitDetails("direct"))
        for stale_copy in (False, True):
            for agent in (None, {"kind": "id", "value": "stale-session"}):
                with self.subTest(agent=agent, stale_copy=stale_copy):
                    self.launched.clear()
                    self.client.rows[0]["agent_session"] = agent
                    entry = original.copy()
                    if stale_copy:
                        entry = pane_launch.retained_launch(entry, staff.StaffSnapshot((owner,)))
                    atomic_file.write_text_atomic(pane_record.record_path("agents"), json.dumps({entry["pane_id"]: entry}))
                    with patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((owner,)) if stale_copy else staff.StaffSnapshot(())):
                        self.assertEqual(stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="maintenance"),
                                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))

    def test_saved_entry_never_selects_a_different_live_conversation(self) -> None:
        self.client.rows[0]["agent_session"] = {"kind": "id", "value": "another-live-session"}
        self.live.return_value = sessions.LiveSessionProcess(789)
        self.client.process_info = {"shell_pid": 998042, "foreground_process_group_id": 789,
            "foreground_processes": [{"pid": 789, "argv": ["claude"]}]}
        _ = self.enterContext(patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
            herdr.EnvironmentThread("another-live-session"), "generation-1", {})))
        _ = self.enterContext(patch.object(self.client, "result", side_effect=self.client_result))
        self.assertEqual(stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="maintenance"), stop_resume.ResumeFailed("019b3a40-4250-7000-8000-000000000102", "unknown name"))
        self.assertEqual(stop_resume.stop("019b3a40-4250-7000-8000-000000000102"), stop_resume.SessionNotFound("019b3a40-4250-7000-8000-000000000102"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_saved_entries_for_closed_panes_do_not_create_ambiguous_matches(self) -> None:
        missing = self.entry.copy()
        missing["pane_id"] = "w1:p-missing"
        pane_record.write("agents", missing)
        self.assertEqual(stop_resume.resume("019b3a40-4250-7000-8000-000000000102", why="maintenance"),
                         stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude3"))


    def test_move_stops_then_runs_the_target_account_in_the_same_pane(self) -> None:
        identity = "019b3a40-4250-7000-8000-000000000102"
        self.entry["environment"]["STAFF_ID"] = identity
        pane_record.write("agents", self.entry)
        tab = dict(self.client.rows[0])
        self.live.side_effect = [sessions.LiveSessionProcess(123), sessions.LiveSessionProcess(123),
                                 sessions.SessionNotLive(), sessions.SessionNotLive()]
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        result = stop_resume.move("alpha", "claude4", why="weekly limit")
        self.assertEqual(result, stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.assertEqual(self.kill.call_args_list, [call(123, signal.SIGTERM)])
        command = self._typed(cast(tuple[str, ...], self.start.call_args.args))
        self.assertEqual(command[command.index("exec") + 1], "claude4")
        self.assertEqual(len(self.client.prompts), 1)
        pane, opening = self.client.prompts[0]
        self.assertEqual(pane, "w1:p2")
        self.assertIn("moved from claude3 to claude4", opening.text)
        self.assertIn("weekly limit", opening.text)
        self.assertNotIn("the herdr server restarted", opening.text)
        self.assertNotIn(f"STAFF_ID={identity}", command)
        self.assertIn(identity, command)
        environment = dict(value.split("=", 1) for value in command[command.index("env") + 1:command.index("exec")] if "=" in value)
        refresh = pane_record.launch("w1:p2", "019b3a40-4250-7000-8000-000000000102", str(self.root),
                                     environment, ["claude", "--resume", "019b3a40-4250-7000-8000-000000000102"])
        pane_record.write("agents", refresh)
        self.assertEqual(cast(pane_launch.NativePaneLaunch, pane_record.read("agents")["w1:p2"])["session_id"], identity)
        self.assertNotIn("STAFF_ID", pane_record.read("agents")["w1:p2"]["environment"])
        self.assertEqual(self.client.rows[0], tab)
        self.assertEqual(self.client.closed, [])
        self.assertEqual(self.client.started, [])

    def test_move_of_a_stopped_session_resumes_but_unreadable_liveness_refuses(self) -> None:
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"))
        self.start.reset_mock()
        self.launched.clear()
        self.live.return_value = sessions.SessionLivenessUnknown("identity unreadable")
        identity = "019b3a40-4250-7000-8000-000000000102"
        self.assertEqual(stop_resume.move("alpha", "claude4", why="limit"), stop_resume.StopFailed(identity, "identity unreadable"))
        self.start.assert_not_called()
        self.kill.assert_not_called()

    def test_a_failed_pane_run_never_reports_resume_success(self) -> None:
        self.start.side_effect = herdr_types.HerdrFailure("pane run refused")
        self.assertEqual(stop_resume.resume("alpha", why="maintenance"), stop_resume.ResumeFailed("019b3a40-4250-7000-8000-000000000102", "pane run refused"))
        self.assertEqual(self.client.closed, [])

    def test_move_records_the_callers_process_account_instead_of_a_stale_source(self) -> None:
        for label in ("codex1", "codex2"):
            home = self.root / f".{label}"
            home.mkdir(exist_ok=True)
            login = label + "@example.invalid"
            write_codex_auth(home, login)
            atomic_file.write_text_atomic(self.root / "notes" / f"{label}.md", f"---\nlogin: {login}\n---\n")
        identity = self.entry["session_id"]
        for family, source, target in (("claude", "claude3", "claude4"), ("codex", "codex2", "codex1")):
            with self.subTest(family=family):
                moves.store_file().unlink(missing_ok=True)
                self.kill.reset_mock()
                self.process.side_effect = [sessions.LiveSessionProcess(456), sessions.SessionNotLive()]
                if family == "codex":
                    _ = fixture_member("producer", name="own-codex", runtime_id=identity,
                                       backend=staff.CodexBackend(self.root / f".{source}"))
                    entry = self.entry.copy()
                    entry.update(kind="codex", config_dir=str(self.root / f".{source}"),
                                 environment={"CODEX_HOME": str(self.root / f".{source}")})
                    pane_record.write("agents", entry)
                up = stop_resume.ClaudeUp(identity, "w1:p2", target)
                def process_home(pid: int) -> account.ProcessCodexHome:
                    self.assertEqual(pid, 456)
                    return account.ProcessCodexHome(self.root / f".{target if resume.called else source}")
                with (patch.object(stop_resume, "resume", return_value=up) as resume,
                      patch.object(stop_resume, "_confirm_move_start", _REAL_CONFIRM_MOVE_START),
                      patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(456)
                                   if family == "claude" else sessions.SessionNotLive()),
                      patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation(
                          "codex" if family == "codex" else "claude", (456,))),
                      patch.object(herdr, "check_native_target", return_value=True),
                      patch.object(account, "process_codex_home", side_effect=process_home),
                      patch.object(stop_resume, "_check_stay", return_value=""),
                      patch.object(showrunners, "tell") as tell):
                    self.assertEqual(stop_resume.move_sessions([identity], target, why="auto: head", cause="auto",
                        source_accounts={identity: "claude1" if family == "claude" else target}), [up])
                entries = moves.read_moves()
                self.assertEqual([entry["kind"] for entry in entries], ["requested", "confirmed"])
                request = entries[0]
                assert request["kind"] == "requested"
                self.assertEqual((request["from"], request["to"]), (source, target))
                self.assertEqual(request["tool"], family)
                resume.assert_called_once_with(identity, why="auto: head", to=target, client=self.client)
                tell.assert_not_called()

    def test_move_refuses_a_failed_source_read_even_if_the_next_read_works(self) -> None:
        identity = self.entry["session_id"]
        failure = "fixture source registry unreadable"
        self.live.side_effect = [sessions.SessionLivenessUnknown(failure), sessions.LiveSessionProcess(123)]
        with (patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              patch.object(stop_resume, "_resume_move", return_value=stop_resume.ClaudeUp(identity, "w1:p2", "claude4")) as resumed):
            result = stop_resume.move_sessions([identity], "claude4", why="move", source_accounts={identity: "claude1"})
        self.assertEqual(result, [stop_resume.StopFailed(identity, failure)])
        live = stop_resume.running_on_account(identity, "w1:p2")
        self.assertIsInstance(live, stop_resume.ClaudeUp)
        assert isinstance(live, stop_resume.ClaudeUp)
        self.assertEqual(live.account, "claude3")
        self.assertEqual(moves.read_moves(), [])
        stopped.assert_not_called()
        resumed.assert_not_called()

    def test_move_refuses_an_unreadable_codex_daemon_without_pane_proof(self) -> None:
        identity = self.entry["session_id"]
        home = self.root / ".codex2"
        _ = fixture_member("producer", name="own-codex", runtime_id=identity, backend=staff.CodexBackend(home))
        self.client.rows.clear()
        daemon = codex_daemon.CodexDaemon(home, home / "daemon.sock")
        hosted = codex_daemon.DaemonRead(daemon, [codex_daemon.DaemonSession(identity, str(self.root), "idle")])
        failure = "fixture daemon read failed"
        unreadable = codex_daemon.DaemonRead(hosted.owner, codex_daemon.DaemonUnreadable(failure))
        log = self.root / "move.log"

        with (patch.object(codex_daemon, "read_daemons", return_value=[unreadable]) as read,
              patch.object(herdr, "codex_conversations", side_effect=READ_CODEX_CONVERSATIONS),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_send_move_push"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.StopFailed(identity, "stop refused")) as stopped,
              redirect_stderr(io.StringIO()) as errors):
            result = stop_resume.main(["batch", "--target", "codex1", "--source-account", f"{identity}=codex2",
                                       "--log", str(log), "--", identity])
            self.assertEqual(result, 1)
            self.assertTrue(read.called)
            read.return_value = [hosted]
            self.assertEqual(herdr.read_conversation(identity, "codex"), herdr.LiveConversation("codex", ()))

        self.assertEqual(moves.read_moves(), [])
        stopped.assert_not_called()
        self.kill.assert_not_called()
        self.assertEqual(errors.getvalue(), "")
        line, = log.read_text().splitlines()
        self.assertIn('"result": "StopFailed"', line)
        self.assertIn(failure, line)

    def test_move_cannot_stop_a_replacement_process_under_the_old_source(self) -> None:
        identity = self.entry["session_id"]
        for replacement_pid in (456, 123):
            with self.subTest(replacement_pid=replacement_pid):
                self.live.return_value = sessions.LiveSessionProcess(123)
                self.process.side_effect = [sessions.LiveSessionProcess(replacement_pid), sessions.SessionNotLive()]
                reads: list[str] = []

                def read_account(pid: int) -> account.ConfiguredClaudeFolder:
                    label = "claude3" if not reads else "claude2"
                    reads.append(label)
                    self.assertEqual(pid, 123)
                    return account.ConfiguredClaudeFolder(self.root / f".{label}")

                def replaced(_identity: str, _kind: str) -> herdr.LiveConversation:
                    self.assertEqual(reads, ["claude3"])
                    return herdr.LiveConversation("claude", (replacement_pid,))

                with (patch.object(account, "process_config_dir", side_effect=read_account),
                      patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
                      patch.object(stop_resume, "_record_launches"),
                      patch.object(herdr, "read_conversation", side_effect=replaced),
                      patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                          herdr.PaneSessionReport(identity), "replacement-generation",
                          {"CLAUDE_CONFIG_DIR": str(self.root / ".claude2")})),
                      patch.object(herdr, "check_native_target", return_value=True),
                      patch.object(stop_resume, "_resume_move", return_value=stop_resume.ClaudeUp(identity, "w1:p2", "claude4")) as resume):
                    result = stop_resume.move(identity, "claude4", why="move")
                self.assertEqual(result, stop_resume.StopFailed(
                    identity, "the move source process changed before the stop; no signal sent"))
                self.kill.assert_not_called()
                resume.assert_not_called()
                self.assertEqual(moves.read_moves(), [])

    def test_move_records_and_reports_an_immediate_resume_exit(self) -> None:
        identity = self.entry["session_id"]
        for family, target in (("claude", "claude4"), ("codex", "codex1")):
            with self.subTest(family=family):
                moves.store_file().unlink(missing_ok=True)
                if family == "codex":
                    home = self.root / f".{target}"
                    home.mkdir(exist_ok=True)
                    write_codex_auth(home, "producer@example.invalid")
                    atomic_file.write_text_atomic(self.root / "notes" / f"{target}.md",
                                                 "---\nlogin: producer@example.invalid\n---\n")
                    source = self.root / ".codex2"
                    source.mkdir(exist_ok=True)
                    write_codex_auth(source, "source@example.invalid")
                    atomic_file.write_text_atomic(self.root / "notes/codex2.md", "---\nlogin: source@example.invalid\n---\n")
                    _ = fixture_member("producer", name="own-codex", runtime_id=identity,
                                       backend=staff.CodexBackend(source))
                    entry = self.entry.copy()
                    entry.update(kind="codex", config_dir=str(source), environment={"CODEX_HOME": str(source)})
                    pane_record.write("agents", entry)
                up = stop_resume.ClaudeUp(identity, "w1:p2", target)
                def process_home(pid: int) -> account.ProcessCodexHome:
                    self.assertEqual(pid, 456)
                    return account.ProcessCodexHome(self.root / (f".{target}" if resumed.called else ".codex2"))

                def claude_liveness(_identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
                    return (sessions.LiveSessionProcess(456) if family == "claude" and resumed.called
                            else sessions.SessionNotLive())

                def codex_liveness(_identity: str, _kind: str) -> herdr.LiveConversation | herdr.StoppedConversation:
                    return herdr.LiveConversation("codex", (456,)) if resumed.called else herdr.StoppedConversation()

                with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
                      patch.object(stop_resume, "resume", return_value=up) as resumed,
                      patch.object(stop_resume, "_confirm_move_start", _REAL_CONFIRM_MOVE_START),
                      patch.object(sessions, "session_liveness", side_effect=claude_liveness),
                      patch.object(herdr, "read_conversation", side_effect=codex_liveness),
                      patch.object(account, "process_codex_home", side_effect=process_home),
                      patch.object(stop_resume, "_same_process", return_value=False) as observed,
                      patch.object(showrunners, "tell", return_value="told director") as tell):
                    result = stop_resume.move_sessions([identity], target, why="auto: head", requester="director")
                self.assertIsInstance(result[0], stop_resume.ResumePending)
                entries = moves.read_moves()
                self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
                failed = entries[-1]
                assert failed["kind"] == "failed"
                self.assertIn(f"stopped 0.0 s after its move or resume to {target}", failed["reason"])
                observed.assert_called_once_with(456, "generation-1")
                tell.assert_called_once()
                self.assertEqual(tell.call_args.args[0], "director")
                self.assertIn(target, str(cast(object, tell.call_args.args[2])))

    def test_move_records_and_reports_a_resume_failure(self) -> None:
        identity = self.entry["session_id"]
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
              patch.object(stop_resume, "resume", return_value=stop_resume.ResumeFailed(identity, "launch failed")),
              patch.object(showrunners, "tell", return_value="told director") as tell):
            result = stop_resume.move_sessions([identity], "claude4", why="auto: head", requester="director")
        self.assertIsInstance(result[0], stop_resume.ResumePending)
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "failed"])
        failed = entries[-1]
        assert failed["kind"] == "failed"
        self.assertEqual(failed["reason"], "launch failed")
        tell.assert_called_once()
        self.assertIn("resume on claude4 failed: launch failed", str(cast(object, tell.call_args.args[2])))

    def test_each_stopped_unit_gets_the_target_or_a_reported_failed_outcome(self) -> None:
        names = ["devtools", "fix-rounds", "style", "verify", "natest", "queue"]

        def stop_unit(identity: str) -> stop_resume.Stopped:
            return stop_resume.Stopped(identity, 123)

        def resume_unit(identity: str, *, why: str, to: str, requester: str,
                        client: herdr.Herdr) -> stop_resume.ResumeFailed:
            self.assertIs(client, self.client)
            self.assertEqual((why, to, requester), ("auto: head", "claude5", "director"))
            return stop_resume.ResumeFailed(identity, "session did not start")

        with (patch.object(stop_resume, "_stop", side_effect=stop_unit),
              patch.object(stop_resume, "resume", side_effect=resume_unit) as resumed,
              patch.object(showrunners, "tell", return_value="fixture report") as tell):
            results = stop_resume.move_sessions(names, "claude5", why="auto: head", requester="director", cause="auto")
        self.assertEqual(resumed.call_count, len(names))
        self.assertEqual(tell.call_count, len(names))
        self.assertEqual(results, [stop_resume.ResumePending(name, "claude5", "session did not start") for name in names])
        entries = moves.read_moves()
        for name in names:
            trail = [entry for entry in entries if entry["subject"] == name]
            self.assertEqual([entry["kind"] for entry in trail], ["requested", "failed"])
            self.assertEqual(trail[0]["move_id"], trail[1]["move_id"])
        self.assertTrue(all("resume on claude5 failed" in str(cast(object, invocation.args[2]))
                            for invocation in tell.call_args_list))

    def test_move_logs_the_live_source_and_final_account_with_its_cause(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "alpha", staff.StaffRoot(),
                             current_session=staff.StaffId(identity))
        self.entry["config_dir"] = str(self.root / ".claude1")
        pane_record.write("agents", self.entry)

        def stop_after_request(_pid: int, _number: int) -> None:
            self.assertEqual([entry["kind"] for entry in moves.read_moves()], ["requested"])

        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        self.kill.side_effect = stop_after_request
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(herdr, "check_native_target", return_value=True),
              patch.object(stop_resume, "resume", return_value=up)):
            self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="weekly limit", cause="auto"), [up])
        self.kill.assert_called_once_with(123, signal.SIGTERM)
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "confirmed"])
        request, confirmed = entries
        assert request["kind"] == "requested" and confirmed["kind"] == "confirmed"
        self.assertEqual(request["move_id"], confirmed["move_id"])
        self.assertEqual(len(request["move_id"]), 32)
        self.assertEqual((request["subject"], confirmed["subject"]), (f"{identity}: alpha", f"{identity}: alpha"))
        self.assertEqual((request["tool"], request["from"], request["to"], request.get("cause"), request["window"]),
                         ("claude", "claude3", "claude4", "auto", "unknown"))
        self.assertEqual(request["why"], "weekly limit")
        self.assertEqual(datetime.fromisoformat(request["at"]).utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(confirmed.get("worker"), "alpha")
        self.assertNotIn("config_dir", confirmed)
        detail = accounts._move_status_lines(entries, timezone.utc)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("(auto)", detail[0])
        self.assertIn("confirmed alpha", detail[0])

    def test_move_logs_failures_and_uses_the_last_launch_for_a_stopped_session(self) -> None:
        identity = self.entry["session_id"]
        for failed in (stop_resume.StopFailed(identity, "cannot stop"),
                       stop_resume.ResumeFailed(identity, "cannot start"), stop_resume.SessionNotFound(identity)):
            with self.subTest(result=failed):
                if moves.store_file().exists():
                    moves.store_file().unlink()
                stop = failed if isinstance(failed, (stop_resume.StopFailed, stop_resume.SessionNotFound)) else stop_resume.NotRunning(identity)
                with (patch.object(stop_resume, "_stop", return_value=stop),
                      patch.object(stop_resume, "resume", return_value=failed)):
                    _ = stop_resume.move_sessions(["alpha"], "claude4", why="move")
                request, outcome = moves.read_moves()
                assert request["kind"] == "requested" and outcome["kind"] == "failed"
                self.assertEqual(request["from"], "claude3")
                self.assertEqual(request["subject"], "alpha")
                self.assertEqual(request.get("cause"), "by hand")
                self.assertEqual(request["move_id"], outcome["move_id"])
                self.assertEqual(outcome["reason"], f"{identity}: session not found" if isinstance(failed, stop_resume.SessionNotFound) else failed.reason)

    def test_move_without_a_live_process_or_saved_account_logs_an_unresolved_home(self) -> None:
        pane_record.save("agents", {})
        with patch.object(stop_resume, "_stop", return_value=stop_resume.SessionNotFound("missing")):
            _ = stop_resume.move_sessions(["missing"], "claude4", why="move")
        request = moves.read_moves()[0]
        assert request["kind"] == "requested"
        self.assertEqual(request["from"], moves.HomeAccountUnresolved(self.root / ".claude"))

    def test_move_already_on_the_target_does_not_write_the_log(self) -> None:
        identity = self.entry["session_id"]
        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        with (patch.object(stop_resume, "running_on_account", return_value=stop_resume.LiveMoveSource(up, 123, "generation-1")),
              patch.object(stop_resume, "_stop") as stopped):
            self.assertEqual(stop_resume.move_sessions(["alpha"], "claude4", why="rerun"), [up])
        self.assertFalse(moves.store_file().exists())
        stopped.assert_not_called()

        home = self.root / ".codex1"
        home.mkdir(exist_ok=True)
        write_codex_auth(home, "producer@example.invalid")
        atomic_file.write_text_atomic(self.root / "notes/codex1.md", "---\nlogin: producer@example.invalid\n---\n")
        row = fixture_member("producer", name="own-codex", runtime_id=identity, backend=staff.CodexBackend(home))
        parent = fixture_member("showrunner", name="missing-tab-parent")
        cases: tuple[tuple[staff.Kind, Literal["claude", "codex"], bool, bool, bool], ...] = (
            ("producer", "codex", False, False, False), ("producer", "codex", False, True, False),
            ("producer", "codex", True, True, False), ("unit", "codex", False, True, False),
            ("showrunner", "codex", False, True, False), ("unit", "claude", False, True, False),
            ("showrunner", "claude", False, True, False), ("producer", "codex", False, True, True),
            ("producer", "claude", False, True, True), ("unit", "codex", False, True, True),
            ("unit", "claude", False, True, True), ("showrunner", "codex", False, True, True),
            ("showrunner", "claude", False, True, True))
        for member_kind, family, detached, missing_tab, unreadable in cases:
            with self.subTest(kind=member_kind, family=family, detached=detached, missing_tab=missing_tab, unreadable=unreadable):
                target = "codex1" if family == "codex" else "claude4"
                home = self.root / f".{target}"
                if member_kind == "producer":
                    row = staff.by_id(staff.StaffId(self.entry["session_id"])) or row
                    _ = write_registry(self.root / "configuration/models.conf", {"producers": {
                        row.name: "codex:gpt-test:high" if family == "codex" else "claude:opus:max"}}, update=True)
                    identity = row.current_session
                else:
                    row = fixture_member(member_kind, name=f"{member_kind}-{family}",
                                         parent=parent.id if member_kind == "unit" else None)
                    identity = row.current_session
                _ = staff.set_home(row.id, staff.SessionHome(self.root / ".claude3"))
                retained = self.entry.copy()
                retained.update(kind=family, session_id=identity)
                pane_record.save("agents", {retained["pane_id"]: retained})
                if missing_tab:
                    self.client.rows.clear()
                with (patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation(family, (456,))),
                      patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(456)),
                      patch.object(account, "process_config_dir", return_value=account.ProcessEnvironmentUnread("fixture account is unreadable")
                                   if unreadable else account.ConfiguredClaudeFolder(home)),
                      patch.object(account, "process_codex_home", return_value=account.ProcessEnvironmentUnread("fixture account is unreadable")
                                   if unreadable else account.ProcessCodexHome(home)),
                      patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                          herdr.PaneSessionReport(identity), "generation-1", {"CODEX_HOME": str(home)})),
                      patch.object(stop_resume, "_stop") as stopped,
                      patch.object(stop_resume, "resume") as started):
                    if detached:
                        self.assertEqual(stop_resume.main(["batch", "--target", target, "--log",
                            str(self.root / "detached.log"), row.name]), 0)
                    else:
                        results = stop_resume.move_sessions([row.name], target, why="rerun")
                        self.assertEqual(len(results), 1)
                        result = results[0]
                        if unreadable:
                            self.assertIsInstance(result, stop_resume.StopFailed)
                            assert isinstance(result, stop_resume.StopFailed)
                            self.assertIn("unreadable", result.reason)
                        else:
                            assert isinstance(result, stop_resume.ClaudeUp)
                            self.assertEqual((result.session_id, result.account), (identity, target))
                stopped.assert_not_called()
                started.assert_not_called()
                self.start.assert_not_called()
                current = staff.by_id(row.id)
                assert current is not None
                self.assertEqual(current.home, staff.SessionHome(self.root / ".claude3" if unreadable else home))

    def test_batch_logs_each_session_independently(self) -> None:
        parent = fixture_member("showrunner", name="move-parent")
        worker = fixture_worker("move-worker", parent.id)
        snapshot = staff.initial_snapshot
        for failed_read in (False, True):
            with self.subTest(failed_read=failed_read):
                first_entry = len(moves.read_moves())
                refused = worker.id if failed_read else "bad"
                subject = f"{worker.current_session}: {worker.name}" if failed_read else refused
                read_failed = False

                def read_staff() -> staff.StaffSnapshot:
                    nonlocal read_failed
                    if (failed_read and not read_failed
                            and len(moves.read_moves()) == first_entry + 2):
                        read_failed = True
                        raise staff.StaffUnreadable("fixture staff read failed")
                    return snapshot()

                def stop_one(identity: str) -> stop_resume.StopFailed | stop_resume.NotRunning:
                    if identity == "bad":
                        return stop_resume.StopFailed(identity, "cannot stop")
                    return stop_resume.NotRunning(identity)

                def resume_one(identity: str, *, to: str, **_options: object) -> stop_resume.ClaudeUp:
                    return stop_resume.ClaudeUp(identity, "own-pane", to)

                up = stop_resume.ClaudeUp("good", "own-pane", "claude4")
                with (patch.object(staff, "initial_snapshot", side_effect=read_staff),
                      patch.object(stop_resume, "_stop", side_effect=stop_one) as stop,
                      patch.object(stop_resume, "resume", side_effect=resume_one) as resume):
                    failed, moved = stop_resume.move_sessions([refused, "good"], "claude4", why="move", cause="auto")
                entries = moves.read_moves()[first_entry:]
                for name, kind in ((subject, "failed"), ("good", "confirmed")):
                    trail = [entry for entry in entries if entry["subject"] == name]
                    self.assertEqual([entry["kind"] for entry in trail], ["requested", kind])
                    self.assertEqual(trail[0]["move_id"], trail[1]["move_id"])
                self.assertNotEqual(entries[0]["move_id"], entries[1]["move_id"])
                self.assertEqual(moved, up)
                if failed_read:
                    self.assertTrue(read_failed)
                    stop.assert_called_once_with("good")
                    resume.assert_called_once_with("good", why="move", to="claude4", client=self.client)
                    self.assertIsInstance(failed, stop_resume.StopFailed)
                    assert isinstance(failed, stop_resume.StopFailed)
                    self.assertEqual(failed.session_id, worker.current_session)
                    self.assertIn("fixture staff read failed", failed.reason)
                    outcome, = [entry for entry in entries if entry["kind"] == "failed"]
                    self.assertEqual(outcome["reason"], failed.reason)

    def test_batch_logs_a_worker_refused_by_the_role_check(self) -> None:
        parent = fixture_member("showrunner", name="move-parent")
        worker = fixture_worker("move-worker", parent.id)
        sibling = fixture_worker("move-sibling", parent.id)
        _ = staff.set_worker_role(sibling.id, staff.KnownWorkerRole("impl"))
        _ = staff.set_home(worker.id, staff.SessionHome(self.root / ".claude3"))
        source = stop_resume.LiveMoveSource(
            stop_resume.ClaudeUp(worker.current_session, "worker-pane", "claude3"), 123, "generation-1")
        sibling_source = stop_resume.LiveMoveSource(
            stop_resume.ClaudeUp(sibling.current_session, "sibling-pane", "claude3"), 124, "generation-2")
        up = stop_resume.ClaudeUp(sibling.current_session, "sibling-pane", "claude4")
        subject = f"{worker.current_session}: {worker.name}"

        write = os.write
        append = moves.append_move
        read_journal = worker_names.migrate_locked_journal
        for live, fail_read in ((True, False), (False, False), (False, True)):
            with self.subTest(live=live, fail_read=fail_read):
                moves.store_file().unlink(missing_ok=True)
                first_id = ""
                append_failed = False
                read_failed = False

                def write_request(descriptor: int, data: bytes) -> int:
                    nonlocal first_id, append_failed
                    if first_id and not append_failed and data == b"\n":
                        append_failed = True
                        raise OSError("write failed after the request JSON")
                    if (not live and not fail_read and not append_failed and b'"kind": "requested"' in data
                            and worker.current_session.encode() in data):
                        first_id = str(cast(dict[str, object], json.loads(data))["move_id"])
                        return write(descriptor, data[:-1])
                    return write(descriptor, data)

                def append_request(entry: moves.MoveWriteEntry) -> None:
                    nonlocal first_id, append_failed
                    append(entry)
                    if fail_read and not append_failed and entry["kind"] == "requested" and entry["subject"] == subject:
                        first_id = entry["move_id"]
                        append_failed = True
                        raise OSError("could not confirm the saved request")

                def read_requests(source: io.TextIOWrapper, path: Path) -> list[str]:
                    nonlocal read_failed
                    if fail_read and append_failed and not read_failed:
                        read_failed = True
                        raise OSError("read failed after the request JSON")
                    return read_journal(source, path)

                def running(identity: str, _pane: str, **_options: object) -> stop_resume.LiveMoveSource | sessions.SessionNotLive:
                    if live and identity == worker.current_session:
                        return source
                    if fail_read and identity == sibling.current_session:
                        return sibling_source
                    return sessions.SessionNotLive()

                sibling_conversation = herdr.StoppedConversation()
                if fail_read:
                    sibling_conversation = herdr.LiveConversation("claude", (sibling_source.pid,))
                    self.process.side_effect = [sessions.LiveSessionProcess(sibling_source.pid), sessions.SessionNotLive()]
                with (patch.object(os, "write", write_request),
                      patch.object(moves, "append_move", side_effect=append_request),
                      patch.object(worker_names, "migrate_locked_journal", read_requests),
                      patch.object(stop_resume, "running_on_account", running),
                      patch.object(herdr, "read_conversation", return_value=sibling_conversation),
                      patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                          herdr.PaneSessionReport(sibling.current_session), sibling_source.started, {})),
                      patch.object(herdr, "check_native_target", return_value=True),
                      patch.object(stop_resume, "_stop", wraps=stop_resume._stop) as stop,  # pyright: ignore[reportPrivateUsage]
                      patch.object(stop_resume, "resume", return_value=up) as resume):
                    failed, moved = stop_resume.move_sessions([worker.id, sibling.id], "claude4", why="move")

                self.assertEqual(append_failed, not live)
                self.assertEqual(read_failed, fail_read)
                self.assertIsInstance(failed, stop_resume.StopFailed)
                assert isinstance(failed, stop_resume.StopFailed)
                self.assertEqual(failed.session_id, worker.current_session)
                self.assertIn(worker_panes.StartRefusal.ROLE_UNAVAILABLE.value, failed.reason)
                self.assertEqual(moved, up)
                stop.assert_called_once_with(sibling.id)
                if fail_read:
                    self.kill.assert_called_once_with(sibling_source.pid, signal.SIGTERM)
                else:
                    self.kill.assert_not_called()
                resume.assert_called_once_with(sibling.current_session, why="move", to="claude4", client=self.client)
                trail = [entry for entry in moves.read_moves() if entry["subject"] == subject]
                self.assertEqual([entry["kind"] for entry in trail], ["requested"] if fail_read else ["requested", "failed"])
                request = trail[0]
                assert request["kind"] == "requested"
                self.assertEqual(request["from"], "claude3")
                if not live:
                    self.assertEqual(request["move_id"], first_id)
                if not fail_read:
                    outcome = trail[1]
                    assert outcome["kind"] == "failed"
                    self.assertEqual(outcome["move_id"], request["move_id"])
                    self.assertEqual(outcome["reason"], failed.reason)

    def test_detached_append_failure_rolls_back_and_keeps_the_request(self) -> None:
        identity = self.entry["session_id"]
        up = stop_resume.ClaudeUp(identity, "w1:p2", "claude4")
        state = shutdown_state_root()
        state.mkdir()
        log = state / "move.log"
        path = moves.store_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        before = (b'{"kind":"requested","at":"2026-01-01T00:00:00Z","tool":"claude",'
                  + b'"subject":"older","move_id":"older","from":"claude2","to":"claude3","why":"fixture","window":"unknown"}\n')
        _ = path.write_bytes(before)
        replay = moves.read_replay_evidence()
        self.assertIsInstance(replay, moves.MoveReplayEvidence)
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": identity}),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              patch.object(stop_resume, "_send_move_push") as send,
              patch.object(stop_resume, "_resume_move", return_value=up) as resumed):
            self.assertEqual(stop_resume.move(identity, "claude4", why="first"), stop_resume.MoveHandedOff(identity))
            argv = cast(list[str], self.detach.call_args.args[0])
            _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            batch_id = argv[argv.index("--request-id") + 1]
            self.assertEqual(path.read_bytes(), before)
            real_write = os.write

            def partial_write(descriptor: int, data: bytes) -> int:
                if data == b"\n":
                    raise OSError("disk failed")
                count = real_write(descriptor, data.removesuffix(b"\n"))
                self.assertEqual(path.read_bytes(), before + data.removesuffix(b"\n"))
                return count

            with patch.object(os, "write", side_effect=partial_write) as write:
                self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
                self.assertEqual(write.call_count, 2)
            stopped.assert_not_called()
            resumed.assert_not_called()
            send.assert_called_once()
            self.assertIn("could not record the move request: disk failed", log.read_text())
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(moves.read_replay_evidence(), replay)
            self.assertEqual(moves.read_latest_request(identity), moves.LatestMoveRequest(batch_id, "claude4"))
            self.assertEqual(stop_resume.main(["retry", batch_id]), 0)
            stopped.assert_called_once_with(identity)
            resumed.assert_called_once()
        self.assertFalse(moves.latest_request_path(identity).exists())
        self.assertEqual([entry["to"] for entry in moves.read_moves() if entry["kind"] == "requested"], ["claude3", "claude4"])

    def test_failed_request_append_prevents_a_signal_to_a_proved_live_process(self) -> None:
        identity = self.entry["session_id"]
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.process.side_effect = [sessions.LiveSessionProcess(123), sessions.SessionNotLive()]
        with (patch.object(herdr, "check_native_target", return_value=True),
              patch.object(moves, "append_move", side_effect=OSError("disk failed")) as append,
              patch.object(stop_resume, "_resume_move") as resumed):
            self.assertEqual(stop_resume.move(identity, "claude4", why="move"),
                             stop_resume.StopFailed(identity, "could not record the move request: disk failed"))
        append.assert_called_once()
        self.kill.assert_not_called()
        resumed.assert_not_called()

    def test_launch_names_read_failure_refuses_the_move(self) -> None:
        identity = self.entry["session_id"]
        with (patch.object(stop_resume, "_tab_of", return_value=stop_resume.SessionNotFound(identity)),
              patch.object(LaunchNames, "read", side_effect=RuntimeError("names failed")) as read,
              patch.object(stop_resume, "_stop") as stopped,
              patch.object(stop_resume, "resume") as resumed):
            self.assertEqual(stop_resume.move_sessions([identity], "claude4", why="move"),
                             [stop_resume.StopFailed(identity, "could not record the move request: names failed")])
        read.assert_called_once()
        stopped.assert_not_called()
        resumed.assert_not_called()
        self.assertFalse(moves.store_file().exists())

    def test_outcome_write_failure_does_not_lose_the_batch_results(self) -> None:
        results = [stop_resume.ClaudeUp("bad", "bad-pane", "claude3"),
                   stop_resume.ClaudeUp("good", "good-pane", "claude4")]

        def resume_one(identity: str, *, why: str, to: str, client: herdr.Herdr) -> stop_resume.ClaudeUp:
            self.assertIs(client, self.client)
            self.assertEqual((why, to), ("move", "claude4"))
            return results[0] if identity == "bad" else results[1]

        append = moves.append_move
        def fail_one(entry: moves.MoveWriteEntry) -> None:
            if entry["kind"] == "confirmed" and entry["subject"] == "bad":
                raise OSError("TEST receipt write failed")
            append(entry)

        with (patch.object(moves, "append_move", side_effect=fail_one),
              patch.object(stop_resume, "_stop", side_effect=stop_resume.NotRunning),
              patch.object(stop_resume, "resume", side_effect=resume_one),
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(stop_resume.move_sessions(["bad", "good"], "claude4", why="move"), results)
        entries = moves.read_moves()
        self.assertEqual([entry["kind"] for entry in entries], ["requested", "requested", "confirmed"])
        self.assertEqual(entries[-1]["subject"], "good")
        self.assertIn("bad: move log anomaly:", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertEqual(output.getvalue().count("session home is unknown"), 2)

    def test_batch_stops_all_before_any_resume_and_uses_each_own_pane(self) -> None:
        self.add_beta()
        third = pane_record.launch("w1:p6", "sid-gamma", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude3")}, ["claude", "-n", "gamma"])
        pane_record.write("agents", third)
        self.client.rows.append({"pane_id": "w1:p6", "terminal_id": "gamma-terminal", "label": "gamma", "workspace_id": "w1",
                                 "agent_session": {"kind": "id", "value": "sid-gamma"}})
        both_stopping = Barrier(3, timeout=3)
        both_resuming = Barrier(3, timeout=3)
        stopped: list[str] = []
        lock = Lock()
        def stop_one(session: str) -> stop_resume.Stopped:
            _ = both_stopping.wait()
            with lock:
                stopped.append(session)
            return stop_resume.Stopped(session, 123)
        def run(*_command: str) -> str:
            self.assertCountEqual(stopped, ["019b3a40-4250-7000-8000-000000000102", "sid-beta", "sid-gamma"])
            _ = both_resuming.wait()
            return ""
        self.start.side_effect = run
        with patch.object(stop_resume, "_stop", side_effect=stop_one):
            results = stop_resume.move_sessions(["019b3a40-4250-7000-8000-000000000102", "sid-beta", "sid-gamma"], "claude4", why="limit")
        self.assertEqual(results, [stop_resume.ClaudeUp("019b3a40-4250-7000-8000-000000000102", "w1:p2", "claude4"),
                                   stop_resume.ClaudeUp("sid-beta", "w1:p4", "claude4"),
                                   stop_resume.ClaudeUp("sid-gamma", "w1:p6", "claude4")])
        calls = [self._typed(cast(tuple[str, ...], invocation.args)) for invocation in self.start.call_args_list]
        self.assertEqual({args[2]: args[args.index("--resume") + 1] for args in calls},
                         {"w1:p2": "019b3a40-4250-7000-8000-000000000102", "w1:p4": "sid-beta", "w1:p6": "sid-gamma"})

    def test_batch_failures_do_not_stop_other_members(self) -> None:
        def stop_one(session: str) -> stop_resume.NotRunning:
            if session == "stop-error":
                raise OSError("cannot signal")
            return stop_resume.NotRunning(session)
        def resume_one(session: str, *, why: str, to: str,
                       client: herdr.Herdr) -> stop_resume.ClaudeUp | stop_resume.SessionNotFound:
            self.assertIs(client, self.client)
            self.assertEqual(why, "limit")
            if session == "resume-error":
                raise OSError("cannot run")
            if session == "gone-tab":
                return stop_resume.SessionNotFound(session)
            self.launched.add(session)
            return stop_resume.ClaudeUp(session, "own-pane", to)
        with patch.object(stop_resume, "_stop", side_effect=stop_one), patch.object(stop_resume, "resume", side_effect=resume_one):
            results = stop_resume.move_sessions(["stop-error", "resume-error", "gone-tab", "good"], "claude4", why="limit")
        self.assertEqual(results, [stop_resume.StopFailed("stop-error", "cannot signal"),
            stop_resume.ResumePending("resume-error", "claude4", "cannot run"),
            stop_resume.ResumePending("gone-tab", "claude4", "session has no tab after stop"), stop_resume.ClaudeUp("good", "own-pane", "claude4")])

    def test_two_watcher_failures_send_one_phone_alert(self) -> None:
        rows = tuple(staff.StaffRow(staff.StaffId(f"staff-{index}"), "showrunner", f"runner-{index}-" + "x" * 70,
                                   staff.StaffRoot(), current_session=staff.StaffId(f"native-{index}"))
                     for index in range(10))
        record = staff.StaffSnapshot(rows)
        selected = [str(row.current_session) for row in rows]
        attempts: list[str] = []
        posted: list[str] = []
        receipt = self.root / "push-receipt"

        def phone(_url: str, data: bytes) -> tuple[int, bytes]:
            posted.append(parse_qs(data.decode())["message"][0])
            return 200, b'{"status":1}'

        def send(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[command.index("--repeat-minutes") + 1],
                             str(conf.read("shutdown").whole("MOVE_ALERT_REPEAT_MINUTES")))
            action = next(argument.removeprefix("action=") for argument in command if argument.startswith("action="))
            self.assertLessEqual(len(action), pushover.ACTION_TEXT_MAX)
            key = command[command.index("--key") + 1]
            attempts.append(key)
            if receipt.is_file() and receipt.read_text() == key:
                return subprocess.CompletedProcess(command, 0, "SKIPPED: repeat window\n", "")
            _ = receipt.write_text(key)
            values = {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in command
                      if entry.partition("=")[0] in ("sender", "action", "body", "title")}
            push = render("alert.decision", **values)
            full = render(push.id, **(values | {"body": "These sessions did not finish their move for showrunners: "
                                                + ", ".join(row.name for row in rows) + "."}))
            self.assertGreater(len(full.message), pushover.phone_message_limit(full.sender, full.id))
            self.assertLessEqual(len(full.message), pushover.MESSAGE_MAX)
            with (patch.object(pushover, "read_keys", return_value={"user": "fixture", "token": "fixture"}),
                  patch.object(pushover, "log"), redirect_stdout(io.StringIO())):
                self.assertEqual(pushover.post_message(push.sender, "1", push.title, push.message,
                                                       self.root / "phone-keys", phone, "", key, push.id), 0)
            return subprocess.CompletedProcess(command, 0, "SENT: fixture\n", "")

        def stop_member(reference: str) -> stop_resume.NotRunning:
            member = record.by_id(staff.StaffId(reference))
            assert member is not None
            return stop_resume.NotRunning(member.current_session)

        def fail(identity: str, **_options: object) -> stop_resume.ResumeFailed:
            return stop_resume.ResumeFailed(identity, "fixture start failed")

        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(staff, "snapshot", return_value=record),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "_stop", side_effect=stop_member) as stop,
              patch.object(stop_resume, "resume", side_effect=fail),
              patch.object(accounts, "current_move_account", return_value="claude4"),
              patch.object(stop_resume, "_send_move_push", SEND_MOVE_PUSH),
              patch.object(remote, "local_machine", return_value="natedev"),
              patch.object(showrunners, "tell", return_value="told fixture") as tell,
              patch.object(subprocess, "run", side_effect=send)):
            for _pass in range(2):
                self.assertEqual(stop_resume.move_sessions(selected, "claude4", why="move", requester="director", cause="auto",
                                                          intent=moves.FollowGroupAccount("showrunners")),
                                 [stop_resume.ResumePending(identity, "claude4", "fixture start failed")
                                  for identity in selected])
            with redirect_stdout(io.StringIO()) as result:
                self.assertEqual(stop_resume.main(["result"]), 0)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(posted), 1)
        self.assertEqual(attempts, ["account-move:showrunners:claude4"] * 2)
        self.assertEqual(stop.call_count, 20)
        self.assertEqual(tell.call_count, 20)
        shown = posted[0]
        self.assertIn("stop_resume.py retry", shown)
        self.assertIn("stop_resume.py result", shown)
        self.assertIn("10 sessions did not finish their move for showrunners.", shown)
        self.assertTrue(shown.endswith("Sent by: shutdown · Template: alert.decision"))
        self.assertLessEqual(len(shown), pushover.MESSAGE_MAX)
        self.assertEqual(len(moves.pending_move_requests()), len(rows))
        for row in rows:
            self.assertIn(f"{row.name}: group:showrunners", result.getvalue().splitlines())
            self.assertNotIn(row.current_session, shown)
        self.assertEqual([entry["kind"] for entry in moves.read_moves()].count("failed"), 20)

    def test_a_move_by_hand_takes_a_busy_row_when_its_operation_ends(self) -> None:
        busy = staff.StaffId("busy")
        rows = staff.StaffSnapshot((
            staff.StaffRow(busy, "showrunner", "busy", staff.StaffRoot(), current_session=busy),
        ))
        original = staff.slot_operation
        attempts = 0
        stopped: list[str] = []

        @contextmanager
        def acquire(key: str, name: str, *, join: bool = True) -> Generator[None]:
            nonlocal attempts
            if key == busy:
                attempts += 1
                if attempts == 1:
                    raise staff.StaffRefused("another move holds this row")
                # The free row moved before the wait for the busy row.
                self.assertEqual(stopped, ["available"])
            with original(key, name, join=join):
                yield

        def stop(identity: str) -> stop_resume.NotRunning:
            stopped.append(identity)
            return stop_resume.NotRunning(identity)

        up = stop_resume.ClaudeUp("available", "own-pane", "claude4")
        with (patch.object(staff, "initial_snapshot", return_value=rows),
              patch.object(staff, "slot_operation", side_effect=acquire),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_stop", side_effect=stop),
              patch.object(stop_resume, "_resume_move", return_value=up),
              patch.object(stop_resume, "_send_move_push") as send):
            results = stop_resume.move_sessions([busy, "available"], "claude4", why="move", user_command=True)
        self.assertEqual(results, [up, up])
        self.assertEqual(attempts, 2)
        self.assertEqual(stopped, ["available", busy])
        self.detach.assert_not_called()
        send.assert_not_called()
        self.assertEqual(moves.pending_move_requests(), ())

    def test_busy_batch_prints_its_own_target_instead_of_retrying_the_saved_move(self) -> None:
        identity = self.entry["session_id"]
        saved = moves.LatestMoveRequest("a" * 32, "claude4")
        key = moves.native_row_key(identity)
        log = self.root / "busy-move.log"
        with (staff.session_operation(identity, native_key=key), staff.mark_row_move(key, "claude4"),
              patch.object(stop_resume, "_load_batch_modules"), patch.object(stop_resume, "_stop") as stopped):
            moves.write_latest_request(identity, saved, name="alpha")
            self.assertEqual(stop_resume.main(["batch", "--target", "claude3", "--log", str(log), "--", identity]), 1)
        stopped.assert_not_called()
        self.assertEqual(moves.read_latest_request(identity), saved)
        line, = log.read_text().splitlines()
        self.assertIn("another operation holds this row", line)
        self.assertIn(f"batch --target claude3 -- {identity}", line)
        self.assertNotIn(moves.RETRY_COMMAND, line)

    def test_busy_group_command_moves_to_the_current_group_account(self) -> None:
        identity = self.entry["session_id"]
        key = moves.native_row_key(identity)
        log = self.root / "group-move.log"
        selected = role_map.RoleMap(role_map.StoredGroupSelections(showrunners="claude3"), "codex1")

        def read_role() -> role_map.RoleMap:
            self.assertIsInstance(staff.row_activity(key), staff.RowBusy)
            return selected

        def resume_member(session: str, target: str, *_args: object) -> stop_resume.ClaudeUp:
            return stop_resume.ClaudeUp(session, "own-pane", target)

        with (patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_stop", side_effect=stop_resume.NotRunning) as stop,
              patch.object(stop_resume, "_resume_move", side_effect=resume_member) as resume):
            with staff.session_operation(identity, native_key=key):
                waiting, = stop_resume.move_sessions([identity], "claude4", why="move",
                    intent=moves.FollowGroupAccount("showrunners"))
            assert isinstance(waiting, moves.MoveWaitingForRow)
            stop.assert_not_called()
            command = shlex.split(waiting.command)[2:]
            with (patch.object(role_map, "is_role_map_host", return_value=True),
                  patch.object(role_map, "_local_role_map", side_effect=read_role) as read):
                self.assertEqual(stop_resume.main([command[0], "--log", str(log), *command[1:]]), 0)
        self.assertEqual(resume.call_args.args[1], "claude3")
        self.assertEqual(command[1:3], ["--group", "showrunners"])
        self.assertNotIn("--target", command)
        read.assert_called_once()

    def test_busy_batch_command_creates_the_default_move_log_directory(self) -> None:
        identity = self.entry["session_id"]
        state = self.root / "missing" / "shutdown"
        moved = stop_resume.ClaudeUp(identity, "own-pane", "claude4")
        with (patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(state)}),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_stop", side_effect=stop_resume.NotRunning) as stop,
              patch.object(stop_resume, "_resume_move", return_value=moved)):
            with staff.session_operation(identity, native_key=moves.native_row_key(identity)):
                waiting, = stop_resume.move_sessions([identity], "claude4", why="move")
            assert isinstance(waiting, moves.MoveWaitingForRow)
            stop.assert_not_called()
            self.assertFalse(state.parent.exists())
            command = shlex.split(waiting.command)[2:]
            self.assertNotIn("--log", command)
            self.assertEqual(stop_resume.main(command), 0)
        stop.assert_called_once()
        self.assertIn('"result": "ClaudeUp"', (state / "move.log").read_text())

    def test_same_target_move_reports_in_progress_without_stopping_its_member(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed"), "showrunner", "renamed", staff.StaffRoot(),
                             current_session=staff.StaffId("current"))
        with (staff.slot_operation(row.id, row.name), staff.mark_row_move(row.id, "claude4"),
              patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(stop_resume, "_stop") as stop):
            result = stop_resume.move_sessions([row.name], "claude4", why="move")
        self.assertEqual(result, [moves.MoveInProgress(row.name, "claude4")])
        stop.assert_not_called()

    def test_delayed_group_handoff_cannot_reverse_a_newer_move(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed"), "showrunner", "fixture", staff.StaffRoot(),
                             current_session=staff.StaffId("current"))
        up = stop_resume.ClaudeUp(row.current_session, "fixture-pane", "claude3")
        cached = role_map.RoleMap(role_map.StoredGroupSelections(showrunners="claude4"), "codex1")
        latest = role_map.RoleMap(role_map.StoredGroupSelections(showrunners="claude3"), "codex1")

        role_reads = iter((cached, latest))

        def read_role() -> role_map.RoleMap:
            self.assertIsInstance(staff.row_activity(row.id), staff.RowBusy)
            return next(role_reads)

        with (role_map.supplied_role_map(cached),
              patch.object(role_map, "is_role_map_host", return_value=True),
              patch.object(role_map, "_local_role_map", side_effect=read_role) as read,
              patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=stop_resume.LiveMoveSource(up, 123, "generation-1")),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_stop") as stop,
              patch.object(stop_resume, "_send_move_push") as push):
            with stop_resume.detached_moves():
                result = stop_resume.move_sessions([row.id], "claude4", why="move",
                    intent=moves.FollowGroupAccount("showrunners"))
            self.assertEqual(result, [stop_resume.MoveHandedOff(row.id)])
            read.assert_called_once()
            self.detach.assert_called_once()
            argv = cast(list[str], self.detach.call_args.args[0])
            self.assertIn("--group", argv)
            self.assertEqual(argv[argv.index("--target") + 1], "claude4")
            log = self.root / "move.log"
            self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 0)
        self.assertEqual(read.call_count, 2)
        self.assertIn('"account": "claude3"', log.read_text())
        stop.assert_not_called()
        push.assert_not_called()

    def test_handoff_moves_to_the_current_role_without_a_prior_row_marker(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed"), "showrunner", "fixture", staff.StaffRoot(),
                             current_session=staff.StaffId("current"))
        up = stop_resume.ClaudeUp(row.current_session, "fixture-pane", "claude3")
        intent = moves.FollowGroupAccount("showrunners")
        request_id = "b" * 32
        moves.write_latest_request(row.current_session, moves.LatestMoveRequest(request_id, "group:showrunners"))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "current_move_account", return_value="claude3"),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(row.current_session)) as stop,
              patch.object(stop_resume, "_resume_move", return_value=up) as resume):
            result = stop_resume.move_sessions([row.id], "claude4", why="move",
                intent=intent, request=stop_resume.DetachedMoveRequest(request_id, frozenset({row.id})))
        self.assertEqual(result, [up])
        stop.assert_called_once_with(row.id)
        self.assertEqual(resume.call_args.args[:2], (row.current_session, "claude3"))

    def test_detached_group_checks_the_destination_after_its_warning(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed"), "showrunner", "fixture", staff.StaffRoot(),
                             current_session=staff.StaffId("current"))
        destination = self.root / ".claude4"
        notes = self.root / "notes"
        notes.mkdir(exist_ok=True)
        _ = (notes / "claude4.md").write_text("---\nlogin: fixture@example.invalid\n---\n")
        _ = (destination / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "fixture@example.invalid"},
        }))
        write_claude_move_credentials(destination)
        self.assertIsInstance(CHECK_MOVE_TARGET("claude4", datetime.now(timezone.utc), repair=False),
                              accounts.MoveTargetReady)
        log = self.root / "move.log"
        batch_id = "d" * 32
        moves.write_latest_request(row.current_session, moves.LatestMoveRequest(batch_id, "group:showrunners"))

        def wait(*_args: object) -> None:
            _ = destination.rename(self.root / "unavailable")

        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(accounts, "current_move_account", return_value="claude4"),
              patch.object(accounts, "check_move_target", wraps=CHECK_MOVE_TARGET) as check,
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_warn_moves", side_effect=wait),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_stop") as stop,
              patch.object(stop_resume, "_resume_move") as resume,
              patch.object(stop_resume, "_send_move_push") as send):
            code = stop_resume.main(["batch", "--group", "showrunners", "--cause", "auto", "--warn-seconds", "1", "--request-id", batch_id, "--staff-member", row.id, "--log", str(log), "--", row.id])
        self.assertEqual(code, 1)
        check.assert_called_once()
        stop.assert_not_called()
        resume.assert_not_called()
        send.assert_called_once()
        self.assertIn("claude4 has no usable account directory", log.read_text())
        self.assertEqual(moves.read_latest_request(row.current_session), moves.LatestMoveRequest(batch_id, "group:showrunners"))

    def test_move_rechecks_target_sign_in_before_request_or_stop(self) -> None:
        identity = self.entry["session_id"]
        destination = self.root / ".claude4"
        notes = self.root / "notes"
        notes.mkdir(exist_ok=True)
        _ = (notes / "claude4.md").write_text("---\nlogin: fixture@example.invalid\n---\n")
        _ = (destination / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "fixture@example.invalid"},
        }))
        write_claude_move_credentials(destination)
        self.assertIsInstance(CHECK_MOVE_TARGET("claude4", datetime.now(timezone.utc), repair=False),
                              accounts.MoveTargetReady)

        def lose_sign_in(row: stop_resume.LockedMoveRow, account: str, record: staff.StaffSnapshot, *,
                         check_presence: bool) -> object:
            _ = (destination / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
                "refreshTokenExpiresAt": int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
            }}))
            return REAL_PREPARE_MOVE(row, account, record, check_presence=check_presence)

        with (patch.object(accounts, "check_move_target", wraps=CHECK_MOVE_TARGET),
              patch.object(stop_resume, "_prepare_move", side_effect=lose_sign_in),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)) as stopped,
              patch.object(stop_resume, "_resume_move", return_value=stop_resume.ClaudeUp(identity, "pane", "claude4"))):
            result = stop_resume.move_sessions([identity], "claude4", why="move", cause="auto")
        failed, = result
        self.assertIsInstance(failed, stop_resume.StopFailed)
        assert isinstance(failed, stop_resume.StopFailed)
        self.assertIn("claude4 has no sign-in on this machine", failed.reason)
        stopped.assert_not_called()
        self.assertEqual(moves.read_moves(), [])

    def test_a_detached_native_move_switches_to_the_staff_lock_on_admission(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed"), "showrunner", "fixture", staff.StaffRoot(),
                             current_session=staff.StaffId("current"))
        record = staff.StaffSnapshot(())
        native_key = moves.native_row_key(row.current_session)
        original = staff.slot_operation

        @contextmanager
        def acquire(key: str, name: str, *, join: bool = True) -> Generator[None]:
            nonlocal record
            with original(key, name, join=join):
                if key == native_key:
                    record = staff.StaffSnapshot((row,))
                yield

        def stop(reference: str) -> stop_resume.NotRunning:
            self.assertEqual(reference, row.id)
            self.assertIsInstance(staff.row_activity(native_key), staff.RowBusy)
            self.assertEqual(staff.row_activity(row.id), staff.RowMoving("claude4"))
            return stop_resume.NotRunning(row.current_session)

        up = stop_resume.ClaudeUp(row.current_session, "fixture-pane", "claude4")
        request_id = "c" * 32
        moves.write_latest_request(row.current_session, moves.LatestMoveRequest(request_id, "claude4"))
        with (patch.object(staff, "initial_snapshot", side_effect=lambda: record),
              patch.object(staff, "slot_operation", side_effect=acquire),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_stop", side_effect=stop) as stopped,
              patch.object(stop_resume, "_resume_move", return_value=up)):
            result = stop_resume.move_sessions([row.current_session], "claude4", why="move",
                request=stop_resume.DetachedMoveRequest(request_id, frozenset()))
        self.assertEqual(result, [up])
        stopped.assert_called_once_with(row.id)

    def test_staff_and_conversation_references_share_one_stop_and_resume(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed"), "showrunner", "fixture", staff.StaffRoot(),
                             current_session=staff.StaffId("current"))
        up = stop_resume.ClaudeUp(row.current_session, "fixture-pane", "claude4")
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(row.current_session)) as stop,
              patch.object(stop_resume, "_resume_move", return_value=up) as resume):
            results = stop_resume.move_sessions([row.current_session, row.id], "claude4", why="move")
        self.assertEqual(results, [up, up])
        stop.assert_called_once_with(row.id)
        resume.assert_called_once()

    def test_handoffs_report_busy_rows_without_sending_them_to_the_child(self) -> None:
        rows = tuple(staff.StaffRow(staff.StaffId(name), "showrunner", name, staff.StaffRoot(),
                                   current_session=staff.StaffId(f"native-{name}"))
                     for name in ("busy", "available"))
        busy, available = rows
        up = stop_resume.ClaudeUp(available.current_session, "fixture-pane", "claude4")
        log = self.root / "move.log"
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": available.current_session}),
              patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(available.current_session)) as stop,
              patch.object(stop_resume, "_resume_move", return_value=up),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "_send_move_push") as send):
            with staff.slot_operation(busy.id, busy.name), staff.mark_row_move(busy.id, "claude3"):
                results = stop_resume.move_sessions([busy.id, available.id], "claude4", why="move", cause="auto")
            self.assertIsInstance(results[0], moves.MoveWaitingForRow)
            self.assertIsInstance(results[1], stop_resume.MoveHandedOff)
            send.assert_not_called()
            self.detach.assert_called_once()
            argv = cast(list[str], self.detach.call_args.args[0])
            self.assertEqual(argv[argv.index("--") + 1:], [available.current_session])
            self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 0)
        stop.assert_called_once_with(available.id)
        send.assert_not_called()
        self.assertEqual(moves.pending_move_requests(), ())

    def test_a_self_move_by_hand_sends_its_held_rows_to_the_child_which_gives_the_batch_command(self) -> None:
        rows = tuple(staff.StaffRow(staff.StaffId(name), "showrunner", name, staff.StaffRoot(),
                                   current_session=staff.StaffId(f"native-{name}"))
                     for name in ("busy", "available"))
        busy, available = rows
        up = stop_resume.ClaudeUp(available.current_session, "fixture-pane", "claude4")
        log = self.root / "move.log"
        number = conf.ConfigRead.number
        clock = [120.0]

        def short_move_limit(settings: conf.ConfigRead, key: str) -> float:
            return 1.0 if key == "move.stuck_seconds" else number(settings, key)

        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": available.current_session}),
              patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(rows)),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(available.current_session)) as stop,
              patch.object(stop_resume, "_resume_move", return_value=up),
              patch.object(stop_resume, "_load_batch_modules"),
              patch.object(stop_resume, "monotonic", side_effect=lambda: clock[0]),
              patch.object(conf.ConfigRead, "number", new=short_move_limit)):
            with staff.slot_operation(busy.id, busy.name):
                results = stop_resume.move_sessions([busy.id, available.id], "claude4", why="move", user_command=True)
                self.assertTrue(all(isinstance(result, stop_resume.MoveHandedOff) for result in results), results)
                self.detach.assert_called_once()
                argv = cast(list[str], self.detach.call_args.args[0])
                self.assertIn("--user-command", argv)
                self.assertEqual(argv[argv.index("--move-deadline") + 1], "121.0")
                self.assertIsInstance(cast(object, self.detach.call_args.kwargs["log_fd"]), int)
                self.assertEqual(set(argv[argv.index("--") + 1:]), {busy.current_session, available.current_session})
                clock[0] = 122.0
                with patch.object(time, "sleep", side_effect=AssertionError("the handoff extended the deadline")):
                    self.assertEqual(stop_resume.main(["batch", "--log", str(log), *argv[1:]]), 1)
        stop.assert_called_once_with(available.id)
        text = log.read_text()
        self.assertIn(moves.batch_move_command(busy.current_session, "claude4", user_command=True), text)
        self.assertNotIn(moves.RETRY_COMMAND, text)
        self.assertEqual(moves.pending_move_requests(), ())

    def test_a_dead_owner_target_is_not_a_live_move_on_a_busy_row(self) -> None:
        row = staff.StaffRow(staff.StaffId("fixed"), "showrunner", "fixture", staff.StaffRoot(),
                             current_session=staff.StaffId("current"))
        with staff.slot_operation(row.id, row.name):
            lock = self.root / "rows" / f"{row.id}.lock"
            _ = lock.write_text(json.dumps({"at": 1, "pid": 73, "start": "old", "target": "claude4"}))
            with patch.object(process_start, "process_start", return_value=process_start.Started("replacement")):
                self.assertEqual(staff.row_activity(row.id), staff.RowBusy())

    def test_freestanding_resume_failure_uses_the_saved_name(self) -> None:
        identity = self.entry["session_id"]
        with (patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(identity)),
              patch.object(stop_resume, "resume", return_value=stop_resume.ResumeFailed(identity, "fixture")),
              patch.object(stop_resume, "_send_move_push") as send):
            result = stop_resume.move_sessions([identity], "claude4", why="move")
        self.assertEqual(result, [stop_resume.ResumePending(identity, "claude4", "fixture")])
        send.assert_called_once()
        pushed = cast(RenderedPush, send.call_args.args[0])
        member = moves.MoveMember(identity, "alpha")
        self.assertIn(member.name, pushed.values["body"])
        self.assertNotIn(identity, pushed.values["body"])
        self.assertEqual(tuple(item.member for item in moves.pending_move_requests()), (member,))

    def test_retry_keeps_units_with_equal_names_in_distinct_productions(self) -> None:
        parents = tuple(staff.StaffRow(staff.StaffId(name), "showrunner", name, staff.StaffRoot(),
                                      current_session=staff.StaffId(f"native-{name}"))
                        for name in ("orchard", "studio"))
        units = tuple(staff.StaffRow(staff.StaffId(f"{parent.id}-build"), "unit", "build",
                                    staff.ReportsTo(parent.id), current_session=staff.StaffId(f"native-{parent.id}-build"))
                      for parent in parents)
        record = staff.StaffSnapshot((*parents, *units))
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "_stop", side_effect=partial(stop_resume.StopFailed, reason="fixture")),
              patch.object(stop_resume, "_send_move_push") as send):
            _ = stop_resume.move_sessions([row.id for row in units], "claude4", why="move")
        send.assert_called_once()
        pushed = cast(RenderedPush, send.call_args.args[0])
        self.assertIn("orchard/build, studio/build", pushed.values["body"])
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
              patch.object(stop_resume, "move_sessions", return_value=[stop_resume.ClaudeUp("unit", "pane", "claude4")]) as move):
            self.assertEqual(stop_resume.main(["retry"]), 0)
        self.assertCountEqual([call.args for call in move.call_args_list], [([row.current_session], "claude4") for row in units])
        for invoked in move.call_args_list:
            request = cast(moves.PendingMoveRequest, invoked.kwargs["request"])
            self.assertEqual(request.member.reference, cast(list[str], invoked.args[0])[0])

    def test_cli_prints_one_line_and_uses_the_result_exit_status(self) -> None:
        for arguments, expected in ((["stop", "alpha"], 0), (["resume", "alpha", "--why", "maintenance"], 0),
                                    (["stop", "elsewhere"], 2)):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(arguments), expected)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.launched.clear()
        self.live.return_value = sessions.LiveSessionProcess(123)
        self.client.native_ids["w1:p2"] = self.entry["session_id"]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["resume", "alpha", "--why", "maintenance"]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)


    def test_own_pane_resume_by_old_tab_label_keeps_the_fixed_staff_reference(self) -> None:
        identity = self.entry["session_id"]
        row = staff.StaffRow(staff.StaffId("fixed-alpha"), "producer", "renamed-alpha", staff.STAFF_ROOT,
                             current_session=staff.StaffId(identity))
        self.tab_titles["w1:t9"] = "alpha"
        retained = pane_launch.retained_launch(self.entry, staff.StaffSnapshot((row,)))
        pane_record.save("agents", {"w1:p2": retained}, record=staff.StaffSnapshot((row,)))
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
              patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}),
              patch.object(stop_resume, "_exec_in_own_pane", side_effect=SystemExit) as execute):
            with self.assertRaises(SystemExit):
                _ = stop_resume.resume(str(row.id))
        self.assertEqual(pane_record.read("agents")["w1:p2"].get("staff_id"), row.id)
        execute.assert_called_once()
        launched = cast(pane_launch.CurrentLaunch, execute.call_args.args[1])
        self.assertEqual(launched["session_id"], identity)
        self.assertEqual(launched["name"], row.name)


REAL_PREPARE_MOVE = stop_resume._prepare_move  # pyright: ignore[reportPrivateUsage]
REAL_MOVE_NOW = stop_resume._move_now  # pyright: ignore[reportPrivateUsage]
REAL_HUNG_ROW_HOLDER = stop_resume._hung_row_holder  # pyright: ignore[reportPrivateUsage]


@final
class MoveNowTests(unittest.TestCase):
    """A move stops and moves each session at once; a move by hand wins over an automatic move on the row."""
    moved: list[list[str]] = []
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        configuration = self.root / "config"
        environment = config_environment(configuration)
        environment.update(HOME=str(self.root), STAFF_FILE=str(self.root / "staff.json"), HERDR_BIN=MISSING_HERDR_BIN)
        _ = write_config_fixture(configuration / "shutdown.conf", "MOVE_STOP_MARGIN_SECONDS=3\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment)))
        _ = self.enterContext(patch.object(sys, "platform", "linux"))
        self.moved = []

        def move_now(selected: list[str], target: str, **_options: object) -> list[stop_resume.MoveResult]:
            self.moved.append(list(selected))
            return [self.up(name, target) for name in selected]

        _ = self.enterContext(patch.object(stop_resume, "_move_now", side_effect=move_now))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        _ = self.enterContext(patch.object(pane_launch, "current_kind", return_value="claude"))
        _ = self.enterContext(patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound))
        _ = self.enterContext(patch.object(stop_resume, "running_on_account", return_value=sessions.SessionNotLive()))
        _ = self.enterContext(patch.object(stop_resume, "_send_move_push"))
        _ = self.enterContext(patch.object(time, "sleep"))

    @staticmethod
    def up(name: str, target: str = "claude3") -> stop_resume.ClaudeUp:
        return stop_resume.ClaudeUp(name, "w1:p1", target)

    def verify_call(self, session: str = "alpha", *, pid: int = 4242, start: str = "123") -> None:
        record = step_record.StepRecord("v20261116T225500-4242-1", pid, start, Path("/w"), "build",
            step_record.NamedPackage("hana"), step_record.NatedevOnly("local"),
            step_record.StepState.ON_NATEDEV, "2026-11-16T22:55:00+00:00")
        step_record.write(record)
        process = self.root / "proc" / str(pid)
        process.mkdir(parents=True, exist_ok=True)
        _ = (process / "stat").write_text(f"{pid} (bash) S 1 " + "0 " * 17 + start + "\n")
        _ = (process / "environ").write_bytes(f"CLAUDE_CODE_SESSION_ID={session}\0".encode())

    def hold_row(self, session: str = "alpha") -> ExitStack:
        holder = ExitStack()
        self.addCleanup(holder.close)
        _ = holder.enter_context(staff.session_operation(session, native_key=moves.native_row_key(session)))
        return holder

    @contextmanager
    def hung_operation(self, command: str, *, row: str = "alpha",
                       ignore_term: bool = False) -> Generator[subprocess.Popen[str]]:
        code = """
import signal
import sys
from contextlib import ExitStack
from scripts.accounts import moves
from scripts.agents import shutdown_record
from scripts.production import staff

if sys.argv[-1] == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
row = sys.argv[2]
operation = (shutdown_record.active_operation() if sys.argv[1].startswith("restart")
             else open(sys.argv[-1], sys.argv[1]) if sys.argv[1] in ("rb", "a+b")
             else staff.slot_operation(moves.native_row_key(row), row))
with operation as activity, ExitStack() as locks:
    if isinstance(activity, shutdown_record.RestartActivity):
        if sys.argv[1] == "restart-slot":
            locks.enter_context(staff.slot_operation(row, row))
        else:
            activity.claim_rows([row])
            if sys.argv[1] == "restart-released":
                activity.release_rows([row])
    print("ready", flush=True)
    signal.pause()
"""
        checkout = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env.update(SHUTDOWN_STATE_DIR=str(self.root / "shutdown"), VERIFY_PROC_ROOT="/proc")
        slot = self.root / "rows" / f"{moves.native_row_key('alpha')}.lock"
        argument = str(slot) if command in ("rb", "a+b") else "ignore" if ignore_term else ""
        with subprocess.Popen([sys.executable, "-c", code, command, row, argument],
                              cwd=checkout, env=env, stdout=subprocess.PIPE, text=True) as child:
            try:
                assert child.stdout is not None
                ready, _, _ = select.select([child.stdout], [], [], 5)
                self.assertTrue(ready, "the fixture command did not start")
                self.assertEqual(child.stdout.readline(), "ready\n")
                def fixture_process(pid: int) -> bool:
                    return pid == child.pid

                with patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": env["SHUTDOWN_STATE_DIR"]}):
                    # The fixture owns the child. A real move runs in a separate process.
                    with patch.object(lock_holder, "_outside_move", new=fixture_process):
                        yield child
            finally:
                if child.poll() is None:
                    child.kill()
                _ = child.wait(timeout=5)

    def move_at_the_limit(self, reference: str = "alpha", *, source_account: str = "") -> stop_resume.MoveResult:
        result, = self.moves_at_the_limit(reference, source_account=source_account)
        return result

    def moves_at_the_limit(self, *references: str, source_account: str = "",
                           grace_seconds: float = 0.2) -> list[stop_resume.MoveResult]:
        clock = [0.0]
        number = conf.ConfigRead.number
        text = conf.ConfigRead.text

        def value(settings: conf.ConfigRead, key: str) -> float:
            return 0.01 if key == "move.stuck_seconds" else number(settings, key)

        def text_value(settings: conf.ConfigRead, key: str, *, recover_invalid: bool = True) -> str:
            return f"{grace_seconds:g}" if key == "KILL_GRACE_SECONDS" else text(settings, key, recover_invalid=recover_invalid)

        def advance(seconds: float) -> None:
            clock[0] += seconds

        with (patch.object(conf.ConfigRead, "number", new=value),
              patch.object(conf.ConfigRead, "text", new=text_value),
              patch.object(stop_resume, "monotonic", side_effect=lambda: clock[0]),
              patch.object(time, "sleep", side_effect=advance)):
            return stop_resume.move_sessions(list(references), "claude3", why="by hand", user_command=True,
                                             source_accounts=dict.fromkeys(references, source_account))

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_ends_a_proved_slot_holder_and_moves_the_row(self) -> None:
        with self.hung_operation("resume") as child, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.move_at_the_limit(), self.up("alpha"))
            self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(self.moved, [["alpha"]])
        self.assertEqual(output.getvalue(), f"alpha: ended a hung resume (pid {child.pid}) after 0.01 s "
                         + "and moved it to claude3\n")

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_uses_the_kernel_lock_when_the_holder_record_is_stale(self) -> None:
        with (self.hung_operation("resume") as child, self.hung_operation("a+b") as waiter,
              self.hung_operation("rb") as reader):
            slot = self.root / "rows" / f"{moves.native_row_key('alpha')}.lock"
            _ = slot.write_text(json.dumps({"pid": 2147483647, "start": "gone", "command": "restart"}))

            def fixture_process(pid: int) -> bool:
                return pid in (child.pid, waiter.pid, reader.pid)

            with patch.object(lock_holder, "_outside_move", new=fixture_process):
                self.assertEqual(self.move_at_the_limit(), self.up("alpha"))
            self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
            self.assertIsNone(waiter.poll())
            self.assertIsNone(reader.poll())
        self.assertEqual(self.moved, [["alpha"]])

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_leaves_a_holder_with_a_wrong_record_start(self) -> None:
        with self.hung_operation("resume") as child:
            path = self.root / "rows" / f"{moves.native_row_key('alpha')}.lock"
            _ = path.write_text(json.dumps({"pid": child.pid, "start": "wrong start", "command": "resume alpha"}))
            with patch.object(signal, "pidfd_send_signal", side_effect=AssertionError("unexpected signal")):
                result = self.move_at_the_limit()
            self.assertIsNone(child.poll())
        assert isinstance(result, moves.MoveWaitingForRow)
        self.assertIn(f"pid {child.pid}: resume alpha", result.reason)
        self.assertEqual(result.command, moves.batch_move_command("alpha", "claude3", user_command=True))
        self.assertEqual(self.moved, [])

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_reports_a_holder_that_stays_live_after_kill(self) -> None:
        sent: list[int] = []

        def signal_fixture(_descriptor: int, number: int) -> None:
            sent.append(number)

        with (self.hung_operation("start", ignore_term=True) as child,
              patch.object(signal, "pidfd_send_signal", new=signal_fixture)):
            result = self.move_at_the_limit()
            self.assertIsNone(child.poll())
        assert isinstance(result, moves.MoveWaitingForRow)
        self.assertIn(f"pid {child.pid}:", result.reason)
        self.assertEqual(result.command, moves.batch_move_command("alpha", "claude3", user_command=True))
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(self.moved, [])

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_ends_a_restart_and_forgets_only_its_row(self) -> None:
        row = self.restart_row()
        up = self.up(row.current_session)
        forget_moved = shutdown_record.forget_moved

        def confirm(*_args: object) -> stop_resume.ClaudeUp:
            self.assertEqual(shutdown_record.read(), [row.id, "another-row"])
            return up

        def record_outcome(*_args: object) -> None:
            self.assertEqual(shutdown_record.read(), ["another-row"])

        def forget_under_lock(row_id: str) -> None:
            path = self.root / "rows" / f"{row.id}.lock"
            with path.open("rb") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            forget_moved(row_id)

        for already_on_target in (False, True):
            source = (stop_resume.LiveMoveSource(up, 123, "fixture") if already_on_target
                      else stop_resume.StoppedMoveSource())
            with (self.subTest(already_on_target=already_on_target), self.hung_operation("restart") as child,
                  redirect_stdout(io.StringIO()) as output,
                  patch.object(stop_resume, "_move_now", new=REAL_MOVE_NOW),
                  patch.object(accounts, "check_move_target", return_value=accounts.MoveTargetReady(
                      accounts.NamedAccount("claude3", self.root / ".claude3"))),
                  patch.object(stop_resume, "_read_move_source", return_value=source),
                  patch.object(member_account, "repair_home", side_effect=confirm) as repair,
                  patch.object(stop_resume, "_record_launches"),
                  patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(row.current_session)) as stop,
                  patch.object(stop_resume, "_resume_move", side_effect=confirm) as resume,
                  patch.object(shutdown_record, "forget_moved", side_effect=forget_under_lock) as forget,
                  patch.object(stop_resume, "_record_move_outcome", new=record_outcome)):
                shutdown_record.save([row.id, "another-row"])
                self.assertEqual(self.move_at_the_limit(row.id), up)
                self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
                self.assertEqual(shutdown_record.read(), ["another-row"])
            forget.assert_called_once_with(row.id)
            if already_on_target:
                repair.assert_called_once()
                resume.assert_not_called()
                stop.assert_not_called()
            else:
                resume.assert_called_once()
            self.assertEqual(output.getvalue(), f"{row.name}: ended a hung restart (pid {child.pid}) after 0.01 s "
                             + "and moved it to claude3. Restart left 1 rows. Run ~/.claude/scripts/lib/py "
                             + "~/.claude/scripts/agents/shutdown.py restart to finish them\n")

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_preserves_a_restart_of_a_different_row(self) -> None:
        row = self.restart_row()
        with (self.hung_operation("restart", row="beta") as child,
              patch.object(signal, "pidfd_send_signal") as send):
            shutdown_record.save([row.id, "beta"])
            result = self.move_at_the_limit(row.id)
            self.assertIsNone(child.poll())
            self.assertEqual(shutdown_record.read(), [row.id, "beta"])
        send.assert_not_called()
        self.assertIsInstance(result, moves.MoveWaitingForRow)
        self.assertEqual(self.moved, [])

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_needs_a_current_restart_claim_or_a_recorded_slot_holder(self) -> None:
        row = self.restart_row()
        for operation in ("restart-released", "restart-slot"):
            with (self.subTest(operation=operation), self.hung_operation(operation) as child,
                  patch.object(signal, "pidfd_send_signal") as send):
                shutdown_record.save([row.id])
                if operation == "restart-slot":
                    (shutdown_record.record_path().with_suffix(".holders") / str(child.pid)).unlink()
                self.assertIsInstance(self.move_at_the_limit(row.id), moves.MoveWaitingForRow)
                self.assertIsNone(child.poll())
                send.assert_not_called()

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_keeps_the_restart_row_when_the_target_refuses(self) -> None:
        row = self.restart_row()
        with (self.hung_operation("restart") as child,
              patch.object(stop_resume, "_move_now", new=REAL_MOVE_NOW),
              patch.object(accounts, "check_move_target", return_value=accounts.MoveRefused("fixture refused"))):
            shutdown_record.save([row.id, "another-row"])
            result = self.move_at_the_limit(row.id)
            self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
            self.assertEqual(shutdown_record.read(), [row.id, "another-row"])
        assert isinstance(result, stop_resume.StopFailed)
        self.assertIn("fixture refused", result.reason)

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_reports_pending_cleanup_after_restart_recovery(self) -> None:
        row = self.restart_row()
        up = self.up(row.current_session)
        with (self.hung_operation("restart") as child, redirect_stdout(io.StringIO()) as output,
              redirect_stderr(io.StringIO()) as errors,
              patch.object(stop_resume, "_move_now", new=REAL_MOVE_NOW),
              patch.object(accounts, "check_move_target", return_value=accounts.MoveTargetReady(
                  accounts.NamedAccount("claude3", self.root / ".claude3"))),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(row.current_session)),
              patch.object(stop_resume, "_resume_move", return_value=up),
              patch.object(shutdown_record, "forget_moved", side_effect=shutdown_record.RecordBusy("fixture busy"))):
            shutdown_record.save([row.id, "another-row"])
            self.assertEqual(self.move_at_the_limit(row.id, source_account="claude1"), up)
            self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
            self.assertEqual(shutdown_record.read(), [row.id, "another-row"])
            self.assertEqual(errors.getvalue(), f"{row.name}: could not remove the row from "
                             + f"{shutdown_record.record_path()}: fixture busy\n")
        self.assertEqual(output.getvalue(), f"{row.name}: ended a hung restart (pid {child.pid}) after 0.01 s "
                         + "and moved it to claude3\n")

    def test_an_ordinary_move_leaves_the_restart_record_alone(self) -> None:
        row = self.restart_row()
        up = self.up(row.current_session)
        with (patch.object(stop_resume, "_move_now", new=REAL_MOVE_NOW),
              patch.object(accounts, "check_move_target", return_value=accounts.MoveTargetReady(
                  accounts.NamedAccount("claude3", self.root / ".claude3"))),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_stop", return_value=stop_resume.NotRunning(row.current_session)),
              patch.object(stop_resume, "_resume_move", return_value=up),
              patch.object(shutdown_record, "forget_moved") as forget):
            for user_command in (False, True):
                with self.subTest(user_command=user_command):
                    shutdown_record.save([row.id])
                    self.assertEqual(stop_resume.move_sessions([row.id], "claude3", why="fixture",
                                                               user_command=user_command), [up])
                    self.assertEqual(shutdown_record.read(), [row.id])
            forget.assert_not_called()

    def test_holder_record_errors_do_not_change_the_restart_result(self) -> None:
        real_unlink = Path.unlink

        def unlink_holder(path: Path, *, missing_ok: bool = False) -> None:
            if path.parent.name == "restart.holders" and path.name == str(os.getpid()):
                raise OSError("fixture cleanup")
            real_unlink(path, missing_ok=missing_ok)

        for publishing in (True, False):
            with (self.subTest(publishing=publishing), redirect_stderr(io.StringIO()) as output,
                  patch.object(restart, "_restart", return_value=17) as action):
                error = "fixture write" if publishing else "fixture cleanup"
                failure = (patch.object(shutdown_record, "write_text_atomic", side_effect=OSError(error))
                           if publishing else patch.object(Path, "unlink", new=unlink_holder))
                with failure:
                    self.assertEqual(restart.restart(), 17)
                action.assert_called_once()
                self.assertEqual(output.getvalue(), f"restart: holder record anomaly: {error}\n")

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_hand_move_ends_both_holders_before_a_slow_first_start(self) -> None:
        with self.hung_operation("resume") as first, self.hung_operation("resume", row="beta") as second:
            sent: list[int] = []
            real_send = signal.pidfd_send_signal

            def outside_move(pid: int) -> bool:
                return pid in (first.pid, second.pid)

            def send(descriptor: int, sig: int) -> None:
                sent.append(sig)
                real_send(descriptor, sig)

            def start(selected: list[str], target: str, **_options: object) -> list[stop_resume.MoveResult]:
                if not self.moved:
                    self.assertEqual(sent, [signal.SIGTERM, signal.SIGTERM])
                    child = first if selected == ["alpha"] else second
                    self.assertEqual(child.poll(), -signal.SIGTERM)
                    time.sleep(2)
                self.moved.append(selected)
                return [self.up(name, target) for name in selected]

            with (patch.object(lock_holder, "_outside_move", new=outside_move),
                  patch.object(signal, "pidfd_send_signal", new=send),
                  patch.object(stop_resume, "_move_now", side_effect=start)):
                self.assertEqual(self.moves_at_the_limit("alpha", "beta"), [self.up("alpha"), self.up("beta")])
            self.assertEqual(first.wait(timeout=5), -signal.SIGTERM)
            self.assertEqual(second.wait(timeout=5), -signal.SIGTERM)
        self.assertCountEqual(self.moved, [["alpha"], ["beta"]])

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_deadline_sends_both_terms_during_a_free_rows_start(self) -> None:
        from threading import Event

        number = conf.ConfigRead.number
        text = conf.ConfigRead.text
        sent: list[int] = []
        escalated = Event()
        starting = Event()
        real_send = signal.pidfd_send_signal

        def seconds(settings: conf.ConfigRead, key: str) -> float:
            return 0.5 if key == "move.stuck_seconds" else number(settings, key)

        def grace(settings: conf.ConfigRead, key: str, *, recover_invalid: bool = True) -> str:
            return "0.2" if key == "KILL_GRACE_SECONDS" else text(settings, key, recover_invalid=recover_invalid)

        def send(descriptor: int, sig: int) -> None:
            self.assertTrue(starting.is_set())
            sent.append(sig)
            real_send(descriptor, sig)
            if sig == signal.SIGKILL:
                escalated.set()

        def prepare(row: stop_resume.LockedMoveRow, target: str, record: staff.StaffSnapshot, *,
                    check_presence: bool) -> object:
            if row.session == "free" and slow_step == "prepare":
                starting.set()
                self.assertTrue(escalated.wait(5))
            return REAL_PREPARE_MOVE(row, target, record, check_presence=check_presence)

        def start(selected: list[str], target: str, **_options: object) -> list[stop_resume.MoveResult]:
            if selected == ["free"] and slow_step == "start":
                starting.set()
                self.assertTrue(escalated.wait(5))
            self.moved.append(selected)
            return [self.up(name, target) for name in selected]

        for slow_step in ("prepare", "start"):
            sent.clear()
            escalated.clear()
            starting.clear()
            self.moved.clear()
            with (self.subTest(slow_step=slow_step), self.hung_operation("resume", ignore_term=True) as first,
                  self.hung_operation("resume", row="beta") as second):
                def outside_move(pid: int) -> bool:
                    return pid in (first.pid, second.pid)

                with (patch.object(lock_holder, "_outside_move", new=outside_move),
                      patch.object(conf.ConfigRead, "number", new=seconds),
                      patch.object(conf.ConfigRead, "text", new=grace),
                      patch.object(signal, "pidfd_send_signal", new=send),
                      patch.object(stop_resume, "_prepare_move", side_effect=prepare),
                      patch.object(stop_resume, "_move_now", side_effect=start)):
                    names = ["alpha", "beta", "free"]
                    self.assertEqual(stop_resume.move_sessions(names, "claude3", why="fixture", user_command=True),
                                     [self.up(name) for name in names])
                self.assertEqual(first.wait(timeout=5), -signal.SIGKILL)
                self.assertEqual(second.wait(timeout=5), -signal.SIGTERM)
                self.assertEqual(sent, [signal.SIGTERM, signal.SIGTERM, signal.SIGKILL])
                self.assertEqual(self.moved[0], ["free"])
                self.assertCountEqual(self.moved[1:], [["alpha"], ["beta"]])

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_row_retries_while_another_holder_still_runs(self) -> None:
        with self.hung_operation("resume", row="beta", ignore_term=True) as second, self.hung_operation("resume") as first:
            def outside_move(pid: int) -> bool:
                return pid in (first.pid, second.pid)

            def start(selected: list[str], target: str, **_options: object) -> list[stop_resume.MoveResult]:
                if selected == ["alpha"]:
                    self.assertEqual(first.poll(), -signal.SIGTERM)
                    self.assertIsNone(second.poll())
                    second.send_signal(signal.SIGKILL)
                self.moved.append(selected)
                return [self.up(name, target) for name in selected]

            with (patch.object(lock_holder, "_outside_move", new=outside_move),
                  patch.object(stop_resume, "_move_now", side_effect=start)):
                self.assertEqual(self.moves_at_the_limit("beta", "alpha", grace_seconds=5),
                                 [self.up("beta"), self.up("alpha")])
            self.assertEqual(second.wait(timeout=5), -signal.SIGKILL)
        self.assertEqual(self.moved, [["alpha"], ["beta"]])

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_the_watch_leaves_a_row_the_move_already_took(self) -> None:
        from threading import Event

        alpha = self.hold_row()
        beta = self.hold_row("beta")
        checking = Event()
        replaced = Event()
        checked = Event()
        number = conf.ConfigRead.number
        end_all = lock_holder.end_all
        children: list[subprocess.Popen[str]] = []
        proofs: list[lock_holder.ProvedLockHolder] = []

        def seconds(settings: conf.ConfigRead, key: str) -> float:
            return 0.1 if key == "move.stuck_seconds" else number(settings, key)

        def prove(row: stop_resume.RowHeld) -> lock_holder.ProvedLockHolder | None:
            if row.session == "alpha":
                checking.set()
                self.assertTrue(replaced.wait(5))
            proof = REAL_HUNG_ROW_HOLDER(row)
            if proof is not None:
                proofs.append(proof)
            return proof

        def retry(_deadline: float) -> bool:
            self.assertTrue(checking.wait(5))
            alpha.close()
            beta.close()
            return True

        def end(holders: Mapping[lock_holder.ProvedLockHolder, Callable[[], lock_holder.ProvedLockHolder | None]],
                seconds: float, *, publish: lock_holder.HolderOutcome,
                send_if_waiting: lock_holder.SignalGuard | None = None) -> None:
            try:
                end_all(holders, seconds, publish=publish, send_if_waiting=send_if_waiting)
            finally:
                checked.set()

        with ExitStack() as replacement:
            def prepare(row: stop_resume.LockedMoveRow, target: str, record: staff.StaffSnapshot, *,
                        check_presence: bool) -> object:
                if row.session == "alpha":
                    return self.up("alpha")
                children.append(replacement.enter_context(self.hung_operation("resume")))
                replaced.set()
                self.assertTrue(checked.wait(5))
                return REAL_PREPARE_MOVE(row, target, record, check_presence=check_presence)

            with (patch.object(conf.ConfigRead, "number", new=seconds),
                  patch.object(stop_resume, "_await_row_operation", new=retry),
                  patch.object(stop_resume, "_hung_row_holder", new=prove),
                  patch.object(lock_holder, "end_all", new=end),
                  patch.object(stop_resume, "_prepare_move", new=prepare)):
                self.assertEqual(stop_resume.move_sessions(["alpha", "beta"], "claude3", why="fixture", user_command=True),
                                 [self.up("alpha"), self.up("beta")])
            child, = children
            self.assertEqual([proof.record.pid for proof in proofs], [child.pid])
            self.assertIsNone(child.poll())
        self.assertEqual(self.moved, [["beta"]])

    def test_mac_rechecks_the_lock_and_process_start_before_kill(self) -> None:
        path = self.root / "slot.lock"
        path.touch()
        target = path.stat()
        record = lock_holder.HolderRecord(12345, "resume", "fixture-start")
        proof = lock_holder.ProvedLockHolder(record, path, "WRITE", target.st_dev, target.st_ino)
        for lock_held, same_start, still_waiting in ((False, True, True), (False, False, True), (True, False, True),
                                                    (True, True, True), (True, True, False)):
            with self.subTest(lock_held=lock_held, same_start=same_start, still_waiting=still_waiting):
                calls: list[str] = []
                outcomes: list[tuple[lock_holder.ProvedLockHolder, lock_holder.HolderEnd]] = []

                def publish(checked: lock_holder.ProvedLockHolder, outcome: lock_holder.HolderEnd) -> None:
                    outcomes.append((checked, outcome))

                def recheck() -> lock_holder.ProvedLockHolder | None:
                    second = bool(calls)
                    calls.append("lock")
                    return proof if not second or lock_held else None

                def read_start(pid: int) -> process_start.Started:
                    self.assertEqual(pid, record.pid)
                    second = "start" in calls
                    calls.append("start")
                    return process_start.Started("new-start" if second and not same_start else record.start)

                def send(pid: int, sig: int) -> None:
                    self.assertEqual(pid, record.pid)
                    calls.append(signal.Signals(sig).name)

                def send_if_waiting(_proof: lock_holder.ProvedLockHolder, send_signal: Callable[[], None]) -> bool:
                    if "SIGTERM" in calls and not still_waiting:
                        return False
                    send_signal()
                    return True

                def observe(pending: list[lock_holder.SignalGroup],
                            report: lock_holder.HolderOutcome,
                            _seconds: float) -> None:
                    if "SIGKILL" in calls:
                        report(proof, lock_holder.HolderEnd.ENDED)
                        pending.clear()

                with (patch.object(sys, "platform", "darwin"),
                      patch.object(process_start, "process_start", new=read_start),
                      patch.object(lock_holder, "_wait_for_exits", new=observe),
                      patch.object(os, "kill", new=send)):
                    lock_holder.end_all({proof: recheck}, 0.2, publish=publish, send_if_waiting=send_if_waiting)
                expected = ["lock", "start", "SIGTERM", "lock"]
                if lock_held:
                    expected.append("start")
                if lock_held and same_start and still_waiting:
                    expected.append("SIGKILL")
                else:
                    expected.append("start")
                self.assertEqual(calls, expected)
                ended = not same_start or (lock_held and still_waiting)
                self.assertEqual(outcomes, [(proof, lock_holder.HolderEnd.ENDED if ended
                                             else lock_holder.HolderEnd.RELEASED)])

    def test_an_automatic_move_with_a_live_step_moves_at_once(self) -> None:
        from scripts.build_queue import admission_client

        # A live verify call and a queue row exist; the move reads neither.
        live_step = {"VERIFY_STEPS_DIR": str(self.root / "records"), "VERIFY_PROC_ROOT": str(self.root / "proc")}
        with patch.dict(os.environ, live_step):
            self.verify_call()
        for cause in ("auto", "refusal", "credits off"):
            with self.subTest(cause=cause), \
                    patch.object(admission_client, "view", side_effect=AssertionError("queue read")):
                self.moved = []
                results = stop_resume.move_sessions(["alpha", "beta"], "claude3", why="auto: head", cause=cause)
                self.assertEqual(results, [self.up("alpha"), self.up("beta")])
                self.assertEqual(self.moved, [["alpha", "beta"]])

    def test_a_caller_hands_the_whole_batch_to_a_child(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "alpha"}), \
                patch.object(stop_resume, "_record_launches"), \
                patch.object(stop_resume, "_handoff_batch") as handoff:
            results = stop_resume.move_sessions(["alpha", "beta"], "claude3", why="auto: head", cause="auto")
        self.assertEqual(results, [stop_resume.MoveHandedOff("alpha"), stop_resume.MoveHandedOff("beta")])
        self.assertEqual(handoff.call_args.args[:2], (["alpha", "beta"], "claude3"))
        self.assertEqual(self.moved, [])

    def test_failed_handoff_reports_each_member(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "alpha"}), \
                patch.object(stop_resume, "_record_launches"), \
                patch.object(stop_resume, "_handoff_batch", side_effect=OSError("fixture launch failed")):
            results = stop_resume.move_sessions(["alpha", "beta"], "claude3", why="move", cause="auto")
        self.assertEqual(results, [stop_resume.StopFailed("alpha", "fixture launch failed"),
                                   stop_resume.StopFailed("beta", "fixture launch failed")])
        self.assertEqual(self.moved, [])

    def test_a_replacement_conversation_during_the_warning_is_not_moved(self) -> None:
        row = staff.StaffRow(staff.StaffId("alpha"), "showrunner", "fixture", staff.StaffRoot(),
                             current_session=staff.StaffId("conversation-before"))
        snapshot = staff.StaffSnapshot((row,))
        request_id = "c" * 32
        moves.write_latest_request(row.current_session, moves.LatestMoveRequest(request_id, "claude3"))

        def warn(*_args: object) -> None:
            nonlocal snapshot
            self.assertIsInstance(staff.row_activity(moves.native_row_key(row.current_session)), staff.RowBusy)
            snapshot = staff.StaffSnapshot((replace(row, current_session=staff.StaffId("conversation-after")),))

        with (patch.object(staff, "initial_snapshot", side_effect=lambda: snapshot),
              patch.object(stop_resume, "_warn_moves", side_effect=warn) as warned):
            results = stop_resume.move_sessions([row.id], "claude3", why="move", cause="refusal", warn_seconds=1,
                request=stop_resume.DetachedMoveRequest(request_id, frozenset({row.id})))
        self.assertEqual(results, [moves.MoveWaitingForRow(row.current_session, "the move owner changed while holding its row")])
        warned.assert_called_once()
        self.assertEqual(self.moved, [])
        self.assertEqual(moves.read_latest_request(row.current_session), moves.LatestMoveRequest(request_id, "claude3"))

    def test_batch_retires_a_prompt_after_an_import_changes_on_disk(self) -> None:
        module = self.root / "unit_lookup.py"
        _ = module.write_bytes(Path(unit_lookup.__file__).read_bytes())
        checkout = Path(__file__).resolve().parents[2]
        code = r"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd()))
from scripts import production

root = Path(sys.argv[1])
production.__path__ = [str(root), *production.__path__]
from scripts.agents import stop_resume

def replace_module(*_args):
    (root / "unit_lookup.py").write_text("# This revision lacks LiveSession.\n")

def move_now(selected, target, **_options):
    stop_resume._retire_prompt(selected[0])
    return [stop_resume.ClaudeUp(selected[0], "w1:p1", target)]

with (patch.object(stop_resume, "_tab_of", side_effect=stop_resume.SessionNotFound),
      patch.object(stop_resume, "running_on_account", return_value=stop_resume.sessions.SessionNotLive()),
      patch.object(stop_resume, "_warn_moves", side_effect=replace_module),
      patch.object(stop_resume, "_move_now", side_effect=move_now)):
    raise SystemExit(stop_resume.main(["batch", "--target", "claude3", "--cause", "auto", "--warn-seconds", "1",
                                      "--log", str(root / "batch.log"), "alpha"]))
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(self.root)], cwd=checkout,
            env={"HOME": str(self.root), "TMPDIR": str(self.root), "CONF_DIR": str(self.root / "config"),
                 "STALL_WATCH_STATE_DIR": str(self.root)}, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("lacks LiveSession", module.read_text())
        cancellations = list(self.root.glob("*.prompt.cancel.json"))
        self.assertEqual(len(cancellations), 1, result.stderr)
        cancellation = cast(dict[str, object], json.loads(cancellations[0].read_text()))
        self.assertEqual(cancellation["session_id"], "alpha")

    def test_batch_moves_every_member_when_main_changes_during_its_warning(self) -> None:
        checkout = Path(__file__).resolve().parents[2]
        code = """
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from scripts.agents.test_stop_resume import StopResumeTests

case = StopResumeTests()
try:
    case.setUp()
    case.check_batch_moves_across_main_change()
finally:
    case.doCleanups()
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code], cwd=checkout,
            env={"HOME": str(self.root), "TMPDIR": str(self.root), "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_move_by_hand_takes_the_row_of_an_automatic_move(self) -> None:
        from threading import Event, Thread, current_thread, main_thread

        for target in ("claude3", "claude4"):
            with self.subTest(target=target):
                self.moved = []
                moves.latest_request_path("alpha").unlink(missing_ok=True)
                hand: list[list[stop_resume.MoveResult]] = []
                workers: list[Thread] = []

                def prepare(row: stop_resume.LockedMoveRow, account: str, record: staff.StaffSnapshot, *,
                            check_presence: bool) -> object:
                    prepared = REAL_PREPARE_MOVE(row, account, record, check_presence=check_presence)
                    if current_thread() is main_thread() and not workers:
                        automatic = moves.read_latest_request("alpha")
                        worker = Thread(target=lambda: hand.append(
                            stop_resume.move_sessions(["alpha"], target, why="by hand", user_command=True)))
                        workers.append(worker)
                        worker.start()
                        limit = time.monotonic() + 10
                        while moves.read_latest_request("alpha") == automatic and time.monotonic() < limit:
                            _ = Event().wait(0.01)
                    return prepared

                with patch.object(stop_resume, "_prepare_move", side_effect=prepare):
                    automatic = stop_resume.move_sessions(["alpha"], "claude3", why="auto: head", cause="auto")
                    workers[0].join(30)
                self.assertEqual(automatic, [moves.MoveSuperseded("alpha")])
                self.assertEqual(hand, [[self.up("alpha", target)]])
                self.assertEqual(self.moved, [["alpha"]])

    def automatic_move_at_a_poll(self, *, past_limit: bool = False,
                                 child: stop_resume.DetachedHandMoveRequest | None = None) -> list[stop_resume.MoveResult]:
        """The holder ends, and an automatic move tries the row before the next try of the waiting hand move."""
        holder = self.hold_row()
        clock = [0.0]
        automatic: list[list[stop_resume.MoveResult]] = []

        def poll(_seconds: float) -> None:
            if past_limit:
                clock[0] = conf.read("shutdown").number("move.stuck_seconds") + 1
            if not automatic:
                holder.close()
                automatic.append(stop_resume.move_sessions(["alpha"], "claude3", why="auto: head", cause="auto"))

        with patch.object(stop_resume, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(time, "sleep", side_effect=poll) as sleep:
            hand = stop_resume.move_sessions(["alpha"], "claude4", why="by hand", user_command=True,
                                             request=child or stop_resume.NewMoveRequest())
        sleep.assert_called_once()
        self.assertEqual(automatic, [[moves.MoveSuperseded("alpha")]])
        self.assertEqual(self.moved, [["alpha"]])
        return hand

    def test_an_automatic_move_never_takes_the_row_of_a_waiting_move_by_hand(self) -> None:
        self.assertEqual(self.automatic_move_at_a_poll(), [self.up("alpha", "claude4")])

    def test_a_waiting_move_by_hand_takes_its_row_at_the_last_try_after_an_automatic_move(self) -> None:
        self.assertEqual(self.automatic_move_at_a_poll(past_limit=True), [self.up("alpha", "claude4")])

    def test_the_child_of_a_self_move_by_hand_keeps_its_row_from_an_automatic_move(self) -> None:
        request = stop_resume.DetachedHandMoveRequest("d" * 32, frozenset(), conf.read("shutdown").number("move.stuck_seconds"))
        # The parent wrote the claim before the handoff and then ended; the child takes the claim at its first try.
        parent = moves.ClaimOwner(os.getpid(), "the parent process ended")
        moves.write_latest_request("alpha", moves.LatestMoveRequest(request.request_id, "claude4", owner=parent))
        self.assertEqual(self.automatic_move_at_a_poll(child=request), [self.up("alpha", "claude4")])

    def test_the_child_takes_the_claims_of_its_parent_before_it_confirms_its_start(self) -> None:
        parent = moves.ClaimOwner(os.getpid(), "the parent process ended")
        moves.write_latest_request("alpha", moves.LatestMoveRequest("d" * 32, "claude3", owner=parent), name="alpha")
        confirmed: list[moves.LatestMoveRequest | moves.NoLatestMoveRequest] = []

        def confirm(ready_fd: int | None, *, systemd: bool, acknowledge: bool) -> None:
            self.assertEqual((ready_fd, systemd, acknowledge), (7, True, True))
            confirmed.append(moves.read_latest_request("alpha"))

        with patch.object(stop_resume, "notify_ready", side_effect=confirm), \
                patch.object(stop_resume, "_load_batch_modules"), \
                patch.object(stop_resume, "move_sessions", return_value=[self.up("alpha")]) as batch:
            self.assertEqual(stop_resume.main(["batch", "--log", str(self.root / "move.log"), "--ready-fd", "7",
                                               "--notify-systemd", "--user-command", "--request-id", "d" * 32,
                                               "--move-deadline", "100", "--target", "claude3", "--", "alpha"]), 0)
        claim, = confirmed
        assert isinstance(claim, moves.LatestMoveRequest)
        self.assertEqual(claim.owner, moves.own_claim_owner())
        self.assertTrue(moves.claim_is_live(claim))
        batch.assert_called_once()

    def test_the_child_does_not_confirm_a_batch_when_one_claim_cannot_transfer(self) -> None:
        request_id = "d" * 32
        for failure in (False, OSError("fixture write failed")):
            with (self.subTest(failure=failure), redirect_stderr(io.StringIO()) as errors,
                  patch.object(moves, "set_claim_owner", side_effect=[True, failure]) as transfer,
                  patch.object(stop_resume, "notify_ready") as confirm,
                  patch.object(stop_resume, "move_sessions") as move):
                self.assertEqual(stop_resume.main(["batch", "--notify-systemd", "--user-command",
                                                   "--request-id", request_id, "--move-deadline", "100", "--target", "claude3",
                                                   "--", "alpha", "beta"]), 1)
                self.assertIn("beta: cannot take the move request:", errors.getvalue())
            self.assertEqual(transfer.call_args_list, [call(name, request_id, moves.own_claim_owner())
                                                       for name in ("alpha", "beta")])
            confirm.assert_not_called()
            move.assert_not_called()

    def test_failed_handoff_removes_the_partially_transferred_held_claim(self) -> None:
        _ = self.hold_row()
        transfer_owner = moves.ClaimOwner(os.getpid(), "fixture child")
        take = moves.set_claim_owner
        requests: list[str] = []

        def transfer(identity: str, request_id: str, owner: moves.ClaimOwner | None) -> bool:
            if identity == "beta" and owner == transfer_owner:
                claim = moves.read_latest_request("alpha")
                assert isinstance(claim, moves.LatestMoveRequest)
                self.assertEqual(claim.owner, transfer_owner)
                return False
            return take(identity, request_id, owner)

        def launch(argv: list[str], *_args: object, **_options: object) -> None:
            boundary = argv.index("--")
            requests.append(argv[argv.index("--request-id") + 1])
            with (patch.object(stop_resume, "_own_claim_owner", return_value=transfer_owner),
                  patch.object(moves, "set_claim_owner", new=transfer),
                  redirect_stderr(io.StringIO())):
                self.assertEqual(stop_resume.main([*argv[:boundary + 1], "alpha", "beta"]), 1)
            raise OSError("fixture handoff failed")

        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "beta"}),
              patch.object(stop_resume, "_record_launches"),
              patch.object(stop_resume, "_detach", side_effect=launch)):
            results = stop_resume.move_sessions(["alpha", "beta"], "claude3", why="fixture", user_command=True)
        self.assertEqual(results, [stop_resume.StopFailed(name, "fixture handoff failed") for name in ("alpha", "beta")])
        request_id, = requests
        self.assertIsInstance(moves.read_latest_request("alpha"), moves.NoLatestMoveRequest)
        self.assertEqual(moves.read_latest_request("beta"), moves.LatestMoveRequest(request_id, "claude3"))
        for name in ("alpha", "beta"):
            self.assertFalse(moves.hand_move_is_queued(name, request_id))
        self.assertEqual(self.moved, [])

    def test_the_child_starts_no_move_when_the_parent_cannot_acknowledge(self) -> None:
        for close_parent in (True, False):
            parent, child = socket.socketpair()
            with (self.subTest(close_parent=close_parent), parent, child,
                  patch.object(stop_resume, "move_sessions") as move, redirect_stderr(io.StringIO()) as output):
                if close_parent:
                    parent.close()
                else:
                    parent.shutdown(socket.SHUT_WR)
                self.assertEqual(stop_resume.main(["batch", "--ready-fd", str(child.detach()),
                                                   "--target", "claude3", "--", "alpha"]), 1)
                move.assert_not_called()
                self.assertIn("could not confirm readiness", output.getvalue())

    @unittest.skipUnless(process_stop.supported(), "requires Linux pidfds")
    def test_a_handed_off_held_row_is_recovered_and_reported_by_the_child(self) -> None:
        from threading import Event

        clock = [0.0]
        limit = conf.read("shutdown").number("move.stuck_seconds")
        real_send = signal.pidfd_send_signal
        for caller in ("alpha", "free"):
            clock[0] = 0.0
            self.moved.clear()
            log = self.root / f"move-{caller}.log"
            names = ["alpha"] if caller == "alpha" else ["alpha", caller]
            signaled = Event()
            with (self.subTest(caller=caller), self.hung_operation("resume") as holder,
                  patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": caller}),
                  patch.object(stop_resume, "monotonic", side_effect=lambda: clock[0]),
                  patch.object(stop_resume, "_record_launches"),
                  patch.object(stop_resume, "_detach") as detach,
                  patch.object(stop_resume, "_load_batch_modules")):
                with patch.object(stop_resume, "_prepare_move", side_effect=AssertionError("parent source lookup")):
                    self.assertEqual(stop_resume.move_sessions(names, "claude3", why="fixture", user_command=True),
                                     [stop_resume.MoveHandedOff(name) for name in names])
                self.assertIsNone(holder.poll())
                self.assertEqual(self.moved, [])
                argv = cast(list[str], detach.call_args.args[0])

                def send(descriptor: int, sig: int) -> None:
                    real_send(descriptor, sig)
                    signaled.set()

                def prepare(row: stop_resume.LockedMoveRow, target: str, record: staff.StaffSnapshot, *,
                            check_presence: bool) -> object:
                    if row.session == "free":
                        self.assertTrue(signaled.wait(10), "the child did not signal during source lookup")
                        return self.up("free")
                    return REAL_PREPARE_MOVE(row, target, record, check_presence=check_presence)

                clock[0] = limit + 1
                with (patch.object(stop_resume, "_prepare_move", new=prepare),
                      patch.object(signal, "pidfd_send_signal", new=send)):
                    result = stop_resume.main(["batch", "--log", str(log), *argv[1:]])
                self.assertEqual(result, 0, log.read_text())
                self.assertEqual(holder.wait(timeout=5), -signal.SIGTERM)
                self.assertIn(f"alpha: ended a hung resume (pid {holder.pid}) after {limit:g} s "
                              + "and moved it to claude3\n", log.read_text())
                self.assertEqual(self.moved, [["alpha"]])

    def test_detached_hand_and_automatic_moves_take_different_deadline_arguments(self) -> None:
        common = ["batch", "--request-id", "d" * 32, "--target", "claude3"]
        for options in (["--user-command"], ["--move-deadline", "100"],
                        ["--user-command", "--move-deadline", "nan"]):
            with (self.subTest(options=options), patch.object(stop_resume, "move_sessions") as move,
                  redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failed):
                _ = stop_resume.main([*common, *options, "--", "alpha"])
            self.assertEqual(failed.exception.code, 2)
            move.assert_not_called()

    def test_a_claim_with_no_start_is_live_while_its_pid_exists(self) -> None:
        with patch.object(process_start, "process_start", return_value=process_start.Unreadable("fixture")) as read:
            owner = moves.own_claim_owner()
        self.assertEqual(read.call_count, 2)
        self.assertEqual(owner, moves.ClaimOwner(os.getpid(), ""))
        ended = subprocess.Popen(["true"])
        _ = ended.wait()
        self.assertTrue(moves.claim_is_live(moves.LatestMoveRequest("e" * 32, "claude3", owner=owner)))
        self.assertFalse(moves.claim_is_live(moves.LatestMoveRequest("e" * 32, "claude3",
                                                                     owner=moves.ClaimOwner(ended.pid, ""))))

    def test_the_child_counts_the_limit_from_the_start_of_its_parent(self) -> None:
        limit = conf.read("shutdown").number("move.stuck_seconds")
        request = stop_resume.DetachedHandMoveRequest("d" * 32, frozenset(), deadline=limit)
        moves.write_latest_request("alpha", moves.LatestMoveRequest(request.request_id, "claude3",
                                                                    owner=moves.own_claim_owner()))
        _ = self.hold_row()
        with patch.object(stop_resume, "monotonic", return_value=limit + 1), patch.object(time, "sleep") as sleep:
            waiting, = stop_resume.move_sessions(["alpha"], "claude3", why="by hand", user_command=True, request=request)
        # The parent used the whole limit, so the child makes its last try at once.
        sleep.assert_not_called()
        assert isinstance(waiting, moves.MoveWaitingForRow)
        self.assertEqual(waiting.command, moves.batch_move_command("alpha", "claude3", user_command=True))
        self.assertEqual(self.moved, [])

    def test_only_a_live_user_claim_stops_an_automatic_move(self) -> None:
        ended = subprocess.Popen(["true"])
        _ = ended.wait()
        for owner, stops in ((moves.own_claim_owner(), True), (moves.ClaimOwner(os.getpid(), "an older process"), False),
                             (moves.ClaimOwner(ended.pid, "a killed process"), False)):
            with self.subTest(owner=owner):
                self.moved = []
                claim = moves.LatestMoveRequest("e" * 32, "claude4", owner=owner)
                moves.write_latest_request("alpha", claim, name="alpha")
                result = stop_resume.move_sessions(["alpha"], "claude3", why="auto: head", cause="auto")
                self.assertEqual(result, [moves.MoveSuperseded("alpha")] if stops else [self.up("alpha")])
                self.assertEqual(self.moved, [] if stops else [["alpha"]])

    def moves_remove_their_requests(self) -> list[Callable[[], None]]:
        """Like the real _move_now, a success removes the request of its own batch; a hook runs during the move."""
        hooks: list[Callable[[], None]] = []

        def move_now(selected: list[str], target: str, **_options: object) -> list[stop_resume.MoveResult]:
            self.moved.append([*selected, target])
            while hooks:
                hooks.pop(0)()
            request_id = stop_resume._move_batch.get().request_id  # pyright: ignore[reportPrivateUsage]
            for name in selected:
                moves.remove_latest_request(name, request_id)
            return [self.up(name, target) for name in selected]

        _ = self.enterContext(patch.object(stop_resume, "_move_now", side_effect=move_now))
        return hooks

    def test_hand_moves_take_the_row_in_arrival_order(self) -> None:
        from threading import Event, Thread, current_thread, main_thread

        _ = self.moves_remove_their_requests()
        holder = self.hold_row()
        newer_is_waiting = Event()
        older_finished = Event()
        newer: list[list[stop_resume.MoveResult]] = []
        worker = Thread(target=lambda: newer.append(
            stop_resume.move_sessions(["alpha"], "claude4", why="second", user_command=True)), daemon=True)

        def poll(_seconds: float) -> None:
            if current_thread() is main_thread():
                before = moves.read_latest_request("alpha")
                worker.start()
                self.assertTrue(newer_is_waiting.wait(5))
                self.assertEqual(moves.read_latest_request("alpha"), before)
                holder.close()
            else:
                newer_is_waiting.set()
                self.assertTrue(older_finished.wait(5))

        with patch.object(time, "sleep", side_effect=poll):
            try:
                older = stop_resume.move_sessions(["alpha"], "claude3", why="first", user_command=True)
            finally:
                older_finished.set()
                worker.join(5)
        self.assertEqual(older, [self.up("alpha", "claude3")])
        self.assertEqual(newer, [[self.up("alpha", "claude4")]])
        self.assertEqual(self.moved, [["alpha", "claude3"], ["alpha", "claude4"]])

    def test_the_claim_stays_live_through_a_long_pass(self) -> None:
        hooks = self.moves_remove_their_requests()
        holder = self.hold_row()
        clock = [0.0]
        automatic: list[list[stop_resume.MoveResult]] = []

        def during_beta() -> None:
            # The move of beta ends after the limit; the holder of alpha ends during it.
            clock[0] = conf.read("shutdown").number("move.stuck_seconds") + 5
            holder.close()
            automatic.append(stop_resume.move_sessions(["alpha"], "claude5", why="auto: head", cause="auto"))

        hooks.append(during_beta)
        with patch.object(stop_resume, "monotonic", side_effect=lambda: clock[0]):
            hand = stop_resume.move_sessions(["alpha", "beta"], "claude3", why="by hand", user_command=True)
        self.assertEqual(automatic, [[moves.MoveSuperseded("alpha")]])
        self.assertEqual(hand, [self.up("alpha"), self.up("beta")])
        self.assertEqual(self.moved, [["beta", "claude3"], ["alpha", "claude3"]])

    def test_a_move_by_hand_waits_for_an_operation_in_flight_then_moves(self) -> None:
        holder = self.hold_row()

        def operation_ends(_seconds: float) -> None:
            holder.close()

        with patch.object(time, "sleep", side_effect=operation_ends) as sleep:
            results = stop_resume.move_sessions(["alpha"], "claude3", why="by hand", user_command=True)
        self.assertEqual(results, [self.up("alpha")])
        sleep.assert_called_once()
        self.assertEqual(self.moved, [["alpha"]])

    def test_a_move_by_hand_moves_the_free_rows_before_it_waits(self) -> None:
        holder = self.hold_row()

        def operation_ends(_seconds: float) -> None:
            self.assertEqual(self.moved, [["beta"]])
            self.assertIsInstance(staff.row_activity(moves.native_row_key("beta")), staff.RowAvailable)
            self.assertEqual(moves.read_latest_request("alpha"), moves.read_latest_request("beta"))
            holder.close()

        with patch.object(time, "sleep", side_effect=operation_ends) as sleep:
            results = stop_resume.move_sessions(["alpha", "beta"], "claude3", why="by hand", user_command=True)
        self.assertEqual(results, [self.up("alpha"), self.up("beta")])
        sleep.assert_called_once()
        self.assertEqual(self.moved, [["beta"], ["alpha"]])

    def restart_row(self) -> staff.StaffRow:
        row = staff.StaffRow(staff.StaffId("alpha"), "showrunner", "fixture", staff.StaffRoot(),
                             current_session=staff.StaffId("conversation-alpha"))
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))))
        return row

    def test_a_move_by_hand_that_starts_in_a_restart_gap_waits_for_the_start(self) -> None:
        row = self.restart_row()
        # The restart stopped the row and holds no row lock now; the row is in its record.
        shutdown_record.save([row.id])
        _ = self.enterContext(shutdown_record.active_operation())
        polls: list[bool] = []

        def restart_runs(_seconds: float) -> None:
            polls.append(True)
            self.assertEqual(self.moved, [])
            self.assertIsInstance(moves.read_latest_request(row.current_session), moves.LatestMoveRequest)
            # The restart starts the row; it still runs for other rows.
            shutdown_record.save([])

        with patch.object(time, "sleep", side_effect=restart_runs):
            results = stop_resume.move_sessions([row.id], "claude3", why="by hand", user_command=True)
        self.assertEqual(results, [self.up(row.id)])
        self.assertEqual(len(polls), 1)
        self.assertEqual(self.moved, [[row.id]])

    def test_a_restart_pauses_only_the_rows_in_its_record(self) -> None:
        row = self.restart_row()
        _ = self.enterContext(shutdown_record.active_operation())
        with patch.object(time, "sleep", side_effect=AssertionError("the move waited")):
            shutdown_record.save([row.id])
            waiting, = stop_resume.move_sessions([row.id], "claude3", why="auto: head", cause="auto")
            assert isinstance(waiting, moves.MoveWaitingForRow)
            self.assertEqual(waiting.reason, "a restart holds this row")
            self.assertFalse(moves.latest_request_path(row.current_session).exists())
            self.assertEqual(self.moved, [])

            shutdown_record.save(["another-row"])
            results = stop_resume.move_sessions([row.id], "claude3", why="by hand", user_command=True)
        self.assertEqual(results, [self.up(row.id)])
        self.assertEqual(self.moved, [[row.id]])

    def test_an_interrupted_move_by_hand_removes_its_claim(self) -> None:
        _ = self.hold_row()

        def interrupt(_seconds: float) -> None:
            self.assertTrue(moves.latest_request_path("alpha").exists())
            raise KeyboardInterrupt

        with patch.object(time, "sleep", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            _ = stop_resume.move_sessions(["alpha"], "claude3", why="by hand", user_command=True)
        self.assertFalse(moves.latest_request_path("alpha").exists())
        self.assertEqual(self.moved, [])

    def test_an_interrupt_in_the_first_pass_ends_each_claim(self) -> None:
        _ = self.hold_row()

        def interrupted(*_args: object, **_options: object) -> list[stop_resume.MoveResult]:
            raise KeyboardInterrupt

        with patch.object(stop_resume, "_move_now", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            _ = stop_resume.move_sessions(["alpha", "beta"], "claude3", why="by hand", user_command=True)
        # The held row was never taken; the free row keeps its request for a retry, as a plain request.
        self.assertFalse(moves.latest_request_path("alpha").exists())
        pending, = moves.pending_move_requests()
        self.assertEqual(pending.member.reference, "beta")
        self.assertIsNone(pending.request.owner)
        self.assertEqual(stop_resume.move_sessions(["beta"], "claude4", why="auto: head", cause="auto"),
                         [self.up("beta", "claude4")])

    def test_a_failed_move_by_hand_leaves_a_plain_request(self) -> None:
        with patch.object(stop_resume, "_move_now", return_value=[stop_resume.StopFailed("alpha", "fixture")]):
            failed, = stop_resume.move_sessions(["alpha"], "claude3", why="by hand", user_command=True)
        self.assertEqual(failed, stop_resume.StopFailed("alpha", "fixture"))
        pending = moves.read_latest_request("alpha")
        assert isinstance(pending, moves.LatestMoveRequest)
        self.assertIsNone(pending.owner)

    def test_a_retry_by_hand_waits_for_a_busy_row(self) -> None:
        moves.write_latest_request("alpha", moves.LatestMoveRequest("a" * 32, "claude3"), name="alpha")
        holder = self.hold_row()

        def operation_ends(_seconds: float) -> None:
            claim = moves.read_latest_request("alpha")
            assert isinstance(claim, moves.LatestMoveRequest)
            self.assertIsNotNone(claim.owner)
            holder.close()

        def stopped(row: stop_resume.LockedMoveRow, *_args: object, **_options: object) -> stop_resume.MoveStopTarget:
            return stop_resume.MoveStopTarget(row.member, row.identity, stop_resume.StoppedMoveSource())

        with patch.object(time, "sleep", side_effect=operation_ends) as sleep, redirect_stdout(io.StringIO()), \
                patch.object(stop_resume, "_prepare_move", side_effect=stopped):
            self.assertEqual(stop_resume._pending_move_command("retry", ""), 0)  # pyright: ignore[reportPrivateUsage]
        sleep.assert_called_once()
        self.assertEqual(self.moved, [["alpha"]])

    def test_a_move_by_hand_names_a_hung_holder_after_its_limit(self) -> None:
        arguments = ["fixture", "resume", "x" * 6000]
        with patch.object(sys, "argv", arguments):
            _ = self.hold_row()
        clock = [0.0]

        def advance(seconds: float) -> None:
            clock[0] += max(seconds, 1.0)

        with patch.object(stop_resume, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(time, "sleep", side_effect=advance):
            waiting, = stop_resume.move_sessions(["alpha"], "claude3", why="by hand", user_command=True)
        assert isinstance(waiting, moves.MoveWaitingForRow)
        self.assertEqual(waiting.reason, f"another operation holds this row (pid {os.getpid()}: {shlex.join(arguments)})")
        self.assertEqual(waiting.command, moves.batch_move_command("alpha", "claude3", user_command=True))
        self.assertGreaterEqual(clock[0], conf.read("shutdown").number("move.stuck_seconds"))
        self.assertFalse(moves.latest_request_path("alpha").exists())
        self.assertEqual(self.moved, [])
        # The command at the limit runs as a user command.
        argv = shlex.split(waiting.command)
        with patch.object(stop_resume, "move_sessions", return_value=[self.up("alpha")]) as again:
            self.assertEqual(stop_resume.main(["batch", "--log", str(self.root / "again.log"),
                                               *argv[argv.index("batch") + 1:]]), 0)
        self.assertTrue(again.call_args.kwargs["user_command"])

    def test_an_automatic_move_with_the_cause_by_hand_does_not_wait_or_claim(self) -> None:
        _ = self.hold_row()
        with patch.object(time, "sleep", side_effect=AssertionError("an automatic move waited")):
            waiting, = stop_resume.move_sessions(["alpha"], "claude3", why="group completion", cause="by hand")
        self.assertIsInstance(waiting, moves.MoveWaitingForRow)
        self.assertFalse(moves.latest_request_path("alpha").exists())
        self.assertEqual(self.moved, [])

    def test_an_automatic_move_reports_a_busy_row_without_a_wait(self) -> None:
        _ = self.hold_row()
        with patch.object(time, "sleep", side_effect=AssertionError("an automatic move waited")):
            results = stop_resume.move_sessions(["alpha", "beta"], "claude3", why="auto: head", cause="auto")
        self.assertIsInstance(results[0], moves.MoveWaitingForRow)
        self.assertEqual(results[1], self.up("beta"))
        self.assertEqual(self.moved, [["beta"]])
