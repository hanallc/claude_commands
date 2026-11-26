"""Check saved pane recovery and supply private pane fixtures."""

from __future__ import annotations

import io
import json
import math
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.agents.test_support import write_machine_config
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production.rename_test_support import FolderMoveFixture
from scripts.herdr import herdr_types
from scripts.production.cli_fixture import invoke_cli
from scripts.lib.state_home_test_support import isolate_state_home
from scripts.accounts import accounts
from scripts.whoami import agent_accounts
from scripts.agents import member_account, stop_resume
from scripts.agents.launch_names import LaunchNames
from scripts.production import staff, session_staff
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_models_registry import write_registry
from scripts.agents import pane_record as restart_records
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.message import sessions as runtime_sessions
from scripts.production import unit_move
from scripts.herdr import herdr
from scripts.agents import pane_record
from scripts.herdr import pane_launch
from scripts.message import sessions


def fixture_launch_transcript(root: Path, entry: pane_launch.PaneLaunch, record: staff.StaffSnapshot) -> None:
    """Give a stopped fixture session its private transcript source."""
    if "staff_id" in entry and "session_id" not in entry and record.by_id(staff.StaffId(entry["staff_id"])) is None:
        record = staff.local_snapshot()
    reference = pane_launch.launch_reference(entry, record)
    owner = record.by_id(reference.id) if isinstance(reference, pane_launch.StaffLaunchReference) else None
    identity = str(owner.current_session) if owner is not None else pane_launch.reference_id(entry)
    backend = record.backends.get(owner.id) if owner is not None else None
    kind = entry.get("kind", backend.family if backend is not None else "claude")
    home = (owner.home.path if owner is not None and isinstance(owner.home, staff.SessionHome) else
            Path(entry.get("config_dir") or root / (".codex2" if kind == "codex" else ".claude3")))
    if not home.is_relative_to(root):
        home = root / (".codex2" if kind == "codex" else ".claude3")
    for opposite in root.glob((".claude*" if kind == "codex" else ".codex*") + "/**/*" + identity + ".jsonl"):
        opposite.unlink()
    target = (home / "sessions" / f"rollout-{identity}.jsonl" if kind == "codex" else
              home / "projects/fixture" / f"{identity}.jsonl")
    process = root / "proc/123/cmdline"
    if process.exists() and Path(process.read_text().split("\0")[0]).name != kind:
        write_text_atomic(process, "\0".join([kind, "resume" if kind == "codex" else "--resume", identity]) + "\0")
    if not target.exists():
        write_text_atomic(target, json.dumps({"type": "session_meta", "payload": {"id": identity, "cwd": str(entry.get("cwd", root))}}) + "\n"
            if kind == "codex" else "")


def setUpModule() -> None:
    isolate_state_home()


