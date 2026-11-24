"""Restart staff rows without a session journal."""
from __future__ import annotations

from scripts.lib.fixture_test_support import set_up_fixture
from scripts.lint.memory_test_support import write_config_fixture

from scripts.agents import remote
from scripts.herdr import herdr_types

import errno
import io
import math
import json
import os
import fcntl
import signal
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock, Thread
from pathlib import Path
from typing import Literal, cast, override
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
from unittest.mock import Mock, call, patch

from scripts.agents import inventory
from scripts.herdr import herdr, pane_launch, turn_end, worker_panes
from scripts.message import sessions, session_records
from scripts.agents import restart, shutdown
from scripts.agents.unit_staff_test_support import unit_host
from scripts.production import fake_unit_host, session_staff, staff, worker, worker_admission
from scripts.production import test_worker
from scripts.accounts import accounts
from scripts.whoami import agent_accounts, note_file, quota_state
from scripts.whoami.account import Account, UnknownAccountName
from scripts.message import codex_daemon
from scripts.agents import member_account, shutdown_record, stop_resume, pane_record, worker_lifecycle
from scripts.lib import conf, file_lock, models_registry, unit_runs, resume_message
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.transcript_test_support import compressed_copy
from scripts.lib.test_models_registry import claude_catalog_environment, write_registry
from scripts.lib.atomic_file import create_text_atomic, write_text_atomic
from scripts.production.staff_test_support import fixture_member
from scripts.herdr.fake_herdr import herdr_listener
from scripts.direct import pass_record, progress_history
from scripts.agents.test_support import ShutdownTestCase, registry_confirmation
from scripts.lib.script_envelope import script_prompt
from scripts.herdr.missing_binary import MISSING_HERDR_BIN

LAUNCH_PREPARED_AGENT = herdr.launch_prepared_agent
STOP_SESSION = stop_resume.stop
STOP_UNIT_WORKERS = stop_resume.stop_unit_workers
CODEX_CONVERSATIONS = herdr.codex_conversations
NATIVE_PROCESS = herdr.native_process
HERDR_RESULT = herdr.Herdr.result
READ_PANES = herdr.Herdr.panes


READ_CONFIG = conf.read
ENUMERATE_RUNNING_SESSIONS = session_records.enumerate_running_sessions


def short_resume_check_config(file: str | Path) -> conf.ConfigRead:
    """Keep the restart check short while using the configuration reader."""
    settings = READ_CONFIG(file)
    if file == "shutdown" and isinstance(settings.settings, dict):
        values = dict(settings.settings)
        for key in ("RESUME_CHECK_SECONDS", "RESUME_CHECK_INTERVAL_SECONDS"):
            values[key] = conf.Setting("0.001", "# range: 0..3600", 0)
        return replace(settings, settings=values)
    return settings


class RestartTestCase(ShutdownTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(conf, "read", side_effect=short_resume_check_config))
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (home / "proc").mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(home), "CODEX_HOME": str(home / ".codex"),
            "HERDR_PROC": str(home / "proc"), "ROSTER_RUN_ROOT": str(home / "runs"),
            "HERDR_BIN": MISSING_HERDR_BIN,
            "UNIT_RUN_DIR": str(home / "run"), "PLAN_DELEGATE_BOARD_DIR": "",
            "XDG_STATE_HOME": str(home / "state"), "SHUTDOWN_STATE_DIR": str(home / "shutdown"),
        })))
        _ = self.enterContext(patch.object(herdr, "_PLATFORM", "linux"))
        _ = cast(object, self.enterContext(patch.multiple(unit_runs, RUN_ROOT=home / "runs",
                                                        FORMER_RUN_ROOT=home / "former-runs",
                                                        ASTRA_RUN_ROOT=home / "astra")))
        _ = self.enterContext(patch.object(codex_daemon, "read_daemons", return_value=[]))
        _ = self.enterContext(patch.object(sessions, "process_start", return_value="fixture-start"))
        _ = self.enterContext(patch.object(session_records, "process_start", return_value="fixture-start"))
        def role_account(role: str) -> accounts.AccountSelection:
            return (accounts.CodexWorkersAccount("codex1", Path.home() / ".codex1") if role == "workers" else
                    accounts.NamedAccount("claude2", Path(os.environ["CLAUDE_CONFIG_DIR"])))
        _ = self.enterContext(patch.object(accounts, "role_account", side_effect=role_account))
        _ = self.enterContext(patch.object(herdr.Herdr, "result", return_value={
            "panes": [], "tabs": [], "workspaces": []}))


class ClaudeLaunchTrustTests(RestartTestCase):
    def test_concurrent_claude_launches_keep_both_repository_trust_keys(self) -> None:
        account = Path.home() / "account"
        account.mkdir()
        target = account / ".claude.json"
        write_text_atomic(target, "{}")
        folders = (Path.home() / "first-folder", Path.home() / "second-folder")
        for folder in folders:
            folder.mkdir()
        first_at_write, second_attempt, first_written = Event(), Event(), Event()
        read_text = Path.read_text
        lock_config = file_lock.claude_config_lock

        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            text = read_text(path, encoding=encoding, errors=errors)
            if path == target and first_at_write.is_set():
                second_attempt.set()
            return text

        @contextmanager
        def lock(path: Path) -> Generator[None]:
            if first_at_write.is_set():
                second_attempt.set()
            with lock_config(path):
                yield

        def publish(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
            if not first_at_write.is_set():
                first_at_write.set()
                self.assertTrue(second_attempt.wait(5), "second writer did not attempt the account update")
                write_text_atomic(path, text, new_file_mode=new_file_mode)
                first_written.set()
            else:
                self.assertTrue(first_written.wait(5), "first writer did not publish its account update")
                write_text_atomic(path, text, new_file_mode=new_file_mode)

        with (patch.object(Path, "read_text", read),
              patch.object(file_lock, "claude_config_lock", lock),
              patch.object(herdr, "write_text_atomic", publish),
              ThreadPoolExecutor(max_workers=2) as writers):
            first = writers.submit(herdr.trust_claude_launch, folders[0], account)
            self.assertTrue(first_at_write.wait(5), "first writer did not reach publication")
            second = writers.submit(herdr.trust_claude_launch, folders[1], account)
            first.result(timeout=10)
            second.result(timeout=10)
        settings = cast(dict[str, dict[str, dict[str, bool]]], json.loads(target.read_text()))
        self.assertEqual(settings["projects"], {
            str(folder.resolve()): {"hasTrustDialogAccepted": True} for folder in folders})

    def test_claude_launch_without_config_dir_writes_the_home_account_file(self) -> None:
        folder = Path.home() / "launch-folder"
        folder.mkdir()
        target = Path.home() / ".claude.json"
        prepared = herdr.PreparedAgentEnvironment(folder, {})

        def start(*_args: object, **_kwargs: object) -> herdr.PromptSubmissionNotRequired:
            self.assertTrue(target.is_file(), "legacy account trust must exist before the start")
            settings = cast(dict[str, dict[str, dict[str, bool]]], json.loads(target.read_text()))
            self.assertTrue(settings["projects"][str(folder.resolve())]["hasTrustDialogAccepted"])
            return herdr.PromptSubmissionNotRequired()

        with (patch.object(herdr, "open_tab", return_value="w1:p1"),
              patch.object(herdr.Herdr, "run_in_pane"),
              patch.object(herdr, "start_agent", side_effect=start) as started):
            result = herdr.launch_prepared_agent("agents", herdr.NewTab("workers", "worker"),
                prepared, "claude", [], herdr.NoOpeningPrompt(), 5000, agent="worker")
        self.assertIsInstance(result.outcome, herdr.PromptSubmissionNotRequired)
        started.assert_called_once()
        self.assertFalse((Path.home() / ".claude/.claude.json").exists())


class CodexSnapshotTests(RestartTestCase):
    root: Path = Path()
    proc: Path = Path()
    rows: tuple[staff.StaffRow, ...] = ()
    claude: set[str] = set()
    codex: dict[int, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.rows = tuple(
            staff.StaffRow(staff.StaffId(f"row-{index}"), "producer", identity, staff.STAFF_ROOT,
                current_session=staff.StaffId(identity), worktree=staff.PresentWorktree(remote.local_machine(), self.root))
            for index, identity in enumerate(("claude-a", "codex-a", "claude-b", "codex-b")))
        self.claude = {"claude-a", "claude-b", "claude-orphan"}
        self.codex = {998081: "codex-a", 998082: "codex-b", 998083: "codex-orphan"}
        for pid in self.codex:
            path = self.proc / str(pid)
            path.mkdir()
            _ = (path / "cmdline").write_bytes(b"codex\0")
        _ = self.enterContext(patch.object(herdr, "process_environment", return_value={}))
        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=self.native))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.liveness))
        records = registry_confirmation(frozenset(self.claude)).records
        _ = self.enterContext(patch.object(session_records, "enumerate_running_sessions",
                           return_value=session_records.RunningSessionEnumeration(tuple(records), ())))

    def native(self, pid: int, _kind: str) -> herdr.NativeAgentProcess:
        return herdr.NativeAgentProcess(herdr.EnvironmentThread(self.codex[pid]), "1", {})

    def liveness(self, identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        return sessions.LiveSessionProcess(998080) if identity in self.claude else sessions.SessionNotLive()

    def test_restart_drops_a_fake_codex_below_a_claude_pane_shell(self) -> None:
        client = herdr.Herdr("agents")
        proc = self.root / "gate-proc"
        environment = {**os.environ, "HERDR_PROC": str(proc)}
        for pid, parent, argv in ((998080, 1, ["zsh"]), (998089, 998080, ["python3"]),
                                  (998090, 998089, ["codex"])):
            fake_unit_host.process(environment, pid, parent, argv, {"HERDR_PANE_ID": "w1:p1"})
        write_text_atomic(proc / "998090/stat", "998090 (codex) S 998089 998090 998080 0 -1 " + "0 " * 14)
        panes: list[dict[str, object]] = [
            {"pane_id": "w1:p1", "tab_id": "w1:t1", "agent": "claude", "label": "TEST-gate"}]

        def listed_rows(kind: str) -> list[dict[str, object]]:
            return panes if kind == "pane" else [{"tab_id": "w1:t1", "label": "TEST-gate"}]

        info = {"shell_pid": 998080, "foreground_process_group_id": 998089,
                "foreground_processes": [{"pid": 998089, "argv": ["python3"]}]}
        with (patch.dict(os.environ, environment),
              patch.object(herdr, "process_environment", return_value={"HERDR_PANE_ID": "w1:p1"}),
              patch.object(herdr, "native_process", side_effect=herdr_types.HerdrFailure("fixture unknown")),
              patch.object(herdr, "_process_display_name", return_value="TEST-gate"),
              patch.object(client, "listed", side_effect=listed_rows) as listed,
              patch.object(client, "result", return_value={"process_info": info}) as process,
              patch.object(session_records, "enumerate_running_sessions",
                           return_value=session_records.RunningSessionEnumeration((), ())),
              patch.object(herdr, "running_codex_threads", return_value=set()),
              patch.object(restart, "_read_live_members", return_value=restart.CompleteLiveRows([])) as members):
            self.assertEqual(herdr.codex_conversations().unknown, ())
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(()), client).rows, [])
        observed = cast(herdr.CodexConversations, members.call_args.kwargs["codex"])
        self.assertEqual(observed.unknown, ())
        self.assertEqual(listed.call_args_list.count(call("pane")), 1)
        process.assert_not_called()

    def test_restart_keeps_a_codex_pane_foreground_process_with_an_unknown_thread(self) -> None:
        client = herdr.Herdr("agents")
        proc = self.root / "codex-proc"
        environment = {**os.environ, "HERDR_PROC": str(proc)}
        fake_unit_host.process(environment, 998091, 998080, ["codex"], {"HERDR_PANE_ID": "w1:p1"})
        (proc / "998091/stat").unlink()
        panes: list[dict[str, object]] = [
            {"pane_id": "w1:p1", "tab_id": "w1:t1", "agent": "codex", "label": "TEST-worker"}]

        def listed_rows(kind: str) -> list[dict[str, object]]:
            return panes if kind == "pane" else []

        info = {"shell_pid": 998080, "foreground_process_group_id": 998091,
                "foreground_processes": [{"pid": 998091, "argv": ["codex"]}]}
        # A failed stat read keeps the unknown process when the pane read also fails.
        def answer(*_: str) -> dict[str, object]:
            return {"process_info": info}

        reads: tuple[object, ...] = (answer, herdr_types.HerdrFailure("fixture unread"))
        for read in reads:
            with self.subTest(read=type(read).__name__):
                self.assert_codex_pane_keeps_unknown(client, environment, listed_rows, read)

    def assert_codex_pane_keeps_unknown(self, client: herdr.Herdr, environment: dict[str, str],
                                        listed_rows: Callable[[str], list[dict[str, object]]],
                                        read: object) -> None:
        with (patch.dict(os.environ, environment),
              patch.object(herdr, "native_process", side_effect=herdr_types.HerdrFailure("fixture unknown")),
              patch.object(herdr, "_process_display_name", return_value="TEST-worker"),
              patch.object(client, "listed", side_effect=listed_rows),
              patch.object(client, "result", side_effect=read) as process,
              patch.object(herdr, "pane_native_process", side_effect=herdr_types.HerdrFailure("fixture unknown")),
              patch.object(session_records, "enumerate_running_sessions",
                           return_value=session_records.RunningSessionEnumeration((), ())),
              patch.object(herdr, "running_codex_threads", return_value=set()),
              patch.object(restart, "_read_live_members", return_value=restart.CompleteLiveRows([])) as members):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(()), client).rows, [])
        observed = cast(herdr.CodexConversations, members.call_args.kwargs["codex"])
        self.assertEqual(observed.unknown, ("TEST-worker (Codex thread unknown)",))
        process.assert_called_once_with("pane", "process-info", "--pane", "w1:p1")

    def test_unassigned_live_session_uses_tab_pane_and_id_before_registry_or_bare_id(self) -> None:
        client = herdr.Herdr("agents")
        identities = ("a-pane", "b-registry", "c-bare")
        self.claude.update(identities)
        records = list(registry_confirmation(frozenset(identities)).records)
        for record in records:
            record.update(name="", cwd="")
            if record["sessionId"] == "a-pane":
                record["cwd"] = "/TEST-ignored-registry-folder"
            elif record["sessionId"] == "b-registry":
                record.update(name="TEST-registry-name", cwd="/TEST-registry-folder")
        panes: list[dict[str, object]] = [{"pane_id": "w11:p1F", "tab_id": "w11:t1", "agent": "claude", "label": "probe28",
                  "agent_session": {"kind": "id", "value": "a-pane"}}]
        tabs: list[dict[str, object]] = [{"tab_id": "w11:t1", "label": "builds-queue"}]

        def listed_rows(kind: str) -> list[dict[str, object]]:
            return panes if kind == "pane" else tabs
        info = {"shell_pid": 998080, "foreground_process_group_id": 998081,
                "foreground_processes": [{"pid": 998081, "argv": ["claude"]}]}
        with (patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())),
              patch.object(session_records, "enumerate_running_sessions",
                           return_value=session_records.RunningSessionEnumeration(tuple(records), ())),
              patch.object(client, "listed", side_effect=listed_rows) as listed,
              patch.object(client, "result", return_value={"process_info": info}),
              patch.object(client, "pane_tab", side_effect=AssertionError("reuse the pane read")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(()), client).rows, [])
        self.assertEqual(errors.getvalue().splitlines(), [
            "builds-queue probe28 (w11:p1F): restart anomaly: unassigned live session",
            "TEST-registry-name in /TEST-registry-folder: restart anomaly: unassigned live session",
            "c-bare: restart anomaly: unassigned live session"])
        self.assertEqual(listed.call_args_list.count(call("pane")), 1)

    def test_full_restart_requires_complete_registry_discovery(self) -> None:
        registry = Path(os.environ["NOTIFIER_SESSIONS_DIR"])
        path = registry / "unfinished.json"
        for policy_error in (None, PermissionError("retry policy denied")):
            with self.subTest(policy_error=policy_error):
                path.unlink(missing_ok=True)
                if policy_error is None:
                    _ = path.write_text("{")
                    reason = f"session registry unreadable: {path}"
                else:
                    reason = f"session registry discovery failed: {policy_error}"
                with (patch.object(session_records, "enumerate_running_sessions", wraps=ENUMERATE_RUNNING_SESSIONS),
                      patch.object(session_records, "retry_policy", wraps=session_records.retry_policy,
                                   side_effect=policy_error),
                      patch.object(herdr.Herdr, "save_layout") as layout,
                      patch.object(herdr.Herdr, "remember_row_tab") as remember,
                      patch.object(restart, "stop_row") as stop,
                      patch.object(restart, "start_row") as start,
                      patch.object(shutdown_record, "update") as update,
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(stop_all=True), 1)
                    self.assertEqual(restart.stop_all(), 1)
                self.assertIn(f"restart: {reason}", errors.getvalue())
                layout.assert_not_called()
                remember.assert_not_called()
                stop.assert_not_called()
                start.assert_not_called()
                update.assert_not_called()

    def test_full_restart_uses_one_registry_read_for_selection(self) -> None:
        path = self.root / "unreadable.json"
        complete = session_records.RegistryRead((), ())
        for later_read in (PermissionError("retry policy denied"), session_records.RegistryRead((), (path,))):
            for keep_stopped in (True, False):
                with (self.subTest(later_read=later_read, keep_stopped=keep_stopped),
                      patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(self.rows)),
                      patch.object(session_records, "read_registry", side_effect=[complete, later_read]) as read,
                      patch.object(session_records, "enumerate_running_sessions", wraps=ENUMERATE_RUNNING_SESSIONS),
                      patch.object(sessions, "session_liveness", wraps=session_records.session_liveness) as liveness,
                      patch.object(herdr.Herdr, "save_layout") as layout,
                      patch.object(restart, "stop_row") as stop,
                      patch.object(restart, "start_row") as start):
                    self.assertEqual(restart.stop_all() if keep_stopped else restart.restart(stop_all=True), 0)
                    read.assert_called_once_with()
                    layout.assert_called_once_with()
                    stop.assert_not_called()
                    start.assert_not_called()
                    self.assertEqual(liveness.call_args_list, [call(str(row.current_session)) for row in self.rows])
                    if isinstance(later_read, PermissionError):
                        with self.assertRaisesRegex(PermissionError, "retry policy denied"):
                            _ = sessions.session_liveness("missing")
                    else:
                        self.assertEqual(sessions.session_liveness("missing"),
                            sessions.SessionLivenessUnknown(f"session registry unreadable: {path}"))

    def test_live_rows_scans_codex_once_for_members_and_unassigned_candidates(self) -> None:
        with (patch.object(herdr, "proc_root", return_value=self.proc) as scans,
              patch.object(herdr.Herdr, "save_layout") as save,
              redirect_stderr(io.StringIO()) as errors):
            selected = restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("agents"), save_tabs=False).rows
        self.assertEqual(selected, list(self.rows))
        self.assertEqual(errors.getvalue().splitlines(), [
            "claude-orphan: restart anomaly: unassigned live session",
            "codex-orphan: restart anomaly: unassigned live session"])
        save.assert_not_called()
        scans.assert_called_once()

    def test_live_members_with_only_claude_rows_does_not_scan_codex(self) -> None:
        rows = self.rows[::2]
        with patch.object(herdr, "proc_root", return_value=self.proc) as scans:
            self.assertEqual(restart.live_members(rows, staff.StaffSnapshot(self.rows)), list(rows))
        scans.assert_not_called()

    def test_live_members_shares_one_scan_and_reads_fresh_evidence_each_pass(self) -> None:
        with patch.object(herdr, "proc_root", return_value=self.proc) as scans:
            self.assertEqual(restart.live_members(iter(self.rows), staff.StaffSnapshot(self.rows)), list(self.rows))
            scans.assert_called_once()
            self.codex[998081] = "changed-thread"
            self.assertEqual(restart.live_members(iter(self.rows), staff.StaffSnapshot(self.rows)), list(self.rows[::2]) + [self.rows[3]])
            self.assertEqual(scans.call_count, 2)

    def test_live_members_uses_a_supplied_snapshot_without_a_scan(self) -> None:
        snapshot = herdr.CodexConversations({"codex-a": (998081,)}, ())
        with patch.object(herdr, "proc_root", return_value=self.proc) as scans:
            self.assertEqual(restart.live_members(self.rows, staff.StaffSnapshot(self.rows), codex=snapshot), list(self.rows[:3]))
        scans.assert_not_called()

    def test_live_rows_reports_one_shared_cause_and_one_unproved_row_count(self) -> None:
        snapshot = herdr.CodexConversations({}, ("test-account (Codex daemon unknown: unreadable)",))
        with (patch.object(herdr, "codex_conversations", return_value=snapshot) as scan,
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(session_records, "enumerate_running_sessions",
                           return_value=session_records.RunningSessionEnumeration((), ())),
              patch.dict(os.environ, {"SHUTDOWN_DETAIL": "0"}),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("agents")).rows, [])
        scan.assert_called_once()
        self.assertEqual(errors.getvalue().splitlines(), [
            "anomaly: test-account (Codex daemon unknown: unreadable); not blocking",
            "anomaly: 4 rows not proved live or stopped (shared cause above); not blocking"])

    def test_live_rows_detail_reports_each_row_with_shared_uncertainty(self) -> None:
        snapshot = herdr.CodexConversations({}, ("test-account (Codex daemon unknown: unreadable)",))
        with (patch.object(herdr, "codex_conversations", return_value=snapshot),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(session_records, "enumerate_running_sessions",
                           return_value=session_records.RunningSessionEnumeration((), ())),
              patch.dict(os.environ, {"SHUTDOWN_DETAIL": "1"}),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("agents")).rows, [])
        lines = errors.getvalue().splitlines()
        self.assertEqual(len(lines), 6)
        for row, line in zip(self.rows, lines[1:-1], strict=True):
            self.assertIn(f"{row.current_session} is not proved live or stopped", line)
        self.assertEqual(lines[-1], "anomaly: 4 rows not proved live or stopped (shared cause above); not blocking")

    def test_live_members_reports_shared_evidence_once_when_it_reads_the_snapshot(self) -> None:
        snapshot = herdr.CodexConversations({}, ("test-account (Codex daemon unknown: unreadable)",))
        with (patch.object(herdr, "codex_conversations", return_value=snapshot) as scan,
              patch.object(herdr, "running_codex_threads") as threads,
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.dict(os.environ, {"SHUTDOWN_DETAIL": "0"}),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_members(self.rows, staff.StaffSnapshot(self.rows)), [])
        scan.assert_called_once()
        threads.assert_not_called()
        self.assertEqual(errors.getvalue().splitlines(), [
            "anomaly: test-account (Codex daemon unknown: unreadable); not blocking",
            "anomaly: 4 rows not proved live or stopped (shared cause above); not blocking"])


def checkout() -> inventory.GitCheckout:
    return inventory.GitCheckout(
        kind="git", path="/tmp/checkout", head=inventory.OnBranch(kind="branch", name="work"),
        upstream=inventory.Tracking(kind="tracking", ahead=0), dirty=[])


def common_session(session_id: str, name: str | None = None, *, cwd: str | None = None,
    model: inventory.LastModel | None = None, run_dirs: list[str] | None = None,
) -> inventory.SessionFields:
    return {
        "session_id": session_id, "config_dir": str(Path.home() / ".claude2"),
        "pid": 1000 + len(session_id), "proc_start": f"start-{session_id}",
        "name": name or session_id, "cwd": cwd or f"/tmp/{session_id}",
        "status": "idle", "updated_at": 0, "probe": False,
        "model": model or inventory.NoReplyYet(kind="no reply yet"),
        "checkout": checkout(), "run_dirs": list(run_dirs or []), "timers": [],
    }


def top_level(session_id: str, *, name: str | None = None, cwd: str | None = None,
    model: inventory.LastModel | None = None, host: inventory.SessionHost | None = None,
    run_dirs: list[str] | None = None, status: str = "idle",
) -> inventory.TopLevelSession:
    result = inventory.TopLevelSession(
        **common_session(session_id, name, cwd=cwd, model=model, run_dirs=run_dirs),
        kind="top-level", host=host or inventory.UnknownHost(kind="unknown"))
    result["status"] = status
    return result


def showrunner(session_id: str, *, host: inventory.SessionHost | None = None) -> inventory.ShowrunnerSession:
    return inventory.ShowrunnerSession(
        **common_session(session_id), kind="showrunner",
        host=host or inventory.HerdrHost(kind="herdr", herdr_session=f"herdr-{session_id}", pane_id="w1:p1", presentation="detached"),
        production="demo", doc="/tmp/demo-production.md")


def unit(session_id: str, *, plan: inventory.UnitPlan | None = None, session_name: str | None = None,
    session_host: Literal["herdr"] = "herdr", run_dirs: list[str] | None = None, doc: str = "/tmp/demo production.md",
) -> inventory.UnitSession:
    return inventory.UnitSession(
        **common_session(session_id, run_dirs=run_dirs), kind="unit",
        host=unit_host(kind="unit", production="demo", unit=f"{session_id}-unit", doc=doc,
            session_name=session_name or f"herdr-{session_id}", session_host=session_host,
            plan=plan or inventory.PlanFromRunRecord(kind="plan", path=f"/tmp/{session_id} plan.md")))


def worker_session(session_id: str, *, owner: str | None, run_dirs: list[str] | None = None) -> inventory.WorkerSession:
    worker_owner: inventory.WorkerOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.WorkerSession(
        **common_session(session_id, run_dirs=run_dirs), kind="worker",
        host=inventory.UnknownHost(kind="unknown"), owner=worker_owner)


class SessionLaunchTests(RestartTestCase):
    def test_restart_reads_a_claude_transcript_without_a_pane_launch_record(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        identity = "saved"
        records = root / "panes"
        records.mkdir()
        config = root / "config"
        project = config / "projects" / "project"
        project.mkdir(parents=True)
        (project / f"{identity}.jsonl").touch()
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                if compressed:
                    _ = compressed_copy(project / f"{identity}.jsonl")
                output = io.StringIO()
                with (patch.object(pane_record, "RECORD_DIR", records),
                      patch("scripts.agents.restart.sessions.config_dirs", return_value=[config]),
                      patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": "/account"}),
                      redirect_stderr(output)):
                    kind, environment, arguments = restart.session_launch(identity, "current-name")
                self.assertEqual(kind, "claude")
                self.assertEqual(environment, {"CLAUDE_CONFIG_DIR": "/account"})
                self.assertEqual(arguments, ["--resume", identity, "--name", "current-name"])
                self.assertEqual(output.getvalue(), "")

    def test_restart_reads_a_codex_transcript_without_a_pane_launch_record(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        identity = "saved-codex"
        home = root / ".codex"
        source = home / "sessions/2026/10/01" / f"rollout-{identity}.jsonl"
        write_text_atomic(source, json.dumps({"type": "session_meta", "payload": {"id": identity}}) + "\n")
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                if compressed:
                    _ = compressed_copy(source)
                with (patch.object(pane_record, "RECORD_DIR", root / "panes"),
                      patch("scripts.agents.restart.sessions.config_dirs", return_value=[]),
                      patch.dict(os.environ, {"CODEX_HOME": str(home)})):
                    self.assertEqual(restart.session_launch(identity, "current-name"),
                                     ("codex", {"CODEX_HOME": str(home)}, ["resume", identity]))
        numbered = root / ".codex2"
        saved = numbered / "sessions" / f"rollout-{identity}.jsonl"
        write_text_atomic(saved, "{}\n")
        alias = root / ".codex7"
        alias.symlink_to(numbered, target_is_directory=True)
        with (patch.object(pane_record, "RECORD_DIR", root / "panes"),
              patch("scripts.agents.restart.sessions.config_dirs", return_value=[]),
              patch.dict(os.environ, {"HOME": str(root), "CODEX_HOME": str(alias)})):
            self.assertEqual(restart.session_launch(identity, "current-name"),
                             ("codex", {"CODEX_HOME": str(alias)}, ["resume", identity]))
            with patch.dict(os.environ, {"CODEX_HOME": ""}):
                self.assertEqual(restart.session_launch(identity, "current-name"),
                                 ("codex", {"CODEX_HOME": str(home)}, ["resume", identity]))
                source.with_suffix(source.suffix + ".zst").unlink()
                self.assertEqual(restart.session_launch(identity, "current-name"),
                                 ("codex", {"CODEX_HOME": str(numbered)}, ["resume", identity]))

    def test_restart_reads_plugin_directories_from_the_shutdown_pane_record(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        registry = root / "models.conf"
        _ = write_registry(registry)
        snapshot_key = models_registry.OPERATION_ENVIRONMENT[0]
        directories = [str(root / "postmaster"), str(root / "second plugin")]
        entry = pane_record.launch("w1:p2", "saved", str(root), {},
            ["claude", "--plugin-dir", directories[0], "--plugin-dir=" + directories[1]])
        with (patch.object(pane_record, "RECORD_DIR", root / "panes"),
              patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": "/account"}),
              patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(registry), snapshot_key: ""})):
            pane_record.write("agents", entry)
            kind, environment, arguments = restart.session_launch("saved", "current-name")
        self.assertEqual(kind, "claude")
        self.assertEqual(environment, {"CLAUDE_CONFIG_DIR": "/account",
                                       snapshot_key: json.dumps([str(registry), registry.read_text()])})
        parsed = pane_record.parse_claude_arguments(["claude", *arguments])
        self.assertEqual(parsed.plugin_directories, tuple(directories))
        self.assertEqual(parsed.values["--resume"], "saved")
        self.assertEqual(arguments.count("--plugin-dir"), 2)


class RestartFixtureTests(RestartTestCase):
    def test_fixture_excludes_host_sessions_from_orphan_candidates(self) -> None:
        host = Path(self.enterContext(tempfile.TemporaryDirectory()))
        process = host / "proc/998099"
        process.mkdir(parents=True)
        write_text_atomic(process / "cmdline", "codex\0")
        transcript = host / ".claude/projects/host/host-session.jsonl"
        write_text_atomic(transcript, "{}\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(host), "HERDR_PROC": str(host / "proc")})))
        _ = self.enterContext(patch.object(herdr, "_PLATFORM", "linux"))
        _ = self.enterContext(patch.object(herdr, "process_environment", return_value={}))
        _ = self.enterContext(patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
            herdr.EnvironmentThread("host-session"), "fixture-start", {})))
        _ = self.enterContext(patch.object(unit_runs, "run_roots", return_value=()))
        _ = self.enterContext(patch.object(unit_runs, "ASTRA_RUN_ROOT", host / "astra"))
        _ = self.enterContext(patch.object(codex_daemon, "read_daemons", return_value=[]))
        _ = self.enterContext(patch.object(herdr.Herdr, "result", return_value={"panes": [{
            "pane_id": "w9:p9", "workspace_id": "w9", "session_id": "host-session", "label": "host"}]}))
        _ = set_up_fixture(self.addCleanup, SessionLaunchTests(
            "test_restart_reads_a_claude_transcript_without_a_pane_launch_record"))
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(()), herdr.Herdr("agents")).rows, [])
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(herdr.Herdr("agents").panes(), [])
        self.assertNotIn(host / ".claude", sessions.config_dirs())
        self.assertEqual(pane_record.launches(), [])
        self.assertTrue(Path(os.environ["STAFF_FILE"]).is_relative_to(Path(os.environ["NOTIFIER_SESSIONS_DIR"]).parent))


class RecordedResumeTests(RestartTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            _ = write_config_fixture(configuration / source.name)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))

    folder: Path = Path("/tmp/recorded-resume")

    def entry(self, kind: Literal["claude", "codex"], model: str) -> pane_launch.CurrentLaunch:
        return {"kind": kind, "pane_id": "w1:p1", "session_id": "recorded-id",
                "config_dir": "/account", "cwd": str(self.folder), "name": "recorded-name",
                "model": model, "remote_control": True, "environment": {}, "flags_read": True,
                "plugin_directories": ["/plugins/first", "/plugins/second plugin"], "written_at": "fixture"}

    def resumed_arguments(self, entry: pane_record.PaneLaunch) -> list[str]:
        row = staff.StaffRow(staff.StaffId("row"), "unit", "current-name", staff.StaffRoot(),
                            current_session=staff.StaffId("current-id"),
                            worktree=staff.PresentWorktree(remote.local_machine(), self.folder), unit_type=staff.UnitDetails("direct"))
        with (patch.object(pane_record, "launch_for_session", return_value=entry),
              patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": "/account"}),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.NewTab("saved", row.name)),
              patch.object(herdr.Herdr, "panes", return_value=[]),
              patch.object(herdr.Herdr, "remember_row_tab"),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(self.folder, {})),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p1", herdr.PromptSubmissionNotRequired())) as launch):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot((row,)), herdr.Herdr("agents")), "w1:p1")
        return cast(list[str], launch.call_args.args[4])

    def test_codex_resume_keeps_the_recorded_model_and_omits_an_empty_model(self) -> None:
        folder = str(self.folder.resolve())
        for model in ("", "codex-model"):
            with self.subTest(model=model):
                arguments = self.resumed_arguments(self.entry("codex", model))
                expected = ["-C", folder, "-c",
                            f'projects={{"{folder}"={{trust_level="trusted"}}}}', "resume", "current-id"]
                if model:
                    expected.extend(("--model", model))
                self.assertEqual(arguments, expected)

    def test_claude_resume_keeps_model_plugins_and_one_current_name(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory())) / "claude account"
        for recorded_name, turn_model, effort in (("recorded-name", "", ""), ("", "", ""),
                                                 ("recorded-name", "claude-opus-5-5", "high")):
            with self.subTest(recorded_name=recorded_name, turn_model=turn_model):
                entry = self.entry("claude", "claude-model")
                entry["name"] = recorded_name
                entry["config_dir"] = str(home)
                if turn_model:
                    write_text_atomic(home / "projects/prior/current-id.jsonl",
                        json.dumps({"type": "assistant", "message": {"model": turn_model}, "effort": effort}) + "\n")
                arguments = self.resumed_arguments(entry)
                expected = ["--resume", "current-id", "-n", "current-name", "--remote-control", "current-name"]
                expected.extend(("--model", turn_model or "claude-model"))
                if effort:
                    expected.extend(("--effort", effort))
                expected.extend(("--plugin-dir", "/plugins/first",
                                 "--plugin-dir", "/plugins/second plugin", "--settings", '{"disableAgentView": true}'))
                self.assertEqual(arguments, expected)
                names = [arguments[index + 1] for index, value in enumerate(arguments) if value in ("-n", "--name")]
                self.assertEqual(names, ["current-name"])

    def test_resume_uses_requested_identity_without_changing_the_record(self) -> None:
        for kind in ("claude", "codex"):
            with (self.subTest(kind=kind),
                  patch.object(pane_record, "launch_for_session", return_value=(entry := self.entry(kind, "saved-model"))),
                  patch.object(restart, "named_account_environment", return_value={"CLAUDE_CONFIG_DIR": "/account"})):
                _kind, _environment, arguments = restart.session_launch("requested-id", "requested-name")
                self.assertEqual(arguments[:2], ["resume" if kind == "codex" else "--resume", "requested-id"])
                self.assertIn("--model", arguments)
                self.assertEqual(arguments[arguments.index("--model") + 1], "saved-model")
                self.assertEqual(entry["session_id"], "recorded-id")
                self.assertEqual(entry["name"], "recorded-name")
                if kind == "claude":
                    self.assertEqual(arguments[arguments.index("-n") + 1], "requested-name")
                    self.assertEqual(arguments[arguments.index("--remote-control") + 1], "requested-name")
                else:
                    self.assertNotIn("-n", arguments)
                    self.assertNotIn("--name", arguments)