@final
class FakeClient(herdr.Herdr):
    """Provide Herdr calls for a private pane layout."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        with mock.patch.object(herdr, "herdr_binary", return_value="fixture-herdr"):
            super().__init__("agents")
        self.rows = rows
        for row in self.rows:
            _ = row.setdefault("terminal_id", str(row["pane_id"]) + "-terminal")
        self.process_info: dict[str, object] = {"shell_pid": 998042, "foreground_process_group_id": 998042,
            "foreground_processes": [{"argv": ["zsh"], "name": "zsh", "pid": 998042}]}
        self.started: list[tuple[str, str, list[str]]] = []
        self.closed: list[str] = []
        self.start_error: herdr_types.HerdrFailure | None = None
        self.native_ids: dict[str, str] = {}
        self.prompts: list[tuple[str, herdr.OpeningPrompt]] = []
        self.opened: list[tuple[str, str, str, str, dict[str, str]]] = []
        self.publish_claude: bool = True

    @override
    def result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        del deadline
        if arguments[:2] == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "w1", "label": "demo"}]}
        if arguments[:2] == ("pane", "list"):
            return {"panes": [{"tab_id": str(row["pane_id"]) + ":tab", **row} for row in self.rows]}
        if arguments[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": row.get("tab_id", str(row["pane_id"]) + ":tab"),
                              "label": row["label"], "pane_count": 1} for row in self.rows]}
        if arguments[:2] == ("pane", "process-info"):
            return {"process_info": self.process_info}
        if arguments[:2] == ("agent", "list"):
            return {"agents": []}
        raise AssertionError(arguments)

    @contextmanager
    def empty_shell(self, pid: int, cwd: Path) -> Generator[None]:
        """Supply the private shell at both process observation boundaries."""
        from scripts.production.fake_unit_host import process

        process(os.environ, pid, 1, ["zsh"], {}, cwd=cwd)
        result = self.result
        program = herdr.run_program
        reply: dict[str, object] = {"process_info": {"shell_pid": pid,
            "foreground_process_group_id": pid,
            "foreground_processes": [{"pid": pid, "name": "zsh", "argv": ["zsh"]}]}}

        def observe(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] == ("pane", "process-info"):
                return reply
            return result(*arguments, deadline=deadline)

        def run(argv: list[str], timeout: float = herdr.PROGRAM_TIMEOUT_SECONDS, *,
                environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv[:5] == self.argv("pane", "process-info"):
                return subprocess.CompletedProcess(argv, 0, json.dumps({"result": reply}), "")
            return program(argv, timeout, environment=environment)

        with (mock.patch.object(self, "result", side_effect=observe),
              mock.patch.object(herdr, "run_program", side_effect=run),
              mock.patch.object(herdr, "_PLATFORM", "linux")):
            yield

    @override
    def pane_activity(self, pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
        _ = pane
        return herdr.PaneAtShell()

    @override
    def start_agent(self, name: str, pane: str, kind: str, agent_arguments: list[str],
                    opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int, *,
                    publication: herdr.LaunchPublicationTiming = herdr.LaunchPublicationTiming.ON_START,
                    ) -> herdr.PromptSubmissionNotRequired:
        _ = (kind, opening, timeout_ms, publication)
        if not any(row["pane_id"] == pane for row in self.rows):
            self.rows.append({"pane_id": pane, "label": name, "workspace_id": "w1",
                              "terminal_id": pane + "-terminal"})
        if self.start_error is not None:
            raise self.start_error
        self.started.append((name, pane, agent_arguments))
        if "--resume" in agent_arguments:
            self.native_ids[pane] = agent_arguments[agent_arguments.index("--resume") + 1]
            if self.publish_claude:
                entries = pane_record.read(self.session)
                owner = staff.initial_snapshot().by_current_session(self.native_ids[pane])
                reference = owner.id if owner is not None else self.native_ids[pane]
                original = pane_launch.current_launch(next(entry for entry in entries.values()
                    if pane_launch.reference_id(entry) == reference), staff.initial_snapshot())
                environment = {**original["environment"], "CLAUDE_CONFIG_DIR": str(cast(staff.SessionHome, pane_launch.config_home(original)).path)}
                replacement = pane_record.launch(pane, self.native_ids[pane], original["cwd"],
                    environment, ["claude", *agent_arguments])
                pane_record.write(self.session, replacement)
        return herdr.PromptSubmissionNotRequired()

    @override
    def run_in_pane(self, pane: str, command: list[str], *, terminal: str = "") -> None:
        _ = pane, command, terminal

    @override
    def native_session(self, pane: str, kind: str, timeout: float, *,
                       pane_row: Mapping[str, object] | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> str:
        del kind, timeout, pane_row
        if self.start_error is not None:
            raise self.start_error
        if pane not in self.native_ids:
            raise herdr_types.HerdrFailure(f"{pane}: fixture has no native process observation")
        return self.native_ids[pane]

    @override
    def submit_opening_prompt(self, pane: str, opening: herdr.OpeningPrompt, launched_at: datetime,
                              timeout_ms: int) -> herdr.PromptSubmitted:
        del timeout_ms
        self.prompts.append((pane, opening))
        return herdr.PromptSubmitted(launched_at)

    @override
    def _close_pane(self, pane: str) -> bool:
        self.close_tab_of(pane)
        return True

    @override
    def close_tab_of(self, pane: str) -> None:
        self.closed.append(pane)
        self.rows[:] = [row for row in self.rows if row["pane_id"] != pane]


@final
class PaneRecordRecoveryTests(unittest.TestCase):
    row = staff.StaffRow(staff.StaffId("restore-row"), "producer", "alpha", staff.StaffRoot(),
        current_session=staff.StaffId("019b3a40-4250-7000-8000-000000000102"),
        worktree=staff.PresentWorktree("natedev", Path("/work/alpha")))

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.config = Path()
        self.opened: list[tuple[str, str, str, str, dict[str, str]]] = []
        self.live: list[str] = []
        self.workspace = "producers"
        self.native_processes: dict[int, herdr.NativeAgentProcess] = {}

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "STAFF_FILE": str(self.root / "staff.json")})))
        self.config = self.root / ".claude3"
        self.row = replace(type(self).row, home=staff.SessionHome(self.config))
        _ = self.enterContext(mock.patch.object(staff, "local_snapshot", side_effect=lambda: staff.StaffSnapshot((self.row,))))
        def codex_home(label: str) -> agent_accounts.CodexHomeHoldingLogin:
            return agent_accounts.CodexHomeHoldingLogin(label, self.root / f".{label}", "private@example.test")
        _ = self.enterContext(mock.patch.object(agent_accounts, "codex_account_home", side_effect=codex_home))
        _ = self.enterContext(mock.patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"))
        select_role = accounts.role_account
        def role_account(role: str) -> accounts.AccountSelection:
            return (accounts.CodexWorkersAccount("codex1", self.root / ".codex1")
                    if role == "workers" else select_role(role))
        _ = self.enterContext(mock.patch.object(accounts, "role_account", side_effect=role_account))
        configuration = self.root / "configuration"
        configuration.mkdir()
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            _ = write_config_fixture(configuration / source.name)
        write_machine_config(configuration)
        _ = write_config_fixture(configuration / "staff.conf", "producer_workspace=producers\n")
        registry = write_registry(configuration / "models.conf", {"producers": {"alpha": "claude:opus:max"}})
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(registry)})))
        for pid in (123, 4242):
            self.native_processes[pid] = herdr.NativeAgentProcess(herdr.EnvironmentThread(self.row.current_session),
                "fixture-generation", {"HERDR_SESSION": "agents", "HERDR_PANE_ID": "w1:p2",
                                       "CLAUDE_CONFIG_DIR": str(self.config)})
        def native_process(pid: int, kind: str, reported: str | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> herdr.NativeAgentProcess:
            del kind, reported
            return self.native_processes[pid]
        _ = self.enterContext(mock.patch.object(herdr, "native_process", side_effect=native_process))

        record_home = staff.set_home
        def set_home(_identity: staff.StaffId, home: staff.SessionHome) -> staff.StaffRow:
            if _identity != self.row.id:
                return record_home(_identity, home)
            self.row = replace(self.row, home=home)
            return self.row
        _ = self.enterContext(mock.patch.object(staff, "set_home", side_effect=set_home))
        _ = self.enterContext(mock.patch.object(staff, "initial_snapshot", side_effect=lambda: staff.StaffSnapshot((self.row,))))
        _ = self.enterContext(mock.patch("scripts.agents.remote.local_machine", return_value="natedev"))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(configuration)})))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "saved_row_tab", return_value=herdr.ExistingPane("w1:p2", "w1:p2-terminal")))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "remember_row_tab"))
        from scripts.production.fake_unit_host import process
        process({"HERDR_PROC": str(self.root / "proc")}, 998042, 1, ["zsh"], {})
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"HERDR_PROC": str(self.root / "proc")})))
        _ = self.enterContext(mock.patch.object(herdr, "_PLATFORM", "linux"))
        (self.config / "projects/work").mkdir(parents=True)
        _ = (self.config / "projects/work/019b3a40-4250-7000-8000-000000000102.jsonl").write_text("{}\n")
        def no_envrc(directory: Path, environment: Mapping[str, str]) -> herdr.nix_env.NoEnvrc:
            _ = directory
            return herdr.nix_env.NoEnvrc(dict(environment))

        def session_liveness(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(123) if identity in self.live else sessions.SessionNotLive()

        for patcher in (mock.patch.object(herdr.nix_env, "ensure", side_effect=no_envrc),
                        mock.patch.object(pane_record, "RECORD_DIR", self.root / "herdr-panes"),
                        mock.patch.object(restart_records, "RECORD_DIR", self.root / "herdr-panes"),
                        mock.patch.object(pane_record, "PROC", self.root / "proc"),
                        mock.patch.dict(os.environ, {"HOME": str(self.root),
                            "HERDR_PANE_RECORD_DIR": str(self.root / "herdr-panes")}),
                        mock.patch.object(sessions, "session_liveness", side_effect=session_liveness),
                        mock.patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())),
                        mock.patch.object(sessions, "running_sessions", list),
                        mock.patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(())),
                        mock.patch.object(herdr, "open_tab", self.open_tab)):
            _ = cast(object, self.enterContext(patcher))

    def open_tab(self, name: str, space: str, title: str, cwd: str, environment: dict[str, str], *, member: str = "") -> str:
        del member
        self.opened.append((name, space, title, cwd, environment))
        return "w1:p3"

    def test_account_resume_checks_only_a_different_source_pane(self) -> None:
        identity = fixture_runtime_id("resume-retained-source")
        source = "w1:p1"
        home = self.root / ".claude4"
        names = LaunchNames(staff.StaffSnapshot(()))
        for same_pane, source_busy in ((True, True), (False, False), (False, True)):
            with self.subTest(same_pane=same_pane, source_busy=source_busy):
                destination = source if same_pane else "w1:p2"
                client = FakeClient([{"pane_id": pane, "label": "", "workspace_id": "w1"}
                                     for pane in dict.fromkeys((source, destination))])
                source_home = self.config if same_pane else home
                original = pane_record.launch(source, identity, str(self.root),
                    {"CLAUDE_CONFIG_DIR": str(source_home)}, ["claude", "--resume", identity])
                original["ended"] = same_pane
                pane_record.save("agents", {source: original})
                tab = herdr.TabSession(destination, "", "w1", identity)
                activity = herdr.PaneRunning(("claude",)) if source_busy else herdr.PaneAtShell()

                def start(selected: herdr.TabSession, entry: pane_launch.CurrentLaunch, label: str,
                          _message: str) -> sessions.LiveSessionProcess:
                    self.assertEqual((selected, label), (tab, "claude4"))
                    if not same_pane:
                        pane_record.save("agents", {source: original, destination: {**entry, "pane_id": destination}})
                    return sessions.LiveSessionProcess(123)

                with (mock.patch.dict(os.environ, {"HERDR_PANE_ID": ""}),
                      client.empty_shell(998042, self.root),
                      mock.patch.object(client, "pane_activity", return_value=activity) as read_activity,
                      mock.patch.object(stop_resume, "_tab_of", return_value=tab),
                      mock.patch.object(stop_resume, "_start_session", side_effect=start),
                      mock.patch.object(stop_resume, "_watch_resume"),
                      mock.patch.object(member_account, "resume_account",
                                        return_value=accounts.NamedAccount("claude4", home)),
                      redirect_stderr(io.StringIO()) as errors):
                    result = stop_resume._resume_once(identity, why="account move", to="claude4", names=names,  # pyright: ignore[reportPrivateUsage]
                                                     client=client)
                self.assertEqual(result, stop_resume.ClaudeUp(identity, destination, "claude4"))
                entries = pane_record.read("agents")
                self.assertFalse(entries[destination].get("ended", False))
                self.assertEqual(entries[destination].get("config_dir"), str(home))
                if same_pane:
                    read_activity.assert_not_called()
                    self.assertEqual(client.closed, [])
                    self.assertNotIn("pane close anomaly:", errors.getvalue())
                elif source_busy:
                    read_activity.assert_called_once_with(source)
                    self.assertEqual(client.closed, [])
                    self.assertEqual(entries[source], original)
                    self.assertIn(f"{source}: pane close anomaly: pane holds other work; kept open", errors.getvalue())
                else:
                    read_activity.assert_called_once_with(source)
                    self.assertEqual(client.closed, [source])
                    self.assertNotIn(source, entries)
                    self.assertNotIn("pane close anomaly:", errors.getvalue())

    def test_seed_writes_the_entry_of_each_running_claude(self) -> None:
        for staffed in (True, False):
            with self.subTest(staffed=staffed):
                (self.root / "proc/4242").mkdir(parents=True, exist_ok=True)
                _ = (self.root / "proc/4242/environ").write_bytes(
                    f"HERDR_SESSION=agents\0HERDR_PANE_ID=w1:p2\0CLAUDE_CONFIG_DIR={self.config}\0SHOWRUNNER_UNIT=demo\0CLAUDE_CODE_OAUTH_TOKEN=secret\0".encode())
                (self.config / "sessions").mkdir(exist_ok=True)
                _ = (self.config / "sessions/4242.json").write_text(json.dumps({"pid": 4242, "sessionId": "019b3a40-4250-7000-8000-000000000102"}))
                client = FakeClient([{"pane_id": "w1:p2", "label": "alpha", "workspace_id": "w1"}])
                client.process_info = {"shell_pid": 998042, "foreground_process_group_id": 4242,
                    "foreground_processes": [{"pid": 4242, "name": "claude",
                        "argv": ["claude", "-n", "alpha", "--model", "opus"], "cwd": "/work/alpha"}]}
                pane_record.save("agents", {})
                membership = staff.StaffSnapshot((self.row,)) if staffed else staff.StaffSnapshot(())
                with (mock.patch.object(staff, "initial_snapshot", return_value=membership),
                      mock.patch.object(staff, "local_snapshot", return_value=membership),
                      mock.patch.object(herdr, "Herdr", return_value=client),
                      mock.patch.object(sessions, "process_start", return_value="fixture-generation"),
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(pane_record.seed("agents"), 0)
                entry = pane_record.read("agents")["w1:p2"]
                self.assertEqual((pane_launch.reference_id(entry), str(cast(staff.SessionHome, pane_launch.config_home(entry)).path),
                                  pane_launch.current_launch(entry, membership)["name"], entry["model"],
                                  pane_launch.current_launch(entry, membership)["cwd"]),
                                 (str(self.row.id if staffed else self.row.current_session), str(self.config), "alpha", "opus", "/work/alpha"))
                self.assertEqual(entry["environment"], {})


@final
class PaneMoveRecoveryTests(FolderMoveFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        def codex_home(label: str) -> agent_accounts.CodexHomeHoldingLogin:
            return agent_accounts.CodexHomeHoldingLogin(label, self.root / "home" / f".{label}", "private@example.test")
        _ = self.enterContext(mock.patch.object(agent_accounts, "codex_account_home", side_effect=codex_home))
        _ = self.enterContext(mock.patch.object(agent_accounts, "codex_home_role_label", return_value="codex2"))
        select_role = accounts.role_account
        def role_account(role: str) -> accounts.AccountSelection:
            return (accounts.CodexWorkersAccount("codex2", self.root / "home/.codex2")
                    if role == "workers" else select_role(role))
        _ = self.enterContext(mock.patch.object(accounts, "role_account", side_effect=role_account))
        configuration = self.root / "configuration"
        configuration.mkdir()
        for source in (Path(__file__).resolve().parents[2] / "config").glob("*.conf"):
            _ = write_config_fixture(configuration / source.name)
        registry = write_registry(configuration / "models.conf")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(configuration), "MODELS_CONFIG_FILE": str(registry), "MODELS_CONFIG_PUBLISH": "0",
        })))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "remember_row_tab"))

    def finished_move_rerun(self) -> subprocess.CompletedProcess[str]:
        arguments = ["move-folder", "--unit", self.owner.id, "--to", str(self.new_folder)]
        return invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, dict(os.environ), self.checkout)

    def test_missing_pane_recovery_retires_its_source_before_the_next_folder_move(self) -> None:
        def recover(self: PaneMoveRecoveryTests, reused: bool) -> None:
            backend = self.folder_backend("claude", live=False)
            fixture_launch_transcript(self.root, {"session_id": self.owner.current_session,
                "pane_id": "w1:p1", "kind": "claude", "cwd": str(self.old_folder), "name": self.owner.name,
                "config_dir": str(backend.config), "model": "", "remote_control": False, "environment": {},
                "flags_read": True, "written_at": "fixture"}, staff.StaffSnapshot((self.owner,)))
            self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(backend.config)}))
            original = pane_record.read("agents")["w1:p1"]
            original["ended"] = False
            pane_record.write("agents", original)
            client = FakeClient([{"pane_id": "w1:p1", "workspace_id": "w1", "label": "foreign",
                "agent_session": {"value": "019b3a40-4250-7000-8000-000000000199"}}] if reused else [])
            if reused:
                from scripts.production.staff_test_support import fixture_member

                foreign = fixture_member("unit", name="foreign-pane-owner", parent=self.owner.parent)
                client.process_info = {"shell_pid": 998042, "foreground_process_group_id": 998043,
                    "foreground_processes": [{"pid": 998043, "name": "claude", "argv": ["claude"]}]}
                native = herdr.native_process

                def current_process(pid: int, kind: str,
                                    reported: str | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> herdr.NativeAgentProcess:
                    if pid == 998043:
                        return herdr.NativeAgentProcess(herdr.EnvironmentThread(foreign.current_session), "fixture-start", {})
                    return native(pid, kind, reported)
                _ = self.enterContext(mock.patch.object(herdr, "native_process", side_effect=current_process))

            def open_tab(_space: str, name: str, cwd: str, _environment: dict[str, str]) -> str:
                self.assertEqual(cwd, str(self.old_folder))
                client.rows.append({"pane_id": "w1:p3", "workspace_id": "w1", "label": name})
                return "w1:p3"

            def start(tab: herdr.TabSession, entry: pane_launch.CurrentLaunch,
                      _account: str, _message: str) -> runtime_sessions.LiveSessionProcess:
                replacement = pane_record.launch(tab.pane_id, self.owner.current_session, entry["cwd"],
                    {"CLAUDE_CONFIG_DIR": str(cast(staff.SessionHome, pane_launch.config_home(entry)).path)}, ["claude", "--resume", self.owner.current_session])
                pane_record.write("agents", replacement)
                return runtime_sessions.LiveSessionProcess(backend.pid)

            with (mock.patch.object(accounts, "group_accounts", return_value=accounts.GroupAccounts(units="claude4")),
                  mock.patch.object(accounts, "claude_account_dir", return_value=backend.config),
                  mock.patch.object(herdr, "live_sessions", return_value=[{"name": "agents"}]),
                  mock.patch.object(herdr, "Herdr", return_value=client),
                  mock.patch.object(client, "open_tab", side_effect=open_tab),
                  mock.patch.object(stop_resume, "_tab_of", return_value=stop_resume.SessionNotFound(self.owner.current_session)),
                  mock.patch.object(stop_resume, "_start_session", side_effect=start),
                  mock.patch.object(stop_resume, "_detach"),
                  mock.patch.object(sessions, "process_start", return_value="fixture-start"),
                  mock.patch.object(herdr, "heal_claude_trust", return_value=herdr.Trusted(
                      self.old_folder, backend.config / ".claude.json", self.old_folder))):
                result = stop_resume.resume(self.owner.id, why="fixture recovery")
            self.assertIsInstance(result, stop_resume.ClaudeUp)
            if reused:
                self.assertNotIn("w1:p1", client.closed)
            with (mock.patch.object(backend, "panes", return_value=[])):
                moved = self.finished_move_rerun()
            self.assertEqual(moved.returncode, 0, moved.stderr)
            owners = [entry for entry in pane_record.read("agents").values() if entry.get("staff_id") == self.owner.id]
            self.assertEqual([(entry["pane_id"], pane_record.resume_launch(entry)["cwd"]) for entry in owners], [("w1:p3", str(self.new_folder))])

        for reused in (False, True):
            with self.subTest(reused=reused):
                case = PaneMoveRecoveryTests("runTest")
                case.setUp()
                self.addCleanup(case.doCleanups)
                recover(case, reused)


if __name__ == "__main__":
    _ = unittest.main()