class MixedRestartTests(ShutdownTestCase):
    maxDiff: int | None = None

    def test_mixed_restart_and_stop_share_all_full_reads(self) -> None:
        for commands, role_case in (
            (("restart", "stop"), ""),
            (("restart",), "selected"),
            (("restart",), "owner"),
            (("restart",), "owner-lock"),
            (("restart",), "role-changed"),
        ):
            with self.subTest(commands=commands, role_case=role_case):
                self.run_commands(commands, frozenset(), role_case=role_case)

    def test_failed_worker_stop_keeps_the_live_process_pending(self) -> None:
        self.run_commands(("restart",), frozenset({991104}))

    def test_restart_discards_pane_evidence_from_the_stop_phase(self) -> None:
        pane = "w1:p5"
        late_reads: list[tuple[herdr.PaneIdentity, dict[str, object]]] = []
        signal_process = stop_resume._signal_process  # pyright: ignore[reportPrivateUsage]
        restore_server = herdr.restore_server

        def stop(pid: int, number: int) -> None:
            if pid == 991105 and number == signal.SIGTERM:
                client = herdr.Herdr(herdr.shared_session())
                location = next(row for row in client.panes() if row["pane_id"] == pane)
                foreground = client.result("pane", "process-info", "--pane", pane)
                late_reads.append((location, foreground))
            signal_process(pid, number)

        def restore(name: str, timeout_ms: int, *, session_kind: herdr.SecuritySession
                    ) -> herdr.ServerCreated | herdr.ServerAdopted | herdr.WindowServerCreated:
            server = restore_server(name, timeout_ms, session_kind=session_kind)
            client = herdr.Herdr(name)
            (location, foreground), = late_reads
            # Complete a stop-phase read after its reported process has exited.
            herdr.forget_pane_evidence(name, pane)
            with patch.object(client, "result", return_value=foreground):
                scan = client.scan_row_panes(staff.initial_snapshot(), [dict(location)])
            self.assertEqual([(item.pane, item.reason) for item in scan.unknown], [(pane, "starting or exiting")])
            return server

        with (patch.object(stop_resume, "_signal_process", stop), patch.object(herdr, "restore_server", restore)):
            self.run_commands(("restart",), frozenset())

    def test_count_fixture_selects_linux_readers_on_macos(self) -> None:
        inherited = (sys.platform, herdr._PLATFORM)  # pyright: ignore[reportPrivateUsage]
        self.addCleanup(lambda: self.assertEqual((sys.platform, herdr._PLATFORM), inherited))  # pyright: ignore[reportPrivateUsage]
        with patch.object(herdr, "_PLATFORM", "darwin"), patch.object(sys, "platform", "darwin"):
            self.run_commands(("stop",), frozenset())

    def run_commands(self, commands: tuple[str, ...], surviving: frozenset[int], *,
                     role_case: str = "") -> None:
        with (patch.object(herdr, "_PLATFORM", "linux"), patch.object(sys, "platform", "linux"),
              patch.object(conf, "read", side_effect=short_resume_check_config), ExitStack() as fixture):
            self.linux_commands(commands, surviving, fixture=fixture, role_case=role_case)

    def linux_commands(self, commands: tuple[str, ...], surviving: frozenset[int], *,
                       fixture: ExitStack, role_case: str) -> None:
        self.assertEqual(herdr._PLATFORM, "linux")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(sys.platform, "linux")
        from scripts.herdr import worker_panes
        from scripts.herdr.test_worker_panes import PaneWorld

        root = Path(fixture.enter_context(tempfile.TemporaryDirectory(prefix="mixed-", dir="/tmp")))
        # The fixture files are disposable; on a loaded disk each fsync waits for seconds.
        _ = cast(object, fixture.enter_context(patch.object(os, "fsync")))
        write_text_atomic(root / "staff.json", '{"schema": 2, "rows": []}\n')
        environment = {"HOME": str(root), "STAFF_FILE": str(root / "staff.json"), "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"), "SHUTDOWN_STATE_DIR": str(root / "shutdown"),
            "HERDR_PROC": str(root / "proc"), "HERDR_PANE_RECORD_DIR": str(root / "panes"),
            "CLAUDE_CONFIG_DIR": str(root / ".claude2"), "CODEX_HOME": str(root / ".codex1"),
            "ROSTER_RUN_ROOT": str(root / "runs"), "UNIT_RUN_DIR": str(root / "runs/pane"),
            "CONF_DIR": str(root / "conf"), "SHUTDOWN_PROOF_WORKSPACE": "w1",
            "MODELS_CONFIG_FILE": str(root / "conf/models.conf"), "MODELS_CONFIG_PUBLISH": "0",
            "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}
        _ = cast(object, fixture.enter_context(patch.dict(os.environ, environment)))
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            _ = write_config_fixture(root / 'conf' / source.name)
        registry = root / "conf/models.conf"
        invalid_role = role_case in ("selected", "owner", "owner-lock")
        if invalid_role:
            _ = write_registry(registry, {"assignments": {"direct": "claude"},
                "direct.claude": {"impl": "opus:invalid-effort"}})
        else:
            write_text_atomic(registry,
                Path(__file__).resolve().parents[1].joinpath("lib/test_models_registry.conf").read_text())
        directory = staff.PresentWorktree(remote.local_machine(), root)
        doc = root / "production.md"
        write_text_atomic(doc, "# Fixture\n")
        show = fixture_member("showrunner", name="mixed-show", doc=staff.LiveDoc(remote.local_machine(), doc))
        (root / "other").mkdir()
        other = fixture_member("unit", name="mixed-other", parent=show.id,
            worktree=staff.PresentWorktree(remote.local_machine(), root / "other"))
        seat = fixture_member("worker", name="mixed-seat", parent=other.id)
        owner = fixture_member("unit", name="mixed-pane", parent=show.id, worktree=directory)
        codex_worker = staff.CodexBackend(root / ".codex1")
        workers = [fixture_member("worker", name="impl", parent=owner.id,
                                  backend=staff.ClaudeBackend() if invalid_role else codex_worker),
                   fixture_member("worker", name="review", parent=owner.id, backend=codex_worker)]
        rows = [show, other, seat, owner, *workers]
        run = root / "runs/pane"
        other_run = root / "runs/other"
        write_text_atomic(other_run / "progress_history_state.json", json.dumps({"owner_staff_id": other.id,
            "main_agent": {"session_id": other.current_session}, "pass": {}, "events": []}))
        write_text_atomic(run / "progress_history_state.json", json.dumps({"owner_staff_id": owner.id,
            "open_phases": [{"id": "work", "instance_id": "active", "status": "active", "started_at": 1}],
            "main_agent": {"session_id": owner.current_session, "family": "claude"},
            "history_file": str(run / "history.jsonl"), "pass": {}, "events": []}))
        prompt = run / "prompt.md"
        write_text_atomic(prompt, "Preserve behavior.\n")
        for row in workers:
            role = "astra-reviewer" if invalid_role and row.name == "review" else "impl"
            _ = progress_history.begin_dispatch(run, slot=row.name, phase="work", role=role, worker_name=row.name,
                worker_id=row.id, launcher_command=["assign.sh"], prompt=prompt, kind="impl", lens="")
        world = PaneWorld()
        panes = world.locations
        shell_pids: dict[str, int] = {}
        live: dict[str, int] = {}
        unconfirmed: dict[str, int] = {}
        shell_env: dict[str, dict[str, str]] = {}
        requests: list[str] = []
        signals: list[int] = []
        launches: list[str] = []
        messages: list[tuple[staff.StaffId, str]] = []
        lock = RLock()
        proc = root / "proc"

        def process(pid: int, parent: int, argv: list[str], marks: dict[str, str]) -> None:
            folder = proc / str(pid)
            write_text_atomic(folder / "stat", f"{pid} (fixture) S {parent} {pid} 0 0 {pid} " + "0 " * 13 + "1\n")
            write_text_atomic(folder / "cmdline", "\0".join(argv) + "\0")
            write_text_atomic(folder / "environ", "\0".join(f"{key}={value}" for key, value in marks.items()) + "\0")
            write_text_atomic(folder / "task" / str(pid) / "children", "")
            if not (folder / "cwd").exists():
                (folder / "cwd").symlink_to(root)

        def start(pane: str, argv: list[str]) -> None:
            row = next(row for row in rows if row.current_session in argv)
            index = rows.index(row)
            pid, shell = 991100 + index, shell_pids[pane]
            if (proc / str(pid)).exists():
                pid += 100
            marks = {**environment, **shell_env.get(pane, {}), "HERDR_SESSION": "agents", "HERDR_PANE_ID": pane}
            process(pid, shell, argv, marks)
            write_text_atomic(proc / str(shell) / "task" / str(shell) / "children", str(pid))
            record: sessions.SessionRecord = {"pid": pid, "procStart": "1", "sessionId": row.current_session,
                "name": row.name, "updatedAt": int(time.time()), "messagingSocketPath": "", "tmux": "", "formerNames": []}
            if argv[0] == "claude":
                transcript = Path(marks["CLAUDE_CONFIG_DIR"]) / "projects/fixture" / f"{row.current_session}.jsonl"
                if not transcript.exists():
                    write_text_atomic(transcript, "")
                for folder in (sessions.sessions_dir(), Path(marks["CLAUDE_CONFIG_DIR"]) / "sessions"):
                    write_text_atomic(folder / f"{pid}.json", json.dumps(record))
            else:
                rollout = root / ".codex1/sessions" / f"rollout-{row.current_session}.jsonl"
                write_text_atomic(rollout, json.dumps({"type": "session_meta", "payload": {"id": row.current_session}}) + "\n")
            live[pane] = pid
            panes[pane]["agent_session"] = {"kind": "id", "value": row.current_session}

        def info(pane: str) -> dict[str, object]:
            shell = shell_pids[pane]
            pid = live.get(pane, unconfirmed.get(pane, shell))
            argv = (proc / str(pid) / "cmdline").read_text().strip("\0").split("\0")
            return {"process_info": {"shell_pid": shell, "foreground_process_group_id": pid,
                "foreground_processes": [{"pid": pid, "name": argv[0], "argv": argv, "cwd": str(root / "other" if pane == "w1:p1" else root)}]}}

        def reply(request: dict[str, object]) -> dict[str, object]:
            method = str(request.get("method", ""))
            requests.append(method)
            params = cast(dict[str, object], request.get("params", {}))
            if method == "pane.process_info":
                return info(str(params["pane_id"]))
            if method == "layout.export":
                return world.socket_request(method, **params)
            arguments = cast(list[str], request.get("argv", []))[2:]
            action = arguments[:2]
            if action in (["pane", "list"], ["pane", "process-info"]):
                requests.append(" ".join(action))
            if action == ["status", "server"]:
                return {"running": True}
            if action == ["pane", "list"]:
                return {"panes": list(panes.values())}
            if action == ["pane", "process-info"]:
                return info(arguments[-1])
            if action == ["agent", "list"]:
                return {"agents": [{"pane_id": pane, "agent_status": "idle", "agent": "fixture"} for pane in live]}
            if action == ["agent", "get"]:
                return {"agent": {"agent_status": "idle", "interactive_ready": True}}
            if action == ["agent", "prompt"]:
                pane, text = arguments[2:4]
                identity = cast(dict[str, object], panes[pane]["agent_session"])["value"]
                row = next(row for row in rows if row.current_session == identity)
                messages.append((row.id, text))
                path = root / ".claude2/projects/fixture" / f"{identity}.jsonl"
                if path.exists():
                    event = {"type": "user", "timestamp": datetime.now(timezone.utc).isoformat(),
                             "message": {"role": "user", "content": text}}
                    write_text_atomic(path, path.read_text() + json.dumps(event) + "\n")
                return {}
            if action == ["workspace", "list"]:
                return {"workspaces": [{"workspace_id": "w1", "label": show.name}]}
            if action == ["tab", "list"]:
                return {"tabs": [{"tab_id": f"w1:t{n}", "workspace_id": "w1", "label": row.name}
                    for n, row in enumerate(rows[:4]) if n != 2]}
            if action == ["pane", "split"]:
                anchor = arguments[2]
                pane = "w1:p" + str(1 + max(int(value.removeprefix("w1:p")) for value in world.used_panes))
                panes[pane] = {**panes[anchor], "pane_id": pane, "terminal_id": "term-" + pane}
                _ = panes[pane].pop("agent_session", None)
                world.used_panes.add(pane)
                world.split_leaf(anchor, pane, arguments[arguments.index("--direction") + 1],
                                 float(arguments[arguments.index("--ratio") + 1]))
                shell_pids[pane] = 991000 + int(pane.removeprefix("w1:p"))
                shell_env[pane] = dict(arguments[index + 1].split("=", 1) for index, value in enumerate(arguments[:-1])
                                       if value == "--env")
                process(shell_pids[pane], 990999, ["zsh"], shell_env[pane])
                return {"pane": {"pane_id": pane}}
            if action == ["pane", "close"]:
                world.remove_pane(arguments[2])
            elif action == ["pane", "rename"]:
                panes[arguments[2]]["label"] = arguments[3]
            elif action == ["pane", "run"]:
                tokens = shlex.split(shlex.split(arguments[3])[1])
                if "export" in tokens:
                    shell_env[arguments[2]] = dict(value.split("=", 1) for value in tokens[tokens.index("export") + 1:])
            elif action == ["agent", "start"]:
                pane = arguments[arguments.index("--pane") + 1]
                kind = arguments[arguments.index("--kind") + 1]
                launches.append(pane)
                start(pane, [kind, *arguments[arguments.index("--") + 1:]])
            elif action == ["pane", "read"]:
                return {"text": "────────\n❯ \n────────"}
            return {}

        class Handler(socketserver.StreamRequestHandler):
            @override
            def handle(self) -> None:
                request = cast(dict[str, object], json.loads(self.rfile.readline()))
                with lock:
                    response = reply(request)
                try:
                    _ = self.wfile.write((json.dumps({"result": response}) + "\n").encode())
                except BrokenPipeError:
                    pass

        for failure in (None, BrokenPipeError("client closed")):
            with self.subTest(client_failure=failure):
                handler = object.__new__(Handler)
                handler.rfile = io.BytesIO(b'{}\n')
                stream = io.BytesIO()
                handler.wfile = stream
                with patch.object(stream, "write", side_effect=failure, wraps=stream.write):
                    handler.handle()
                self.assertEqual(stream.getvalue(), b"" if failure else b'{"result": {}}\n')

        endpoint = root / "config/herdr/sessions/agents/herdr.sock"
        endpoint.parent.mkdir(parents=True)
        server = socketserver.ThreadingUnixStreamServer(str(endpoint), Handler)
        # A short poll makes shutdown return at once, not after the default half second.
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        _ = fixture.callback(server.server_close)
        _ = fixture.callback(server.shutdown)
        cli = root / "herdr"
        write_text_atomic(cli, f"#!{sys.executable}\n" +
            "import json, socket, sys\n" + f"with socket.socket(socket.AF_UNIX) as stream:\n    stream.connect({str(endpoint)!r})\n" +
            "    stream.sendall((json.dumps({'argv': sys.argv[1:]}) + '\\n').encode())\n" +
            "    response = stream.makefile().readline()\n" +
            "    if sys.argv[3:5] == ['pane', 'read']:\n        response = json.loads(response)['result']['text']\n" +
            "    print('status: running' if sys.argv[-2:] == ['status', 'server'] else response, end='\\n')\n", new_file_mode=0o700)
        _ = cast(object, fixture.enter_context(patch.dict(os.environ, {"HERDR_BIN": str(cli)})))
        program = herdr.run_program

        def run_program(argv: list[str], timeout: float = herdr.PROGRAM_TIMEOUT_SECONDS, *,
                        environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            # The CLI script gives the same stdout; each start of it costs an interpreter.
            if argv[:1] != [str(cli)]:
                return program(argv, timeout, environment=environment)
            with lock:
                response = reply({"argv": argv[1:]})
            if argv[3:5] == ["pane", "read"]:
                output = str(response["text"])
            else:
                output = "status: running" if argv[-2:] == ["status", "server"] else json.dumps({"result": response})
            return subprocess.CompletedProcess(argv, 0, output + "\n", "")
        _ = fixture.enter_context(patch.object(herdr, "run_program", run_program))
        _ = cast(object, fixture.enter_context(patch.multiple(unit_runs, RUN_ROOT=root / "runs", FORMER_RUN_ROOT=root / "former", ASTRA_RUN_ROOT=root / "astra")))
        def role_account(role: str) -> accounts.AccountSelection:
            return (accounts.CodexWorkersAccount("codex1", root / ".codex1") if role == "workers" else
                    accounts.NamedAccount("claude2", root / ".claude2"))
        _ = fixture.enter_context(patch.object(accounts, "role_account", side_effect=role_account))
        process(990999, 1, ["herdr", "server"], {})
        herdr_listener(proc, 990999, "agents", os.environ)
        read_bytes, kill = Path.read_bytes, os.kill
        def proc_bytes(path: Path) -> bytes:
            return read_bytes(proc / path.relative_to("/proc")) if str(path).startswith("/proc/991") else read_bytes(path)
        def exit_process(pid: int, number: int) -> None:
            if not (proc / str(pid)).exists():
                raise ProcessLookupError(pid)
            if number:
                signals.append(pid)
                pane = next(pane for pane, current in live.items() if current == pid)
                del live[pane]
                if pid in surviving:
                    unconfirmed[pane] = pid
                    raise PermissionError("TEST worker stop is unconfirmed")
                _ = panes[pane].pop("agent_session", None)
                write_text_atomic(proc / str(shell_pids[pane]) / "task" / str(shell_pids[pane]) / "children", "")
                shutil.rmtree(proc / str(pid))
        def signal_process(pid: int, number: int) -> None:
            if not 990999 <= pid < 991300:
                return kill(pid, number)
            # The fake server answers under this lock; an exit is one change to its world.
            with lock:
                exit_process(pid, number)
        _ = fixture.enter_context(patch.object(Path, "read_bytes", proc_bytes))
        _ = fixture.enter_context(patch.object(os, "kill", side_effect=signal_process))
        if role_case == "owner-lock":
            lock_path = root / "rows" / f"{workers[0].id}.lock"
            lock_path.parent.mkdir(exist_ok=True)
            refused_lock = fixture.enter_context(lock_path.open("a+b"))
            fcntl.flock(refused_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsInstance(staff.row_activity(workers[0].id), staff.RowBusy)
        if role_case == "role-changed":
            row_operation = staff.row_operation

            @contextmanager
            def change_role_before_lock(reference: str) -> Generator[None]:
                if reference == workers[0].id:
                    with row_operation(reference):
                        _ = staff.set_worker_role(workers[0].id, staff.KnownWorkerRole("review"))
                with row_operation(reference):
                    yield

            _ = fixture.enter_context(patch.object(staff, "row_operation", change_role_before_lock))
        for command in commands:
            for pid in tuple(live.values()):
                signal_process(pid, signal.SIGTERM)
            requests.clear()
            signals.clear()
            launches.clear()
            messages.clear()
            panes.clear()
            world.layouts.clear()
            for index, row in enumerate(rows):
                if index != 2:
                    pane = f"w1:p{index}"
                    world.add(row, pane, live=False, tab="w1:t3" if index >= 3 else f"w1:t{index}")
                    panes[pane]["terminal_id"] = f"term-{index}"
                    shell_pids[pane] = 991000 + index
                    process(shell_pids[pane], 990999, ["zsh"], {})
            for pane in panes:
                kind = "codex" if pane in ("w1:p1", "w1:p5") or (pane == "w1:p4" and not invalid_role) else "claude"
                start(pane, [kind, "resume" if kind == "codex" else "--resume", str(rows[int(pane[-1])].current_session)])

            def serial_executor(**_options: object) -> ThreadPoolExecutor:
                return ThreadPoolExecutor(max_workers=1)

            with (patch.object(unit_runs, "held_by_session", wraps=unit_runs.held_by_session) as searches,
                  patch.object(herdr, "codex_conversations", wraps=herdr.codex_conversations) as scans,
                  patch.object(herdr, "_scan_herdr_processes", wraps=herdr._scan_herdr_processes) as processes,  # pyright: ignore[reportPrivateUsage]
                  patch.object(restart, "ThreadPoolExecutor", side_effect=serial_executor),
                  patch.object(worker_panes, "start_or_resume", wraps=worker_panes.start_or_resume) as starts,
                  patch.object(pane_record, "seed", wraps=pane_record.seed) as seeds,
                  patch.object(herdr.Herdr, "order_tabs", autospec=True, side_effect=herdr.Herdr.order_tabs) as orders,
                  redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                options = (["--only", ",".join(str(row.id) for row in workers)] if role_case == "selected" else [])
                self.assertEqual(shutdown.main([command, "--all", *options]), 0, errors.getvalue())
            launched_sessions = {cast(dict[str, object], panes[pane]["agent_session"])["value"] for pane in launches}
            if role_case:
                expected_restarts = workers[1:] if role_case == "selected" else [show, other, owner, workers[1]]
                self.assertEqual(live["w1:p4"], 991104)
                self.assertNotIn(991104, signals)
                self.assertEqual(launched_sessions, {row.current_session for row in expected_restarts}, errors.getvalue())
                self.assertEqual(shutdown_record.read(), [])
                self.assertEqual(errors.getvalue(), "")
                (anomaly,) = [line for line in output.getvalue().splitlines() if " / impl:" in line]
                if invalid_role:
                    self.assertIn("impl: invalid role configuration", anomaly)
                    self.assertIn("invalid-effort", anomaly)
                else:
                    self.assertIn("impl: worker role changed from impl to review", anomaly)
                    current = staff.initial_snapshot().rows_by_id[workers[0].id]
                    self.assertEqual(worker_panes.dispatch_role(current), "review")
                continue
            self.assertCountEqual([identity for identity, text in messages if "Continue where you stopped." in text],
                                  [show.id, other.id, owner.id] if command == "restart" else [],
                                  errors.getvalue())
            if surviving:
                self.assertNotIn(workers[0].current_session, launched_sessions, errors.getvalue())
                self.assertTrue((proc / "991104/stat").exists())
                self.assertNotIn("w1:p4", live)
                self.assertEqual(shutdown_record.read(), [str(workers[0].id)])
                self.assertEqual(sum("impl:" in line and "target process still runs" in line
                    for line in output.getvalue().splitlines()), 1, output.getvalue())
                self.assertIn(workers[1].current_session, launched_sessions)
                continue
            self.assertEqual(errors.getvalue(), "", output.getvalue())
            # Both commands use direct native pane reads.
            self.assertEqual((searches.call_count, scans.call_count, processes.call_count),
                             (1, 1, 0), command)
            self.assertNotIn("w1:p2", launches)
            self.assertCountEqual(signals, [991100 + index for index in (0, 1, 3, 4, 5)])
            self.assertEqual(len(launches), 5 if command == "restart" else 0)
            self.assertEqual(len(live), 5 if command == "restart" else 0)
            self.assertGreater(seeds.call_count, 0)
            self.assertIn("pane.process_info", requests)
            if command == "stop":
                # Each row stop reads its panes once; five rows took 60 pane lists before the row scope.
                self.assertLessEqual(requests.count("pane list"), 25, command)
                self.assertLessEqual(requests.count("pane process-info"), 25, command)
            self.assertEqual(starts.call_count, 2 if command == "restart" else 0)
            self.assertEqual(bool(orders.call_count), command == "restart")


class WorkerRestartTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.case: test_worker.WorkerCommandTests = test_worker.WorkerCommandTests("runTest")

    @override
    def setUp(self) -> None:
        _ = set_up_fixture(self.addCleanup, self.case)
        world = self.case.fixture.world
        world.locations["w1:p0"]["agent_session"] = {"value": self.case.fixture.parent.current_session}

    def test_start_row_keeps_an_idle_worker_closed(self) -> None:
        opened = self.case.opened()
        world = self.case.fixture.world
        _ = world.stop(opened.row, world)
        before = len(world.launches)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(restart, "_saved_launch", side_effect=AssertionError("workers require worker restore")) as saved,
              patch.object(restart, "session_launch", wraps=restart.session_launch) as generic,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.start_row(opened.row, staff.snapshot(), world), "")
        self.assertEqual(output.getvalue(), f"{self.case.fixture.parent.name}: 0 workers restored\n")
        self.assertEqual(len(world.launches), before)
        self.assertNotIn(opened.pane, world.foreground)
        saved.assert_not_called()
        generic.assert_not_called()

    def test_start_row_keeps_a_closed_selected_worker_with_an_unreadable_sibling_closed(self) -> None:
        selected, sibling = self.case.opened(), self.case.opened("review", "review")
        world = self.case.fixture.world
        _ = world.stop(selected.row, world)
        self.assertIsInstance(worker_panes.close(self.case.fixture.parent.id,
            staff.WorkerName.parse(selected.row.name)), worker_panes.Closed)
        world.unknown[sibling.pane] = "sibling process is unreadable"
        before = len(world.launches)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.start_row(selected.row, staff.snapshot(), world,
                worker_scope=restart.WorkerRestartScope.SELECTED_ROWS), "")
        self.assertEqual(output.getvalue(), f"{self.case.fixture.parent.name}: 0 workers restored\n")
        self.assertEqual(errors.getvalue(), f"herdr agents:{sibling.pane} tab w1:t1: pane evidence is unknown: "
                         + "sibling process is unreadable; row placement retained\n")
        self.assertEqual(len(world.launches), before)
        self.assertNotIn(selected.pane, world.locations)
        self.assertIn(sibling.pane, world.foreground)

    def test_start_row_resumes_one_worker_without_starting_its_owner(self) -> None:
        opened = self.case.opened("review", "review")
        initial = self.case.fixture.world.requests[-1][1]
        _ = progress_history.begin_dispatch(self.case.run_dir, slot="review", phase="8", role="review",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.case.prompt, kind="review", lens="")
        world = self.case.fixture.world
        _ = world.stop(opened.row, world)
        before = len(world.launches)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(worker, "restore", wraps=worker.restore) as restore,
              patch.object(restart, "_saved_launch", side_effect=AssertionError("workers require worker restore")) as saved,
              patch.object(restart, "session_launch", wraps=restart.session_launch) as generic,
              redirect_stdout(io.StringIO())):
            resumed_pane = restart.start_row(opened.row, staff.snapshot(), world)
        self.assertEqual(resumed_pane, opened.pane)
        self.assertEqual(world.locations[resumed_pane]["terminal_id"], opened.terminal)
        self.assertEqual(world.closed, [])
        self.assertEqual(staff.snapshot().by_id(opened.row.id), replace(opened.row, role=staff.KnownWorkerRole("review")))
        restore.assert_called_once()
        self.assertEqual(restore.call_args.args, (self.case.fixture.parent.id,))
        self.assertEqual(restore.call_args.kwargs["only"], frozenset({opened.row.id}))
        saved.assert_not_called()
        generic.assert_not_called()
        self.assertEqual(len(world.launches), before + 1)
        self.assertIn("w1:p0", world.foreground)
        self.assertIn(resumed_pane, world.foreground)
        kind, arguments, environment = world.requests[-1]
        self.assertEqual(kind, "codex")
        self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-test")
        self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
        self.assertEqual(arguments[arguments.index("--ask-for-approval") + 1], "never")
        self.assertTrue(any(argument.startswith('model_reasoning_effort="') for argument in arguments))
        self.assertEqual(arguments[arguments.index("--model"):], initial[initial.index("--model"):])
        self.assertEqual(environment["UNIT_TEAM_ROLE"], "review")
        self.assertIn(opened.row.current_session, arguments)
        with (patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", ())),
              patch.object(worker, "restore", wraps=worker.restore) as restore,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.start_row(opened.row, staff.snapshot(), world), "")
        restore.assert_not_called()
        self.assertEqual(output.getvalue(), f"{opened.row.name}: unchanged\n")

    def test_start_row_resumes_the_selected_worker_on_the_requested_account_without_its_sibling(self) -> None:
        world = self.case.fixture.world
        for index, folder in enumerate(("codex2", ".codex2")):
            with self.subTest(folder=folder):
                selected = self.case.opened(f"impl{index}")
                sibling = self.case.opened(f"review{index}", "review")
                for opened in (selected, sibling):
                    _ = progress_history.begin_dispatch(self.case.run_dir, slot=opened.row.name, phase="8", role="impl",
                        worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
                        prompt=self.case.prompt, kind="impl", lens="")
                    _ = world.stop(opened.row, world)
                destination = self.case.fixture.root / folder
                before = len(world.launches)
                with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
                      patch.object(agent_accounts, "codex_account_home", return_value=agent_accounts.CodexHomeHoldingLogin(
                          "codex2", destination, "fixture-login")),
                      redirect_stdout(io.StringIO())):
                    resumed_pane = restart.start_row(selected.row, staff.snapshot(), world,
                        target_account=accounts.CodexWorkersAccount("codex2", destination),
                        worker_scope=restart.WorkerRestartScope.SELECTED_ROWS)
                self.assertEqual(resumed_pane, selected.pane)
                self.assertEqual(world.locations[resumed_pane]["terminal_id"], selected.terminal)
                self.assertEqual(world.closed, [])
                self.assertEqual(staff.snapshot().by_id(selected.row.id), replace(selected.row,
                    role=staff.KnownWorkerRole("impl"), home=staff.SessionHome(destination)))
                self.assertIn(selected.row.current_session, world.requests[-1][1])
                self.assertEqual(len(world.launches), before + 1)
                self.assertEqual(world.requests[-1][2]["CODEX_HOME"], str(destination))
                self.assertIn(resumed_pane, world.foreground)
                self.assertNotIn(sibling.pane, world.foreground)
                self.assertIsInstance(progress_history.active_dispatch(sibling.row.id, self.case.run_dir),
                                      progress_history.ActiveDispatch)

    def test_start_row_keeps_the_selected_unnumbered_codex_home(self) -> None:
        opened = self.case.opened()
        _ = progress_history.begin_dispatch(self.case.run_dir, slot=opened.row.name, phase="8", role="impl",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.case.prompt, kind="impl", lens="")
        world = self.case.fixture.world
        _ = world.stop(opened.row, world)
        destination = self.case.fixture.root / ".codex"
        selected = accounts.CodexWorkersAccount("codex", destination)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(agent_accounts, "codex_account_home", wraps=agent_accounts.codex_account_home) as resolve,
              patch.object(worker, "restore", wraps=worker.restore) as restore,
              redirect_stdout(io.StringIO())):
            resumed_pane = restart.start_row(opened.row, staff.snapshot(), world,
                target_account=selected, worker_scope=restart.WorkerRestartScope.SELECTED_ROWS)
        self.assertEqual(resumed_pane, opened.pane)
        self.assertEqual(world.locations[resumed_pane]["terminal_id"], opened.terminal)
        self.assertEqual(world.closed, [])
        self.assertEqual(staff.snapshot().by_id(opened.row.id), replace(opened.row,
            role=staff.KnownWorkerRole("impl"), home=staff.SessionHome(destination)))
        self.assertIs(cast(object, restore.call_args.kwargs["account"]), selected)
        resolve.assert_not_called()
        self.assertEqual(world.requests[-1][2]["CODEX_HOME"], str(destination))
        self.assertIn(opened.row.current_session, world.requests[-1][1])


class RestartTests(RestartTestCase):
    root: Path = Path()
    rows: tuple[staff.StaffRow, ...] = ()
    running: set[str] = set()
    launches: list[tuple[str, herdr.NewTab | herdr.SplitPane | herdr.ExistingPane]] = []
    stops: list[str] = []
    workspace_label: str | None = "live"
    inherited_log: int = -1

    def launch_detached(self, arguments: list[str], _task: str, *, log: Path, log_fd: int, **_options: object) -> Path:
        with os.fdopen(os.dup(log_fd), "ab") as stream:
            self.inherited_log = stream.fileno()
            self.assertEqual(self.detached_stop(log, arguments), 0)
        return log

    def claim_detached_log(self, _arguments: list[str], _task: str, *, log: Path, **_options: object) -> Path:
        log.with_suffix(".claim").touch(exist_ok=False)
        return log

    def detached_stop(self, path: Path, arguments: list[str]) -> int:
        argv = ["restart.py", "--log", str(path), "--log-fd", str(self.inherited_log), *arguments]
        with patch.object(sys, "argv", argv):
            return restart.main()

    @override
    def setUp(self) -> None:
        super().setUp()
        configuration = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            _ = write_config_fixture(configuration / source.name)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        _ = write_config_fixture(configuration / "staff.conf", "PRODUCER_WORKSPACE=producers\n")
        _ = write_registry(configuration / "models.conf", {"producers": {
            "unit-0": "claude:opus:max", "probe-codex": "codex:gpt-test:high",
            "morty": "claude:opus:max", "TEST-agent": "claude:opus:max",
            "TEST-brain": "codex:gpt-test:high", "owner": "claude:opus:max"}})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(configuration / "models.conf")})))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / ".claude2"
        config.mkdir()
        _ = (config / ".claude.json").write_text(json.dumps({
            "projects": {str(self.root): {"hasTrustDialogAccepted": True}},
        }))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root),
                                                     "XDG_STATE_HOME": str(self.root), "HOME": str(self.root),
                                                     "CODEX_HOME": str(self.root / ".codex"),
                                                     "CLAUDE_CONFIG_DIR": str(config)})))
        _ = self.enterContext(patch.object(herdr.Herdr, "run_in_pane"))
        self.rows = tuple(staff.StaffRow(staff.StaffId(f"row-{n}"), "unit", f"unit-{n}", staff.StaffRoot(),
            current_session=staff.StaffId(f"conversation-{n}"),
            worktree=staff.PresentWorktree(remote.local_machine(), self.root),
            home=staff.SessionHome(config), unit_type=staff.UnitDetails("direct")) for n in range(3))
        from scripts.production.staff_test_support import fixture_session

        for row in self.rows:
            fixture_session(str(row.current_session), staff.ClaudeBackend())
        self.running = {str(row.current_session) for row in self.rows}
        self.launches = []
        self.stops = []
        record = staff.StaffSnapshot(self.rows)
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=record))
        _ = self.enterContext(patch.object(session_records, "read_registry", side_effect=self.registry))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.liveness))
        _ = self.enterContext(patch.object(stop_resume, "stop", side_effect=self.stop))
        _ = self.enterContext(patch.object(stop_resume, "stop_unit_workers", return_value=[], create=True))
        _ = self.enterContext(patch.object(restart, "session_launch", side_effect=self.session_launch))
        _ = self.enterContext(patch.object(accounts, "accounts_home", return_value=self.root))
        _ = self.enterContext(patch.object(accounts, "trust_sources", return_value=[]))
        _ = self.enterContext(patch.object(herdr, "prepare_session_environment",
            return_value=herdr.PreparedAgentEnvironment(self.root, {})))
        _ = self.enterContext(patch.object(herdr, "launch_prepared_agent", side_effect=self.launch))
        _ = self.enterContext(patch.object(herdr.Herdr, "prompt"))
        _ = self.enterContext(patch.object(herdr, "restore_server"))
        _ = self.enterContext(patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())))
        _ = self.enterContext(patch.object(herdr.Herdr, "sort_tabs"))
        _ = self.enterContext(patch.object(herdr.Herdr, "save_layout"))
        _ = self.enterContext(patch.object(herdr.Herdr, "remember_row_tab"))
        _ = self.enterContext(patch.object(herdr.Herdr, "tab_sessions", return_value=[]))
        from scripts.agents.test_pane_record_recovery import fixture_launch_transcript

        saved_records = pane_record.save
        def save(session: str, entries: dict[str, pane_record.PaneLaunch]) -> None:
            for entry in entries.values():
                fixture_launch_transcript(self.root, entry, staff.StaffSnapshot(self.rows))
            saved_records(session, entries)
        _ = self.enterContext(patch.object(pane_record, "save", side_effect=save))
        publish_record = pane_record.write
        def write(session: str, entry: pane_record.PaneLaunch) -> None:
            fixture_launch_transcript(self.root, entry, staff.StaffSnapshot(self.rows))
            publish_record(session, entry)
        _ = self.enterContext(patch.object(pane_record, "write", side_effect=write))
        _ = self.enterContext(patch.object(herdr.Herdr, "result", side_effect=self.layout_result))
        _ = self.enterContext(patch.object(herdr.Herdr, "panes", side_effect=self.panes))
        _ = self.enterContext(patch.object(herdr.Herdr, "native_session", side_effect=self.native))
        def native_process(pid: int, _kind: str) -> herdr.NativeAgentProcess:
            return herdr.NativeAgentProcess(herdr.EnvironmentThread(
                str(self.rows[pid - 990100].current_session)), "fixture-start", {})
        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=native_process))
        _ = self.enterContext(redirect_stderr(io.StringIO()))
        layout: herdr.SavedLayout = {"at": 1, "workspaces": [{"name": "saved", "seen": 1,
            "tabs": [{"staff_id": str(row.id), "seen": 1} for row in reversed(self.rows)]}]}
        _ = self.enterContext(patch.object(herdr, "saved_layout", return_value=layout))

    @staticmethod
    def records_for(identities: set[str]) -> list[sessions.SessionRecord]:
        records = list(registry_confirmation(frozenset(identities)).records)
        for record in records:
            record.update(pid=os.getpid(), procStart=sessions.process_start(os.getpid()) or "")
        return records

    @staticmethod
    def registry_for(records: list[sessions.SessionRecord]) -> session_records.RegistryRead:
        return session_records.RegistryRead(
            tuple(session_records.RegistryObservation(record, {}) for record in records), ())

    def registry(self, **_kwargs: object) -> session_records.RegistryRead:
        return self.registry_for(self.records_for(self.running))

    @staticmethod
    def session_launch(identity: str, name: str) -> tuple[Literal["claude"], dict[str, str], list[str]]:
        return "claude", {}, ["--resume", identity, "--name", name]

    def layout_result(self, *arguments: str) -> dict[str, object]:
        if arguments == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "saved", "label": "saved"}, {"workspace_id": "w1", "label": self.workspace_label}]}
        if arguments == ("tab", "list"):
            return {"tabs": [{"tab_id": f"w1:t{n}", "workspace_id": "w1", "label": row.name}
                             for n, row in enumerate(self.rows)]}
        if arguments == ("pane", "list"):
            return {"panes": [{**pane, "tab_id": f"w1:t{pane['pane_id'].rsplit('p', 1)[1]}"} for pane in self.panes()]}
        if arguments[:3] == ("pane", "process-info", "--pane"):
            index = int(arguments[3].rsplit("p", 1)[1])
            return {"process_info": {"shell_pid": 990090, "foreground_process_group_id": 990100 + index,
                "foreground_processes": [{"pid": 990100 + index, "argv": ["claude"]}]}}
        raise AssertionError(arguments)

    def liveness(self, identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        if identity in self.running:
            for index, row in enumerate(self.rows):
                if row.current_session == identity:
                    return sessions.LiveSessionProcess(990100 + index)
        return sessions.SessionNotLive()

    def panes(self) -> list[herdr.PaneIdentity]:
        return [{"pane_id": f"w1:p{n}", "workspace_id": "w1", "session_id": row.current_session,
                 "terminal_id": f"terminal-{n}"} for n, row in enumerate(self.rows)
                if row.current_session in self.running]

    def native(self, pane: str, _kind: str, _timeout: float) -> str:
        return str(self.rows[int(pane.rsplit("p", 1)[1])].current_session)

    def stop(self, identity: str, *, deadline: float = math.inf,
             codex: herdr.CodexConversations | None = None,
             runs: dict[str, list[str]] | None = None) -> stop_resume.Stopped:
        _ = deadline, codex, runs
        row = next(row for row in self.rows if row.current_session == identity)
        self.stops.append(str(row.id))
        self.running.discard(str(row.current_session))
        return stop_resume.Stopped(str(row.current_session), 123)

    def launch(self, _name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
               _prepared: herdr.PreparedAgentEnvironment, _kind: str, arguments: list[str],
               _opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, _timeout: int, *, agent: str = "", reported_unknown: str = "",
               codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT,
               client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> herdr.AgentLaunched:
        _ = reported_unknown, codex, client
        row = next(row for row in self.rows if row.name == agent)
        expected = ["--resume", str(row.current_session), "--name", row.name]
        if row.kind == "producer":
            expected.extend(("--remote-control", row.name, "--model", "opus", "--effort", "max",
                             "--settings", '{"disableAgentView": true}'))
        self.assertEqual(arguments, expected)
        if isinstance(_opening, herdr.OpeningPrompt):
            self.assertIn(row.kind, ("unit", "showrunner"))
            self.assertIn("Continue where you stopped.", _opening.text)
        else:
            self.assertIsInstance(_opening, herdr.NoOpeningPrompt)
        self.assertEqual(_prepared.directory, self.root)
        self.launches.append((agent, destination))
        self.running.add(str(row.current_session))
        return herdr.AgentLaunched(f"w1:p{self.rows.index(row)}", herdr.PromptSubmissionNotRequired())

    def test_unconfirmed_stop_of_live_codex_does_not_launch(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {str(row.current_session): (990100,)}, ())) as scans,
              patch.object(restart, "stop_row", return_value=False),
              patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", str(row.current_session)])),
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.PaneSessionReport(str(row.current_session)), "fresh", {})),
              patch.object(sessions, "process_liveness", return_value=sessions.LiveSessionProcess(990100)),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.restart(stop_all=True), 0)
        launch.assert_not_called()
        self.assertEqual(scans.call_count, 1)
        self.assertIn(f"{row.name}: unchanged", output.getvalue())

    def test_unconfirmed_stop_of_exited_codex_launches(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {str(row.current_session): (990100,)}, ())) as scans,
              patch.object(restart, "stop_row", return_value=False),
              patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", str(row.current_session)])),
              patch.object(herdr, "native_process", side_effect=ProcessLookupError("target exited")),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.restart(stop_all=True), 0)
        launch.assert_called_once()
        self.assertEqual(scans.call_count, 1)
        self.assertIn(f"{row.name}: restarted", output.getvalue())

    def test_unclear_identity_after_stop_keeps_the_running_target_pending(self) -> None:
        row = self.rows[0]
        for reason in ("rollout is missing", "generation changed"):
            with self.subTest(reason=reason):
                self.running.clear()
                shutdown_record.save([])
                with (patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
                      patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                          {str(row.current_session): (990100,)}, ())),
                      patch.object(restart, "stop_row", return_value=False),
                      patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", str(row.current_session)])),
                      patch.object(herdr, "native_process", side_effect=FileNotFoundError(reason) if reason.startswith("rollout") else None,
                          return_value=herdr.NativeAgentProcess(herdr.PaneSessionReport(str(row.current_session)), "fresh", {})),
                      patch.object(sessions, "process_liveness", return_value=sessions.SessionNotLive()),
                      patch.object(os, "kill"),
                      patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                          "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(stop_all=True), 0)
                launch.assert_not_called()
                self.assertEqual(shutdown_record.read(), [str(row.id)])
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertTrue(errors.getvalue().startswith(f"{row.name}: restart anomaly:"))
                self.assertIn(reason, errors.getvalue())

    def test_restart_retains_dead_row_after_unconfirmed_stop_and_failed_launch(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations(
                  {str(row.current_session): (990100,)}, ())),
              patch.object(restart, "stop_row", return_value=False),
              patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", str(row.current_session)])),
              patch.object(herdr, "launch_prepared_agent", side_effect=OSError("launch failed")) as launch,
              redirect_stdout(io.StringIO())):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(shutdown_record.read(), [str(row.id)])
        launch.assert_called_once()

    def test_restart_reports_workers_without_finished_dispatches(self) -> None:
        owner = replace(self.rows[0], name="efficiency-unit")
        good, bad = (replace(row, name=name, kind="worker", role=staff.KnownWorkerRole("impl"),
                             unit_type=staff.NotUnit(), parent=staff.ReportsTo(owner.id))
                     for row, name in zip(self.rows[1:], ("restart-output", "macdata-bytes")))
        record = staff.StaffSnapshot((owner, good, bad))
        run = self.root / "owner-run"
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({
            unit_runs.OWNER_FIELD: str(owner.id),
            "main_agent": {"session_id": str(owner.current_session)},
            "open_phases": [{"id": "46", "instance_id": "open-phase", "status": "active"}],
            "pass": {}, "history_file": str(run / "history.jsonl")}))
        reason = "pane identity is unavailable"
        states: list[worker_panes.WorkerPane] = [
            worker_panes.OpenWorker(good, "w1:p1", "terminal", herdr.AgentStatus.WORKING),
            worker_panes.UncertainWorker(bad, reason)]
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(staff, "authority_snapshot", return_value=record),
              patch.object(worker, "_read_workers", return_value=worker.PaneWorkerStates(states, [run])),
              patch.object(worker_panes, "worker_run", return_value=run)):
            for number in range(30):
                prompt = run / f"prompt-{number}.md"
                write_text_atomic(prompt, f"Read report {number}.\n")
                dispatch = progress_history.begin_dispatch(run, slot=good.name, phase="46", role="impl",
                    worker_name=good.name, worker_id=good.id, launcher_command=["assign.sh"], prompt=prompt, kind="impl", lens="")
                progress_history.complete_dispatch(run, good.name, dispatch)
            shutdown_record.save([str(row.id) for row in record.rows])
            with redirect_stdout(io.StringIO()) as manual, redirect_stderr(io.StringIO()):
                self.assertEqual(worker.restore(owner.id, record=record), worker.ExitCode.UNCERTAIN)
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(restart.restart(), 0)
        self.assertEqual(sum("; dispatch " in line for line in manual.getvalue().splitlines()), 30)
        self.assertEqual(output.getvalue().splitlines(), ["efficiency-unit: 1 of 2 workers restored; see anomaly"])
        self.assertEqual(errors.getvalue().splitlines(), [
            "restart anomaly: efficiency-unit: worker macdata-bytes not restored: pane identity is unavailable; "
            + "run worker.py restore in efficiency-unit"])
        self.assertEqual(shutdown_record.read(), [str(bad.id)])

    def test_bulk_command_output_with_twenty_units_and_506_finished_dispatches(self) -> None:
        history_sizes = (1, 2, 3, 3, 4, 5, 5, 5, 6, 8, 12, 18, 20, 29, 46, 54, 63, 65, 68, 89)
        self.assertEqual(sum(history_sizes), 506)
        rows: list[staff.StaffRow] = []
        observed: dict[staff.StaffId, worker.PaneWorkerStates] = {}
        last_director: staff.StaffRow | None = None
        causes = ("The worker session is unavailable", "The worker transcript is unreadable",
                  "A detached run is waiting for the worker's DONE line", "The worker's staff row is unavailable")
        for index, size in enumerate(history_sizes):
            name = f"unit-{index:02}"
            owner = replace(self.rows[0], id=staff.StaffId(name), name=name, current_session=staff.StaffId(f"director-{index}"))
            ready, waiting = (replace(self.rows[1], kind="worker", name=label, id=staff.StaffId(f"row-{name}-{label}"),
                                      current_session=staff.StaffId(f"{name}-{label}-session"), unit_type=staff.NotUnit(),
                                      role=staff.KnownWorkerRole("impl"), parent=staff.ReportsTo(owner.id))
                              for label in ("ready", "waiting"))
            rows.extend((owner, ready, waiting))
            run = self.root / "scale" / name
            history = run / "history.jsonl"
            write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: owner.id,
                "open_phases": [{"id": "46", "instance_id": "open-phase", "status": "active"}],
                "pass": {}, "history_file": str(history)}))
            write_text_atomic(history, "".join(json.dumps({"event_type": "pass_finished", "status": "completed",
                "phase_id": "46", "phase_instance_id": "open-phase", "worker_id": ready.id,
                "pass_instance_id": f"dispatch-{number}"}) + "\n" for number in range(size)))
            waiting_state: worker_panes.WorkerPane
            if index == len(history_sizes) - 1:
                last_director = owner
                waiting_state = worker_panes.OpenWorker(waiting, "last-worker", "last-terminal", herdr.AgentStatus.WORKING)
            else:
                waiting_state = worker_panes.UncertainWorker(waiting, causes[index % len(causes)])
            observed[owner.id] = worker.PaneWorkerStates([
                worker_panes.OpenWorker(ready, f"pane-{index}", f"terminal-{index}", herdr.AgentStatus.WORKING),
                waiting_state], [run])
        bob = replace(self.rows[2], id=staff.StaffId("scale-bob"), name="bob", kind="producer", unit_type=staff.NotUnit())
        rows.append(bob)
        self.rows = tuple(rows)
        record = staff.StaffSnapshot(tuple(rows))
        assert last_director is not None
        last_identity = last_director.current_session
        row_operation = staff.row_operation
        patch_text = json.dumps("*** Begin Patch\n*** Update File: part`one\n*** End Patch").replace("`", r"\`")
        saved_tool: dict[str, object] = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
            "input": f"await tools.apply_patch({patch_text});"}}

        def read_workers(owner: staff.StaffRow, _record: staff.StaffSnapshot, **_options: object) -> worker.PaneWorkerStates:
            _ = worker_admission.transcript_entry(saved_tool, set())
            herdr.conversation_note(herdr.UnknownConversation("The worker observation is incomplete"))
            return observed[owner.id]

        def worker_run(row: staff.StaffRow, **_options: object) -> Path:
            owner = record.parent_of(row)
            assert owner is not None
            return observed[owner.id].runs[0]

        def observe(owner: staff.StaffId, _record: staff.StaffSnapshot) -> list[worker_panes.WorkerPane]:
            # Exercise worker diagnostics during the stop observation.
            _ = worker.restore(owner, record=record)
            return observed[owner].states

        def conversation(identity: str, _kind: str = "", **_options: object
                         ) -> herdr.LiveConversation | herdr.StoppedConversation:
            if identity == last_identity and identity not in self.running:
                return herdr.StoppedConversation()
            return herdr.LiveConversation("claude", ())

        @contextmanager
        def locked_row(reference: str) -> Generator[None]:
            if reference == bob.id:
                raise staff.StaffRefused("Herdr upkeep still holds bob (pid 123)")
            with row_operation(reference):
                yield

        def notice_stop(row: staff.StaffRow, _client: herdr.Herdr, **_options: object) -> bool:
            print(f"{row.name}: stopped")
            return True

        for arguments in (["restart"], ["stop", "--all"]):
            with (patch.object(staff, "initial_snapshot", return_value=record),
                  patch.object(staff, "authority_snapshot", return_value=record),
                  patch.object(staff, "row_operation", side_effect=locked_row),
                  patch.object(herdr, "read_conversation", side_effect=conversation),
                  patch.object(restart.DirectorResumeCheck, "progress", return_value=restart.ResumeCheckState.RESUMED),
                  patch.object(worker, "_read_workers", side_effect=read_workers),
                  patch.object(worker_panes, "worker_run", side_effect=worker_run),
                  patch.object(worker_panes, "observe", side_effect=observe),
                  patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows(rows)),
                  patch.object(stop_resume, "stop_unit_workers", wraps=STOP_UNIT_WORKERS),
                  patch.object(restart, "stop_row", side_effect=notice_stop),
                  warnings.catch_warnings(record=True) as notices, redirect_stdout(io.StringIO()) as output,
                  redirect_stderr(io.StringIO()) as errors):
                warnings.simplefilter("always", SyntaxWarning)
                shutdown_record.save([str(row.id) for row in rows] if arguments == ["restart"] else [])
                self.assertEqual(shutdown.main(arguments), 0)
            combined = (output.getvalue() + errors.getvalue()).splitlines()
            with self.subTest(command=arguments[0], output_kind="finished dispatches"):
                self.assertEqual(sum("; dispatch " in line for line in combined), 0)
            with self.subTest(command=arguments[0], output_kind="syntax warnings"):
                self.assertEqual(sum(issubclass(item.category, SyntaxWarning) for item in notices), 0)
            with self.subTest(command=arguments[0], output_kind="unnamed anomalies"):
                self.assertEqual(sum(line.startswith("anomaly: ") for line in combined), 0)
            with self.subTest(command=arguments[0], output_kind="line limit"):
                self.assertLessEqual(len(combined), len(rows) + 5)
            lines = output.getvalue().splitlines()
            with self.subTest(command=arguments[0], output_kind="report"):
                self.assertEqual(len(lines), 23, output.getvalue())
                self.assertEqual(Path(lines[0]).read_text(), output.getvalue())
                self.assertEqual(errors.getvalue(), "")
                self.assertIn("41 of 61 sessions " + ("back" if arguments == ["restart"] else "stopped"), lines[1])
                self.assertIn("bob: Herdr upkeep still holds bob", lines[2])
                self.assertIn("19 units need a step", lines[-1])
                self.assertIn(f"Nate runs agents {' '.join(arguments)} again.", lines[-1])
                for index in range(19):
                    self.assertIn(f"unit-{index:02} / waiting", lines[index + 3])
                for cause, count in zip(causes, (5, 5, 5, 4)):
                    self.assertEqual(sum(cause in line for line in lines), count)
                for legacy in ("worker restore is incomplete", "directors resumed", "workers unchanged", ": unchanged", ": stopped\n"):
                    self.assertNotIn(legacy, output.getvalue())

    def test_restart_waits_for_upkeep_and_names_a_row_still_locked(self) -> None:
        row = self.rows[0]
        path = staff.record_path().parent / "rows" / f"{row.id}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        for release in (True, False):
            with self.subTest(release=release):
                config = Path(os.environ["CONF_DIR"]) / "shutdown.conf"
                text = config.read_text().replace("UPKEEP_ROW_WAIT_SECONDS=5", "UPKEEP_ROW_WAIT_SECONDS=0.04")
                write_text_atomic(config, text)
                shutdown_record.save([str(row.id)])
                with path.open("w+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    _ = lock.write(json.dumps({"pid": 123, "command": "/path/herdr_upkeep.py --work"}) + "\n")
                    lock.flush()

                    def release_row(_seconds: float) -> None:
                        fcntl.flock(lock, fcntl.LOCK_UN)

                    with (patch.object(time, "sleep", side_effect=release_row if release else time.sleep) as waited,
                          redirect_stdout(io.StringIO()) as output):
                        self.assertEqual(shutdown.main(["restart"]), 0)
                    waited.assert_called()
                lines = output.getvalue().splitlines()
                self.assertEqual(Path(lines[0]).read_text(), output.getvalue())
                if release:
                    self.assertIn("1 of 1 sessions back", lines[1])
                    self.assertEqual(lines[-1], "Nothing for you to do.")
                    self.assertEqual(shutdown_record.read(), [])
                else:
                    self.assertIn("0 of 1 sessions back", lines[1])
                    self.assertIn(f"{row.name}: Herdr upkeep still holds {row.name}", lines[2])
                    self.assertIn("Nate runs agents restart again.", lines[-1])
                    self.assertEqual(shutdown_record.read(), [str(row.id)])

    def test_detached_stop_appends_to_the_log_printed_by_the_caller(self) -> None:
        row = self.rows[0]

        def launch(arguments: list[str], task: str, *, log: Path, log_fd: int, **_options: object) -> Path:
            stop.assert_not_called()
            self.assertEqual(caller.getvalue(), str(log))
            self.assertEqual(log.read_text(), f"{log}\n")
            _ = log.rename(log.with_suffix(".moved"))
            log.mkdir()
            with redirect_stdout(io.StringIO()) as child:
                result = self.launch_detached(arguments, task, log=log, log_fd=log_fd)
            self.assertEqual(child.getvalue(), "")
            return result

        with (patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              patch.object(restart, "stop_row", return_value=True) as stop,
              patch.object(stop_resume, "detach", side_effect=launch) as detach,
              patch.dict(os.environ, {"CODEX_THREAD_ID": str(row.current_session)}),
              redirect_stdout(io.StringIO()) as caller):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        stop.assert_called_once()
        path = cast(Path, detach.call_args.kwargs["log"])
        self.assertEqual(caller.getvalue().splitlines(), [
            f"{path}  agents stop --all runs in the background. Read its report in this log."])
        self.assertEqual(path.with_suffix(".moved").read_text().splitlines(),
            [str(path), "Sessions to stop (1).", f"{row.name} ({row.id})",
            "agents stop --all: 1 of 1 sessions stopped.", "Nothing for you to do."])

    def test_detached_stop_reports_a_session_with_unreadable_liveness(self) -> None:
        caller, unreadable, stopped = self.rows

        def conversation(identity: str, **_options: object
                         ) -> herdr.LiveConversation | herdr.StoppedConversation | herdr.UnknownConversation:
            if identity == unreadable.current_session:
                return herdr.UnknownConversation("The session liveness is unreadable")
            if identity == stopped.current_session:
                return herdr.StoppedConversation()
            return herdr.LiveConversation("claude", ())

        with (patch.object(herdr, "read_conversation", side_effect=conversation),
              patch.object(restart, "stop_row", return_value=True) as stop,
              patch.object(stop_resume, "detach", side_effect=self.launch_detached) as detach,
              patch.dict(os.environ, {"CODEX_THREAD_ID": str(caller.current_session)}),
              redirect_stdout(io.StringIO()) as parent):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[0], caller)
        path = cast(Path, detach.call_args.kwargs["log"])
        self.assertTrue(parent.getvalue().startswith(f"{path}  agents stop --all runs in the background."))
        self.assertTrue(path.read_text().startswith(f"{path}\n"))
        lines = path.read_text().splitlines()[3:]
        self.assertEqual(lines[0], "agents stop --all: 1 of 2 sessions stopped.")
        self.assertIn(f"{unreadable.name}: The session liveness is unreadable.", lines[1])
        self.assertIn(f"{unreadable.name}: Nate runs agents stop --all again.", lines[-1])

    def test_detached_stop_refuses_a_log_descriptor_that_cannot_write(self) -> None:
        path = self.root / "read-only.log"
        header = f"{path}\n"
        write_text_atomic(path, header)
        row = self.rows[0]
        shutdown_record.save([str(row.id)])
        with (path.open("rb") as log, patch.object(restart, "stop_row") as stop,
              patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              redirect_stderr(io.StringIO()) as errors):
            self.inherited_log = log.fileno()
            self.assertEqual(self.detached_stop(path, ["stop", "--all", "--only", str(row.id)]), 2)
        stop.assert_not_called()
        self.assertEqual(path.read_text(), header)
        self.assertEqual(shutdown_record.read(), [str(row.id)])
        self.assertIn(f"Could not write the log at {path}. The command did not start.", errors.getvalue())

    def test_detached_stop_refuses_to_stop_when_only_one_plan_byte_fits(self) -> None:
        row = self.rows[0]
        path = self.root / "short-plan.log"
        shutdown_record.save([str(row.id)])

        class ShortLog(io.FileIO):
            @override
            def write(self, b: object, /) -> int | None:
                if self.tell():
                    return 0
                return super().write(bytes(memoryview(cast(bytes, b))[:1]))

        with (path.open("wb") as log, patch.object(io, "FileIO", ShortLog),
              patch.object(restart, "stop_row") as stop,
              patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              redirect_stderr(io.StringIO()) as errors):
            self.inherited_log = log.fileno()
            self.assertEqual(self.detached_stop(path, ["stop", "--all", "--only", str(row.id)]), 2)
        stop.assert_not_called()
        self.assertEqual(path.read_text(), "S")
        self.assertIn("The command did not start. Incomplete log write", errors.getvalue())
        self.assertEqual(shutdown_record.read(), [str(row.id)])

    def test_detached_stop_reports_a_failed_result_write_to_stderr(self) -> None:
        row = self.rows[0]
        path = self.root / "failed-result.log"
        stopped = False

        def stop_row(*_args: object, **_options: object) -> bool:
            nonlocal stopped
            self.assertEqual(path.read_text().splitlines(), ["Sessions to stop (1).", f"{row.name} ({row.id})"])
            stopped = True
            return True

        class FailingLog(io.FileIO):
            @override
            def write(self, b: object, /) -> int | None:
                if stopped:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return super().write(cast(bytes, b))

        with (path.open("wb") as log, patch.object(io, "FileIO", FailingLog),
              patch.object(restart, "stop_row", side_effect=stop_row) as stop,
              patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              redirect_stderr(io.StringIO()) as errors):
            self.inherited_log = log.fileno()
            self.assertEqual(self.detached_stop(path, ["stop", "--all", "--only", str(row.id)]), 2)
        stop.assert_called_once()
        self.assertIn("No space left on device", errors.getvalue())
        self.assertIn("agents stop --all: 1 of 1 sessions stopped.", errors.getvalue())
        self.assertEqual(shutdown_record.read(), [str(row.id)])

    def test_detached_launcher_passes_the_open_log_on_both_platforms(self) -> None:
        path = self.root / "detach.log"
        program = Path(restart.__file__)

        def confirm_start(argv: list[str], *, pass_fds: tuple[int, ...], **_options: object) -> None:
            ready_fd = int(argv[argv.index("--ready-fd") + 1])
            self.assertEqual(pass_fds, (ready_fd,))
            _ = os.write(ready_fd, b"1")

        with path.open("xb") as log:
            for platform in ("darwin", "linux"):
                with (self.subTest(platform=platform), patch.object(sys, "platform", platform),
                      patch.object(subprocess, "Popen", side_effect=confirm_start) as popen,
                      patch.object(subprocess, "run") as run):
                    self.assertEqual(stop_resume.detach(["stop", "--all", "--requester", "fixture"],
                        "restart", program=program, log=path, log_fd=log.fileno()), path)
                    launch = run if platform == "linux" else popen
                    launch.assert_called_once()
                    argv = cast(list[str], launch.call_args.args[0])
                    self.assertEqual(argv[argv.index("--log-fd") + 1], "1")
                    if platform == "linux":
                        self.assertEqual(argv[0], "systemd-run")
                        self.assertTrue(run.call_args.kwargs["check"])
                        self.assertIn("--service-type=notify", argv)
                        self.assertIn("--notify-systemd", argv)
                        self.assertNotIn("--wait", argv)
                        self.assertIn(f"--property=StandardOutput=append:/proc/{os.getpid()}/fd/{log.fileno()}", argv)
                        self.assertIn("--property=StandardError=journal", argv)
                    else:
                        self.assertEqual(argv[0], sys.executable)
                        self.assertEqual(popen.call_args.kwargs["stdout"], log.fileno())
                        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_self_stop_reports_a_failed_linux_launch_and_can_run_from_a_shell(self) -> None:
        row = self.rows[0]
        arguments = ["stop", "--all"]
        refusal = "The user manager refused the service"
        step = "Nate opens a plain terminal without a staff session. Nate runs agents stop --all there."
        for error, reason in (
            (subprocess.CalledProcessError(1, ["systemd-run"], stderr=refusal), refusal),
            (FileNotFoundError(errno.ENOENT, "No such file or directory", "systemd-run"),
             "The systemd-run command is missing"),
            (OSError(errno.EIO, "Input/output error"), "Input/output error"),
            (subprocess.CalledProcessError(-signal.SIGTERM, ["systemd-run"]),
             f"A signal stopped the launcher (signal {signal.SIGTERM})"),
            (subprocess.TimeoutExpired(["systemd-run"], 15),
             "The launcher timed out before the detached process claimed the log"),
        ):
            with (self.subTest(error=error), patch.object(sys, "platform", "linux"),
                  patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
                  patch.object(restart, "stop_row", return_value=True) as stop):
                with (patch.dict(os.environ, {"CODEX_THREAD_ID": str(row.current_session)}),
                      patch.object(stop_resume, "detach", wraps=stop_resume.detach) as detach,
                      patch.object(subprocess, "run", side_effect=error) as launch,
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(shutdown.main(arguments), 2)
                detach.assert_called_once()
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[0][0], "systemd-run")
                stop.assert_not_called()
                path, count, issue, summary = output.getvalue().splitlines()
                self.assertEqual(Path(path).read_text(), output.getvalue())
                self.assertEqual(count, "agents stop --all: 0 of 1 sessions stopped.")
                self.assertIn(f"{row.name}: The launcher could not start the detached command.", issue)
                self.assertIn(reason, issue)
                self.assertIn(step, issue)
                self.assertIn(step, summary)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(shutdown.main(arguments), 0)
                stop.assert_called_once()

    def test_self_stop_keeps_the_child_report_after_each_launcher_result(self) -> None:
        row = self.rows[0]
        for outcome in ("exit 0", "signal", "timeout", "full disk"):
            with self.subTest(outcome=outcome):
                self.assert_child_report_survives_launcher(outcome, row)

    def assert_child_report_survives_launcher(self, outcome: str, row: staff.StaffRow) -> None:
        log_path = Path()
        written = b""
        notification = self.root / f"notify-{outcome}"

        def create_claim(path: Path, content: str) -> bool:
            if outcome == "full disk" and path.exists():
                raise OSError(errno.ENOSPC, "The claim folder is full")
            return create_text_atomic(path, content)

        def launch(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            nonlocal log_path, written
            self.assertEqual(argv[0], "systemd-run")
            log_index = argv.index("--log")
            log_path = Path(argv[log_index + 1])
            self.assertEqual(output.getvalue(), str(log_path))
            arguments = [argv[log_index - 1], *argv[log_index + 4:]]
            with (log_path.open("ab") as log, patch.dict(os.environ, {"NOTIFY_SOCKET": str(notification)})):
                self.inherited_log = log.fileno()
                self.assertEqual(self.detached_stop(log_path, arguments), 0)
            self.assertEqual(notifier.recv(16), b"READY=1")
            written = log_path.read_bytes()
            if outcome == "signal":
                raise subprocess.CalledProcessError(-signal.SIGTERM, argv)
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(argv, 15)
            return subprocess.CompletedProcess(argv, 0)

        with (socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier,
              patch.object(sys, "platform", "linux"),
              patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              patch.object(restart, "stop_row", return_value=True) as stop,
              patch.dict(os.environ, {"CODEX_THREAD_ID": str(row.current_session)}),
              patch.object(restart, "create_text_atomic", side_effect=create_claim) as create,
              patch.object(subprocess, "run", side_effect=launch), redirect_stdout(io.StringIO()) as output):
            notifier.bind(str(notification))
            notifier.setblocking(False)
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        stop.assert_called_once()
        create.assert_called_once()
        self.assertEqual(log_path.read_bytes(), written)
        self.assertIn(b"agents stop --all: 1 of 1 sessions stopped.", written)
        self.assertEqual(output.getvalue().splitlines(), [
            f"{log_path}  agents stop --all runs in the background. Read its report in this log."])

    def test_parent_rereads_a_late_child_claim_after_enospc(self) -> None:
        row = self.rows[0]
        child_report = "agents stop --all: 1 of 1 sessions stopped.\nNothing for you to do.\n"

        def fail_after_child_claim(claim: Path, _content: str) -> bool:
            self.assertFalse(claim.exists())
            _ = claim.write_text(f"{os.getpid() + 1}\n")
            log = claim.with_suffix(".log")
            with log.open("a") as stream:
                _ = stream.write(child_report)
            raise OSError(errno.ENOSPC, "The claim folder is full")

        with (patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              patch.object(restart, "stop_row") as stop,
              patch.object(stop_resume, "detach", return_value=self.root) as launch,
              patch.object(restart, "create_text_atomic", side_effect=fail_after_child_claim) as claim,
              patch.dict(os.environ, {"CODEX_THREAD_ID": str(row.current_session)}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        claim.assert_called_once()
        stop.assert_not_called()
        log = cast(Path, launch.call_args.kwargs["log"])
        self.assertEqual(log.read_text(), f"{log}\n{child_report}")
        self.assertEqual(output.getvalue().splitlines(), [
            f"{log}  agents stop --all runs in the background. Read its report in this log."])

    def test_detached_stop_reports_failed_readiness_in_the_log(self) -> None:
        rows = list(self.rows[:2])
        address = str(self.root / "notify-refused")
        saved = ["earlier"]
        shutdown_record.save(saved)
        for refused, reason in ((False, "NOTIFY_SOCKET is missing"), (True, "Connection refused")):
            with self.subTest(reason=reason):
                def launch(arguments: list[str], _task: str, *, log: Path, log_fd: int, **_options: object) -> Path:
                    connect = Mock(side_effect=ConnectionRefusedError(errno.ECONNREFUSED, reason))
                    send = Mock()
                    notifier = nullcontext(SimpleNamespace(connect=connect, send=send))
                    with (patch.dict(os.environ), patch.object(socket, "socket", return_value=notifier) as factory,
                          os.fdopen(os.dup(log_fd), "ab") as stream):
                        _ = os.environ.pop("NOTIFY_SOCKET", None)
                        if refused:
                            os.environ["NOTIFY_SOCKET"] = address
                        self.inherited_log = stream.fileno()
                        self.assertEqual(self.detached_stop(log, [*arguments, "--notify-systemd"]), 2)
                        if refused:
                            connect.assert_called_once_with(address)
                            send.assert_not_called()
                        else:
                            factory.assert_not_called()
                    raise subprocess.CalledProcessError(1, ["systemd-run"])

                with (patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows(rows)) as discover,
                      patch.object(restart, "stop_row") as stop,
                      patch.dict(os.environ, {"CODEX_THREAD_ID": str(rows[0].current_session)}),
                      patch.object(stop_resume, "detach", side_effect=launch) as launcher,
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(shutdown.main(["stop", "--all"]), 0)
                stop.assert_not_called()
                discover.assert_called_once()
                self.assertEqual(shutdown_record.read(), saved)
                path = cast(Path, launcher.call_args.kwargs["log"])
                self.assertTrue(path.with_suffix(".claim").exists())
                self.assertEqual(output.getvalue().splitlines(), [
                    f"{path}  agents stop --all runs in the background. Read its report in this log."])
                header, count, *issues, summary = path.read_text().splitlines()
                self.assertEqual(header, str(path))
                self.assertEqual(count, "agents stop --all: 0 of 2 sessions stopped.")
                self.assertEqual([issue.partition(":")[0] for issue in issues], sorted(row.name for row in rows))
                step = "Nate opens a plain terminal without a staff session. Nate runs agents stop --all there."
                for issue in issues:
                    self.assertIn("Could not confirm the detached process is ready.", issue)
                    self.assertIn(reason, issue)
                    self.assertIn(step, issue)
                self.assertIn(step, summary)

    def test_late_child_exits_when_the_parent_claimed_the_log(self) -> None:
        row = self.rows[0]
        with (patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              patch.object(restart, "stop_row") as stop,
              patch.object(stop_resume, "detach", return_value=self.root) as launch,
              patch.dict(os.environ, {"CODEX_THREAD_ID": str(row.current_session)}),
              redirect_stdout(io.StringIO()) as parent):
            self.assertEqual(shutdown.main(["stop", "--all"]), 2)
            path = cast(Path, launch.call_args.kwargs["log"])
            written = path.read_bytes()
            self.assertEqual(written.decode(), parent.getvalue())
            self.assertIn("The detached process did not claim the log", parent.getvalue())
            arguments = cast(list[str], launch.call_args.args[0])
            with (path.open("ab") as log, patch.object(staff, "initial_snapshot") as discover,
                  redirect_stdout(io.StringIO()) as child, redirect_stderr(io.StringIO()) as errors):
                self.inherited_log = log.fileno()
                self.assertEqual(self.detached_stop(path, arguments), 0)
            discover.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(child.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(path.read_bytes(), written)

    def test_detached_child_reports_each_fault_after_the_claim(self) -> None:
        # Keep these steps in sync with the guard in restart._run_detached_child.
        for step, detail, completed in (
            ("claim publication", "The claim was interrupted", ()),
            ("claim close", "Input/output error", ()),
            ("claim read", "Readiness failed with an unreadable claim", ()),
            ("claim read and log write", "Readiness failed with an unreadable claim", ()),
            ("log open", "Could not open the log", ()),
            ("readiness", "The readiness check failed", ()),
            ("staff read", "The staff record is unreadable", ()),
            ("plan write", "Could not save the stop plan", ()),
            ("plan sync", "Could not sync the stop plan", ()),
            ("stop 0", "Could not stop the session", (1,)),
            ("stop 1", "Could not stop the session", (0,)),
            ("resume 0", "Could not resume the session", (1,)),
            ("resume 1", "Could not resume the session", (0,)),
            ("report write", "Could not save the report", (0, 1)),
            ("report sync", "Could not sync the report", (0, 1)),
        ):
            with self.subTest(step=step):
                self.assert_detached_fault_report(step, detail, completed)
        for step in ("stop both", "resume both"):
            with self.subTest(step=step):
                self.assert_detached_fault_report(step, "First session error", (), second_detail="Second session error")
        for number in (signal.SIGTERM, signal.SIGQUIT):
            with self.subTest(step="readiness", number=number):
                self.assert_detached_signal_report(number, step="readiness")
        for step in ("claim", "stop", "record save"):
            with self.subTest(step=step):
                self.assert_detached_signal_report(signal.SIGTERM, step=step)

    def test_detached_stop_reports_a_busy_restart_record(self) -> None:
        self.assert_detached_signal_report(signal.SIGTERM, step="busy lock")

    def test_detached_stop_records_a_signal_during_final_sync(self) -> None:
        self.assert_detached_signal_report(signal.SIGTERM, step="final sync")

    def detached_fault_arguments(self, command: str, *, resuming: bool = False, count: int = 2) -> list[str]:
        rows = self.rows[:count]
        data: restart.DetachedReport = {
            "retry": f"{command} --all" if resuming else command,
            "targets": {row.id: row.name for row in rows},
            "units": {row.id: row.name for row in rows},
            "issues": {},
        }
        return ["restart" if resuming else "stop", "--all", "--only", ",".join(row.id for row in rows),
                "--parent-report", json.dumps(data)]

    def assert_detached_signal_report(self, number: signal.Signals, *,
                                      step: Literal["claim", "readiness", "stop", "record save",
                                                    "busy lock", "final sync"]) -> None:
        path = self.root / f"{number.name}-{step}.log"
        script = """\
import errno
import fcntl
import json
import os
import resource
import runpy
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from threading import Event

number = int(sys.argv.pop(1))
step = sys.argv.pop(1)
ready_fd = int(sys.argv[sys.argv.index('--ready-fd') + 1])
log_fd = int(sys.argv[sys.argv.index('--log-fd') + 1])
log_path = Path(sys.argv[sys.argv.index('--log') + 1])
write = os.write
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

if step == 'claim':
    claim = log_path.with_suffix('.claim')
    link = os.link
    read_bytes = Path.read_bytes

    def link_claim(source, destination):
        link(source, destination)
        if destination == claim:
            signal.raise_signal(number)

    def read_claim(path):
        if path == claim:
            raise OSError(errno.EIO, 'Could not read the claim')
        return read_bytes(path)

    os.link = link_claim
    Path.read_bytes = read_claim

if step == 'final sync':
    sync = os.fsync
    signalled = False

    def signal_during_sync(fd):
        global signalled
        if fd == log_fd and not signalled and 'sessions stopped.' in log_path.read_text():
            signalled = True
            signal.raise_signal(number)
        sync(fd)

    os.fsync = signal_during_sync

def configure_collection():
    from scripts.agents import shutdown_record
    from scripts.herdr import herdr
    from scripts.production import staff

    restart = sys.modules['scripts.agents.restart']
    data = json.loads(sys.argv[sys.argv.index('--parent-report') + 1])
    rows = tuple(staff.StaffRow(staff.StaffId(key), 'unit', name, staff.StaffRoot(),
                               current_session=staff.StaffId(f'conversation-{key}'),
                               unit_type=staff.UnitDetails('direct')) for key, name in data['targets'].items())
    record = staff.StaffSnapshot(rows)
    pending = Event()
    release = Event()
    launched = []
    save = shutdown_record.remove_started

    def hold_task():
        pending.set()
        release.wait()
        if not shutdown_record.active():
            raise RuntimeError('The activity lock released before the task ended')

    def stop_row(row, *args, **kwargs):
        if step == 'busy lock':
            raise RuntimeError('A stop began while the restart record was busy')
        if step == 'stop':
            if row.id == rows[1].id:
                hold_task()
            if row.id == rows[2].id:
                raise RuntimeError('A new stop began after the signal')
        return True

    def start_row(row, *args, **kwargs):
        if step != 'record save' or row.id != rows[0].id:
            raise RuntimeError('A new resume began after the signal')
        launched.append(row.id)
        return ''

    def remove_started(ids):
        if step == 'record save' and ids == [rows[0].id]:
            if launched != [rows[0].id]:
                raise RuntimeError('The resume did not finish before the record save')
            hold_task()
        save(ids)

    class CollectionPool(ThreadPoolExecutor):
        count = 0

        def __init__(self, **kwargs):
            CollectionPool.count += 1
            self.inject = (step == 'stop' and CollectionPool.count == 1 or
                           step == 'record save' and CollectionPool.count == 2)
            if self.inject:
                kwargs['max_workers'] = 1
            super().__init__(**kwargs)
            self.futures = []

        def submit(self, fn, *args):
            future = super().submit(fn, *args)
            if self.inject:
                self.futures.append(future)
                if len(self.futures) == (2 if step == 'stop' else 1):
                    result = future.result

                    def signal_before_result():
                        try:
                            pending.wait()
                            if future.done():
                                raise RuntimeError('The selected task did not remain in progress')
                            if self.futures[-1].running() or self.futures[-1].done():
                                raise RuntimeError('The last task did not remain queued')
                            if step == 'stop' and not self.futures[0].done():
                                raise RuntimeError('The first stop did not finish')
                            signal.raise_signal(number)
                            if not shutdown_record.active():
                                raise RuntimeError('The activity lock released after the signal')
                        finally:
                            release.set()
                        return result()

                    future.result = signal_before_result
            return future

    if step == 'busy lock':
        flock = fcntl.flock
        record_lock = shutdown_record.record_path().with_suffix('.lock')
        signalled = False

        def signal_during_lock(handle, flags):
            nonlocal signalled
            if not signalled and getattr(handle, 'name', None) == str(record_lock):
                signalled = True
                os.kill(os.getpid(), number)
            flock(handle, flags)

        fcntl.flock = signal_during_lock

    staff.initial_snapshot = lambda: record
    staff.row_operation = lambda *args: nullcontext()
    herdr.restore_server = lambda *args, **kwargs: None
    herdr.Herdr.sort_tabs = lambda self: None
    shutdown_record.remove_started = remove_started
    restart._observations = lambda *args: restart.RestartObservations(herdr.CodexConversations({}, ()), {}, frozenset(), {})
    restart.live_rows = lambda *args, **kwargs: restart.CompleteLiveRows(list(rows))
    restart.stop_row = stop_row
    restart.start_row = start_row
    restart.ThreadPoolExecutor = CollectionPool

def on_ready(fd, data):
    # conf_reads keeps its own os.write reference, and its log can reuse the closed ready fd number.
    if fd == ready_fd and os.write is on_ready:
        os.write = write
        if step == 'readiness':
            signal.raise_signal(number)
        elif step in ('stop', 'record save', 'busy lock', 'final sync'):
            configure_collection()
    return write(fd, data)

os.write = on_ready
runpy.run_module('scripts.agents.restart', run_name='__main__', alter_sys=True)
"""
        resuming = step == "record save"
        command = "agents restart" if resuming else "agents stop --all"
        state = "back" if resuming else "stopped"
        count = 3 if step == "stop" else 1 if step == "busy lock" else 2
        rows = self.rows[:count]
        completed = 1 if resuming else count if step == "final sync" else 2 if step == "stop" else 0
        shutdown_record.save([])
        record_lock = shutdown_record.record_path().with_suffix(".lock")
        if step == "busy lock":
            config = Path(os.environ["CONF_DIR"]) / "shutdown.conf"
            _ = write_config_fixture(config, "RESTART_RECORD_LOCK_SECONDS=0.05\n")
        read_fd, ready_fd = os.pipe()
        lock_context = record_lock.open("a") if step == "busy lock" else nullcontext()
        with lock_context as lock, os.fdopen(read_fd, "rb"), os.fdopen(ready_fd, "wb"), path.open("ab") as log:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            _ = log.write(f"{path}\n".encode())
            log.flush()
            result = subprocess.run([sys.executable, "-c", script, str(number), step,
                                     "--log", str(path), "--log-fd", str(log.fileno()), "--ready-fd", str(ready_fd),
                                     *self.detached_fault_arguments(command, resuming=resuming, count=count)],
                                    cwd=Path(restart.__file__).resolve().parents[2],
                                    pass_fds=(ready_fd, log.fileno()), capture_output=True,
                                    timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, -number, result.stderr.decode())
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        lines = path.read_text().splitlines()
        self.assertEqual(lines[0], str(path))
        self.assertEqual(lines.count(f"{command}: {completed} of {count} sessions {state}."), 1)
        self.assertEqual(lines.count(f"{command}: {number.name} stopped the run."), 1)
        self.assertNotEqual(lines[-1], "Nothing for you to do.")
        for row in rows[:completed]:
            self.assertIn(f"{row.name}: The session is {state}.", lines)
        for row in rows[completed:]:
            retry = f"agents restart --only {row.id}" if resuming else command
            step_text = f"Nate opens a plain terminal without a staff session. Nate runs {retry} there."
            status = "not started: the restart was stopped"
            if step == "busy lock":
                status = f"could not save {row.id} to the restart record: the record is busy"
                step_text = "Nate waits for the command that holds the restart record lock to finish. " + step_text
            self.assertIn(f"{row.name}: {status}. {step_text}", lines)
            self.assertIn(f"Nate runs {retry} there.", lines[-1])
        self.assertFalse(shutdown_record.active())
        pending = rows[completed:] if resuming else rows[:completed]
        self.assertEqual(shutdown_record.read(), [str(row.id) for row in pending])

    def assert_detached_fault_report(self, step: str, detail: str, completed: tuple[int, ...], *,
                                     second_detail: str | None = None) -> None:
        rows = self.rows[:2]
        record = staff.StaffSnapshot(rows)
        path = self.root / f"{step}.log"
        resuming = step.startswith("resume")
        command = "agents restart" if resuming else "agents stop --all"
        state = "back" if resuming else "stopped"
        arguments = self.detached_fault_arguments(command, resuming=resuming)
        injected = False
        close = os.close
        link = os.link
        sync = os.fsync
        file_io = io.FileIO
        read_bytes = Path.read_bytes
        write = restart._write_log  # pyright: ignore[reportPrivateUsage]

        def fail_at(check: str, error: BaseException) -> None:
            nonlocal injected
            if step == check and not injected:
                injected = True
                raise error

        def close_claim(descriptor: int) -> None:
            close(descriptor)
            if path.with_suffix(".claim").exists():
                fail_at("claim close", OSError(errno.EIO, "Input/output error"))

        def publish_claim(source: Path, destination: Path) -> None:
            link(source, destination)
            if destination == path.with_suffix(".claim"):
                fail_at("claim publication", KeyboardInterrupt(detail))

        def open_log(descriptor: int, mode: str, *, closefd: bool) -> io.FileIO:
            fail_at("log open", OSError(detail))
            return file_io(descriptor, mode, closefd=closefd)

        def read_file(target: Path) -> bytes:
            if step.startswith("claim read") and target == path.with_suffix(".claim"):
                raise OSError(errno.EIO, "Could not read the claim")
            return read_bytes(target)

        def ready(*_args: object, **_options: object) -> None:
            if step.startswith("claim read"):
                fail_at(step, OSError(detail))
            fail_at("readiness", SystemExit(detail))

        def snapshot() -> staff.StaffSnapshot:
            fail_at("staff read", OSError(detail))
            return record

        def fail_row(action: str, row: staff.StaffRow) -> None:
            nonlocal injected
            index = rows.index(row)
            if step == f"{action} both":
                injected = True
                raise BaseException(detail if index == 0 else second_detail)
            fail_at(f"{action} {index}", BaseException(detail))

        def stop_row(row: staff.StaffRow, *_args: object, **_options: object) -> bool:
            fail_row("stop", row)
            return True

        def start_row(row: staff.StaffRow, *_args: object, **_options: object) -> str:
            fail_row("resume", row)
            return ""

        def write_log(log: io.FileIO, result: str) -> None:
            if step == "claim read and log write":
                raise OSError(errno.EIO, "Could not save the report")
            if result.startswith("Sessions to stop"):
                fail_at("plan write", OSError(detail))
            elif step == "report write" and not injected:
                write(log, "Partial report")
                fail_at("report write", BaseException(detail))
            write(log, result)

        def sync_log(descriptor: int) -> None:
            if descriptor == self.inherited_log:
                check = "report sync" if " sessions stopped." in path.read_text() else "plan sync"
                fail_at(check, OSError(detail))
            sync(descriptor)

        with (path.open("ab") as log, patch.object(os, "close", side_effect=close_claim),
              patch.object(os, "link", side_effect=publish_claim),
              patch.object(Path, "read_bytes", read_file),
              patch.object(io, "FileIO", side_effect=open_log),
              patch.object(stop_resume, "notify_ready", side_effect=ready),
              patch.object(staff, "initial_snapshot", side_effect=snapshot),
              patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows(list(rows))),
              patch.object(restart, "stop_row", side_effect=stop_row) as stop,
              patch.object(restart, "start_row", side_effect=start_row) as start,
              patch.object(restart, "_write_log", side_effect=write_log),
              patch.object(os, "fsync", side_effect=sync_log), redirect_stderr(io.StringIO()) as errors):
            _ = log.write(f"{path}\n".encode())
            log.flush()
            self.inherited_log = log.fileno()
            self.assertEqual(self.detached_stop(path, arguments), 2)
        self.assertTrue(injected)
        contents = path.read_text()
        if step == "claim read and log write":
            error_line, _, result = errors.getvalue().partition("\n")
            self.assertIn(f"Could not finish writing the log at {path}", error_line)
            self.assertIn("Could not save the report", error_line)
            self.assertEqual(contents, f"{path}\n")
            contents += result
        else:
            self.assertEqual(errors.getvalue(), "")
        before_stop = {"claim publication", "claim close", "claim read", "claim read and log write",
                       "log open", "readiness", "staff read", "plan write", "plan sync"}
        if step in before_stop:
            stop.assert_not_called()
        self.assertEqual(start.call_count, len(rows) if resuming else 0)
        self.assertEqual(path.with_suffix(".claim").read_text(), f"{os.getpid()}\n")
        recovery = ("Nate runs agents stop --all there." if step in before_stop - {"staff read"} else
                    "Nate sends this error to the efficiency unit.")
        self.assert_detached_fault_log(path, command, state, completed, (detail, second_detail or detail), recovery,
                                       contents=contents)

    def assert_detached_fault_log(self, path: Path, command: str, state: str, completed: tuple[int, ...],
                                  details: tuple[str, str], recovery: str, *, contents: str | None = None) -> None:
        rows = self.rows[:2]
        if contents is None:
            contents = path.read_text()
        result = contents.splitlines()
        self.assertEqual(result[0], str(path))
        self.assertNotIn("Partial report", contents)
        count = f"{command}: {len(completed)} of {len(rows)} sessions {state}."
        self.assertEqual(sum(line.startswith(f"{command}: ") and " sessions " in line for line in result), 1)
        self.assertIn(count, result)
        for index, row in enumerate(rows):
            issue, = [line for line in result if line.startswith(f"{row.name}: ")]
            if index in completed:
                self.assertIn(f"The session is {state}.", issue)
            else:
                self.assertIn(f"Could not confirm the session is {state}", issue)
                self.assertIn(details[index], issue)
                self.assertIn(recovery, issue)
                if details[0] != details[1]:
                    self.assertNotIn(details[1 - index], issue)
        for detail in details:
            self.assertIn(detail, contents)
        self.assertIn(recovery, result[-1])

    def test_restart_counts_an_idle_worker_when_its_last_turn_has_no_reply(self) -> None:
        owner = self.rows[0]
        child = replace(self.rows[1], kind="worker", unit_type=staff.NotUnit(),
            parent=staff.ReportsTo(owner.id), role=staff.KnownWorkerRole("impl"))
        record = staff.StaffSnapshot((owner, child))
        run = self.root / "idle-worker"
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: owner.id, "pass": {}}))
        state = worker_panes.OpenWorker(child, "w1:p1", "terminal", herdr.AgentStatus.IDLE)
        shutdown_record.save([str(child.id)])
        before = worker.PaneWorkerStates([worker_panes.ClosedWorker(child)], [run])
        after = worker.PaneWorkerStates([state], [run])
        with (patch.object(staff, "initial_snapshot", return_value=record), staff.supplied_snapshot(record),
              patch.object(staff, "authority_snapshot", return_value=record),
              patch.object(staff, "local_snapshot", return_value=record),
              patch.object(worker, "_read_workers", side_effect=[before, after]),
              patch.object(worker_panes, "worker_run", return_value=run),
              patch.object(worker_panes, "_transcript_messages", return_value=["user:\nOne moment."]) as messages,
              patch.object(worker_panes, "close") as close, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["restart"]), 0)
        self.assertEqual(output.getvalue().splitlines()[1:], [
            "agents restart: 1 of 1 sessions back.", "Nothing for you to do."])
        self.assertEqual(shutdown_record.read(), [])
        messages.assert_called_once_with(child)
        close.assert_not_called()

    def test_reboot_reports_the_first_worker_before_the_second_worker_restores(self) -> None:
        from scripts.agents import reboot

        owner = self.rows[0]
        first, second = (replace(row, kind="worker", name=name, unit_type=staff.NotUnit(),
                                parent=staff.ReportsTo(owner.id), role=staff.KnownWorkerRole("impl"))
                         for row, name in zip(self.rows[1:], ("first", "second"), strict=True))
        record = staff.StaffSnapshot((owner, first, second))
        run = self.root / "idle-worker"
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: owner.id, "pass": {}}))
        states: list[worker_panes.WorkerPane] = [
            worker_panes.OpenWorker(first, "w1:p1", "terminal-1", herdr.AgentStatus.IDLE),
            worker_panes.OpenWorker(second, "w1:p2", "terminal-2", herdr.AgentStatus.IDLE),
        ]
        shutdown_record.save([owner.id, first.id, second.id])
        reboot.Checkpoint(reboot.Step.START).save(self.root / "agents/reboot/reboot.json")
        output = io.StringIO()

        def messages(row: staff.StaffRow) -> list[str]:
            if row.id == second.id:
                self.assertIn(f"2 of 3 back - {owner.name} / {first.name}", output.getvalue())
                self.assertNotIn(f"back - {owner.name} / {second.name}", output.getvalue())
            return ["user:\nOne moment."]

        with (patch.object(staff, "initial_snapshot", return_value=record), staff.supplied_snapshot(record),
              patch.object(staff, "authority_snapshot", return_value=record),
              patch.object(staff, "local_snapshot", return_value=record),
              patch.object(worker, "_read_workers", return_value=worker.PaneWorkerStates(states, [run])),
              patch.object(worker_panes, "worker_run", return_value=run),
              patch.object(worker_panes, "_transcript_messages", side_effect=messages) as transcripts,
              patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root), "HERDR_PANE_ID": "", "HERDR_ENV": ""}),
              redirect_stdout(output)):
            self.assertEqual(shutdown.main(["reboot"]), 0)
        self.assertEqual(transcripts.call_args_list, [call(first), call(second)])
        progress = [line for line in output.getvalue().splitlines() if "back -" in line]
        self.assertEqual(len(progress), 3)
        self.assertIn(f"3 of 3 back - {owner.name} / {second.name}", progress[2])
        self.assertEqual(shutdown_record.read(), [])

    def test_restart_sends_an_unsubmitted_dispatch_even_when_the_worker_is_working(self) -> None:
        owner = self.rows[0]
        child = replace(self.rows[1], kind="worker", name="impl", unit_type=staff.NotUnit(),
            parent=staff.ReportsTo(owner.id), role=staff.KnownWorkerRole("impl"))
        record = staff.StaffSnapshot((owner, child))
        current, previous = self.root / "current", self.root / "previous"
        for run in (current, previous):
            write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: owner.id,
                "open_phases": [{"id": "46", "instance_id": "open-phase", "status": "active"}],
                "history_file": str(run / "history.jsonl"), "pass": {}}))
        prompt = previous / "prompt.md"
        write_text_atomic(prompt, "Read the report.\n")
        state = worker_panes.OpenWorker(child, "w1:p1", "terminal", herdr.AgentStatus.WORKING)
        director = herdr.TabSession("w1:p0", owner.name, "", owner.current_session)
        shutdown_record.save([str(child.id)])
        with (patch.object(staff, "initial_snapshot", return_value=record), staff.supplied_snapshot(record),
              patch.object(staff, "authority_snapshot", return_value=record),
              patch.object(staff, "local_snapshot", return_value=record),
              patch.object(worker, "resume_receipt", return_value=worker.DispatchNotSubmitted()) as receipt,
              patch.object(worker, "_read_workers", return_value=worker.PaneWorkerStates([state], [current, previous])),
              patch.object(worker_panes, "worker_run", return_value=current),
              patch.object(herdr.Herdr, "tab_sessions", return_value=[director]),
              patch.object(herdr.Herdr, "prompt") as send,
              patch.object(worker, "_dispatch_transcript", return_value=worker.TranscriptReceipt(
                  worker.DispatchNotSubmitted(), frozenset())) as transcript,
              redirect_stdout(io.StringIO()) as output):
            dispatch = progress_history.begin_dispatch(previous, slot=child.name, phase="46", role="impl",
                worker_name=child.name, worker_id=child.id, launcher_command=["assign.sh"], prompt=prompt, kind="impl", lens="")
            delivery = pass_record.DispatchDelivery(f"End your final reply with DONE {dispatch}.", dispatch, "")
            progress_history.save_dispatch_delivery(previous, dispatch, delivery)

            def accept_assignment(*_args: object, **_kwargs: object) -> None:
                transcript.return_value = worker.TranscriptReceipt(worker.DispatchSubmitted(), frozenset())

            send.side_effect = accept_assignment
            self.assertEqual(shutdown.main(["restart"]), 0)
        receipt.assert_called_once_with(child, progress_history.ActiveDispatch(dispatch, ("assign.sh",)))
        send.assert_called_once_with(director.pane_id, resume_message.launcher_prompt(child.id, dispatch, ("assign.sh",)),
                                     target=str(owner.current_session))
        self.assertIn("1 of 1 sessions back", output.getvalue())
        self.assertEqual(shutdown_record.read(), [])

    def test_restart_keeps_a_renamed_workers_row_until_it_sends_the_resume(self) -> None:
        from scripts.herdr.test_worker_panes import live_detached_run

        owner = self.rows[0]
        child = replace(self.rows[1], kind="worker", name="new-worker", unit_type=staff.NotUnit(),
            parent=staff.ReportsTo(owner.id), role=staff.KnownWorkerRole("impl"))
        former = replace(child, name="old-worker")
        record = staff.StaffSnapshot((owner, child))
        current, previous = self.root / "current", self.root / "previous"
        for run in (current, previous):
            write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: owner.id, "pass": {}}))
        pane = "w1:p1"
        state = worker_panes.OpenWorker(child, pane, "terminal", herdr.AgentStatus.IDLE)
        saved = [str(child.id)]
        shutdown_record.save(saved)
        with (live_detached_run(previous, former), patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(staff, "authority_snapshot", return_value=record),
              staff.supplied_snapshot(record), patch.object(staff, "local_snapshot", return_value=record),
              patch.object(worker, "_read_workers", return_value=worker.PaneWorkerStates([state], [current, previous])),
              patch.object(worker_panes, "worker_run", return_value=current),
              patch.object(herdr.Herdr, "tab_sessions", return_value=[herdr.TabSession(pane, child.name, "", child.current_session)]),
              patch.object(herdr.Herdr, "agents", return_value=[{"pane_id": pane, "agent_status": herdr.AgentStatus.IDLE}]),
              patch.object(herdr.Herdr, "prompt", side_effect=herdr_types.HerdrFailure("Could not send the resume message")) as send,
              patch.object(worker_panes, "close") as close):
            with redirect_stdout(io.StringIO()) as failed:
                self.assertEqual(shutdown.main(["restart"]), 0)
            self.assertEqual(shutdown_record.read(), saved)
            self.assertIn("0 of 1 sessions back", failed.getvalue())
            self.assertIn(f"{child.name}: Could not send the resume message", failed.getvalue())
            send.assert_called_once()
            send.reset_mock(side_effect=True)
            with redirect_stdout(io.StringIO()) as retried:
                self.assertEqual(shutdown.main(["restart"]), 0)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], pane)
        self.assertIn(f"Detached run {former.name} remains live", cast(str, send.call_args.args[1]))
        self.assertEqual(send.call_args.kwargs["target"], child.current_session)
        self.assertEqual(shutdown_record.read(), [])
        self.assertIn("1 of 1 sessions back", retried.getvalue())
        close.assert_not_called()

    def test_restart_resumes_a_worker_whose_reply_waits_for_a_live_run(self) -> None:
        from scripts.herdr.test_worker_panes import live_detached_run

        owner = self.rows[0]
        child = replace(self.rows[1], kind="worker", name="impl", unit_type=staff.NotUnit(),
            parent=staff.ReportsTo(owner.id), role=staff.KnownWorkerRole("impl"))
        record = staff.StaffSnapshot((owner, child))
        run = self.root / "active-worker"
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: owner.id,
            "open_phases": [{"id": "46", "instance_id": "open-phase", "status": "active"}],
            "history_file": str(run / "history.jsonl"), "pass": {}}))
        prompt = run / "prompt.md"
        write_text_atomic(prompt, "Read the report.\n")
        source = run / "transcript.jsonl"
        pane = "w1:p1"
        state = worker_panes.OpenWorker(child, pane, "terminal", herdr.AgentStatus.DONE)
        shutdown_record.save([str(child.id)])

        recover = turn_end.recover_completion
        with (patch.object(staff, "initial_snapshot", return_value=record), staff.supplied_snapshot(record),
              patch.object(staff, "authority_snapshot", return_value=record),
              patch.object(staff, "local_snapshot", return_value=record),
              patch.object(staff, "backend", return_value=staff.ClaudeBackend()),
              patch.object(worker_admission, "transcript_source", return_value=source),
              patch.object(worker, "_read_workers", return_value=worker.PaneWorkerStates([state], [run])),
              patch.object(worker_panes, "worker_run", return_value=run),
              patch.object(herdr.Herdr, "tab_sessions", return_value=[herdr.TabSession(pane, child.name, "", child.current_session)]),
              patch.object(herdr.Herdr, "agents", return_value=[{"pane_id": pane, "agent_status": herdr.AgentStatus.DONE}]),
              patch.object(herdr.Herdr, "prompt") as send, redirect_stdout(io.StringIO()) as output):
            dispatch = progress_history.begin_dispatch(run, slot=child.name, phase="46", role="impl",
                worker_name=child.name, worker_id=child.id, launcher_command=["assign.sh"], prompt=prompt, kind="impl", lens="")
            line = f"Read the report. End your final reply with DONE {dispatch}."
            progress_history.save_dispatch_delivery(run, dispatch, pass_record.DispatchDelivery(line, dispatch, str(run / "reply.md")))
            write_text_atomic(source, "".join(json.dumps({"type": role, "message": {"role": role, "content": text}}) + "\n"
                for role, text in (("user", line), ("assistant", f"The report is ready.\nDONE {dispatch}"))))

            def recover_after_send(row: staff.StaffRow, parent: staff.StaffRow, path: Path,
                                   hold: worker_panes.WorkerLockHold, *, record: staff.StaffSnapshot,
                                   pane: worker.CloseCompletedPane | worker.KeepCompletedPane) -> worker.CompletionRecovery:
                send.assert_called_once()
                self.assertIs(pane, worker.KeepCompletedPane.KEEP)
                result = recover(row, parent, path, hold, record=record, pane=pane)
                self.assertIs(result, worker.CompletionRecovery.WAITING_ON_RUNS)
                return result

            with (live_detached_run(run, child),
                  patch.object(turn_end, "recover_completion", side_effect=recover_after_send) as complete):
                self.assertEqual(shutdown.main(["restart"]), 0)
        complete.assert_called_once()
        self.assertEqual(send.call_args.args[0], pane)
        self.assertIn(f"Detached run {child.name} remains live", cast(str, send.call_args.args[1]))
        self.assertEqual(output.getvalue().splitlines()[1:], [
            "agents restart: 1 of 1 sessions back.", "Nothing for you to do."])
        self.assertEqual(shutdown_record.read(), [])

    def test_restart_reports_save_errors_after_the_worker_resumes(self) -> None:
        owner = self.rows[0]
        child = replace(self.rows[1], kind="worker", name="impl", unit_type=staff.NotUnit(),
            parent=staff.ReportsTo(owner.id), role=staff.KnownWorkerRole("impl"))
        record = staff.StaffSnapshot((owner, child))
        run = self.root / "active-worker"
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: owner.id,
            "open_phases": [{"id": "46", "instance_id": "open-phase", "status": "active"}],
            "history_file": str(run / "history.jsonl"), "pass": {}}))
        prompt = run / "prompt.md"
        write_text_atomic(prompt, "Read the report.\n")
        source, reply_path = run / "transcript.jsonl", run / "reply.md"
        pane = "w1:p1"
        state = worker_panes.OpenWorker(child, pane, "terminal", herdr.AgentStatus.IDLE)
        saved = [str(child.id)]

        def transcript_entry(role: str, text: str) -> str:
            return json.dumps({"type": role, "message": {"role": role, "content": text}}) + "\n"

        with (patch.object(staff, "initial_snapshot", return_value=record), staff.supplied_snapshot(record),
              patch.object(staff, "authority_snapshot", return_value=record),
              patch.object(staff, "local_snapshot", return_value=record),
              patch.object(staff, "backend", return_value=staff.ClaudeBackend()),
              patch.object(worker_admission, "transcript_source", return_value=source),
              patch.object(worker, "_read_workers", return_value=worker.PaneWorkerStates([state], [run])),
              patch.object(worker_panes, "worker_run", return_value=run),
              patch.object(herdr.Herdr, "tab_sessions", return_value=[herdr.TabSession(pane, child.name, "", child.current_session)]),
              patch.object(herdr.Herdr, "agents", return_value=[{"pane_id": pane, "agent_status": herdr.AgentStatus.IDLE}])):
            dispatch = progress_history.begin_dispatch(run, slot=child.name, phase="46", role="impl",
                worker_name=child.name, worker_id=child.id, launcher_command=["assign.sh"], prompt=prompt, kind="impl", lens="")
            line = f"Read the report. End your final reply with DONE {dispatch}."
            reply = f"The report is ready.\nDONE {dispatch}"
            progress_history.save_dispatch_delivery(run, dispatch, pass_record.DispatchDelivery(line, dispatch, str(reply_path)))
            prompt_entry = transcript_entry("user", line)

            def send_reply(*_args: object, **_options: object) -> None:
                write_text_atomic(source, prompt_entry + transcript_entry("assistant", reply))

            error = OSError(errno.EIO, "Input/output error")
            for failed_save in ("reply", "restart record"):
                shutdown_record.save(saved)
                write_text_atomic(source, prompt_entry)
                with (self.subTest(failed_save=failed_save), redirect_stdout(io.StringIO()) as output,
                      patch.object(herdr.Herdr, "prompt", side_effect=send_reply) as send,
                      patch.object(worker, "save_reply", wraps=worker.save_reply,
                                   side_effect=error if failed_save == "reply" else None) as save,
                      patch.object(shutdown_record, "remove_started", wraps=shutdown_record.remove_started,
                                   side_effect=error if failed_save == "restart record" else None) as remove):
                    self.assertEqual(shutdown.main(["restart"]), 0)
                    send.assert_called_once()
                    self.assertEqual(send.call_args.args[0], pane)
                    save.assert_called_once_with(reply_path, reply)
                    remove.assert_called_once_with(saved)
                    path, count, issue, summary = output.getvalue().splitlines()
                    self.assertEqual(Path(path).read_text(), output.getvalue())
                    self.assertEqual(count, "agents restart: 1 of 1 sessions back.")
                    self.assertIn(f"{owner.name} / {child.name}:", issue)
                    self.assertIn("Input/output error", issue)
                    self.assertIn("1 unit needs a step.", summary)
                    if failed_save == "reply":
                        self.assertEqual(shutdown_record.read(), [])
                        self.assertIn(f"The director runs worker.py restore in {owner.name}.", issue)
                        self.assertIn("The affected directors run worker.py restore.", summary)
                    else:
                        self.assertEqual(shutdown_record.read(), saved)
                        self.assertIn("Could not save the restart record.", issue)
                        self.assertEqual(summary, "1 unit needs a step. Nate sends this error to the efficiency unit.")

    def test_stop_waits_for_upkeep_to_start_a_producer_before_reading_liveness(self) -> None:
        producer = fixture_member("producer", name="unit-0", worktree=self.rows[0].worktree)
        new_session = staff.native_id(str(uuid5(NAMESPACE_URL, "upkeep-new-session")))
        launched = replace(producer, current_session=new_session)
        self.rows = (producer,)
        self.running.clear()
        path = staff.record_path().parent / "rows" / f"{producer.id}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)

        def conversation(identity: str, **_options: object) -> herdr.LiveConversation | herdr.StoppedConversation:
            if identity not in self.running:
                return herdr.StoppedConversation()
            return herdr.LiveConversation("claude", ())

        with path.open("w+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _ = lock.write(json.dumps({"pid": 123, "command": "/path/herdr_upkeep.py --work"}) + "\n")
            lock.flush()

            def finish_upkeep(_seconds: float) -> None:
                self.assertTrue(shutdown_record.active())
                fcntl.flock(lock, fcntl.LOCK_UN)
                self.rows = (staff.update_current_session(producer.current_session, new_session),)
                self.running.add(new_session)

            with (patch.object(staff, "initial_snapshot",
                               side_effect=lambda: staff.StaffSnapshot(tuple(staff.snapshot().find(kind="producer")))),
                  patch.dict(os.environ, {"USER_ZONE": "UTC"}),
                  patch.object(time, "sleep", side_effect=finish_upkeep) as waited,
                  patch.object(herdr, "read_conversation", side_effect=conversation),
                  patch.object(restart, "stop_row", return_value=True) as stop,
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        waited.assert_called_once()
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[0], launched)
        self.assertEqual(shutdown_record.read(), [str(producer.id)])
        self.assertIsInstance(staff.snapshot().rows_by_id[producer.id].exited, staff.Exited)
        self.assertEqual(output.getvalue().splitlines()[1:], [
            "agents stop --all: 1 of 1 sessions stopped.", "Nothing for you to do."])

    def test_restart_prints_the_report_when_the_log_directory_is_unavailable(self) -> None:
        row = self.rows[0]
        logs = self.root / "logs"
        _ = logs.touch()
        shutdown_record.save([str(row.id)])
        with (patch.object(restart, "start_row", side_effect=staff.StaffRefused("The row is locked")),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(shutdown.main(["restart"]), 0)
        first, count, issue, last = output.getvalue().splitlines()
        self.assertTrue(first.startswith(f"Could not write the log at {logs}/restart-"))
        self.assertEqual(count, "agents restart: 0 of 1 sessions back.")
        self.assertEqual(issue, f"{row.name}: The row is locked. Nate runs agents restart again.")
        self.assertIn("Nate runs agents restart again.", last)
        self.assertEqual(errors.getvalue(), "")

    def test_self_stop_succeeds_after_the_named_log_folder_repair(self) -> None:
        row = self.rows[0]
        root = self.root / "blocked"
        root.touch()
        arguments = ["stop", "--all"]
        with (patch.object(restart, "shutdown_state_root", return_value=root),
              patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
              patch.object(restart, "stop_row", return_value=True) as stop,
              patch.object(stop_resume, "detach", side_effect=self.launch_detached) as detach,
              patch.dict(os.environ, {"CODEX_THREAD_ID": str(row.current_session)})):
            with redirect_stdout(io.StringIO()) as failed:
                self.assertEqual(shutdown.main(arguments), 2)
            detach.assert_not_called()
            stop.assert_not_called()
            lines = failed.getvalue().splitlines()
            self.assertIn("0 of 1 sessions stopped", lines[1])
            self.assertIn(f"Nate moves the file at {root} and creates a writable folder at that path.", lines[-1])
            self.assertIn("Then Nate runs agents stop --all again.", lines[-1])
            _ = root.rename(root.with_suffix(".old"))
            root.mkdir()
            with redirect_stdout(io.StringIO()) as retry:
                self.assertEqual(shutdown.main(arguments), 0)
            path = cast(Path, detach.call_args.kwargs["log"])
            self.assertIn(str(path), retry.getvalue())
            stop.assert_called_once()
        self.assertIn("1 of 1 sessions stopped", path.read_text())

    def test_self_stop_uses_the_log_error_to_choose_a_repair(self) -> None:
        row = self.rows[0]
        arguments = ["stop", "--all"]
        create_log = restart._create_log  # pyright: ignore[reportPrivateUsage]
        for cause in (errno.EACCES, errno.ENOSPC, errno.EIO, errno.EPERM, errno.EEXIST):
            with self.subTest(cause=cause):
                root = self.root / str(cause)
                logs = root / "logs"
                logs.mkdir(parents=True)
                full = root / "space-used"
                if cause == errno.EACCES:
                    logs.chmod(0o500)
                if cause == errno.ENOSPC:
                    full.touch()

                def open_log(files: ExitStack, path: Path) -> io.FileIO:
                    # Simulate the filesystem refusal even when the test user has extra permissions.
                    if (cause in (errno.EIO, errno.EPERM, errno.EEXIST)
                            or (cause == errno.EACCES and not logs.stat().st_mode & 0o200) or full.exists()):
                        raise OSError(cause, os.strerror(cause), str(path))
                    return create_log(files, path)

                with (patch.object(restart, "shutdown_state_root", return_value=root),
                      patch.object(restart, "_create_log", side_effect=open_log),
                      patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([row])),
                      patch.object(restart, "stop_row", return_value=True) as stop,
                      patch.object(stop_resume, "detach", side_effect=self.launch_detached) as detach,
                      patch.dict(os.environ, {"CODEX_THREAD_ID": str(row.current_session)})):
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(shutdown.main(arguments), 2)
                    detach.assert_not_called()
                    stop.assert_not_called()
                    _, count, issue, step = output.getvalue().splitlines()
                    self.assertEqual(count, "agents stop --all: 0 of 1 sessions stopped.")
                    self.assertIn(os.strerror(cause), issue)
                    if cause == errno.EACCES:
                        self.assertIn(f"Nate gives this account write and search access to {logs}.", step)
                        logs.chmod(0o700)
                    elif cause == errno.ENOSPC:
                        self.assertIn(f"Nate frees space on the filesystem that contains {logs}.", step)
                        full.unlink()
                    else:
                        self.assertIn("Nate sends this error to the efficiency unit.", step)
                        self.assertNotIn("again", output.getvalue())
                        continue
                    self.assertIn("Then Nate runs agents stop --all again.", step)
                    with redirect_stdout(io.StringIO()) as retried:
                        self.assertEqual(shutdown.main(arguments), 0)
                    path = cast(Path, detach.call_args.kwargs["log"])
                    self.assertIn(str(path), retried.getvalue())
                    stop.assert_called_once()
                    self.assertIn("1 of 1 sessions stopped", path.read_text())

    def test_restart_dry_run_prints_a_count_and_only_named_entries(self) -> None:
        row, *other = self.rows
        shutdown_record.save([str(row.id), "missing-row"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["restart", "--dry-run"]), 0)
        path, count, *entries, last = output.getvalue().splitlines()
        self.assertEqual(Path(path).read_text(), output.getvalue())
        self.assertEqual(count, "agents restart: 0 of 1 sessions back (dry run).")
        self.assertCountEqual([line.partition(":")[0] for line in entries],
                              [row.name, *(item.name for item in other), "missing-row"])
        self.assertEqual(last, "Nothing for you to do.")
        self.assertEqual(shutdown_record.read(), [str(row.id), "missing-row"])
        self.assertEqual((self.stops, self.launches), ([], []))

    def account_processes(self) -> None:
        def observe(identity: str, _kind: str = "", *,
                    codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation:
            _ = codex
            index = next(index for index, row in enumerate(self.rows) if row.current_session == identity)
            return herdr.LiveConversation("claude", (990100 + index,))

        _ = self.enterContext(patch.object(herdr, "read_conversation", side_effect=observe))
        def environment(pid: int) -> dict[str, str]:
            return {"CLAUDE_CONFIG_DIR": str(self.root / (".claude3" if pid == 990100 else ".claude2"))}

        _ = self.enterContext(patch.object(herdr, "process_environment", side_effect=environment))
        _ = self.enterContext(patch.object(shutdown, "named_claude_account",
            return_value=Account("claude", "test@example.invalid", "claude3")))

    def test_restart_account_all_dry_run_lists_only_matching_live_rows(self) -> None:
        self.account_processes()
        shutdown_record.update(started=[], pending=[str(self.rows[2].id)])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["restart", "claude3", "--all", "--dry-run"]), 0)
        self.assertIn(self.rows[0].name + ":", output.getvalue())
        self.assertNotIn(self.rows[1].name + ":", output.getvalue())
        self.assertNotIn(self.rows[2].name + ":", output.getvalue())
        self.assertEqual(self.stops, [])

    def test_restart_account_all_stops_and_starts_only_matching_rows_on_that_account(self) -> None:
        self.account_processes()
        with (patch.object(restart, "stop_row") as stop,
              patch.object(restart, "start_row", return_value="w1:p0") as start,
              patch.object(staff, "row_operation"),
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["restart", "claude3", "--all"]), 0)
        self.assertEqual([item.args[0] for item in stop.call_args_list], [self.rows[0]])
        self.assertEqual([item.args[0] for item in start.call_args_list], [self.rows[0]])
        self.assertEqual(start.call_args.kwargs["target_account"],
                         accounts.NamedAccount("claude3", self.root / ".claude3"))

    def test_restart_codex_account_filters_live_process_homes(self) -> None:
        def observe(identity: str, _kind: str = "", *,
                    codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation:
            _ = codex
            index = next(index for index, row in enumerate(self.rows) if row.current_session == identity)
            return herdr.LiveConversation("codex", (990100 + index,))

        def environment(pid: int) -> dict[str, str]:
            return {"CODEX_HOME": str(self.root / (".codex1" if pid == 990100 else ".codex2"))}

        with (patch.object(herdr, "read_conversation", side_effect=observe),
              patch.object(herdr, "process_environment", side_effect=environment),
              patch.object(agent_accounts, "codex_account_home",
                  return_value=agent_accounts.CodexSetupHome("codex1", self.root / ".codex1")),
              patch.object(note_file, "read_note",
                  return_value=note_file.Note(self.root / "codex1.md", "codex", "", 0, 0, "codex1")),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["restart", "codex1", "--all", "--dry-run"]), 0)
        self.assertIn(self.rows[0].name + ":", output.getvalue())
        self.assertNotIn(self.rows[1].name + ":", output.getvalue())
        self.assertNotIn(self.rows[2].name + ":", output.getvalue())

    def test_restart_account_without_all_filters_saved_session_accounts(self) -> None:
        self.account_processes()
        self.running.clear()
        shutdown_record.update(started=[], pending=[str(row.id) for row in self.rows])

        def saved(identity: str, name: str) -> tuple[Literal["claude"], dict[str, str], list[str]]:
            return "claude", {"CLAUDE_CONFIG_DIR": str(self.root / (
                ".claude3" if identity == self.rows[0].current_session else ".claude2"))}, [
                    "--resume", identity, "--name", name]

        with (patch.object(restart, "session_launch", side_effect=saved),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["restart", "claude3", "--dry-run"]), 0)
        self.assertIn(self.rows[0].name + ":", output.getvalue())
        self.assertNotIn(self.rows[1].name + ":", output.getvalue())
        self.assertNotIn(self.rows[2].name + ":", output.getvalue())
        self.assertEqual(shutdown_record.read(), [str(row.id) for row in self.rows])

    def test_restart_all_only_dry_run_lists_only_the_requested_live_row(self) -> None:
        self.account_processes()
        for account, requested in (([], self.rows[0]), (["claude3"], self.rows[1])):
            with self.subTest(account=account), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(["restart", *account, "--all", "--only",
                    str(requested.id), "--dry-run"]), 0)
                lines = output.getvalue().splitlines()
                self.assertEqual(Path(lines[0]).read_text(), output.getvalue())
                self.assertEqual(lines[-1], "Nothing for you to do.")
                self.assertEqual(lines[1], f"agents restart: 0 of {0 if account else 1} sessions back (dry run).")
                self.assertEqual([line.split(":", 1)[0] for line in lines[2:-1]],
                                 [self.rows[0].name] if not account else [])

    def test_restart_all_only_stops_and_starts_only_the_requested_live_row(self) -> None:
        from contextlib import ExitStack
        from scripts.herdr.test_worker_panes import WorkerPaneTests

        for incomplete in (False, True):
            with self.subTest(unselected_evidence_incomplete=incomplete), ExitStack() as scope:
                unselected: list[staff.StaffRow] = []
                if incomplete:
                    fixture = set_up_fixture(scope.callback, WorkerPaneTests("runTest"))
                    config = Path(os.environ["CONF_DIR"]) / "shutdown.conf"
                    _ = write_config_fixture(config, "UPKEEP_ROW_WAIT_SECONDS=0\n")
                    owner = staff.rename(fixture.parent.id, "TEST-selected-owner")
                    other = fixture_member("unit", name="TEST-other-owner", parent=owner.parent)
                    selected = fixture.worker("TEST-selected")
                    selected = staff.set_worker_role(selected.id, staff.KnownWorkerRole("impl"))
                    sibling = fixture.worker("TEST-sibling")
                    missing = fixture_member("worker", name="TEST-unselected", parent=other.id)
                    world = fixture.world
                    _ = scope.enter_context(patch.object(world, "panes", side_effect=lambda: READ_PANES(world)))
                    world.add(selected, "w1:p1")
                    world.add(other, "w2:p0", workspace="w2", tab="w2:t1")
                    for parent, pane, tab in ((owner, "w1:p9", "w1:t1"), (other, "w2:p9", "w2:t1")):
                        world.add(parent, pane, live=False, label="TEST-unreadable",
                                  workspace=pane.split(":")[0], tab=tab)
                        world.unknown[pane] = "TEST process evidence unavailable"
                    self.rows = (selected, owner, other, sibling, missing)
                    unselected = [sibling, missing]
                    record = staff.snapshot()
                    _ = scope.enter_context(patch.object(staff, "initial_snapshot", return_value=record))
                    self.running = {"TEST-unassigned"}
                with (nullcontext() if incomplete else
                      patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([self.rows[0]])),
                      patch.object(restart, "stop_row") as stop,
                      patch.object(restart, "start_row", return_value="w1:p0") as start,
                      patch.object(staff, "row_operation"),
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(stop_all=True, only=(str(self.rows[0].id),)), 0)
                self.assertEqual([item.args[0] for item in stop.call_args_list], [self.rows[0]])
                self.assertEqual([item.args[0] for item in start.call_args_list], [self.rows[0]])
                for row in unselected:
                    self.assertNotIn(row.name, errors.getvalue())
                self.assertNotIn("TEST-unassigned", errors.getvalue())

    def test_restart_selected_caller_detaches_before_any_stop(self) -> None:
        for variable in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            with (self.subTest(variable=variable),
                  patch.dict(os.environ, {variable: str(self.rows[0].current_session)}),
                  patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([self.rows[0]])),
                  patch.object(stop_resume, "detach", create=True, side_effect=self.claim_detached_log) as detach,
                  patch.object(restart, "stop_row") as stop,
                  patch.object(restart, "start_row") as start,
                  patch.object(staff, "row_operation"),
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(restart.restart(stop_all=True, only=(str(self.rows[0].id),)), 0)
                detach.assert_called_once()
                stop.assert_not_called()
                start.assert_not_called()
                path = cast(Path, detach.call_args.kwargs["log"])
                self.assertEqual(output.getvalue().splitlines(), [
                    f"{path}  agents restart runs in the background. Read its report in this log."])

    def test_restart_only_keeps_worker_and_owner_operations_within_selected_rows(self) -> None:
        owner = self.rows[0]
        children = tuple(replace(row, kind="worker", unit_type=staff.NotUnit(), parent=staff.ReportsTo(owner.id),
                                 role=staff.KnownWorkerRole("impl")) for row in self.rows[1:])
        record = staff.StaffSnapshot((owner, *children))
        for selected in (owner, children[0]):
            with (self.subTest(selected=selected.id),
                  patch.object(staff, "initial_snapshot", return_value=record),
                  patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([selected])),
                  patch.object(restart.stop_resume, "stop_unit_workers") as stop_workers,
                  patch.object(restart, "stop_row") as stop,
                  patch.object(restart, "start_row", return_value="w1:p1") as start,
                  patch.object(staff, "row_operation")):
                self.assertEqual(restart.restart(stop_all=True, only=(str(selected.id),)), 0)
                self.assertEqual([item.args[0] for item in stop.call_args_list], [selected])
                self.assertEqual([item.args[0] for item in start.call_args_list], [selected])
                stop_workers.assert_not_called()

    def test_account_filter_selects_a_hosted_codex_thread_by_its_daemon_home(self) -> None:
        row = self.rows[0]
        home = self.root / ".codex1"
        observed = codex_daemon.DaemonRead(codex_daemon.CodexDaemon(home, home / "control.sock"), [
            codex_daemon.DaemonSession(str(row.current_session), str(self.root), "idle")])
        with (patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", ())),
              patch.object(codex_daemon, "read_daemons", return_value=[observed])):
            self.assertEqual(restart.live_members([row], staff.StaffSnapshot(self.rows),
                codex=herdr.CodexConversations({str(row.current_session): ()}, ()),
                account=accounts.CodexWorkersAccount("codex1", home)), [row])

    def test_restart_rejects_a_codex_label_without_an_account_note(self) -> None:
        with (patch.object(note_file, "read_note", return_value=note_file.NotAccountNote(self.root / "missing.md")),
              patch.object(agent_accounts, "codex_account_home",
                  return_value=agent_accounts.CodexSetupHome("codex987654321", self.root / ".codex987654321")),
              patch.object(restart, "restart") as run,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["restart", "codex987654321", "--all"]), 2)
        run.assert_not_called()
        self.assertIn("unknown account codex987654321", output.getvalue())
        self.assertEqual(output.getvalue().splitlines()[-1], "1 item needs a step: Nate selects an available account.")

    def test_account_filters_partition_an_owner_and_its_workers_by_their_own_accounts(self) -> None:
        self.account_processes()
        owner = self.rows[0]
        children = tuple(replace(row, kind="worker", unit_type=staff.NotUnit(), parent=staff.ReportsTo(owner.id)) for row in self.rows[1:])
        record = staff.StaffSnapshot((owner, *children))
        states = [worker_panes.OpenWorker(row, f"w1:p{index}", f"terminal-{index}", herdr.AgentStatus.IDLE)
                  for index, row in enumerate(children, 1)]
        with patch.object(worker_panes, "observe_parents", return_value={owner.id: states}):
            for label, expected in (("claude3", [owner]), ("claude2", list(children))):
                with self.subTest(account=label):
                    self.assertEqual(restart.live_rows(record, herdr.Herdr("agents"),
                        account=accounts.NamedAccount(label, self.root / ("." + label))).rows, expected)

    def test_restart_names_unclassified_claude_and_codex_workers_once_without_acting(self) -> None:
        row = replace(self.rows[0], kind="worker", unit_type=staff.NotUnit())
        for kind in ("claude", "codex"):
            account = (accounts.NamedAccount("claude3", self.root / ".claude3") if kind == "claude" else
                       accounts.CodexWorkersAccount("codex1", self.root / ".codex1"))
            for dry_run in (False, True):
                with (self.subTest(kind=kind, dry_run=dry_run),
                      patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                      patch.object(session_records, "enumerate_running_sessions",
                           return_value=session_records.RunningSessionEnumeration((), ())),
                      patch.object(herdr, "read_conversation",
                          return_value=herdr.LiveConversation(kind, ())),
                      patch.object(restart, "stop_row") as stop,
                      patch.object(restart, "start_row") as start,
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(restart.restart(stop_all=True, dry_run=dry_run, account=account), 0)
                    self.assertEqual(output.getvalue().splitlines(),
                                     [f"{row.name}: left alone; account unknown"])
                    stop.assert_not_called()
                    start.assert_not_called()

    def test_restart_unknown_account_returns_two_before_any_stop(self) -> None:
        with (patch.object(shutdown, "named_claude_account",
                  side_effect=UnknownAccountName("unknown account: missing")),
              patch.object(restart, "stop_row") as stop,
              patch.object(restart, "start_row") as start,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["restart", "missing", "--all"]), 2)
        stop.assert_not_called()
        start.assert_not_called()
        self.assertIn("unknown account: missing", output.getvalue())

    def test_restart_uses_each_reader_outcome_for_both_runtimes(self) -> None:
        self.running.clear()
        row = self.rows[0]
        for kind in ("claude", "codex"):
            for state in ("live", "stopped", "unknown"):
                with self.subTest(kind=kind, state=state):
                    observed = (herdr.LiveConversation(kind, (990100,)) if state == "live" else
                                herdr.UnknownConversation("test-tab (conversation unknown)") if state == "unknown" else
                                herdr.StoppedConversation())
                    with (patch.object(herdr, "read_conversation", return_value=observed),
                          patch.object(restart, "session_launch", return_value=(kind, {},
                              ["--resume", str(row.current_session)] if kind == "claude" else ["resume", str(row.current_session)])),
                          patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                              "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
                          redirect_stderr(io.StringIO()) as errors,
                          redirect_stdout(io.StringIO()) as output):
                        pane = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
                    self.assertEqual(pane, "" if state == "live" else "w1:p0")
                    self.assertEqual(launch.call_count, 0 if state == "live" else 1)
                    self.assertIn("unchanged" if state == "live" else "restarted", output.getvalue())
                    self.assertEqual(errors.getvalue().count("anomaly: test-tab"), 1 if state == "unknown" else 0)

    def test_restart_reports_unknown_once_across_the_real_launch_boundary(self) -> None:
        self.running.clear()
        row = self.rows[0]
        for kind in ("claude", "codex"):
            with (self.subTest(kind=kind),
                  patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation(
                      "test-tab (conversation unreadable)")) as read,
                  patch.object(restart, "session_launch", return_value=(kind, {},
                      ["--resume", str(row.current_session)] if kind == "claude" else ["resume", str(row.current_session)])),
                  patch.object(herdr, "launch_prepared_agent", LAUNCH_PREPARED_AGENT),
                  patch.object(herdr.Herdr, "open_tab", return_value="w1:p9"),
                  patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired()) as start,
                  redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO())):
                self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p9")
            self.assertEqual(read.call_count, 3)
            start.assert_called_once()
            self.assertEqual(errors.getvalue().splitlines(), [
                "anomaly: test-tab (conversation unreadable); not blocking"])

    def test_restart_retains_unproved_panes_and_starts_independent_work(self) -> None:
        self.running.clear()
        row = self.rows[0]
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": row.current_session,
                                     "terminal_id": "kept-terminal"}
        for fault in ("duplicate-panes", "unreadable-panes"):
            with self.subTest(fault=fault):
                selected = {**pane}
                with (patch.object(herdr.Herdr, "panes", return_value=[selected, selected] if fault == "duplicate-panes" else [selected],
                          side_effect=herdr_types.HerdrFailure("pane list unavailable") if fault == "unreadable-panes" else None),
                      patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                          "w1:p9", herdr.PromptSubmissionNotRequired())) as launch,
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p9")
                self.assertIsInstance(cast(object, launch.call_args.args[1]), herdr.NewTab)
                self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_start_row_ignores_a_managed_daemon_at_both_live_checks(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", row.current_session])),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())) as scan,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p0")
        self.assertEqual(scan.call_count, 2)
        launch.assert_called_once()
        self.assertEqual(output.getvalue(), f"{row.name}: restarted\n")

    def test_live_members_reads_only_selected_local_rows(self) -> None:
        remote_row = replace(self.rows[0], id=staff.StaffId("remote-row"),
            current_session=staff.StaffId("remote-conversation"),
            worktree=staff.PresentWorktree("mac", self.root))
        record = staff.StaffSnapshot((*self.rows, remote_row))
        resolver = session_staff.RowResolver(record)
        snapshot = herdr.CodexConversations({}, ())
        with (patch.object(herdr, "read_conversation", side_effect=[
                  herdr.LiveConversation("claude", (123,)), herdr.StoppedConversation()]) as read,
              patch.object(session_records, "enumerate_running_sessions") as registry,
              patch.object(herdr, "running_codex_threads") as codex,
              redirect_stderr(io.StringIO()) as errors):
            selected = restart.live_members(iter((*self.rows[:2], remote_row)), record, resolver=resolver, codex=snapshot)
        self.assertEqual(selected, [self.rows[0]])
        self.assertEqual(read.call_args_list, [
            call(str(self.rows[0].current_session), codex=snapshot), call(str(self.rows[1].current_session), codex=snapshot)])
        registry.assert_not_called()
        codex.assert_not_called()
        self.assertEqual(errors.getvalue(), "")

    def test_live_members_reports_unknown_and_continues_after_row_errors(self) -> None:
        for error in (OSError("read failed"), ValueError("invalid conversation")):
            with (self.subTest(error=error),
                  patch.object(herdr, "read_conversation", side_effect=[
                      herdr.UnknownConversation("test-tab (conversation unknown)"), error,
                      herdr.LiveConversation("codex", (123,))]),
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(restart.live_members(self.rows, staff.StaffSnapshot(self.rows),
                    codex=herdr.CodexConversations({}, ())), [self.rows[2]])
            self.assertEqual(errors.getvalue().splitlines(), [
                "anomaly: test-tab (conversation unknown); not blocking",
                f"{self.rows[1].name}: restart anomaly: {error}"])

    def test_codex_clients_stay_live_and_only_daemon_rows_are_omitted(self) -> None:
        self.running.clear()
        row = self.rows[0]
        for arguments, live in ((["codex", "app-server", "--managed-daemon"], False),
                                (["codex", "resume", str(row.current_session)], True),
                                (["codex", "app-server", "--listen", "ws://127.0.0.1:1234"], True)):
            with (self.subTest(arguments=arguments),
                  patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", row.current_session])),
                  patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({str(row.current_session): (990100,)} if live else {}, ())),
                  patch.object(herdr, "launch_prepared_agent") as launch,
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(restart.live_rows(staff.StaffSnapshot((row,)), herdr.Herdr("agents")).rows, [row] if live else [])
                if live:
                    self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "")
                    self.assertEqual(output.getvalue(), f"{row.name}: unchanged\n")
                launch.assert_not_called()

    def test_bulk_restart_starts_every_selected_row_after_sigterm_zombies(self) -> None:
        kinds: tuple[Literal["claude", "codex"], ...] = ("claude", "codex")
        for kind in kinds:
            with self.subTest(kind=kind):
                self.running = {str(row.current_session) for row in self.rows}
                self.launches.clear()
                elapsed = 0.0
                signalled: list[int] = []
                environment = {"HERDR_PROC": str(self.root / (kind + "-proc"))}
                for index in range(len(self.rows)):
                    fake_unit_host.process(environment, 990100 + index, 1, [kind], {}, cwd=self.root)

                def observe(identity: str, _kind: str = "", *,
                            runtime: Literal["claude", "codex"] = kind,
                            codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation | herdr.StoppedConversation:
                    _ = codex
                    row = next(row for row in self.rows if row.current_session == identity)
                    return (herdr.LiveConversation(runtime, (990100 + self.rows.index(row),))
                            if identity in self.running else herdr.StoppedConversation())

                def process(pid: int, _started: str) -> sessions.LiveSessionProcess:
                    return sessions.LiveSessionProcess(pid)

                def native(pid: int, _kind: str) -> herdr.NativeAgentProcess:
                    identity = str(self.rows[pid - 990100].current_session)
                    evidence = herdr.PaneSessionReport(identity) if kind == "codex" else herdr.EnvironmentThread(identity)
                    return herdr.NativeAgentProcess(evidence, "fixture-start", {})

                def signal_process(pid: int, number: int) -> None:
                    self.assertEqual(number, signal.SIGTERM)
                    stat = Path(environment["HERDR_PROC"]) / str(pid) / "stat"
                    content = stat.read_text()
                    self.assertIn(") S ", content)
                    _ = stat.write_text(content.replace(") S ", ") Z "))
                    self.running.discard(str(self.rows[pid - 990100].current_session))
                    signalled.append(pid)

                def launch_arguments(identity: str, name: str) -> tuple[str, dict[str, str], list[str]]:
                    arguments = ["resume", identity] if kind == "codex" else ["--resume", identity, "--name", name]
                    return kind, {}, arguments

                def launched(_session: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                             _prepared: herdr.PreparedAgentEnvironment, launch_kind: str, arguments: list[str],
                             _opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, _timeout: int, *, agent: str = "",
                             reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT, client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> herdr.AgentLaunched:
                    _ = client
                    _ = codex
                    self.assertEqual((launch_kind, reported_unknown), (kind, ""))
                    row = next(row for row in self.rows if row.name == agent)
                    self.assertIn(row.current_session, arguments)
                    self.launches.append((agent, destination))
                    self.running.add(str(row.current_session))
                    return herdr.AgentLaunched(f"w1:p{self.rows.index(row)}", herdr.PromptSubmissionNotRequired())

                def advance(seconds: float) -> None:
                    nonlocal elapsed
                    elapsed += seconds

                def now() -> float:
                    return elapsed

                class ClockEvent:
                    def wait(self, seconds: float) -> None:
                        advance(seconds)

                with (patch.dict(os.environ, environment),
                      patch.object(herdr, "_PLATFORM", "linux"),
                      patch.object(herdr, "read_conversation", side_effect=observe),
                      patch.object(herdr, "native_process", side_effect=native),
                      patch.object(restart, "session_launch", side_effect=launch_arguments),
                      patch.object(herdr, "launch_prepared_agent", side_effect=launched),
                      patch.object(stop_resume, "stop", STOP_SESSION),
                      patch.object(sessions, "process_liveness", side_effect=process),
                      patch.object(stop_resume, "_signal_process", side_effect=signal_process),
                      patch.object(time, "monotonic", side_effect=now),
                      patch.object(time, "sleep", side_effect=advance),
                      patch.object(stop_resume, "monotonic", side_effect=now),
                      patch.object(stop_resume, "Event", return_value=ClockEvent()),
                      patch.object(pane_record, "seed", return_value=0),
                      patch.object(stop_resume, "_retire_prompt"),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(stop_all=True), 0, errors.getvalue())
                self.assertCountEqual(signalled, [990100, 990101, 990102])
                self.assertCountEqual([name for name, _ in self.launches], [row.name for row in self.rows])
                self.assertEqual(errors.getvalue(), "")

    def test_start_row_uses_the_target_account_environment(self) -> None:
        source, target = self.root / ".claude3", self.root / ".claude4"
        environment = {"CLAUDE_CONFIG_DIR": str(self.root / ".claude4"), "ACCOUNT_SETTING": "target"}
        for requested in ("claude4", ""):
            with (self.subTest(target_account=requested),
                  patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude4", target)) as selection,
                  patch.object(restart, "named_account_environment", return_value=environment) as account,
                  patch.object(restart, "session_launch", return_value=("claude", {"CLAUDE_CONFIG_DIR": str(source)},
                      ["--resume", str(self.rows[0].current_session), "--name", self.rows[0].name])),
                  patch.object(herdr, "prepare_session_environment",
                      return_value=herdr.PreparedAgentEnvironment(self.root, environment)) as prepare,
                  patch("scripts.accounts.accounts.account_dir", return_value=target)):
                self.running.clear()
                pane = restart.start_row(self.rows[0], staff.StaffSnapshot(self.rows),
                                         herdr.Herdr("agents"), target_account=requested)
            if requested:
                selection.assert_not_called()
            else:
                selection.assert_called_once_with("unit")
            account.assert_called_once_with(target)
            prepare.assert_called_once_with(str(self.rows[0].current_session), str(self.root),
                                            environment, herdr.NO_ENVIRONMENT_REMOVALS)
            self.assertEqual(pane, "w1:p0")

    def test_start_row_uses_the_configured_opening_prompt_budget_only_for_claude_prompts(self) -> None:
        for start_seconds, opening_seconds in ((7, 43), (51, 17)):
            for kind, opening in (
                    ("claude", herdr.OpeningPrompt("Continue where you stopped.")),
                    ("claude", herdr.NoOpeningPrompt()),
                    ("codex", herdr.OpeningPrompt("Continue where you stopped."))):
                for deadline in (math.inf, 12.0):
                    with self.subTest(start=start_seconds, prompt=opening_seconds, kind=kind,
                                      opening=opening, deadline=deadline):
                        self.running.clear()
                        config = Path(os.environ["CONF_DIR"]) / "shutdown.conf"
                        write_text_atomic(config,
                            f"# range: 1..\nSTART_SECONDS={start_seconds}\n"
                            + f"# range: 1..\nOPENING_PROMPT_SECONDS={opening_seconds}\n")
                        row = self.rows[0]
                        arguments = (["--resume", str(row.current_session)] if kind == "claude" else
                                     ["resume", str(row.current_session)])
                        with (patch.object(restart, "session_launch", return_value=(kind, {}, arguments)),
                              patch.object(time, "monotonic", return_value=0),
                              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch):
                            pane = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"),
                                opening=opening, deadline=deadline)
                        self.assertEqual(pane, "w1:p0")
                        expected = (max(start_seconds, opening_seconds)
                                    if kind == "claude" and isinstance(opening, herdr.OpeningPrompt) else start_seconds)
                        self.assertEqual(launch.call_args.args[6], int(min(expected, deadline) * 1000))
                        self.assertIsInstance(cast(object, launch.call_args.args[5]),
                            herdr.OpeningPrompt if kind == "claude" and isinstance(opening, herdr.OpeningPrompt)
                            else herdr.NoOpeningPrompt)

    def test_start_row_selects_the_workers_account_unless_the_target_is_explicit(self) -> None:
        self.running.clear()
        for row in (self.rows[0], replace(self.rows[0], kind="producer", unit_type=staff.NotUnit(), name="probe-codex")):
            for saved in (False, True):
                for requested in ("", "codex3"):
                    expected = requested or "codex1"
                    entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
                        {"CODEX_HOME": str(self.root / ".codex2")}, ["codex", "--model", "old-model", "--sandbox", "read-only",
                         "-c", 'model_reasoning_effort="low"', "-c", 'service_tier="flex"'], kind="codex")
                    from scripts.agents.test_pane_record_recovery import fixture_launch_transcript
                    fixture_launch_transcript(self.root, entry, staff.StaffSnapshot((row,)))
                    publication = pane_launch.LaunchPublication(self.root / "agents.json", entry) if saved else None
                    with (self.subTest(kind=row.kind, saved=saved, target=requested),
                          patch.object(pane_record, "publication_for_session", return_value=publication),
                          patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount(
                              "codex1", self.root / ".codex1")) as role,
                          patch.object(restart, "session_launch", return_value=("codex", {},
                              ["resume", str(row.current_session)])),
                          patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                              self.root, {})) as prepare,
                          patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                              "w1:p0", herdr.PromptSubmissionNotRequired())) as launch):
                        _ = restart.start_row(row, staff.StaffSnapshot((row,)), herdr.Herdr("agents"), target_account=requested)
                    self.assertEqual(prepare.call_args.args[2]["CODEX_HOME"], str(self.root / f".{expected}"))
                    if row.kind == "producer":
                        arguments = cast(list[str], launch.call_args.args[4])
                        self.assertEqual(launch.call_args.args[3], "codex")
                        self.assertIn(str(row.current_session), arguments)
                        self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-test")
                        self.assertIn('model_reasoning_effort="high"', arguments)
                        self.assertNotIn('model_reasoning_effort="low"', arguments)
                        self.assertNotIn('service_tier="flex"', arguments)
                        if saved:
                            self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
                    if requested:
                        role.assert_not_called()
                    else:
                        role.assert_called_once_with("workers")

    def test_start_row_with_a_saved_entry_uses_the_role_account(self) -> None:
        self.running.clear()
        row = self.rows[0]
        source, target = self.root / ".claude3", self.root / ".claude2"
        environment = claude_catalog_environment(source, {"fable": "claude-fable-source-fixture"})
        _ = claude_catalog_environment(target, {"fable": "claude-fable-selected-fixture"})
        for saved_model, expected in (("", ""), ("fable", "claude-fable-selected-fixture"),
                                      ("claude-fable-saved-fixture", "claude-fable-saved-fixture")):
            arguments = ["claude", "--resume", str(row.current_session)]
            if saved_model:
                arguments.extend(("--model", saved_model, "--effort", "high"))
            entry = pane_record.launch("w1:p0", str(row.id), str(self.root),
                {"CLAUDE_CONFIG_DIR": str(source)}, arguments)
            with (self.subTest(saved_model=saved_model), patch.dict(os.environ, environment),
                  patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude2", target)),
                  patch.object(pane_record, "publication_for_session",
                      return_value=pane_launch.LaunchPublication(self.root / "agents.json", entry)),
                  patch.object(herdr, "prepare_session_environment",
                      return_value=herdr.PreparedAgentEnvironment(self.root,
                          {**os.environ, **environment, "CLAUDE_CONFIG_DIR": str(target)})) as prepare,
                  patch.object(herdr, "launch_prepared_agent", LAUNCH_PREPARED_AGENT),
                  patch.object(herdr.Herdr, "open_tab", return_value="w1:p0"),
                  patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
                  patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired()) as launch):
                self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p0")
            self.assertEqual(prepare.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(target))
            if saved_model:
                resumed = cast(list[str], launch.call_args.args[3])
                self.assertEqual(resumed[resumed.index("--model") + 1], expected)
                self.assertEqual(entry["model"], saved_model)

    def test_start_row_uses_the_producer_account_unless_the_target_is_explicit(self) -> None:
        self.running.clear()
        row = replace(self.rows[0], kind="producer", unit_type=staff.NotUnit())
        source, target = self.root / ".claude3", self.root / ".claude2"
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
            {"CLAUDE_CONFIG_DIR": str(source)}, ["claude", "--resume", str(row.current_session), "--remote-control", "old-name",
             "--plugin-dir", str(self.root / "plugins")])
        for requested, expected in (("", target), ("claude3", source)):
            with (self.subTest(target=requested),
                  patch.object(pane_record, "publication_for_session",
                      return_value=pane_launch.LaunchPublication(self.root / "agents.json", entry)),
                  patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude2", target)) as role,
                  patch.object(herdr, "prepare_session_environment",
                      return_value=herdr.PreparedAgentEnvironment(self.root, {})) as prepare,
                  patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                      "w1:p0", herdr.PromptSubmissionNotRequired())) as launch):
                self.assertEqual(restart.start_row(row, staff.StaffSnapshot((row,)), herdr.Herdr("agents"),
                                                  target_account=requested), "w1:p0")
                self.assertEqual(prepare.call_args.args[2]["CLAUDE_CONFIG_DIR"], str(expected))
                arguments = cast(list[str], launch.call_args.args[4])
                self.assertEqual(launch.call_args.args[3], "claude")
                self.assertIn(str(row.current_session), arguments)
                self.assertEqual(arguments[arguments.index("--model") + 1], "opus")
                self.assertEqual(arguments[arguments.index("--effort") + 1], "max")
                self.assertEqual(arguments[arguments.index("--remote-control") + 1], row.name)
                self.assertEqual(arguments[arguments.index("--plugin-dir") + 1], str(self.root / "plugins"))
                if requested:
                    role.assert_not_called()
                else:
                    role.assert_called_once_with("producer")

    def test_a_claude_producer_starts_and_resumes_with_remote_control(self) -> None:
        row = replace(self.rows[0], kind="producer", unit_type=staff.NotUnit())
        _kind, first = restart.producer_first_arguments(row.name)
        self.assertEqual(first[first.index("--remote-control") + 1], row.name)
        # A producer that started without the flag gets it at its next restart.
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root), {},
                                   ["claude", "--resume", str(row.current_session)])
        self.assertFalse(entry["remote_control"])
        kind, _environment, arguments = restart.producer_resume(row, entry)
        self.assertEqual(kind, "claude")
        self.assertEqual(arguments[arguments.index("--remote-control") + 1], row.name)
        self.assertFalse(entry["remote_control"])

    def test_claude_launches_and_restarts_get_their_role_compact_window(self) -> None:
        _ = write_registry(Path(os.environ["MODELS_CONFIG_FILE"]), {"compact_windows": {"producer": "300000"}}, update=True)
        row = replace(self.rows[0], kind="producer", unit_type=staff.NotUnit())
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root), {},
                                   ["claude", "--resume", str(row.current_session), "--autocompact", "200000"])
        launches = {"first start": restart.producer_first_arguments(row.name)[1],
                     "start without a saved launch": restart.producer_launch(row)[2],
                     "restart or move": restart.producer_resume(row, entry)[2]}
        for path, arguments in launches.items():
            with self.subTest(path=path):
                self.assertEqual(arguments.count("--autocompact"), 1)
                self.assertEqual(arguments[arguments.index("--autocompact") + 1], "300000")
                self.assertFalse(pane_record.parse_claude_arguments(["claude", *arguments]).has_prompt)
        codex = replace(row, name="probe-codex")
        self.assertNotIn("--autocompact", restart.producer_first_arguments(codex.name)[1])
        unit = pane_record.launch("w1:p1", str(self.rows[1].current_session), str(self.root), {},
                                  ["claude", "--resume", str(self.rows[1].current_session), "-n", self.rows[1].name])
        self.assertNotIn("--autocompact", restart._saved_launch(unit, str(self.rows[1].current_session),  # pyright: ignore[reportPrivateUsage]
                                                                 self.rows[1].name)[2])
        _ = write_registry(Path(os.environ["MODELS_CONFIG_FILE"]), {"compact_windows": {"unit": "250k"}}, update=True)
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            arguments = restart._saved_launch(unit, str(self.rows[1].current_session),  # pyright: ignore[reportPrivateUsage]
                                              self.rows[1].name)[2]
        self.assertEqual(arguments[arguments.index("--autocompact") + 1], "250k")

        registry = Path(os.environ["MODELS_CONFIG_FILE"])
        _ = write_registry(registry, {"compact_windows": {"producer": "30k"}}, update=True)
        for attempt in range(2):
            with (self.subTest(attempt=attempt), redirect_stderr(io.StringIO()) as errors,
                  patch.object(models_registry, "registry_text", wraps=models_registry.registry_text) as read):
                arguments = restart.producer_resume(row, entry)[2]
            read.assert_called_once_with(registry)
            self.assertNotIn("--autocompact", arguments)
            self.assertEqual(errors.getvalue().splitlines(), [
                f"anomaly: {registry}: compact_windows.producer: --autocompact 30k is outside 100k to 1M. "
                + "Use the settings.json window."])

    def test_account_trust_with_the_same_account_keeps_the_file(self) -> None:
        target = self.root / ".claude2"
        before = (target / ".claude.json").read_text()
        self.assertIsInstance(stop_resume.heal_account_trust(self.root, target, target, "test"), herdr.Trusted)
        self.assertEqual((target / ".claude.json").read_text(), before)

    def test_start_row_opens_and_trusts_the_codex_folder(self) -> None:
        self.running.clear()
        row = self.rows[0]
        with (patch.object(restart, "session_launch", return_value=("codex", {}, ["resume", row.current_session])),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
        self.assertEqual(launch.call_args.args[4], ["-C", str(self.root), "-c",
            'projects={' + json.dumps(str(self.root)) + '={trust_level="trusted"}}', "resume", row.current_session])

    def test_account_trust_reports_anomalies_and_returns_the_result(self) -> None:
        target = self.root / ".claude4"
        for result in (herdr.Untrusted(self.root, target / ".claude.json", (self.root,)),
                       herdr.UnreadableTrustFile(self.root, target / ".claude.json", "ValueError")):
            with (patch.object(herdr, "heal_claude_trust", return_value=result),
                  patch("scripts.accounts.accounts.trust_sources", return_value=[]),
                  redirect_stderr(io.StringIO()) as output):
                self.assertIs(stop_resume.heal_account_trust(self.root, target, self.root, "test"), result)
            reason = str(result) if isinstance(result, herdr.UnreadableTrustFile) else f"Claude has not trusted {self.root}"
            self.assertEqual(output.getvalue(), f"test: trust anomaly: {reason}\n")

    def test_saved_codex_record_uses_workers_home_without_claude_trust(self) -> None:
        self.running.clear()
        row = self.rows[0]
        home = self.root / ".codex2"
        workers = self.root / ".codex1"
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
            {"CLAUDE_CONFIG_DIR": str(home)}, ["codex"], kind="codex")
        entry["config_dir"] = str(home)
        pane_record.write("agents", entry)
        with (patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex1", workers)) as role,
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                  self.root, {"CODEX_HOME": str(workers)})) as prepare):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
        trust.assert_not_called()
        role.assert_called_once_with("workers")
        self.assertEqual(prepare.call_args.args[2]["CODEX_HOME"], str(workers))

    def test_saved_codex_restart_uses_explicit_account(self) -> None:
        self.running.clear()
        row = self.rows[0]
        home = self.root / ".codex2"
        target = self.root / ".codex3"
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
            {"CLAUDE_CONFIG_DIR": str(home)}, ["codex"], kind="codex")
        entry["config_dir"] = str(home)
        pane_record.write("agents", entry)
        with (patch.object(accounts, "role_account") as role,
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(stop_resume, "heal_account_trust") as trust,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())),
              patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(
                  self.root, {"CODEX_HOME": str(target)})) as prepare):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"), target_account="codex3")
        role.assert_not_called()
        trust.assert_not_called()
        self.assertEqual(prepare.call_args.args[2]["CODEX_HOME"], str(target))

    def test_dry_run_names_a_morty_producer_with_an_unproved_native_identity(self) -> None:
        row = replace(self.rows[0], kind="producer", unit_type=staff.NotUnit(), name="morty", worktree=staff.WORKTREE_UNFILLED)
        self.rows = (row,)
        record = staff.StaffSnapshot(self.rows)
        entry = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
            {"CODEX_HOME": str(self.root / ".codex2")}, ["codex"], kind="codex")
        pane_record.write("agents", entry)
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(herdr, "read_conversation", return_value=herdr.UnknownConversation("native identity unavailable")),
              patch.object(herdr.Herdr, "panes", return_value=[{
                  "pane_id": "w1:p0", "workspace_id": "w1", "session_id": None, "label": "morty"}]),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            "morty: left alone; no proved local restart target"])
        self.assertEqual((self.stops, self.launches), ([], []))

    def test_restart_all_dry_run_lists_every_live_row_with_its_tab_position(self) -> None:
        def shell_result(*arguments: str) -> dict[str, object]:
            if arguments[:2] == ("pane", "process-info"):
                return {"process_info": {"shell_pid": 990090, "foreground_process_group_id": 990090,
                    "foreground_processes": [{"pid": 990090, "name": "zsh", "argv": ["zsh"]}]}}
            return self.layout_result(*arguments)

        self.rows = (replace(self.rows[0], name="unit"),
            replace(self.rows[1], kind="worker", unit_type=staff.NotUnit(), name="codex-worker", parent=staff.ReportsTo(self.rows[0].id),
                    worktree=staff.WORKTREE_UNFILLED),
            replace(self.rows[2], kind="producer", unit_type=staff.NotUnit(), name="morty", parent=staff.StaffRoot(),
                    worktree=staff.WORKTREE_UNFILLED, doc=staff.LiveDoc(remote.local_machine(), self.root / "morty.md")))
        layout: herdr.SavedLayout = {"at": 1, "workspaces": [{"name": "saved", "seen": 1,
            "tabs": [{"staff_id": str(self.rows[1].id), "seen": 1}]}]}
        shutdown_record.save([str(self.rows[2].id)])
        before = shutdown_record.record_path().read_bytes()
        for identity, label, unavailable in ((str(self.rows[0].current_session), "live", False), (None, "live", False),
                                           (str(self.rows[0].current_session), None, False), (None, "live", True)):
            with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(self.rows)),
                  patch.object(restart.worker_panes, "observe_parents", return_value={self.rows[0].id: [
                      worker_panes.OpenWorker(self.rows[1], "worker-pane", "worker-terminal", herdr.AgentStatus.IDLE)]}),
                  patch.object(herdr.Herdr, "panes", return_value=[{**self.panes()[0], "session_id": identity}],
                               side_effect=herdr_types.HerdrFailure("socket unavailable") if unavailable else None),
                  patch.object(herdr, "saved_layout", return_value=layout),
                  patch.object(self, "workspace_label", label),
                  (patch.object(herdr.Herdr, "result", side_effect=shell_result)
                   if identity is None else nullcontext()),
                  redirect_stderr(io.StringIO()) as errors,
                  patch.object(staff, "row_operation") as lock, patch.object(herdr, "_write_layout") as write,
                  patch.object(herdr.Herdr, "save_layout") as save, redirect_stdout(io.StringIO()) as output):
                self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
            self.assertEqual(output.getvalue().splitlines(), [
                f"{row.name}: {row.id}, {row.kind}, {position}" for row, position in zip(self.rows,
                    ("workspace live, tab 1 (unit)" if identity and label else "no tab", "workspace saved, tab 1 (codex-worker)", "no tab"))])
            self.assertEqual(len(errors.getvalue().splitlines()), int(unavailable or (identity is not None and label is None)))
            if unavailable:
                self.assertEqual(errors.getvalue(), "restart: layout anomaly: socket unavailable\n")
            for unused in (lock, save, write):
                unused.assert_not_called()
            self.assertEqual(shutdown_record.record_path().read_bytes(), before)
            self.assertEqual((self.stops, self.launches), ([], []))
            self.assertEqual(shutdown_record.row_folder(self.rows[2], staff.StaffSnapshot(self.rows)), (remote.local_machine(), self.root))
            self.assertEqual(shutdown_record.row_folder(replace(self.rows[2], worktree=staff.PresentWorktree(remote.local_machine(), self.root / "own")),
                staff.StaffSnapshot(self.rows)), (remote.local_machine(), self.root / "own"))

    def test_codex_dry_run_uses_the_newest_pane_record_and_reports_rows_without_a_tab(self) -> None:
        def shell_result(*arguments: str) -> dict[str, object]:
            if arguments[:2] == ("pane", "process-info"):
                return {"process_info": {"shell_pid": 990090, "foreground_process_group_id": 990090,
                    "foreground_processes": [{"pid": 990090, "name": "zsh", "argv": ["zsh"]}]}}
            return self.layout_result(*arguments)

        self.rows = (replace(self.rows[0], kind="producer", unit_type=staff.NotUnit(), name="morty"), *self.rows[1:])
        row = self.rows[0]
        older: pane_record.PaneLaunch = {"kind": "codex", "pane_id": "w1:p2", "session_id": str(row.id),
            "config_dir": "/account", "cwd": str(self.root), "name": row.name, "model": "",
            "remote_control": False, "environment": {}, "flags_read": True, "written_at": "1"}
        for reference in (str(row.id), str(row.current_session)):
            newest = older.copy()
            newest.update(session_id=reference, pane_id="w1:p0", written_at="2")
            with (self.subTest(reference=reference),
                  patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(self.rows)),
                  patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows(list(self.rows))),
                  patch.object(herdr.Herdr, "panes", return_value=[{**pane, "session_id": None} for pane in self.panes()]),
                  patch.object(herdr, "saved_layout", return_value={"at": 1, "workspaces": []}),
                  patch.object(pane_record, "read", return_value={newest["pane_id"]: newest, older["pane_id"]: older}) as launches,
                  patch.object(herdr.Herdr, "result", side_effect=shell_result),
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
            launches.assert_called_once_with("agents")
            self.assertEqual(output.getvalue().splitlines(), [
                f"{row.name}: {row.id}, producer, workspace live, tab 1 ({row.name})",
                *[f"{other.name}: {other.id}, {other.kind}, no tab" for other in self.rows[1:]]])

    def test_row_panes_keeps_session_matches_and_ignores_an_absent_recorded_pane(self) -> None:
        row = self.rows[0]
        entry: pane_record.PaneLaunch = {"kind": "codex", "pane_id": "w1:p9", "session_id": str(row.id),
            "config_dir": "/account", "cwd": str(self.root), "name": row.name, "model": "",
            "remote_control": False, "environment": {}, "flags_read": True, "written_at": "2"}
        older = entry.copy()
        older.update(pane_id="w1:p1", written_at="1")
        with patch.object(pane_record, "read", return_value={entry["pane_id"]: entry, older["pane_id"]: older}):
            self.assertEqual(restart.row_panes([row], herdr.Herdr("agents")), [(row, "w1:p0")])
            with (patch.object(herdr.Herdr, "panes", return_value=[{**pane, "session_id": None} for pane in self.panes()]),
                  patch.object(herdr.Herdr, "result", return_value={"process_info": {"shell_pid": 990090,
                      "foreground_process_group_id": 990090,
                      "foreground_processes": [{"pid": 990090, "name": "zsh", "argv": ["zsh"]}]}})):
                self.assertEqual(restart.row_panes([row], herdr.Herdr("agents")), [])

        from scripts.herdr.test_worker_panes import WorkerPaneTests
        from dataclasses import replace

        case = set_up_fixture(self.addCleanup, WorkerPaneTests("runTest"))
        _ = case.enterContext(patch.object(pane_record, "RECORD_DIR", pane_launch.record_directory()))
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

        with (self.subTest(record="reused pane"),
              patch.object(case.world, "panes", side_effect=lambda: READ_PANES(case.world))):
            self.assertEqual(restart.row_panes([owner], case.world), [(owner, "w1:p7")])

        case.world.end_agent("w1:p7")
        case.world.record(owner, "w1:p7")
        retained = pane_record.read("agents")["w1:p7"].copy()
        retained["written_at"] = "2026-11-12T00:00:00+00:00"
        pane_record.save("agents", {"w1:p7": retained})
        other = retained.copy()
        cast(pane_launch.StaffPaneLaunch, other).update(staff_id=foreign.id,
                     written_at="2026-11-12T01:00:00+00:00")
        pane_record.save("other", {"w1:p7": other})
        with (self.subTest(record="another server reuses the pane id"),
              patch.object(case.world, "panes", side_effect=lambda: READ_PANES(case.world))):
            self.assertEqual(restart.row_panes([owner], case.world), [(owner, "w1:p7")])


    def test_restart_lock_refusal_leaves_the_record_unchanged(self) -> None:
        reference = str(self.rows[0].id)
        for stop_all in (False, True):
            for saved in ([], [reference, "another-pending-row"]):
                with self.subTest(stop_all=stop_all, saved=saved):
                    shutdown_record.record_path().unlink(missing_ok=True)
                    if saved:
                        shutdown_record.save(saved)
                    path = shutdown_record.record_path()
                    before = path.read_bytes() if path.exists() else None
                    with (patch.object(staff, "row_operation", side_effect=staff.StaffRefused("row is locked")),
                          patch.object(restart, "start_row") as start,
                          redirect_stderr(io.StringIO()) as errors):
                        self.assertEqual(restart.restart(stop_all=stop_all, only=(reference,)), 0)
                    self.assertEqual(path.read_bytes() if path.exists() else None, before)
                    start.assert_not_called()
                    self.assertEqual(errors.getvalue().splitlines(),
                        [f"{self.rows[0].name}: restart anomaly: row is locked"])

    def test_restart_record_delta_keeps_another_runs_failed_row(self) -> None:
        self.running.clear()
        other = "another-failed-row"

        def start(_row: staff.StaffRow, _record: staff.StaffSnapshot, _client: herdr.Herdr, *,
                  present: bool, resolver: session_staff.RowResolver,
                  target_account: str | accounts.AccountSelection,
                  worker_scope: restart.WorkerRestartScope,
                  observed: restart.RestartObservations,
                  opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                  resume_checks: restart.DirectorResumeChecks) -> None:
            _ = present, resolver, target_account, worker_scope, observed, opening, resume_checks
            shutdown_record.update(started=[], pending=[other])

        shutdown_record.save([str(self.rows[0].id), str(self.rows[1].id)])
        with patch.object(restart, "start_row", side_effect=start):
            self.assertEqual(restart.restart(only=(str(self.rows[0].id),)), 0)
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id), other])

    def test_failed_trust_write_keeps_the_pending_restart_and_reports_once(self) -> None:
        self.running.clear()
        row = self.rows[0]
        shutdown_record.save([str(row.id)])
        reason = f"{self.root}: trust anomaly: {self.root / '.claude2/.claude.json'}: PermissionError"

        def launch(*_args: object, **_kwargs: object) -> herdr.AgentLaunched:
            print(reason, file=sys.stderr)
            return herdr.AgentLaunched("", herdr.ClaudeTrustNotWritten(reason))

        with (patch.object(herdr, "launch_prepared_agent", side_effect=launch),
              redirect_stderr(io.StringIO()) as errors):
            result = restart.restart()
        self.assertEqual(result, 1)
        self.assertEqual(shutdown_record.read(), [str(row.id)])
        self.assertEqual(errors.getvalue().splitlines(), [reason])
        self.assertEqual(self.launches, [])

    def test_started_row_leaves_pending_when_layout_save_fails(self) -> None:
        self.running.clear()
        shutdown_record.save([str(self.rows[0].id)])
        with (patch.object(herdr.Herdr, "remember_row_tab", side_effect=herdr_types.HerdrFailure("layout unavailable")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertIn(str(self.rows[0].current_session), self.running)
        self.assertEqual(errors.getvalue(), f"{self.rows[0].name}: restart anomaly: layout unavailable\n")

    def test_unit_without_a_saved_tab_opens_in_its_parent_workspace(self) -> None:
        self.running.clear()
        parent = staff.StaffRow(staff.StaffId("parent"), "showrunner", "production", staff.STAFF_ROOT,
                                current_session=staff.StaffId("parent-session"))
        owner = staff.StaffRow(staff.StaffId("owner"), "producer", "owner", staff.ReportsTo(parent.id),
                               current_session=staff.StaffId("owner-session"))
        row = replace(self.rows[0], parent=staff.ReportsTo(owner.id))
        record = staff.StaffSnapshot((parent, owner, row))
        with (patch.object(pane_record, "publication_for_session", return_value=None),
              patch.object(herdr, "saved_layout", return_value=herdr.SavedLayout(at=1, workspaces=[]))):
            self.assertEqual(restart.start_row(row, record, herdr.Herdr("agents")), "w1:p0")
        self.assertEqual(self.launches, [(row.name, herdr.NewTab(parent.name, row.name))])

    def test_unit_with_a_saved_tab_keeps_its_saved_workspace(self) -> None:
        self.running.clear()
        parent = staff.StaffRow(staff.StaffId("parent"), "showrunner", "saved", staff.STAFF_ROOT,
                                current_session=staff.StaffId("parent-session"))
        row = replace(self.rows[0], parent=staff.ReportsTo(parent.id))
        with patch.object(pane_record, "publication_for_session", return_value=None):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot((parent, row)),
                                             herdr.Herdr("agents")), "w1:p0")
        self.assertEqual(self.launches, [(row.name, herdr.NewTab("saved", row.name))])

    def test_saved_tab_waits_for_its_own_shell_and_never_splits(self) -> None:
        settings = Path(os.environ["CONF_DIR"]) / "shutdown.conf"
        _ = write_config_fixture(settings, "OWN_PANE_SHELL_SECONDS=0.2\n")
        layout: herdr.SavedLayout = {"at": 1, "workspaces": [{"name": "saved", "seen": 1,
            "tabs": [{"staff_id": "row-0", "seen": 1}]}]}
        self.rows = (self.rows[0], replace(self.rows[1], kind="worker", parent=staff.ReportsTo(self.rows[0].id)), self.rows[2])
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(self.rows)))
        sibling_identity = str(self.rows[1].current_session)
        foreign = staff.StaffRow(staff.StaffId("foreign-producer"), "producer", "another-production", staff.STAFF_ROOT,
                                 current_session=staff.StaffId("foreign-producer-session"))
        read_native = herdr.native_process

        def native_process(pid: int, kind: str) -> herdr.NativeAgentProcess:
            if pid == 990102 and kind == "claude":
                return herdr.NativeAgentProcess(herdr.EnvironmentThread(str(foreign.current_session)), "fixture-start", {})
            if pid == 990101 and kind == "claude":
                return herdr.NativeAgentProcess(herdr.EnvironmentThread(sibling_identity), "fixture-start", {})
            return read_native(pid, kind)

        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=native_process))
        path = pane_launch.record_directory() / "fixture.json"
        launch: pane_launch.StaffPaneLaunch = {"pane_id": "w1:own", "staff_id": self.rows[0].id,
            "model": "fixture", "remote_control": False, "environment": {}, "flags_read": True, "written_at": "fixture-start"}
        pane_launch.save(path, {"w1:own": launch})

        workspace_label = "saved"
        own_only = False

        def result(*arguments: str) -> dict[str, object]:
            if arguments == ("workspace", "list"):
                return {"workspaces": [{"workspace_id": "w1", "label": workspace_label}]}
            if arguments == ("tab", "list"):
                return {"tabs": [{"workspace_id": "w1", "tab_id": "t1", "label": "unit-0"}]}
            if arguments == ("pane", "list"):
                own = {"pane_id": "w1:own", "workspace_id": "w1", "tab_id": "t1", "terminal_id": "own-terminal"}
                sibling = {"pane_id": "w1:sibling", "workspace_id": "w1", "tab_id": "t1", "terminal_id": "sibling-terminal"}
                return {"panes": [own] if own_only else [sibling, own]}
            if arguments[:2] == ("pane", "process-info"):
                pid = 990101 if arguments[-1] == "w1:sibling" else 990000
                if workspace_label == "morty" and own_only:
                    pid = 990102
                argv = ["claude"] if pid in (990101, 990102) else ["zsh"]
                return {"process_info": {"shell_pid": 990000, "foreground_process_group_id": pid,
                    "foreground_processes": [{"pid": pid, "name": argv[0], "argv": argv}]}}
            raise AssertionError(arguments)

        client = herdr.Herdr("fixture")
        with (patch.object(herdr, "saved_layout", return_value=layout),
              patch.object(client, "result", side_effect=result),
              patch.object(client, "pane_activity", return_value=herdr.PaneAtShell()) as activity):
            self.assertEqual(client.saved_row_tab("row-0", "unit-0", "fallback"), herdr.ExistingPane("w1:own", "own-terminal"))
            activity.assert_called_once_with("w1:own")
            # The move race: the stopped agent and the shell setup still hold the own pane for a moment.
            activity.side_effect = [herdr.PaneRunning(("claude",)), herdr.PaneRunning(("direnv",)), herdr.PaneAtShell()]
            self.assertEqual(client.saved_row_tab("row-0", "unit-0", "fallback"), herdr.ExistingPane("w1:own", "own-terminal"))
            activity.side_effect = None
            activity.return_value = herdr.PaneRunning(("codex",))
            with self.assertRaises(herdr.OwnPaneNotAtShell) as refused:
                _ = client.saved_row_tab("row-0", "unit-0", "fallback")
            self.assertEqual(str(refused.exception),
                f"unit-0: restart refused: own pane w1:own is not at the shell after 0.2 s; session {self.rows[0].current_session} "
                + "stays stopped; when that pane is at the shell, run: agents resume unit-0")
            pane_launch.save(path, {})
            activity.reset_mock()
            self.assertEqual(client.saved_row_tab("row-0", "unit-0", "fallback"),
                             herdr.NewTab(herdr.row_workspace(self.rows[0], staff.StaffSnapshot(self.rows), "fallback"), "unit-0"))
            activity.assert_not_called()

        producer = replace(self.rows[0], kind="producer", unit_type=staff.NotUnit())
        self.rows = (producer,)
        self.running.clear()
        record = staff.StaffSnapshot((*self.rows, foreign))

        def resume(row: staff.StaffRow, entry: pane_record.PaneLaunch, *,
                   configuration: models_registry.LaunchModelConfiguration | None = None
                   ) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
            del entry, configuration
            return restart.producer_launch(row)

        _ = self.enterContext(patch.object(restart, "producer_resume", side_effect=resume))
        for workspace_label, own_only, shell in (("morty", True, True), ("producers", False, False), ("producers", True, False),
                                                 ("producers", False, True), ("producers", True, True)):
            with self.subTest(workspace=workspace_label, own_only=own_only, shell=shell):
                layout["workspaces"][0]["name"] = workspace_label
                pane_launch.save(path, {"w1:own": launch})
                self.running.clear()
                self.launches.clear()
                with (patch.object(herdr, "saved_layout", return_value=layout),
                      patch.object(client, "result", side_effect=result),
                      patch.object(client, "panes", return_value=[]),
                      patch.object(client, "pane_activity", return_value=herdr.PaneAtShell()
                                   if shell else herdr.PaneRunning(("codex",)))):
                    if not shell and own_only:
                        self.assertRaises(herdr.OwnPaneNotAtShell, restart.start_row, producer, record, client)
                        self.assertEqual(self.launches, [])
                        continue
                    self.assertEqual(restart.start_row(producer, record, client), "w1:p0")
                expected = (herdr.ExistingPane("w1:own", "own-terminal") if workspace_label == "producers"
                            and own_only else herdr.NewTab("producers", producer.name))
                self.assertEqual(self.launches, [(producer.name, expected)])

        from scripts.agents.test_pane_record_recovery import FakeClient

        base_producer = producer
        for commands, refused, failure, scenario in (
                (("editor",), False, "", "retained"), (("background-job",), False, "", "retained"),
                ((), True, "", "retained"), ((), False, "wrong identity", "retained"),
                ((), False, "control failed start", "retained"),
                ((), False, "", "cross session"), ((), False, "", "document folder"),
                ((), False, "", "source unavailable")):
            with self.subTest(commands=commands, refused=refused, failure=failure, scenario=scenario):
                self.running.clear()
                producer = replace(base_producer, home=staff.SessionHome(self.root / ".claude2"))
                folder = self.root
                if scenario == "document folder":
                    folder = self.root / "producer-documents"
                    folder.mkdir(exist_ok=True)
                    producer = replace(producer, worktree=staff.WORKTREE_UNFILLED,
                                       doc=staff.LiveDoc(remote.local_machine(), folder / "plan.md"))
                record = staff.StaffSnapshot((producer,))
                client = FakeClient([{"pane_id": "old", "tab_id": "old-tab", "label": producer.name,
                    "workspace_id": "old-space", "agent_session": {"kind": "id", "value": producer.current_session}}])
                locations = client.rows
                directory = self.root / ("producer-records-" + scenario.replace(" ", "-"))
                directory.mkdir(exist_ok=True)
                original = pane_record.launch("old", producer.id, str(self.root),
                                              {"CLAUDE_CONFIG_DIR": str(self.root / ".claude2")}, ["claude"])
                original = pane_launch.retained_launch(original, record)
                cross = scenario in ("cross session", "source unavailable")
                source_path = directory / ("legacy.json" if cross else "agents.json")
                pane_launch.save(source_path, {"old": original})
                source_client = FakeClient([dict(client.rows[0])]) if cross else client
                source_client.session = "legacy" if cross else "agents"
                if cross:
                    client.rows[0]["agent_session"] = {"kind": "none"}
                def client_for(session: str) -> herdr.Herdr:
                    if session == "legacy":
                        if scenario == "source unavailable":
                            raise OSError("fixture source is unavailable")
                        return source_client
                    self.assertEqual(session, "agents")
                    return client

                def panes() -> list[herdr.PaneIdentity]:
                    return [{"pane_id": str(item["pane_id"]), "workspace_id": str(item["workspace_id"]),
                             "terminal_id": str(item["terminal_id"]), "session_id": producer.current_session}
                            for item in locations]

                def activity(pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
                    return herdr.PaneRunning(commands) if pane == "old" and commands else herdr.PaneAtShell()

                def publish(*arguments: object, **options: object) -> herdr.AgentLaunched:
                    self.assertIsInstance(arguments[1], herdr.NewTab)
                    self.assertEqual(options["agent"], producer.name)
                    if failure == "control failed start":
                        raise OSError("fixture start failed")
                    locations.append({"pane_id": "new", "tab_id": "new-tab", "label": producer.name,
                        "workspace_id": "new-space", "terminal_id": "new-terminal",
                        "agent_session": {"kind": "id", "value": producer.current_session}})
                    launch = pane_record.launch("new", producer.current_session, str(folder),
                        {"CLAUDE_CONFIG_DIR": str(self.root / ".claude2")}, ["claude", *cast(list[str], arguments[4])])
                    launch.update(pid=456, proc_start="fixture-start")
                    pane_record.write("agents", launch)
                    self.running.add(producer.current_session)
                    return herdr.AgentLaunched("new", herdr.PromptSubmissionNotRequired())

                observed = herdr.NativeAgentProcess(herdr.EnvironmentThread(
                    "wrong" if failure == "wrong identity" else producer.current_session), "fixture-start",
                    {"HERDR_PANE_ID": "new", "HERDR_SESSION": "agents", "CLAUDE_CONFIG_DIR": str(self.root / ".claude2")})
                process_info = client.result
                def current_process(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
                    if arguments == ("pane", "process-info", "--pane", "new") and producer.current_session in self.running:
                        return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": 456,
                            "foreground_processes": [{"pid": 456, "name": "claude", "argv": ["claude"]}]}}
                    return process_info(*arguments, deadline=deadline)
                def close_source(pane: str) -> bool:
                    source_client.close_tab_of(pane)
                    return True
                def close_destination(pane: str) -> bool:
                    if scenario != "source unavailable":
                        return False
                    client.close_tab_of(pane)
                    return True
                source_scope = (patch.object(source_client, "panes", return_value=[{
                    "pane_id": "old", "workspace_id": "old-space", "terminal_id": "old-terminal", "session_id": None}])
                    if cross else nullcontext())
                with (patch.object(staff, "initial_snapshot", return_value=record),
                      patch.object(staff, "local_snapshot", return_value=record),
                      patch.object(pane_record, "RECORD_DIR", directory),
                      patch.object(herdr, "shared_session", return_value="agents"),
                      patch.object(herdr, "Herdr", side_effect=client_for), source_scope,
                      patch.object(client, "panes", side_effect=panes),
                      patch.object(client, "result", side_effect=current_process),
                      patch.object(client, "pane_activity", side_effect=activity),
                      patch.object(client, "close_tab_confirmed", side_effect=close_destination) as destination_close,
                      (patch.object(source_client, "close_tab_confirmed", side_effect=close_source)
                       if cross else nullcontext()),
                      patch.object(herdr, "prepare_session_environment", return_value=herdr.PreparedAgentEnvironment(folder, {})),
                      patch.object(herdr, "native_process", return_value=observed),
                      patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
                      patch.object(herdr, "launch_prepared_agent", side_effect=publish)):
                    if failure:
                        with self.assertRaises((OSError, ValueError)):
                            _ = restart.start_row(producer, record, client)
                        self.assertEqual(pane_record.read("agents"), {"old": original})
                    else:
                        self.assertEqual(restart.start_row(producer, record, client), "new")
                        self.assertEqual(list(pane_record.read("agents")), ["new"] if cross else ["new", "old"])
                        second = stop_resume.resume(producer.name)
                        self.assertIsInstance(second, stop_resume.ClaudeUp)
                        assert isinstance(second, stop_resume.ClaudeUp)
                        self.assertEqual((second.session_id, second.pane_id), (producer.current_session, "new"))
                    self.assertEqual(client.closed, [])
                    self.assertEqual(client.rows[0]["pane_id"], "old")
                    if scenario == "cross session":
                        self.assertEqual(source_client.closed, ["old"])
                    if scenario == "source unavailable":
                        self.assertEqual(source_client.closed, [])
                        destination_close.assert_not_called()

    def test_held_sessions_prunes_live_rows_and_keeps_down_rows_and_tabs(self) -> None:
        shutdown_record.save([str(row.id) for row in self.rows])
        self.running.remove(str(self.rows[2].current_session))
        with (patch.object(shutdown, "restart", restart), patch.object(shutdown, "wait_for_workers") as wait,
              patch.object(herdr.Herdr, "save_layout") as save,
              patch.object(herdr.Herdr, "remember_row_tab") as remember,
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["held-sessions"]), 0)
        self.assertEqual(shutdown_record.read(), ["row-2"])
        save.assert_called_once()
        self.assertEqual([call.args for call in remember.call_args_list], [("row-0", "w1:p0"), ("row-1", "w1:p1")])
        wait.assert_called_once()
        self.assertRegex(output.getvalue(), r"^saved 2 live row tabs at \d{2}:\d{2}\n$")
        self.assertEqual(self.stops, [])

    def test_staff_read_error_keeps_pending_rows_and_reports_one_anomaly(self) -> None:
        shutdown_record.save(["earlier"])
        with (patch.object(shutdown, "restart", restart), patch.object(shutdown, "wait_for_workers") as wait,
              patch.object(staff, "initial_snapshot", side_effect=OSError("staff unavailable")),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(shutdown.main(["held-sessions"]), 0)
        self.assertEqual(shutdown_record.read(), ["earlier"])
        wait.assert_called_once_with(staff.StaffSnapshot(()))
        self.assertTrue(output.getvalue().startswith("saved 0 live row tabs at "))
        self.assertEqual(errors.getvalue(), "restart: save anomaly: staff unavailable\n")

    def test_restart_shares_the_prelaunch_cursor_without_losing_resume_proof(self) -> None:
        row = self.rows[0]
        self.running.clear()
        home = self.root / ".claude2"
        requested = herdr.SessionTranscript(home, str(row.current_session))
        original = home / "projects/fixture" / f"{row.current_session}.jsonl"
        history = json.dumps({"timestamp": "2020-01-01T00:00:00+00:00", "type": "assistant",
                              "message": {"role": "assistant", "content": "prelaunch-only-history"}}) + "\n"
        for native_identity_changes in (False, True):
            with self.subTest(native_identity_changes=native_identity_changes):
                native = herdr.SessionTranscript(home, str(row.current_session) + ("-native" if native_identity_changes else ""))
                source = original.with_name(native.session_id + ".jsonl")
                write_text_atomic(original, history)
                write_text_atomic(source, history)
                shutdown_record.save([str(row.id)])
                cursors: list[herdr.TranscriptCursor] = []

                def launched(*arguments: object, **_options: object) -> herdr.AgentLaunched:
                    opening = arguments[5]
                    assert isinstance(opening, herdr.OpeningPrompt)
                    cursor = opening.cursor
                    assert isinstance(cursor, herdr.TranscriptCursor)
                    cursors.append(cursor)
                    self.assertEqual(cursor.transcript, requested)
                    self.assertEqual(cursor.offsets, {original: len(history.encode())})
                    user_at = cursor.since + timedelta(seconds=1)
                    user = {"timestamp": user_at.isoformat(), "type": "user",
                            "message": {"role": "user", "content": opening.text}}
                    write_text_atomic(source, source.read_text() + json.dumps(user) + "\n")
                    assistant = {"timestamp": (user_at + timedelta(seconds=1)).isoformat(), "type": "assistant",
                                 "message": {"role": "assistant", "content": "Resumed work"}}
                    write_text_atomic(source, source.read_text() + json.dumps(assistant) + "\n")
                    _ = opening.before_prompt(native)
                    self.assertEqual(cursor.transcript, requested)
                    self.assertEqual(cursor.offsets, {original: len(history.encode())})
                    native_cursor = cursor if native == requested else herdr.TranscriptCursor(native, cursor.since)
                    proof = native_cursor.submitted_prompt(opening.text)
                    self.assertIsInstance(proof, herdr.PromptSubmitted)
                    return herdr.AgentLaunched("w1:p0", herdr.PromptSubmitted(user_at))

                with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                      patch.object(herdr, "launch_prepared_agent", side_effect=launched),
                      patch.object(json, "loads", wraps=json.loads) as parsed,
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(), 0)
                self.assertEqual(len(cursors), 1)
                self.assertEqual([line for line in output.getvalue().splitlines() if "restart check:" in line],
                                 ["restart check: 1 of 1 directors resumed"])
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(shutdown_record.read(), [])
                self.assertEqual(any("prelaunch-only-history" in str(value)
                                    for parsed_call in parsed.call_args_list
                                    for value in cast(tuple[object, ...], parsed_call.args)), native_identity_changes)

    def test_restart_callback_preserves_a_receipt_under_the_requested_id(self) -> None:
        row = self.rows[0]
        self.running.clear()
        client = herdr.Herdr("agents")
        checks = restart.DirectorResumeChecks()
        discovered = str(row.current_session) + "-native"

        def launched(*arguments: object, **_options: object) -> herdr.AgentLaunched:
            opening = arguments[5]
            assert isinstance(opening, herdr.OpeningPrompt)
            cursor = opening.cursor
            assert isinstance(cursor, herdr.TranscriptCursor)
            transcript = cursor.transcript
            path = transcript.config_dir / "projects/fixture" / f"{transcript.session_id}.jsonl"
            at = cursor.since + timedelta(seconds=1)
            write_text_atomic(path, json.dumps({"timestamp": at.isoformat(), "type": "user",
                "message": {"role": "user", "content": script_prompt(opening.text)}}) + "\n")
            pane = "w1:p9"
            outcome = client.start_agent(row.name, pane, "claude", ["--resume", transcript.session_id], opening, 1000)
            assert not isinstance(outcome, herdr.PromptSubmissionNotRequired)
            self.assertEqual(outcome.state, "confirmed")
            self.assertEqual(cursor.transcript, transcript)
            return herdr.AgentLaunched(pane, outcome)

        with (patch.object(herdr, "launch_prepared_agent", side_effect=launched),
              patch.object(client, "start_command", return_value=subprocess.CompletedProcess([], 0, "", "")),
              patch.object(client, "native_session", return_value=discovered),
              patch.object(herdr, "check_target", return_value=True),
              patch.object(client, "wait_for_prompt_input", return_value=herdr.PromptInputReady()),
              patch.object(client, "run", return_value="────────\n❯ \n────────"),
              patch.object(client, "_prompt") as typed,
              patch.object(client, "_send_keys") as keys,
              redirect_stdout(io.StringIO())):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), client,
                                              opening=herdr.OpeningPrompt("continue the saved work"),
                                              resume_checks=checks), "w1:p9")
        typed.assert_not_called()
        keys.assert_not_called()
        self.assertEqual(checks.checks[0].reader.transcript.session_id, discovered)

    def test_restart_fixed_reference_uses_current_native_id_name_and_folder(self) -> None:
        class ContinuationDelivery(Enum):
            SENT = "the director receives the message"
            FAILED = "the prompt command fails"

        parent = next(row for row in staff.local_snapshot().rows if row.kind == "showrunner")
        self.rows = (replace(self.rows[0],
            id=staff.StaffId(str(uuid5(NAMESPACE_URL, "restart-held-row"))),
            current_session=staff.StaffId(str(uuid5(NAMESPACE_URL, "restart-held-session"))),
            parent=staff.ReportsTo(parent.id)), *self.rows[1:])
        cases: tuple[tuple[Literal["unit", "showrunner"], staff.Active | staff.Paused | staff.Exited,
                           tuple[str, ...], str, Literal["claude", "codex"], ContinuationDelivery, bool], ...] = (
            ("unit", staff.Active(), (), "agents restart ran", "claude", ContinuationDelivery.SENT, False),
            ("showrunner", staff.Active(), (), "agents restart ran", "claude", ContinuationDelivery.SENT, False),
            ("unit", staff.Active(), ("--why", "scheduled maintenance"), "scheduled maintenance",
             "claude", ContinuationDelivery.SENT, False),
            ("unit", staff.Active(), (), "agents restart ran", "codex", ContinuationDelivery.FAILED, False),
            ("unit", staff.Paused("2026-11-11", "user", "wait for review"), (), "",
             "claude", ContinuationDelivery.SENT, False),
            ("unit", staff.Exited("2026-11-11", "user", "finished"), (), "",
             "claude", ContinuationDelivery.SENT, False),
            ("unit", staff.Active(), (), "agents restart ran", "claude", ContinuationDelivery.SENT, True),
        )
        notice = "Codex workers have no capacity. Send no assign."
        for kind, hold, flags, cause, family, delivery, workers_held in cases:
            with self.subTest(kind=kind, hold=hold, flags=flags, cause=cause, family=family, delivery=delivery,
                              workers_held=workers_held):
                native_id = str(self.rows[0].current_session)
                shutdown_record.save([str(self.rows[0].id)])
                folder = self.root / "renamed-folder"
                folder.mkdir(exist_ok=True)
                self.rows = (replace(self.rows[0], name="renamed", kind=kind, paused=hold if isinstance(hold, staff.Paused) else staff.Active(),
                    exited=hold if isinstance(hold, staff.Exited) else staff.Active(),
                    worktree=staff.PresentWorktree(remote.local_machine(), folder)), *self.rows[1:])
                record = staff.StaffSnapshot(self.rows)
                held = isinstance(hold, (staff.Paused, staff.Exited))
                if held:
                    write_text_atomic(Path(os.environ["STAFF_FILE"]), json.dumps({
                        "schema": 2, "rows": [parent.to_json(), self.rows[0].to_json()]}))
                self.running.clear()
                prepared = herdr.PreparedAgentEnvironment(folder, {})
                arguments = (["--resume", native_id, "--name", "renamed"] if family == "claude" else
                             ["resume", native_id])
                with (patch.object(staff, "initial_snapshot", side_effect=staff.local_snapshot)
                      if held else patch.object(staff, "initial_snapshot", return_value=record),
                      patch.object(restart, "session_launch", return_value=(family, {}, arguments)),
                      patch.object(herdr, "prepare_session_environment", return_value=prepared) as prepare,
                      patch.object(quota_state, "no_worker_mode", return_value=workers_held),
                      patch.object(quota_state, "no_worker_notice", return_value=notice),
                      patch.object(herdr.Herdr, "prompt", side_effect=(
                          herdr_types.HerdrFailure("fixture prompt failed\nsecond error line")
                          if delivery is ContinuationDelivery.FAILED else None)) as prompt,
                      patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                          "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
                      redirect_stdout(io.StringIO()) as output,
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(shutdown.main(["restart", *flags]), 0)
                if held:
                    prepare.assert_not_called()
                    launch.assert_not_called()
                    self.assertEqual(shutdown_record.read(), [str(self.rows[0].id)])
                    saved = next(row for row in staff.local_snapshot().rows if row.id == self.rows[0].id)
                    self.assertEqual(saved.paused if isinstance(hold, staff.Paused) else saved.exited, hold)
                    continue
                prepare.assert_called_once()
                self.assertEqual(prepare.call_args.args[:2], (native_id, str(folder)))
                self.assertEqual(prepare.call_args.args[3], herdr.NO_ENVIRONMENT_REMOVALS)
                if family == "claude":
                    self.assertEqual(launch.call_args.args[3:5], (family, arguments))
                else:
                    self.assertEqual(launch.call_args.args[3], "codex")
                    launched_arguments = cast(list[str], launch.call_args.args[4])
                    self.assertEqual(launched_arguments[:2], ["-C", str(folder)])
                    self.assertEqual(launched_arguments[-2:], ["resume", native_id])
                self.assertEqual(launch.call_args.kwargs["agent"], "renamed")
                self.assertEqual(shutdown_record.read(), [])
                launch.assert_called_once()
                if family == "claude":
                    opening = cast(herdr.OpeningPrompt, launch.call_args.args[5])
                    self.assertIsInstance(opening, herdr.OpeningPrompt)
                    self.assertIn(cause, opening.text)
                    self.assertIn("Continue where you stopped.", opening.text)
                    self.assertEqual(notice in opening.text, workers_held)
                    prompt.assert_not_called()
                    self.assertEqual(errors.getvalue(), "")
                else:
                    prompt.assert_called_once()
                    self.assertEqual(prompt.call_args.args[0], "w1:p0")
                    self.assertEqual(prompt.call_args.kwargs["target"], native_id)
                    prompt_text = cast(str, prompt.call_args.args[1])
                    self.assertIn(cause, prompt_text)
                    self.assertIn("Continue where you stopped.", prompt_text)
                    self.assertEqual(errors.getvalue(), "")
                    self.assertIn("renamed: Could not send the continuation message: fixture prompt failed. "
                        + "The director checks the continuation message.", output.getvalue().splitlines())

        class TranscriptAfterRestart(Enum):
            ASSISTANT = "assistant"
            MESSAGE = "message"
            MISSING = "missing"
            HELD_AND_WORKER = "held-and-worker"
            UNREADABLE = "unreadable"
            PARTIAL_TAIL = "partial-tail"
            PARTIAL_AT_LAUNCH = "partial-at-launch"
            REMOVED = "removed"
            TORN_LINE = "torn-line"

        checks: tuple[tuple[Literal["claude", "codex"], TranscriptAfterRestart, str, int], ...] = (
            ("claude", TranscriptAfterRestart.ASSISTANT, "", 1),
            ("claude", TranscriptAfterRestart.MESSAGE, "no work after the continuation message", 0),
            ("claude", TranscriptAfterRestart.MISSING, "no continuation message", 0),
            ("codex", TranscriptAfterRestart.ASSISTANT, "", 1),
            ("codex", TranscriptAfterRestart.MISSING, "no continuation message", 0),
            ("claude", TranscriptAfterRestart.HELD_AND_WORKER, "", 1),
            ("claude", TranscriptAfterRestart.UNREADABLE, "transcript unreadable", 0),
            ("codex", TranscriptAfterRestart.PARTIAL_TAIL, "no work after the continuation message", 0),
            ("codex", TranscriptAfterRestart.PARTIAL_AT_LAUNCH, "", 1),
            ("claude", TranscriptAfterRestart.REMOVED, "transcript unreadable", 0),
            ("codex", TranscriptAfterRestart.TORN_LINE, "transcript unreadable", 0),
        )
        for family, transcript_case, failure, passed in checks:
            with self.subTest(family=family, transcript_case=transcript_case):
                row = replace(self.rows[0], name="TEST-director", kind="unit",
                              paused=staff.Active(), exited=staff.Active())
                held = replace(self.rows[1], name="TEST-held", kind="showrunner",
                               paused=staff.Paused("2026-11-11", "user", "review"))
                child = replace(self.rows[2], name="TEST-worker", kind="worker",
                                parent=staff.ReportsTo(row.id))
                self.rows = (row, held, child)
                record = staff.StaffSnapshot(self.rows)
                home = self.root / (family + "-" + transcript_case.value)
                native_id = str(row.current_session) + ("-launched" if family == "claude" else "")
                source = (home / "projects/fixture" / (native_id + ".jsonl")
                          if family == "claude" else
                          home / "sessions/rollout-" / (str(row.current_session) + ".jsonl"))
                message = resume_message.resume_message("agents restart ran")
                historical = [
                    {"timestamp": "2020-01-01T00:00:00+00:00", "type": "user" if family == "claude" else "response_item",
                     "message" if family == "claude" else "payload": {"role": "user", "content": message}},
                    {"timestamp": "2020-01-01T00:00:01+00:00", "type": "assistant" if family == "claude" else "response_item",
                     "message" if family == "claude" else "payload": {"role": "assistant", "content": "Old work"}},
                ]
                write_text_atomic(source, "".join(json.dumps(item) + "\n" for item in historical))
                if transcript_case is TranscriptAfterRestart.PARTIAL_AT_LAUNCH:
                    write_text_atomic(source, source.read_text() + '{"type":')
                self.running.clear()
                requested = self.rows if transcript_case is TranscriptAfterRestart.HELD_AND_WORKER else (row,)
                shutdown_record.save([str(item.id) for item in requested])
                account = (accounts.NamedAccount("claude2", home) if family == "claude" else
                           accounts.CodexWorkersAccount("codex1", home))

                now = [0.0]
                intervals: list[float] = []

                def advance(seconds: float) -> None:
                    now[0] += seconds
                    intervals.append(seconds)
                    if transcript_case is TranscriptAfterRestart.REMOVED:
                        source.unlink()
                    if family == "claude" and transcript_case is TranscriptAfterRestart.ASSISTANT:
                        work = {"timestamp": datetime.now(timezone.utc).isoformat(), "type": "assistant",
                                "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "fixture"}]}}
                        write_text_atomic(source, source.read_text() + json.dumps(work) + "\n")

                def launched(session: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                             prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                             opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout: int, *,
                             agent: str, reported_unknown: str = "",
                             codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT
                             , client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> herdr.AgentLaunched:
                    _ = client
                    del session, destination, prepared, kind, arguments, timeout, agent, reported_unknown, codex
                    if isinstance(opening, herdr.OpeningPrompt):
                        _ = opening.before_prompt(herdr.SessionTranscript(home, native_id))
                    message = (opening.text if isinstance(opening, herdr.OpeningPrompt) else
                               resume_message.resume_message("agents restart ran"))
                    if transcript_case is TranscriptAfterRestart.UNREADABLE:
                        source.unlink()
                        source.mkdir()
                        return herdr.AgentLaunched("w1:p0", herdr.PromptSubmissionNotRequired())
                    rows: list[dict[str, object]] = []
                    for role in (("user",) if family == "claude" and transcript_case is TranscriptAfterRestart.ASSISTANT else
                                 ("user", "assistant") if transcript_case in (TranscriptAfterRestart.ASSISTANT, TranscriptAfterRestart.HELD_AND_WORKER, TranscriptAfterRestart.PARTIAL_AT_LAUNCH)
                                 else ("user",) if transcript_case in (TranscriptAfterRestart.MESSAGE, TranscriptAfterRestart.PARTIAL_TAIL,
                                                                     TranscriptAfterRestart.REMOVED, TranscriptAfterRestart.TORN_LINE)
                                 else ("assistant",)):
                        payload: dict[str, object] = {"role": role, "content": message if role == "user" else "Resumed work"}
                        rows.append({"timestamp": datetime.now(timezone.utc).isoformat(),
                                     "type": role if family == "claude" else "response_item",
                                     "message" if family == "claude" else "payload": payload})
                    previous = source.read_text()
                    if transcript_case is TranscriptAfterRestart.PARTIAL_AT_LAUNCH:
                        previous += '"event_msg", "timestamp": "2020-01-01T00:00:00+00:00"}\n'
                    tail = ('{"type":' if transcript_case is TranscriptAfterRestart.PARTIAL_TAIL else
                            '{"type":\n' if transcript_case is TranscriptAfterRestart.TORN_LINE else "")
                    write_text_atomic(source, previous + "".join(json.dumps(item) + "\n" for item in rows) + tail)
                    return herdr.AgentLaunched("w1:p0", herdr.PromptSubmissionNotRequired())

                def restore_worker(_parent: staff.StaffId, *, report: worker.RestoreReport, **_options: object) -> worker.ExitCode:
                    report.restored.add(child.id)
                    return worker.ExitCode.OK

                staff_before = Path(os.environ["STAFF_FILE"]).read_bytes()
                with (patch.object(staff, "initial_snapshot", return_value=record),
                      patch.object(restart, "session_launch", return_value=(family, {},
                          ["--resume" if family == "claude" else "resume", str(row.current_session)])),
                      patch.object(member_account, "resume_account", return_value=account),
                      patch.object(worker, "restore", side_effect=restore_worker),
                      patch.object(herdr, "launch_prepared_agent", side_effect=launched),
                      patch.object(time, "monotonic", side_effect=lambda: now[0]),
                      patch.object(time, "sleep", side_effect=advance),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(restart.restart(only=tuple(str(item.id) for item in requested)), 0)
                lines = [line for line in output.getvalue().splitlines() if "restart check:" in line]
                self.assertEqual(lines, ([f"{row.name}: restart check: {failure}"] if failure else []) +
                                 [f"restart check: {passed} of 1 directors resumed"])
                self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), staff_before)
                self.assertEqual(len(intervals), int(bool(failure) or (
                    family == "claude" and transcript_case is TranscriptAfterRestart.ASSISTANT)))
                self.assertEqual(shutdown_record.read(), [str(held.id)] if transcript_case is TranscriptAfterRestart.HELD_AND_WORKER else [])

    def test_restart_failure_keeps_only_the_failed_fixed_reference(self) -> None:
        identities = [str(row.id) for row in self.rows]
        shutdown_record.save(identities)
        self.running.clear()
        launch = self.launch

        def fail_first(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                       prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                       opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "",
                       reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT, client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> herdr.AgentLaunched:
            _ = client
            if agent == self.rows[0].name:
                raise herdr_types.HerdrFailure("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent,
                          reported_unknown=reported_unknown, codex=codex)

        with (patch.object(herdr, "launch_prepared_agent", side_effect=fail_first),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [identities[0]])
        self.assertEqual(errors.getvalue(), "")
        self.assertIn("unit-0: down: fixture launch failed", output.getvalue().splitlines())
        self.assertCountEqual([name for name, _ in self.launches], [row.name for row in self.rows[1:]])

    def test_stopped_director_start_retries_once_with_the_same_arguments(self) -> None:
        self.running.clear()
        for kind in ("unit", "showrunner"):
            with self.subTest(kind=kind):
                row = replace(self.rows[0], kind=kind)
                shutdown_record.save([str(row.id)])
                with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                      patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()) as read,
                      patch.object(restart, "start_row", side_effect=[ValueError("fixture placement failed"), "w1:p0"]) as start,
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(why="fixture restart cause"), 0)
                self.assertEqual(start.call_count, 2)
                read.assert_called_once_with(str(row.current_session))
                first = cast(tuple[object, ...], start.call_args_list[0].args)
                second = cast(tuple[object, ...], start.call_args_list[1].args)
                self.assertEqual(first, second)
                first_options = cast(dict[str, object], start.call_args_list[0].kwargs)
                second_options = cast(dict[str, object], start.call_args_list[1].kwargs)
                for key in ("present", "observed", "opening", "resume_checks"):
                    self.assertEqual(first_options[key], second_options[key])
                self.assertEqual(output.getvalue(), f"{row.name}: down; started\n")
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(shutdown_record.read(), [])

    def test_stopped_director_retry_prints_one_success_line_through_start_row(self) -> None:
        self.running.clear()
        for kind in ("unit", "showrunner"):
            with self.subTest(kind=kind):
                row = replace(self.rows[0], kind=kind)
                shutdown_record.save([str(row.id)])
                with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                      patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
                      patch.object(herdr, "launch_prepared_agent", side_effect=[
                          ValueError("fixture placement failed"),
                          herdr.AgentLaunched("w1:p0", herdr.PromptSubmissionNotRequired()),
                      ]) as launch,
                      patch.object(restart.DirectorResumeCheck, "progress", return_value=restart.ResumeCheckState.RESUMED),
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(), 0)
                self.assertEqual(launch.call_count, 2)
                self.assertEqual([line for line in output.getvalue().splitlines()
                                  if line.startswith(f"{row.name}: ")], [f"{row.name}: down; started"])
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(shutdown_record.read(), [])

    def test_empty_retry_error_keeps_the_row_pending_and_allows_the_next_retry(self) -> None:
        failed, restored = self.rows[:2]
        self.running.clear()
        shutdown_record.save([str(failed.id), str(restored.id)])
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(restart, "start_row", side_effect=[
                  ValueError("first failure"), ValueError("first failure"), OSError(), "w1:p1",
              ]) as start,
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(start.call_count, 4)
        self.assertEqual(output.getvalue().splitlines(), [
            f"{failed.name}: down: OSError", f"{restored.name}: down; started",
        ])
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(shutdown_record.read(), [str(failed.id)])

    def test_director_that_becomes_live_during_retry_prints_only_unchanged(self) -> None:
        self.running.clear()
        for kind in ("unit", "showrunner"):
            for stopped_retry_reads in (0, 1):
                with self.subTest(kind=kind, stopped_retry_reads=stopped_retry_reads):
                    row = replace(self.rows[0], kind=kind)
                    shutdown_record.save([str(row.id)])
                    with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((row,))),
                          patch.object(herdr, "read_conversation", side_effect=[
                              *[herdr.StoppedConversation()] * (3 + stopped_retry_reads),
                              herdr.LiveConversation("claude", (990100,)),
                          ]) as read,
                          patch.object(member_account, "repair_home") as repair,
                          patch.object(herdr, "launch_prepared_agent",
                                       side_effect=ValueError("fixture placement failed")) as launch,
                          redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                        self.assertEqual(restart.restart(), 0)
                    self.assertEqual(read.call_count, 4 + stopped_retry_reads)
                    launch.assert_called_once()
                    repair.assert_called_once_with(row, "claude", 990100)
                    self.assertEqual(output.getvalue(), f"{row.name}: unchanged\n")
                    self.assertEqual(errors.getvalue(), "")
                    self.assertEqual(shutdown_record.read(), [])

    def test_stopped_director_second_start_failure_stays_pending_without_an_anomaly(self) -> None:
        row = self.rows[0]
        self.running.clear()
        shutdown_record.save([str(row.id)])
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(restart, "start_row", side_effect=[ValueError("first failure"), OSError("second failure\nextra detail")]) as start,
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(start.call_count, 2)
        self.assertEqual(output.getvalue(), f"{row.name}: down: second failure\n")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(shutdown_record.read(), [str(row.id)])

    def test_failed_live_or_unknown_director_start_keeps_its_anomaly_without_retry(self) -> None:
        row = self.rows[0]
        self.running.clear()
        for current in (herdr.LiveConversation("claude", (990100,)), herdr.UnknownConversation("fixture identity unreadable")):
            with self.subTest(current=current):
                shutdown_record.save([str(row.id)])
                with (patch.object(herdr, "read_conversation", return_value=current) as read,
                      patch.object(restart, "start_row", side_effect=ValueError("fixture placement failed")) as start,
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.restart(), 0)
                start.assert_called_once()
                read.assert_called_once_with(str(row.current_session))
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), f"{row.name}: restart anomaly: fixture placement failed\n")
                self.assertEqual(shutdown_record.read(), [str(row.id)])

    def test_restart_staff_read_error_preserves_fixed_references(self) -> None:
        shutdown_record.save([str(self.rows[0].id)])
        before = shutdown_record.record_path().read_bytes()
        with (patch.object(staff, "initial_snapshot", side_effect=OSError("staff unavailable")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.record_path().read_bytes(), before)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("staff unavailable", errors.getvalue())
        self.assertEqual(self.launches, [])

    def test_restart_removes_a_saved_id_with_no_staff_row_and_never_selects_a_row_by_name(self) -> None:
        shutdown_record.save([str(self.rows[0].id), "missing"])
        self.running.clear()
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertEqual([name for name, _ in self.launches], [self.rows[0].name])
        path = shutdown_record.record_path()
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns)
        self.rows = (self.rows[0], replace(self.rows[1], name="missing"), *self.rows[2:])
        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(self.rows)),
              redirect_stderr(errors)):
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(restart.restart(), 0)
        self.assertEqual([name for name, _ in self.launches], [self.rows[0].name])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns), before)
        self.assertEqual(errors.getvalue().splitlines(),
            ["missing: restart anomaly: no staff row; removed from the restart record"])
        self.assertEqual(self.stops, [])

    @contextmanager
    def hold_restart_record_until_timeout(self) -> Generator[None]:
        path = shutdown_record.record_path().with_suffix(".lock")
        acquire_flock = worker_lifecycle.acquire_flock
        with path.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            contend = True

            def release_after_timeout(handle: int | worker_lifecycle.LockFileDescriptor, *,
                                      mode: int = fcntl.LOCK_EX, deadline: float = math.inf) -> None:
                nonlocal contend
                if not contend or getattr(handle, "name", None) != str(path):
                    acquire_flock(handle, mode=mode, deadline=deadline)
                    return
                contend = False
                try:
                    acquire_flock(handle, mode=mode, deadline=min(deadline, time.monotonic() + 0.05))
                finally:
                    fcntl.flock(held, fcntl.LOCK_UN)

            with patch.object(worker_lifecycle, "acquire_flock", side_effect=release_after_timeout):
                yield

    def test_restart_keeps_a_deleted_id_when_its_record_lock_times_out(self) -> None:
        missing = "missing-row"
        rows = self.rows[:2]
        shutdown_record.save([missing, *(str(row.id) for row in rows)])
        self.running.clear()
        with self.hold_restart_record_until_timeout(), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["restart"]), 0)

        report = output.getvalue()
        self.assertCountEqual([name for name, _ in self.launches], [row.name for row in rows], report)
        self.assertEqual(shutdown_record.read(), [missing])
        self.assertEqual(Path(report.splitlines()[0]).read_text(), report)
        self.assertIn("agents restart: 2 of 2 sessions back.", report)
        self.assertIn(f"{missing}: could not remove {missing} from the restart record: the record is busy.", report)
        self.assertIn("Nate waits for the command that holds the restart record lock to finish.", report)
        self.assertIn("Nate runs agents restart there.", report)
        self.assertNotIn("Restart removed the entry.", report)

    def test_restart_busy_record_retry_keeps_the_account(self) -> None:
        missing = "missing-row"
        first, other = self.rows[:2]
        shutdown_record.save([missing, str(first.id), str(other.id)])
        self.running.clear()
        self.account_processes()

        def saved(identity: str, name: str) -> tuple[Literal["claude"], dict[str, str], list[str]]:
            label = ".claude3" if identity == first.current_session else ".claude4"
            return "claude", {"CLAUDE_CONFIG_DIR": str(self.root / label)}, ["--resume", identity, "--name", name]

        with (patch.object(restart, "session_launch", side_effect=saved),
              patch.object(restart, "start_row", return_value="w1:p0") as start):
            with self.hold_restart_record_until_timeout(), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(["restart", "claude3"]), 0)
            self.assertEqual([item.args[0] for item in start.call_args_list], [first])
            self.assertEqual(shutdown_record.read(), [missing, str(other.id)])

            issue = next(line for line in output.getvalue().splitlines() if line.startswith(f"{missing}:"))
            self.assertIn(f"could not remove {missing} from the restart record: the record is busy.", issue)
            retry = shlex.split(issue.partition("Nate runs ")[2].removesuffix(" there."))
            self.assertEqual(retry, ["agents", "restart", "claude3"])
            start.reset_mock()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(shutdown.main(retry[1:]), 0)
            start.assert_not_called()
            self.assertEqual(shutdown_record.read(), [str(other.id)])

    def test_restart_only_removes_a_retired_producer_from_the_record(self) -> None:
        retired = replace(self.rows[0], id=staff.StaffId("alpha-id"), name="alpha", kind="producer",
                          unit_type=staff.NotUnit(), retirement=staff.RetiredProducer("claude:sonnet:high"))
        record = staff.StaffSnapshot((retired, *self.rows[1:]))
        other = str(self.rows[1].id)
        shutdown_record.save([retired.id, other])
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(restart, "start_row") as start, redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["restart", "--only", retired.id]), 0)

        start.assert_not_called()
        self.assertEqual(shutdown_record.read(), [other])
        self.assertIn("agents restart: 0 of 0 sessions back.", output.getvalue())
        self.assertIn(f"{retired.id}: the producer is retired. Restart removed the entry.", output.getvalue())

    def test_restart_saved_conversation_reference_does_not_select_a_member(self) -> None:
        reference = str(self.rows[0].current_session)
        shutdown_record.save([reference])
        self.running.clear()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.launches, [])
        self.assertEqual(errors.getvalue(),
            f"{reference}: restart anomaly: no staff row; removed from the restart record\n")
        self.assertEqual(shutdown_record.read(), [])

    def test_restart_command_still_resolves_a_name_without_selecting_saved_entries(self) -> None:
        shutdown_record.save([str(self.rows[1].id), "missing"])
        self.running.clear()
        self.assertEqual(restart.restart(only=(self.rows[0].name,)), 0)
        self.assertEqual([name for name, _ in self.launches], [self.rows[0].name])
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id), "missing"])

    def test_bare_dry_run_reports_record_time_and_missing_rows(self) -> None:
        shutdown_record.save(["row-0", "gone"])
        saved_at = time.strftime("%H:%M", time.localtime(shutdown_record.record_path().stat().st_mtime))
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(restart.restart(dry_run=True), 0)
        self.assertEqual(output.getvalue().splitlines(), [f"restart record: 2 rows saved at {saved_at}",
            "unit-1: live, not in the restart record", "unit-2: live, not in the restart record",
            "unit-0: row-0, unit, workspace live, tab 1 (unit-0)"])
        self.assertEqual(errors.getvalue(), "gone: restart anomaly: no staff row; would leave the restart record\n")
        self.assertEqual(shutdown_record.read(), ["row-0", "gone"])

    def test_layout_error_keeps_the_live_rows_and_other_tab_saves(self) -> None:
        with (patch.object(herdr.Herdr, "save_layout", side_effect=herdr_types.HerdrFailure("layout unavailable")),
              patch.object(herdr.Herdr, "remember_row_tab") as remember,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("fixture"), save_tabs=True).rows,
                             list(self.rows))
        self.assertEqual(remember.call_count, 3)
        self.assertEqual(errors.getvalue(), "restart: layout anomaly: layout unavailable\n")

    def test_restart_all_resumes_a_pending_holder_after_an_interruption(self) -> None:
        pending = str(self.rows[0].id)
        shutdown_record.save([pending, "missing-row"])
        self.running.clear()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual([name for name, _ in self.launches], [self.rows[0].name])
        self.assertEqual(shutdown_record.read(), [])
        self.assertIn("missing-row: restart anomaly: no staff row; removed from the restart record", errors.getvalue())

    def owner_and_worker(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffSnapshot]:
        owner = self.rows[0]
        child = replace(self.rows[1], kind="worker", unit_type=staff.NotUnit(), parent=staff.ReportsTo(owner.id))
        return owner, child, staff.StaffSnapshot((owner, child))

    def test_stop_all_saves_no_worker_that_a_restart_never_starts(self) -> None:
        from scripts.production import worker

        owner, child, record = self.owner_and_worker()

        def not_restored(row: staff.StaffRow, _record: staff.StaffSnapshot, *,
                         runs: dict[str, list[str]] | None = None) -> worker.NotRestored | None:
            del runs
            return worker.NotRestored("worker with no active dispatch") if row.id == child.id else None

        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([owner, child])),
              patch.object(restart, "prune_live_rows"),
              patch.object(restart.stop_resume, "stop_unit_workers", return_value=()) as stop_workers,
              patch.object(restart, "stop_row", return_value=True) as stop,
              patch.object(restart, "start_row") as start,
              patch.object(staff, "row_operation"),
              patch.object(worker, "not_restored", side_effect=not_restored)):
            self.assertEqual(restart.stop_all(), 0)
        self.assertEqual(shutdown_record.read(), [str(owner.id)])
        self.assertEqual([item.args[0] for item in stop.call_args_list], [owner])
        stop_workers.assert_called_once()
        start.assert_not_called()

    def test_restart_removes_a_saved_worker_that_restore_never_starts(self) -> None:
        from scripts.production import worker

        _owner, child, record = self.owner_and_worker()
        shutdown_record.save([str(child.id)])
        for dry_run, saved, action in ((True, [str(child.id)], "would leave"), (False, [], "removed from")):
            with (self.subTest(dry_run=dry_run),
                  patch.object(staff, "initial_snapshot", return_value=record),
                  patch.object(worker, "not_restored", return_value=worker.NotRestored("Codex worker in no-worker mode")),
                  patch.object(restart, "live_rows", return_value=restart.CompleteLiveRows([])),
                  patch.object(restart, "start_row") as start,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(restart.restart(dry_run=dry_run), 0)
                self.assertEqual(shutdown_record.read(), saved)
                start.assert_not_called()
                self.assertIn(f"{child.name}: restart anomaly: Codex worker in no-worker mode; {action} the restart record\n",
                              errors.getvalue())

    def test_stop_all_saves_each_row_before_stop_and_starts_nothing(self) -> None:
        def stop(identity: str, *, deadline: float = math.inf,
                 codex: herdr.CodexConversations | None = None,
                 runs: dict[str, list[str]] | None = None) -> stop_resume.Stopped:
            row = next(row for row in self.rows if row.current_session == identity)
            self.assertIn(str(row.id), shutdown_record.read())
            return self.stop(identity, deadline=deadline, codex=codex, runs=runs)

        with patch.object(stop_resume, "stop", side_effect=stop):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        self.assertCountEqual(shutdown_record.read(), [str(row.id) for row in self.rows])
        self.assertCountEqual(self.stops, [str(row.id) for row in self.rows])
        self.assertEqual(self.launches, [])

    def test_stop_all_prunes_43_live_entries_and_keeps_a_recorded_down_row(self) -> None:
        base = self.rows[0]
        self.rows = tuple(replace(base, id=staff.StaffId(f"row-{index}"), name=f"unit-{index}",
                                  current_session=staff.StaffId(f"conversation-{index}")) for index in range(44))
        self.running = {str(row.current_session) for row in self.rows[:43]}
        down = str(self.rows[43].id)
        shutdown_record.save([str(row.id) for row in self.rows])

        def refused(row: staff.StaffRow, _client: herdr.Herdr, *, codex: herdr.CodexConversations) -> bool:
            _ = codex
            self.assertIn(down, shutdown_record.read())
            self.assertIn(str(row.id), shutdown_record.read())
            return False

        with (patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot(self.rows)),
              patch.object(restart, "stop_row", side_effect=refused) as stopped):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        self.assertEqual(stopped.call_count, 43)
        self.assertEqual(shutdown_record.read(), [down])
        self.assertEqual(self.launches, [])

    def test_concurrent_scoped_stop_survives_both_unscoped_commands(self) -> None:
        record = staff.StaffSnapshot(self.rows)
        first, concurrent, _other = self.rows
        for command in (["stop", "--all"], ["held-sessions"]):
            with self.subTest(command=command):
                self.running = {str(first.current_session)}
                shutdown_record.save([str(first.id)])

                def read(*args: object, **kwargs: object) -> restart.CompleteLiveRows:
                    _ = args, kwargs
                    # Another command stops a new row after the unscoped liveness read.
                    self.running.add(str(concurrent.current_session))
                    self.assertEqual(shutdown.main(["stop", "--all", "--only", str(concurrent.id)]), 0)
                    return restart.CompleteLiveRows([first])

                # The nested scoped stop uses its own read to select only its target.
                nested = False

                def snapshot(*args: object, **kwargs: object) -> restart.CompleteLiveRows | restart.PartialLiveRows:
                    nonlocal nested
                    if nested:
                        return restart.CompleteLiveRows([concurrent])
                    nested = True
                    return read(*args, **kwargs)

                with (patch.object(staff, "initial_snapshot", return_value=record),
                      patch.object(restart, "live_rows", side_effect=snapshot)):
                    self.assertEqual(shutdown.main(command), 0)
                self.assertIn(str(concurrent.id), shutdown_record.read())
                self.assertNotIn(str(concurrent.current_session), self.running)

    def test_pruning_rechecks_a_concurrent_stop_under_the_row_lock(self) -> None:
        row = self.rows[0]
        reading = restart.CompleteLiveRows([row])
        shutdown_record.save([str(row.id)])
        self.assertEqual(shutdown.main(["stop", "--all", "--only", str(row.id)]), 0)
        restart.prune_live_rows(reading)
        self.assertEqual(shutdown_record.read(), [str(row.id)])
        self.assertNotIn(str(row.current_session), self.running)

    def test_pruning_preserves_entries_when_their_row_locks_are_refused(self) -> None:
        row = self.rows[0]
        shutdown_record.save([str(row.id)])
        with patch.object(staff, "row_operation", side_effect=staff.StaffRefused("fixture lock refused")):
            restart.prune_live_rows(restart.CompleteLiveRows([row]))
        self.assertEqual(shutdown_record.read(), [str(row.id)])

    def test_duplicate_pane_stop_refusal_leaves_no_live_entry(self) -> None:
        row = self.rows[0]
        self.rows = (row,)
        panes = self.panes()
        duplicate = {**panes[0], "pane_id": "w1:p9"}
        for command in (["stop", "--all"], ["restart", "--all"]):
            with self.subTest(command=command):
                shutdown_record.save(["down-row"])
                with (patch.object(pane_launch, "reference_panes", return_value=[panes[0], duplicate]),
                      redirect_stderr(io.StringIO())):
                    self.assertEqual(shutdown.main(command), 0)
                # A restart removes the saved id with no staff row; a stop reads no saved ids.
                self.assertEqual(shutdown_record.read(), ["down-row"] if command[0] == "stop" else [])
                self.assertIn(str(row.current_session), self.running)
                self.assertEqual((self.stops, self.launches), ([], []))

    def test_partial_liveness_preserves_stopped_rows_for_both_unscoped_commands(self) -> None:
        read = herdr.read_conversation
        failed = self.rows[0]
        retained = str(self.rows[2].id)

        def observe(identity: str, kind: str = "", *, codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation | herdr.StoppedConversation | herdr.UnknownConversation:
            if identity == failed.current_session:
                raise OSError("fixture liveness read failed")
            return read(identity, kind, codex=codex)

        for command in (["stop", "--all"], ["held-sessions"]):
            with self.subTest(command=command):
                self.running = {str(row.current_session) for row in self.rows[:2]}
                shutdown_record.save([retained])
                with (patch.object(herdr, "read_conversation", side_effect=observe),
                      redirect_stdout(io.StringIO()) as output,
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(shutdown.main(command), 0)
                self.assertIn(retained, shutdown_record.read())
                if command[0] == "stop":
                    self.assertIn(str(self.rows[1].id), shutdown_record.read())
                else:
                    self.assertNotIn(str(self.rows[1].id), shutdown_record.read())
                self.assertNotIn(str(failed.id), shutdown_record.read())
                if command[0] == "stop":
                    self.assertEqual(output.getvalue().count(f"{failed.name}: fixture liveness read failed"), 1)
                    self.assertEqual(errors.getvalue(), "")
                else:
                    self.assertEqual(errors.getvalue().count("partial liveness read"), 1)

    def test_refused_row_lock_saves_no_live_row_for_both_unscoped_commands(self) -> None:
        operation = staff.row_operation
        refused = str(self.rows[0].id)

        @contextmanager
        def lock(reference: str) -> Generator[None]:
            if reference == refused:
                raise staff.StaffRefused("fixture row lock refused")
            with operation(reference):
                yield

        for command in (["stop", "--all"], ["held-sessions"]):
            with self.subTest(command=command):
                self.running = {str(row.current_session) for row in self.rows}
                shutdown_record.save([])
                with patch.object(staff, "row_operation", side_effect=lock):
                    self.assertEqual(shutdown.main(command), 0)
                self.assertNotIn(refused, shutdown_record.read())
                self.assertIn(str(self.rows[0].current_session), self.running)
                expected = [str(row.id) for row in self.rows[1:]] if command[0] == "stop" else []
                self.assertCountEqual(shutdown_record.read(), expected)

    def test_restart_cleanup_precedes_a_stop_after_the_row_lock_releases(self) -> None:
        row = self.rows[0]
        self.rows = (row,)
        shutdown_record.save([str(row.id)])
        operation = staff.row_operation
        stopped_again: list[str] = []

        @contextmanager
        def lock(reference: str) -> Generator[None]:
            with operation(reference):
                yield
            if not stopped_again:
                stopped_again.append(reference)
                with operation(reference):
                    shutdown_record.update(started=[], pending=[reference])

        with patch.object(staff, "row_operation", side_effect=lock):
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(stopped_again, [str(row.id)])
        self.assertEqual(shutdown_record.read(), [str(row.id)])

    def test_scoped_stop_all_keeps_other_saved_rows(self) -> None:
        self.account_processes()
        for arguments in (["--only", str(self.rows[0].id)], ["--account", "claude3"]):
            with self.subTest(arguments=arguments):
                shutdown_record.save(["earlier", str(self.rows[2].id)])
                self.running = {str(row.current_session) for row in self.rows}
                self.stops.clear()
                self.assertEqual(shutdown.main(["stop", "--all", *arguments]), 0)
                self.assertEqual(shutdown_record.read(), ["earlier", str(self.rows[2].id), str(self.rows[0].id)])
                self.assertEqual(self.stops, [str(self.rows[0].id)])
                self.assertEqual(self.launches, [])

    def test_stop_all_with_no_live_rows_keeps_the_down_record(self) -> None:
        shutdown_record.save(["earlier"])
        self.running.clear()
        self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        self.assertEqual(shutdown_record.read(), ["earlier"])
        self.assertEqual((self.stops, self.launches), ([], []))

    def test_stop_all_preserves_down_rows_before_a_scoped_detached_stop(self) -> None:
        shutdown_record.save(["earlier"])
        selected = [str(row.id) for row in self.rows]
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": str(self.rows[0].current_session)}),
              patch.object(stop_resume, "detach", side_effect=self.claim_detached_log) as detached):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        arguments = cast(list[str], detached.call_args.args[0])
        self.assertEqual(arguments[:-1], ["stop", "--all", "--only", ",".join(selected), "--parent-report"])
        self.assertEqual(json.loads(arguments[-1]), {
            "retry": "agents stop --all", "targets": {row.id: row.name for row in self.rows},
            "units": {row.id: row.name for row in self.rows}, "issues": {}})
        self.assertEqual(shutdown_record.read(), ["earlier"])
        self.assertEqual((self.stops, self.launches), ([], []))

    def test_stop_all_keeps_a_producer_for_its_saved_tab(self) -> None:
        row = fixture_member("producer", name="TEST-agent")
        self.rows = (row,)
        self.running = {str(row.current_session)}
        with (patch.object(staff, "initial_snapshot", side_effect=staff.snapshot),
              patch.dict(os.environ, {"USER_ZONE": "UTC"})):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
            self.assertEqual(shutdown_record.read(), [str(row.id)])
            self.assertEqual(self.launches, [])
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.stops, [str(row.id)])
        self.assertEqual(self.launches, [(row.name, herdr.NewTab("producers", row.name))])
        self.assertEqual(shutdown_record.read(), [])

    def test_reboot_refuses_a_live_codex_pane_with_an_unknown_thread(self) -> None:
        from scripts.agents import reboot

        pane = {"pane_id": "w1:p9", "tab_id": "w1:t9", "label": "scratch", "agent": "codex"}
        planned = reboot.MoveResult("session_move: would move 0 folders (0.00 GiB), kept 0 live, refused 0 other", 0, ())

        def results(*arguments: str) -> dict[str, object]:
            if arguments == ("pane", "list"):
                return {"panes": [*self.panes(), pane]}
            if arguments == ("pane", "process-info", "--pane", "w1:p9"):
                return {"process_info": {"foreground_processes": [{"pid": 990109, "argv": ["codex"]}]}}
            return self.layout_result(*arguments)

        def native(pid: int, _kind: str, _reported: str = "") -> herdr.NativeAgentProcess:
            if pid == 990109:
                raise herdr_types.HerdrFailure("Codex thread is unknown")
            return herdr.NativeAgentProcess(herdr.EnvironmentThread(
                str(self.rows[pid - 990100].current_session)), "fixture-start", {})

        with (patch.object(herdr.Herdr, "result", side_effect=results),
              patch.object(herdr.Herdr, "record_layout"),
              patch.object(herdr, "native_process", side_effect=native),
              patch.object(reboot, "stop_server") as server,
              patch.object(reboot, "move_folders", return_value=planned),
              patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root), "HERDR_PANE_ID": "", "HERDR_ENV": ""}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["reboot"]), 1)
            reboot.Checkpoint(reboot.Step.SERVER).save(self.root / "agents/reboot/reboot.json")
            self.assertEqual(shutdown.main(["reboot"]), 1)
        issue = ("scratch (w1:p9): Could not identify the session in this pane. Codex thread is unknown. "
                 + "Nate checks which session uses this pane and runs agents reboot again.")
        self.assertEqual(output.getvalue().splitlines().count(issue), 2)
        server.assert_not_called()
        self.assertEqual(self.stops, [])
        self.assertEqual(shutdown_record.read(), [])

    def test_reboot_retry_keeps_previous_restart_entries_when_a_stop_fails(self) -> None:
        from scripts.agents import reboot

        row, already_stopped, _other = self.rows
        self.running = {str(row.current_session)}
        earlier = [str(already_stopped.id), str(row.id)]
        shutdown_record.save(earlier)
        reboot.Checkpoint(reboot.Step.SERVER).save(self.root / "agents/reboot/reboot.json")
        planned = reboot.MoveResult("session_move: would move 0 folders (0.00 GiB), kept 0 live, refused 0 other", 0, ())
        with (patch.object(herdr.Herdr, "record_layout"),
              patch.object(restart, "stop_row", return_value=False) as stop,
              patch.object(reboot, "stop_server") as server,
              patch.object(reboot, "move_folders", return_value=planned),
              patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root), "HERDR_PANE_ID": "", "HERDR_ENV": ""}),
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["reboot"]), 1)
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[0], row)
        server.assert_not_called()
        self.assertEqual(shutdown_record.read(), earlier)

    def test_reboot_reuses_bulk_stop_and_restart_for_a_producer(self) -> None:
        from scripts.agents import reboot

        row = fixture_member("producer", name="TEST-agent")
        self.rows = (row,)
        self.running = {str(row.current_session)}
        planned = reboot.MoveResult("session_move: would move 0 folders (0.00 GiB), kept 1 live, refused 0 other", 0, ())
        moved = reboot.MoveResult("session_move: moved 0 folders (0.00 GiB), kept 0 live, refused 0 other", 0, ())

        def producer_snapshot() -> staff.StaffSnapshot:
            return staff.StaffSnapshot(tuple(staff.snapshot().find(kind="producer")))

        with (patch.object(staff, "initial_snapshot", side_effect=producer_snapshot),
              patch.object(herdr.Herdr, "record_layout",
                           side_effect=(herdr_types.HerdrFailure("layout unavailable"), None)) as saved_layout,
              patch.object(reboot, "stop_server") as server,
              patch.object(reboot, "move_folders", side_effect=(planned, planned, moved)),
              patch.dict(os.environ, {"USER_ZONE": "UTC", "XDG_CONFIG_HOME": str(self.root),
                                      "HERDR_PANE_ID": "", "HERDR_ENV": ""}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["reboot"]), 1)
            self.assertIn("Could not save the Herdr layout", output.getvalue())
            self.assertEqual(self.stops, [])
            server.assert_not_called()
            self.assertEqual(shutdown.main(["reboot"]), 0)
        self.assertEqual(saved_layout.call_count, 2)
        server.assert_called_once()
        self.assertEqual(self.stops, [row.id])
        self.assertEqual(self.launches, [(row.name, herdr.NewTab("producers", row.name))])
        self.assertEqual(shutdown_record.read(), [])
        self.assertIn(f"1 of 1 stopped - {row.name}", output.getvalue())
        self.assertIn(f"1 of 1 back - {row.name}", output.getvalue())

    def test_stop_all_reports_a_failed_hold_write_and_keeps_stopping_other_rows(self) -> None:
        blocked = fixture_member("producer", name="TEST-agent")
        ready = fixture_member("producer", name="owner")
        self.rows = (blocked, ready)
        self.running = {str(row.current_session) for row in self.rows}
        save_hold = staff.exit

        def write_hold(reference: str, why: str) -> staff.StaffRow:
            if reference == blocked.id:
                raise ValueError("fixture hold write failed")
            return save_hold(reference, why)

        with (patch.object(staff, "initial_snapshot", side_effect=staff.snapshot),
              patch.object(staff, "exit", side_effect=write_hold),
              patch.dict(os.environ, {"USER_ZONE": "UTC"}), redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["stop", "--all"]), 0)
        self.assertEqual(self.stops, [ready.id])
        self.assertEqual(self.running, {str(blocked.current_session)})
        self.assertEqual(shutdown_record.read(), [ready.id])
        self.assertIn(f"{blocked.name}: Could not save the exit hold. fixture hold write failed", output.getvalue())
        current = staff.snapshot().rows_by_id
        self.assertIsInstance(current[blocked.id].exited, staff.Active)
        self.assertIsInstance(current[ready.id].exited, staff.Exited)

    def test_stop_all_dry_run_saves_nothing_and_names_each_row_once(self) -> None:
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["stop", "--all", "--dry-run"]), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(Path(lines[0]).read_text(), output.getvalue())
        self.assertEqual(lines[-1], "Nothing for you to do.")
        self.assertEqual(lines[1], "agents stop --all: 0 of 3 sessions stopped (dry run).")
        self.assertEqual(lines[2:-1], [
            f"{row.name}: {row.id}, {row.kind}, workspace live, tab {n + 1} ({row.name}); would stop and save"
            for n, row in enumerate(self.rows)])
        self.assertFalse(shutdown_record.record_path().exists())
        self.assertEqual((self.stops, self.launches), ([], []))

    def test_live_rows_proves_a_codex_resume_without_an_open_rollout_from_its_pane(self) -> None:
        row = replace(self.rows[0], kind="producer", unit_type=staff.NotUnit(), name="TEST-brain", worktree=staff.WORKTREE_UNFILLED,
                      current_session=staff.StaffId(str(uuid5(NAMESPACE_URL, "codex-resume-without-rollout"))))
        self.rows = (row,)
        self.running.clear()
        pane = {"pane_id": "w1:p0", "workspace_id": "w1", "tab_id": "w1:t0",
                "terminal_id": "terminal-0", "agent": "codex"}
        result = self.layout_result

        def layout(*arguments: str) -> dict[str, object]:
            if arguments == ("pane", "list"):
                return {"panes": [pane]}
            if arguments[:2] == ("pane", "process-info"):
                return {"process_info": {"shell_pid": 990090, "foreground_process_group_id": 990100,
                    "foreground_processes": [{"pid": 990100, "argv": ["codex", "resume", str(row.current_session)]}]}}
            return result(*arguments)

        def run(*arguments: str) -> str:
            return json.dumps({"result": layout(*arguments)})

        def codex_inventory() -> herdr.CodexConversations:
            return herdr.CodexConversations({}, ("TEST-brain (Codex thread unknown)",))

        def native_process(*arguments: object) -> herdr.NativeAgentProcess:
            del arguments
            return herdr.NativeAgentProcess(herdr.LaunchArgument(str(row.current_session)), "fixture-start", {})

        for inventory in (False, True):
            with self.subTest(inventory=inventory):
                proc = {"HERDR_PROC": os.environ["HERDR_PROC"]}
                fake_unit_host.process(proc, 990100, 1, ["codex", "resume", str(row.current_session)], {})
                with (patch.object(herdr, "codex_conversations", CODEX_CONVERSATIONS if inventory else codex_inventory),
                      patch.object(herdr.Herdr, "result", HERDR_RESULT),
                      patch.object(herdr.Herdr, "run", side_effect=run) as reads,
                      patch.object(herdr.Herdr, "socket_request", side_effect=OSError("private socket unavailable")),
                      patch.object(herdr, "native_process", NATIVE_PROCESS if inventory else native_process),
                      patch.object(herdr, "herdr_places", return_value={990100: herdr.HerdrPlace("agents", "w1:p0")}),
                      patch.object(herdr, "codex_resume_thread", return_value=str(row.current_session)),
                      patch.object(herdr, "_process_birth", return_value="fixture-start"),
                      patch.object(herdr, "process_environment", return_value={}),
                      patch.object(codex_daemon, "read_daemons", return_value=[]),
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(restart.live_rows(staff.StaffSnapshot(self.rows), herdr.Herdr("agents")).rows, [row])
                self.assertEqual(errors.getvalue(), "")
                if inventory:
                    self.assertEqual(sum(call.args == ("pane", "process-info", "--pane", "w1:p0")
                                         for call in reads.call_args_list), 1)

    def test_restart_all_starts_each_row_in_its_saved_tab(self) -> None:
        parent = staff.StaffRow(staff.StaffId("saved-parent"), "showrunner", "saved", staff.STAFF_ROOT,
                                current_session=staff.StaffId("saved-parent-session"))
        self.rows = tuple(replace(row, parent=staff.ReportsTo(parent.id)) for row in self.rows)
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((*self.rows, parent))))
        with (patch.object(herdr.Herdr, "save_layout") as save,
              patch.object(herdr.Herdr, "remember_row_tab") as remember):
            self.assertEqual(restart.restart(stop_all=True), 0)
        save.assert_called_once()
        self.assertEqual([call.args for call in remember.call_args_list[:3]],
                         [(str(row.id), f"w1:p{n}") for n, row in enumerate(self.rows)])
        self.assertCountEqual(self.stops, [str(row.id) for row in self.rows])
        self.assertCountEqual(self.launches, [(row.name, herdr.NewTab("saved", row.name)) for row in self.rows])
        self.assertEqual(self.running, {str(row.current_session) for row in self.rows})

    def test_inventory_keeps_sessions_with_unknown_staff_folders(self) -> None:
        for session in (unit("unknown"), worker_session("unknown-worker", owner=None)):
            for record in (staff.StaffSnapshot(()), staff.StaffSnapshot((
                    replace(self.rows[0], current_session=staff.StaffId(session["session_id"]),
                            worktree=staff.WORKTREE_UNFILLED),))):
                with self.subTest(kind=session["kind"], rows=len(record.rows)):
                    with patch.object(staff, "snapshot", return_value=record):
                        parsed = inventory.parse_session(session, "fixture")
                    self.assertEqual(parsed["session_id"], session["session_id"])
                    self.assertEqual(parsed["cwd"], {"kind": "unknown folder"})
                    self.assertEqual(parsed["checkout"], {"kind": "not a checkout"})

    def test_inventory_uses_the_current_staff_folder_without_rewriting_the_record(self) -> None:
        session = unit(str(self.rows[0].current_session))
        saved = json.dumps(session)
        for folder in (self.root / "before", self.root / "after"):
            record = staff.StaffSnapshot((replace(self.rows[0],
                worktree=staff.PresentWorktree(remote.local_machine(), folder)),))
            with patch.object(staff, "snapshot", return_value=record):
                parsed = inventory.parse_session(session, "fixture")
            self.assertEqual(parsed["cwd"], str(folder))
            self.assertEqual(json.dumps(session), saved)

    def test_restart_skips_an_unknown_unit_folder_and_starts_the_other_rows(self) -> None:
        parent = staff.StaffRow(staff.StaffId("parent"), "showrunner", "parent", staff.StaffRoot(),
                               current_session=staff.StaffId("parent-session"),
                               doc=staff.LiveDoc(remote.local_machine(), self.root / "production.md"))
        self.rows = (replace(self.rows[0], parent=staff.ReportsTo(parent.id), worktree=staff.WORKTREE_UNFILLED), *self.rows[1:])
        record = staff.StaffSnapshot((*self.rows, parent))
        self.running.clear()
        shutdown_record.save([str(row.id) for row in self.rows])
        with (patch.object(staff, "initial_snapshot", return_value=record),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertCountEqual([name for name, _ in self.launches], [row.name for row in self.rows[1:]])
        self.assertEqual(errors.getvalue(), "")
        self.assertIn("unit-0: down: unit-0: the staff worktree is unavailable", output.getvalue().splitlines())
        self.assertEqual(shutdown_record.read(), [str(self.rows[0].id)])

    def test_socketless_running_sessions_restart_or_print_and_unassigned_sessions_print_once(self) -> None:
        for row in self.rows:
            assert isinstance(row.home, staff.SessionHome)
            (row.home.path / "projects/fixture" / f"{row.current_session}.jsonl").unlink()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                records = self.records_for(self.running | {"orphan"})
                records = [record for record in records if record["sessionId"] != self.rows[0].current_session]
                orphan = next(record for record in records if record["sessionId"] == "orphan")
                orphan.update(name="unassigned", cwd="/tmp/orphan")
                def codex_conversations(*, wanted: str = "") -> herdr.CodexConversations:
                    _ = wanted
                    identities: dict[str, tuple[int, ...]] = {"orphan": (456,), "codex-orphan": (789,)}
                    if self.rows[0].current_session in self.running:
                        identities[str(self.rows[0].current_session)] = (123,)
                    return herdr.CodexConversations(identities, ())
                with (patch.object(session_records, "read_registry", return_value=self.registry_for(records)),
                      patch.object(herdr, "codex_conversations", side_effect=codex_conversations),
                      patch.object(herdr, "running_codex_threads",
                                   return_value={str(self.rows[0].current_session), "orphan", "codex-orphan"}) as codex,
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(sessions.live_sessions(), [])
                    self.assertEqual(restart.restart(stop_all=True, dry_run=dry_run), 0)
                self.assertEqual(errors.getvalue().splitlines(), [
                    "codex-orphan: restart anomaly: unassigned live session",
                    "unassigned in /tmp/orphan: restart anomaly: unassigned live session"])
                codex.assert_called_once()
                expected = [f"{row.name}: {row.id}, {row.kind}, workspace live, tab {index} ({row.name})"
                            if dry_run else f"{row.name}: restarted" for index, row in enumerate(self.rows, 1)]
                if dry_run:
                    self.assertEqual(output.getvalue().splitlines(), expected)
                else:
                    expected.extend(f"{row.name}: restart check: transcript unreadable" for row in self.rows)
                    expected.append(f"restart check: 0 of {len(self.rows)} directors resumed")
                    self.assertCountEqual(output.getvalue().splitlines(), expected)
                self.assertCountEqual(self.stops, [] if dry_run else [str(row.id) for row in self.rows])
                self.assertCountEqual([name for name, _place in self.launches], [] if dry_run else [row.name for row in self.rows])

    def test_restart_all_rerun_starts_only_the_rows_that_failed(self) -> None:
        parent = staff.StaffRow(staff.StaffId("saved-parent"), "showrunner", "saved", staff.STAFF_ROOT,
                                current_session=staff.StaffId("saved-parent-session"))
        self.rows = tuple(replace(row, parent=staff.ReportsTo(parent.id)) for row in self.rows)
        _ = self.enterContext(patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((*self.rows, parent))))
        launch = self.launch
        def fail_one(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                     prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                     opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "", reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT, client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> herdr.AgentLaunched:
            _ = client
            if agent == self.rows[1].name:
                raise OSError("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent, reported_unknown=reported_unknown, codex=codex)
        with patch.object(herdr, "launch_prepared_agent", side_effect=fail_one):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.launches.clear()
        self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.launches, [(self.rows[1].name, herdr.NewTab("saved", self.rows[1].name))])
        self.launches.clear()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(self.launches, [])
        self.assertEqual(output.getvalue(), "")

    def test_restart_all_record_keeps_only_rows_that_failed_to_start(self) -> None:
        self.running.remove(str(self.rows[2].current_session))
        launch = self.launch

        def fail_one(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                     prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                     opening: herdr.NoOpeningPrompt, timeout: int, *, agent: str = "", reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT, client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> herdr.AgentLaunched:
            _ = client
            if agent == self.rows[1].name:
                raise OSError("fixture launch failed")
            return launch(name, destination, prepared, kind, arguments, opening, timeout, agent=agent, reported_unknown=reported_unknown, codex=codex)

        with patch.object(herdr, "launch_prepared_agent", side_effect=fail_one):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(shutdown_record.read(), [str(self.rows[1].id)])
        self.assertCountEqual(self.stops, [str(row.id) for row in self.rows[:2]])
        self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertEqual(self.running, {str(row.current_session) for row in self.rows[:2]})

    def test_restart_all_writes_only_live_row_ids(self) -> None:
        self.running.remove(str(self.rows[1].current_session))
        self.assertEqual(restart.restart(stop_all=True, dry_run=True), 0)
        self.assertFalse(shutdown_record.record_path().exists())
        self.assertEqual(self.stops, [])
        self.assertEqual(self.launches, [])
        with patch.object(restart, "start_row", side_effect=OSError("fixture launch failed")):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(json.loads(shutdown_record.record_path().read_text()), [self.rows[0].id, self.rows[2].id])

    def test_stop_row_reports_one_reason_when_the_target_terminal_is_unknown(self) -> None:
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": str(self.rows[0].current_session)}
        with (patch.object(herdr.Herdr, "panes", return_value=[pane]),
              patch.object(stop_resume, "stop") as stopped, redirect_stderr(io.StringIO()) as errors):
            self.assertFalse(restart.stop_row(self.rows[0], herdr.Herdr("agents")))
        stopped.assert_not_called()
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("terminal is unknown", errors.getvalue())

    def test_restart_all_skips_a_stop_when_the_pane_runs_another_conversation(self) -> None:
        with (patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.EnvironmentThread("another-conversation"), "fixture-start", {})),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(stop_all=True), 0)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.launches, [])
        self.assertIn("would signal a process that does not run session", errors.getvalue())

        foreign = replace(self.rows[1], current_session=staff.StaffId("another-conversation"))
        record = staff.StaffSnapshot((self.rows[0], foreign, *self.rows[2:]))
        with (self.subTest(owner="staffed foreign process with a stale report"),
              patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.EnvironmentThread(foreign.current_session), "fixture-start", {})),
              patch.object(herdr, "check_target", return_value=True),
              patch.object(pane_record, "end") as ended,
              patch.object(stop_resume, "stop") as stopped,
              redirect_stderr(io.StringIO())):
            self.assertFalse(restart.stop_row(self.rows[0], herdr.Herdr("agents")))
        stopped.assert_not_called()
        ended.assert_not_called()

    def retained_director(self, session: str = "agents") -> pane_launch.LaunchPublication:
        row = self.rows[0]
        entry: pane_record.PaneLaunch = {"kind": "claude", "pane_id": "w1:p0", "session_id": str(row.id),
            "config_dir": str(self.root / ".claude2"), "cwd": str(self.root), "name": row.name,
            "model": "", "remote_control": False, "environment": {}, "flags_read": True, "written_at": "fixture"}
        return pane_launch.LaunchPublication(pane_record.record_path(session), entry)

    def test_stopped_director_reuses_its_retained_shell_without_a_saved_binding(self) -> None:
        row = self.rows[0]
        self.running.clear()
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": None,
                                     "terminal_id": "original-terminal"}
        with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director()),
              patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.NewTab("saved", row.name)),
              patch.object(herdr.Herdr, "panes", return_value=[pane]),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              patch.object(herdr.Herdr, "pane_activity", return_value=herdr.PaneAtShell()),
              patch.object(herdr, "open_tab") as opened,
              patch.object(herdr.Herdr, "close_tab_confirmed") as closed):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p0")
        self.assertEqual(launch.call_args.args[1], herdr.ExistingPane("w1:p0", "original-terminal"))
        opened.assert_not_called()
        closed.assert_not_called()

        with self.subTest(source="agent exits after the scan"):
            reads = 0

            def process_info(*arguments: str) -> dict[str, object]:
                nonlocal reads
                self.assertEqual(arguments, ("pane", "process-info", "--pane", "w1:p0"))
                reads += 1
                pid = 990100 if reads == 1 else 990090
                return {"process_info": {"shell_pid": 990090, "foreground_process_group_id": pid,
                    "foreground_processes": [{"pid": pid, "name": "claude" if reads == 1 else "zsh",
                                               "argv": ["claude"] if reads == 1 else ["zsh"]}]}}

            with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director()),
                  patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.NewTab("saved", row.name)),
                  patch.object(herdr.Herdr, "panes", return_value=[pane]),
                  patch.object(herdr.Herdr, "result", side_effect=process_info),
                  patch.object(herdr, "native_process", side_effect=herdr.NativeRegistryUnavailable(
                      "pid 990100: native registry identity is unavailable")),
                  patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                      "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
                  patch.object(herdr.Herdr, "pane_activity", return_value=herdr.PaneAtShell()),
                  patch.object(herdr, "open_tab") as opened,
                  patch.object(herdr.Herdr, "close_tab_confirmed") as closed):
                self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p0")
            self.assertEqual(launch.call_args.args[1], herdr.ExistingPane("w1:p0", "original-terminal"))
            opened.assert_not_called()
            closed.assert_not_called()

        for source, shared_checks in (
                ("exits within start", True), ("exits during check", True), ("outlives check", True),
                ("foreign", True), ("live", True), ("exits during check", False), ("outlives check", False),
                ("missing publication", True)):
            with self.subTest(source=source, shared_checks=shared_checks):
                self.running.clear()
                now = [100.0]
                start_seconds = READ_CONFIG("shutdown").number("START_SECONDS")
                exit_at = (100.3 if source == "exits within start" else
                           100.0 + start_seconds + 1 if source in ("exits during check", "missing publication") else math.inf)
                locations = {"w1:p0": pane.copy()}
                source_session = "legacy" if source == "missing publication" else "agents"
                destination_locations: dict[str, herdr.PaneIdentity] = {}
                closed: list[str] = []
                destination_closes: list[tuple[str, str]] = []
                destinations: list[herdr.NewTab | herdr.SplitPane | herdr.ExistingPane] = []
                original = pane_record.launch("w1:p0", str(row.current_session), str(self.root),
                    {"CLAUDE_CONFIG_DIR": str(self.root / ".claude2")}, ["claude", "--name", row.name])
                pane_record.save("agents", {})
                pane_record.save(source_session, {"w1:p0": original})
                retained_entries = pane_record.read(source_session)
                checks = restart.DirectorResumeChecks()

                def advance(seconds: float) -> None:
                    now[0] += seconds

                def panes(client: herdr.Herdr) -> list[herdr.PaneIdentity]:
                    if source == "missing publication" and client.session == "agents":
                        return list(destination_locations.values())
                    return list(locations.values())

                def process_info(*arguments: str) -> dict[str, object]:
                    selected = arguments[-1]
                    shell = selected == "w1:p0" and now[0] >= exit_at
                    pid = 990090 if shell else 990100 if selected == "w1:p0" else 990101
                    return {"process_info": {"shell_pid": 990090, "foreground_process_group_id": pid,
                        "foreground_processes": [{"pid": pid, "name": "zsh" if shell else "claude",
                                                   "argv": ["zsh"] if shell else ["claude"]}]}}

                def native(pid: int, kind: str) -> herdr.NativeAgentProcess:
                    self.assertEqual(kind, "claude")
                    if pid == 990100 and source not in ("foreign", "live"):
                        raise herdr.NativeRegistryUnavailable("pid 990100: native registry identity is unavailable")
                    identity = self.rows[1].current_session if pid == 990100 and source == "foreign" else row.current_session
                    return herdr.NativeAgentProcess(herdr.EnvironmentThread(identity), "fixture-start", {})

                def activity(selected: str) -> herdr.PaneAtShell | herdr.PaneRunning:
                    return (herdr.PaneAtShell() if selected == "w1:p0" and now[0] >= exit_at else
                            herdr.PaneRunning(("claude",)))

                def launched(session: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                             prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                             opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout: int, *,
                             agent: str, reported_unknown: str = "",
                             codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT,
                             client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> herdr.AgentLaunched:
                    del prepared, kind, opening, timeout, agent, reported_unknown, codex, client
                    destinations.append(destination)
                    selected = destination.pane if isinstance(destination, herdr.ExistingPane) else "w1:p9"
                    selected_locations = destination_locations if source == "missing publication" else locations
                    replacement_pane: herdr.PaneIdentity = {
                        "pane_id": selected, "workspace_id": "w1", "session_id": row.current_session,
                        "terminal_id": "original-terminal" if selected == "w1:p0" else "new-terminal"}
                    selected_locations[selected] = replacement_pane
                    if source == "missing publication":
                        locations[selected] = {"pane_id": selected, "workspace_id": "w1", "session_id": None,
                                               "terminal_id": "unrelated-terminal"}
                        return herdr.AgentLaunched(selected, herdr.PromptSubmissionNotRequired())
                    replacement = pane_record.launch(selected, str(row.current_session), str(self.root),
                        {"CLAUDE_CONFIG_DIR": str(self.root / ".claude2")}, ["claude", *arguments])
                    pane_record.write(session, replacement)
                    return herdr.AgentLaunched(selected, herdr.PromptSubmissionNotRequired())

                def close(client: herdr.Herdr, selected: str, *, target: str = "", terminal: str = "") -> bool:
                    del target
                    if source == "missing publication":
                        selected_locations = destination_locations if client.session == "agents" else locations
                        if selected in selected_locations:
                            destination_closes.append((client.session, selected))
                            del selected_locations[selected]
                        return True
                    self.assertEqual(selected, "w1:p0")
                    self.assertGreaterEqual(now[0], exit_at)
                    self.assertEqual(terminal, "original-terminal")
                    closed.append(selected)
                    del locations[selected]
                    return True

                observed = herdr.LiveConversation("claude", (990100,)) if source == "live" else herdr.StoppedConversation()
                with (patch.object(conf, "read", READ_CONFIG),
                      patch.object(time, "monotonic", lambda: now[0]),
                      patch.object(time, "sleep", advance),
                      patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.NewTab("saved", row.name)),
                      patch.object(herdr.Herdr, "panes", panes),
                      patch.object(herdr.Herdr, "result", side_effect=process_info),
                      patch.object(herdr.Herdr, "pane_activity", side_effect=activity),
                      patch.object(herdr.Herdr, "close_tab_confirmed", close),
                      patch.object(herdr, "native_process", side_effect=native),
                      patch.object(herdr, "read_conversation", return_value=observed),
                      patch.object(herdr, "launch_prepared_agent", side_effect=launched),
                      redirect_stderr(io.StringIO()) as errors):
                    if shared_checks:
                        selected = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"),
                                                     resume_checks=checks)
                    else:
                        selected = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
                    if source == "exits within start":
                        self.assertEqual(selected, "w1:p0")
                        self.assertEqual(destinations, [herdr.ExistingPane("w1:p0", "original-terminal")])
                        self.assertGreaterEqual(now[0], exit_at)
                        self.assertLess(now[0], 100.0 + start_seconds)
                    elif source == "live":
                        self.assertEqual(selected, "")
                        self.assertEqual(destinations, [])
                    else:
                        self.assertEqual(selected, "w1:p9")
                        self.assertEqual(destinations, [herdr.NewTab("saved", row.name)])
                        if source != "foreign":
                            self.assertGreaterEqual(now[0], 100.0 + start_seconds)
                    if shared_checks:
                        checks.report()
                if source == "missing publication":
                    self.assertEqual(destination_closes, [("agents", "w1:p9")])
                    self.assertEqual(set(locations), {"w1:p0", "w1:p9"})
                    self.assertEqual(destination_locations, {})
                    self.assertEqual(pane_record.read("legacy"), retained_entries)
                self.assertEqual(closed, ["w1:p0"] if source == "exits during check" else [])
                self.assertEqual("w1:p0" in locations, source != "exits during check")
                if source == "outlives check":
                    self.assertEqual([line for line in errors.getvalue().splitlines() if "source pane" in line],
                        [f"{row.name}: restart anomaly: source pane agents:w1:p0 still has work after the restart check"])

    def test_retained_shell_from_another_session_keeps_the_saved_destination(self) -> None:
        row = self.rows[0]
        self.running.clear()
        destination = herdr.NewTab("saved", row.name)
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": None,
                                     "terminal_id": "other-terminal"}
        with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director("other")),
              patch.object(herdr.Herdr, "saved_row_tab", return_value=destination),
              patch.object(herdr.Herdr, "panes", return_value=[pane]),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
              patch.object(herdr.Herdr, "pane_activity", return_value=herdr.PaneAtShell())):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
        self.assertEqual(launch.call_args.args[1], destination)

    def test_row_without_a_retained_launch_keeps_the_saved_destination(self) -> None:
        row = self.rows[0]
        self.running.clear()
        destination = herdr.NewTab("other", row.name)
        with (patch.object(pane_record, "publication_for_session", return_value=None),
              patch.object(herdr.Herdr, "saved_row_tab", return_value=destination)):
            _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
        self.assertEqual(self.launches, [(row.name, destination)])

    def test_retained_pane_requires_a_terminal_and_a_shell_for_reuse(self) -> None:
        row = self.rows[0]
        destination = herdr.NewTab("saved", row.name)
        for terminal, activity in (("original-terminal", herdr.PaneRunning(("vim",))), ("", herdr.PaneAtShell())):
            with self.subTest(terminal=terminal, activity=activity):
                self.running.clear()
                self.launches.clear()
                pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": None,
                                             "terminal_id": terminal}
                with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director()),
                      patch.object(herdr.Herdr, "saved_row_tab", return_value=destination),
                      patch.object(herdr.Herdr, "panes", return_value=[pane]),
                      patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                          "w1:p0", herdr.PromptSubmissionNotRequired())) as launch,
                      patch.object(herdr.Herdr, "pane_activity", return_value=activity),
                      patch.object(herdr.Herdr, "result", return_value={"process_info": {"shell_pid": 990090,
                          "foreground_process_group_id": 990091 if isinstance(activity, herdr.PaneRunning) else 990090,
                          "foreground_processes": [{"pid": 990091 if isinstance(activity, herdr.PaneRunning) else 990090,
                              "name": "vim" if isinstance(activity, herdr.PaneRunning) else "zsh",
                              "argv": ["vim"] if isinstance(activity, herdr.PaneRunning) else ["zsh"]}]}})):

                    _ = restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents"))
                self.assertEqual(launch.call_args.args[1], destination)

    def test_unlabelled_old_pane_with_a_program_stays_open_and_workers_restore(self) -> None:
        row = self.rows[0]
        self.running.clear()
        pane: herdr.PaneIdentity = {"pane_id": "w1:p0", "workspace_id": "w1", "session_id": None,
                                     "terminal_id": "old-terminal", "label": None}
        with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director()),
              patch.object(herdr.Herdr, "panes", return_value=[pane]),
              patch.object(herdr.Herdr, "pane_activity", return_value=herdr.PaneRunning(("vim",))) as activity,
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p9", herdr.PromptSubmissionNotRequired())),
              patch.object(pane_launch, "finish_restore") as finish,
              patch.object(herdr.Herdr, "close_tab_confirmed") as closed,
              patch.object(restart, "_restore_workers") as workers,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p9")
        self.assertIn(call("w1:p0"), activity.call_args_list)
        self.assertEqual(errors.getvalue().splitlines(), [
            "w1:p0: pane close anomaly: pane holds other work; kept open"])
        finish.assert_not_called()
        closed.assert_not_called()
        workers.assert_called_once_with(row, staff.StaffSnapshot(self.rows))

    def test_failed_restore_cleanup_reports_once_and_workers_restore(self) -> None:
        row = self.rows[0]
        self.running.clear()
        with (patch.object(pane_record, "publication_for_session", return_value=self.retained_director()),
              patch.object(herdr, "launch_prepared_agent", return_value=herdr.AgentLaunched(
                  "w1:p9", herdr.PromptSubmissionNotRequired())),
              patch.object(pane_launch, "finish_restore", side_effect=herdr_types.HerdrFailure("wrong_target", code="wrong_target")),
              patch.object(herdr.Herdr, "remember_row_tab") as remember,
              patch.object(restart, "_restore_workers") as workers,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.start_row(row, staff.StaffSnapshot(self.rows), herdr.Herdr("agents")), "w1:p9")
        self.assertEqual(errors.getvalue().splitlines(), [f"{row.name}: restart anomaly: wrong_target"])
        remember.assert_called_once_with(str(row.id), "w1:p9")
        workers.assert_called_once_with(row, staff.StaffSnapshot(self.rows))

    def test_restart_rerun_after_director_tab(self) -> None:
        from scripts.production import worker

        holder = self.rows[0]
        child = replace(self.rows[1], kind="worker", unit_type=staff.NotUnit(), parent=staff.ReportsTo(holder.id))
        record = staff.StaffSnapshot((holder, child))
        self.running.clear()
        shutdown_record.save([str(holder.id), str(child.id)])

        first = True

        def restore_attempt(_parent: staff.StaffId, *, report: worker.RestoreReport, **_options: object) -> worker.ExitCode:
            nonlocal first
            if first:
                first = False
                return worker.ExitCode.UNCERTAIN
            report.restored.add(child.id)
            return worker.ExitCode.OK

        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(worker, "restore", side_effect=restore_attempt) as restore):
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(shutdown_record.read(), [str(child.id)])
            self.assertEqual(restart.restart(), 0)
        self.assertEqual([item.args[0] for item in restore.call_args_list], [holder.id, holder.id])
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(shutdown_record.read(), [])

    def test_restart_rerun_after_first_worker(self) -> None:
        from scripts.production import worker

        holder = self.rows[0]
        children = tuple(replace(row, kind="worker", unit_type=staff.NotUnit(), parent=staff.ReportsTo(holder.id)) for row in self.rows[1:])
        record = staff.StaffSnapshot((holder, *children))
        resumed: set[staff.StaffId] = set()

        def restore(_parent: staff.StaffId, *, record: staff.StaffSnapshot,
                    runs: dict[str, list[str]],
                    report: worker.RestoreReport,
                    codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT,
                    **_options: object) -> worker.ExitCode:
            _ = record, runs
            self.assertIsInstance(codex, herdr.CodexConversations)
            if not resumed:
                resumed.add(children[0].id)
                raise OSError("interrupted after first worker")
            resumed.update(child.id for child in children)
            report.restored.update(resumed)
            return worker.ExitCode.OK

        shutdown_record.save([str(row.id) for row in record.rows])
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(worker, "restore", side_effect=restore)):
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(shutdown_record.read(), [str(child.id) for child in children])
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(restart.restart(only=(str(holder.id),)), 0)
        self.assertEqual(resumed, {child.id for child in children})
        self.assertEqual(self.launches, [])
        self.assertEqual(shutdown_record.read(), [])

        child = children[0]
        self.running.add(str(child.current_session))
        with (patch.object(staff, "initial_snapshot", return_value=record),
              patch.object(worker_panes, "observe") as observe):
            self.assertTrue(restart.stop_row(child, herdr.Herdr("fixture")))
        observe.assert_not_called()


class RetainedSplitRestartTests(RestartTestCase):
    def test_interrupted_restart_keeps_the_director_and_resumes_workers_in_their_panes(self) -> None:
        from scripts.direct import progress_history
        from scripts.herdr.test_worker_panes import PaneWorld
        from scripts.lib.test_models_registry import registry_text
        from scripts.production import worker
        from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_worker

        fixture = set_up_fixture(self.addCleanup, StaffTestCase())
        root = fixture.root
        config = root / "conf"
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            _ = write_config_fixture(config / source.name)
        write_text_atomic(config / "models.conf", registry_text())
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config),
            "MODELS_CONFIG_FILE": str(config / "models.conf"), "MODELS_CONFIG_PUBLISH": "0",
            "SHUTDOWN_STATE_DIR": str(root / "shutdown"), "XDG_STATE_HOME": str(root / "state"),
            "UNIT_RUN_DIR": str(root / "run"), "ROSTER_RUN_ROOT": str(root)})))
        showrunner = fixture_member("showrunner", name="showrunner")
        holder = fixture_member("unit", name="unit", parent=showrunner.id,
            worktree=staff.PresentWorktree("natedev", root))
        children = tuple(fixture_worker(name, holder.id, backend=staff.CodexBackend(root / "codex"))
                         for name in ("impl1", "impl2"))
        world = PaneWorld()
        for index, row in enumerate((holder, *children)):
            world.add(row, f"w1:p{index}", live=False)
        original = {pane: value["terminal_id"] for pane, value in world.locations.items()}
        entry: pane_record.PaneLaunch = {"kind": "claude", "pane_id": "w1:p0", "session_id": str(holder.id),
            "config_dir": os.environ["CLAUDE_CONFIG_DIR"], "cwd": str(root), "name": holder.name,
            "model": "", "remote_control": False, "environment": {}, "flags_read": True,
            "written_at": "2026-11-06T00:00:00+00:00"}
        pane_record.write("agents", entry)
        run = root / "run"
        write_text_atomic(run / "progress_history_state.json", json.dumps({
            "main_agent": {"session_id": str(holder.current_session)},
            "open_phases": [{"id": "earlier", "title": "Earlier", "instance_id": "earlier-phase",
                             "started_at": 1, "status": "active"}],
            "pass": {}, "history_file": str(run / "history.jsonl")}))
        prompt = run / "prompt.md"
        write_text_atomic(prompt, "Preserve the source behavior.\n")
        for row in children:
            _ = progress_history.begin_dispatch(run, slot=row.name, phase="earlier", role="impl",
                worker_name=row.name, worker_id=row.id, launcher_command=["bash", "assign.sh", "impl", str(prompt)],
                prompt=prompt, kind="impl", lens="")
        shutdown_record.save([str(row.id) for row in (holder, *children)])
        world.interrupt = "agent"

        def conversation(identity: str, kind: str = "", *,
                         codex: herdr.CodexConversations | None = None) -> herdr.LiveConversation | herdr.StoppedConversation:
            _ = kind, codex
            for pid, family, _arguments in world.foreground.values():
                if world.processes[pid].current_identity == identity:
                    return herdr.LiveConversation(cast(Literal["claude", "codex"], family), (pid,))
            return herdr.StoppedConversation()

        def prepare(identity: str, directory: str, environment: dict[str, str],
                    removals: frozenset[str]) -> herdr.PreparedAgentEnvironment:
            _ = identity, removals
            return herdr.PreparedAgentEnvironment(Path(directory), environment)

        def role_account(role: str) -> accounts.NamedAccount | accounts.CodexWorkersAccount:
            return (accounts.CodexWorkersAccount("codex1", root / "codex") if role == "workers" else
                    accounts.NamedAccount("claude2", Path(os.environ["CLAUDE_CONFIG_DIR"])))

        def launch(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                   prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                   opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout: int, *, agent: str,
                   reported_unknown: str = "", agent_name: str | herdr.AgentNameSource = herdr.AgentNameSource.MEMBER_REFERENCE,
                   codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT,
                   client: herdr.Herdr | herdr.LaunchClientSource = herdr.LaunchClientSource.NEW) -> herdr.AgentLaunched:
            _ = codex, client
            return world.launch(name, destination, prepared, kind, arguments, opening, timeout,
                                agent=agent, reported_unknown=reported_unknown, agent_name=agent_name)

        with (patch.object(herdr, "Herdr", return_value=world),
              patch.object(herdr, "read_conversation", side_effect=conversation),
              patch.object(herdr, "native_process", side_effect=world.native_process),
              patch.object(herdr, "_startup_processes", side_effect=world.descendants),
              patch.object(herdr, "launch_prepared_agent", side_effect=launch),
              patch.object(herdr, "prepare_session_environment", side_effect=prepare),
              patch.object(herdr, "saved_layout", return_value={"at": 1, "workspaces": []}),
              patch.object(herdr, "restore_server"),
              patch.object(world, "remember_row_tab"), patch.object(world, "sort_tabs"),
              patch.object(accounts, "role_account", side_effect=role_account),
              patch.object(accounts, "account_dir", return_value=Path(os.environ["CLAUDE_CONFIG_DIR"])),
              patch.object(restart.stop_resume, "heal_account_trust"),
              patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(len(shutdown_record.read()), 3)
            self.assertIn("interrupted after agent start", errors.getvalue())
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertEqual(world.locations["w1:p0"]["terminal_id"], original["w1:p0"])
        self.assertEqual(len(world.locations), len(original))
        self.assertEqual(world.launches[0], herdr.ExistingPane("w1:p0", "terminal-w1:p0"))
        self.assertEqual(len(world.launches), len(original))
        self.assertCountEqual(world.launches[1:], [herdr.ExistingPane(pane, cast(str, terminal))
                                                 for pane, terminal in original.items() if pane != "w1:p0"])
        self.assertEqual(world.closed, [])
        places = {world.processes[process[0]].current_identity: pane for pane, process in world.foreground.items()}
        for row in children:
            self.assertIn(places[row.current_session], original)
            self.assertEqual(world.locations[places[row.current_session]]["terminal_id"], original[places[row.current_session]])
            self.assertEqual(staff.snapshot().by_id(row.id), replace(row, role=staff.KnownWorkerRole("impl")))
        self.assertEqual({world.processes[pid].current_identity for pid, _family, _arguments in world.foreground.values()},
                         {row.current_session for row in (holder, *children)})
