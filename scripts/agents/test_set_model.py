"""Exercise set-model with fixture registries, hosts and panes."""

from __future__ import annotations

import io
import json
import math
import os
import tempfile
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast, override
from unittest.mock import patch

from scripts.lib.conf import TEST_CONFIG_DIRECTORY
from scripts.agents import remote, set_model, shutdown, worker_lifecycle
from scripts.herdr import herdr
from scripts.lib import models_registry
from scripts.message import codex_daemon
from scripts.production import session_staff, staff, unit_lookup

REGISTRY = """[producer_defaults]
model=claude:fable:high
[producers]
brain=claude:fable:high
morty=claude:opus:medium
[assignments]
production=claude
direct=codex
[production.claude]
director=opus:medium
showrunner=opus:medium
[direct.codex]
impl=gpt-6.1-sol:medium
test=gpt-6.1-sol:medium
[codex.agents]
gpt-6.1-sol=medium,high
gpt-6.2-sol=medium,high
gpt-6-astra=medium,high
gpt-6.2-astra=medium,high
[codex.families]
sol=gpt-6.1-sol
astra=gpt-6-astra
[claude.agents]
opus=medium,high
fable=medium,high
[claude.families]
opus=opus
fable=fable
"""
OPUS_ID = "claude-opus-5-5"
OPUS_COMMAND = f"/model {OPUS_ID}"
DAEMON = codex_daemon.CodexDaemon(Path("/fixture"), Path("/fixture/control.sock"))


def codex_settings_record(thread: str, model: str, effort: str) -> str:
    return json.dumps({
        "timestamp": "2026-11-19T20:30:00.000Z",
        "type": "event_msg",
        "payload": {
            "type": "thread_settings_applied",
            "thread_id": thread,
            "thread_settings": {"model": model, "reasoning_effort": effort},
        },
    }) + "\n"


@dataclass
class FakePane:
    family: str
    history: str
    draft: str = ""
    refuses: bool = False
    status: str = ""
    model: str = ""
    model_receipt: str = ""
    agent_status: herdr.AgentStatus = herdr.AgentStatus.IDLE
    marker: str = "›"
    codex_rollout: Path = Path()
    codex_records: tuple[str, ...] = ()
    final_status: str = ""
    conversation: str = ""
    next_conversation: str = ""

    def screen(self) -> str:
        if self.family == "codex":
            composer = f"  {self.marker} {self.draft}"
        else:
            composer = f"────────\n❯ {self.draft}\n────────"
        return f"{self.history}\n{composer}\n\n  {self.status}"

    def append_codex_records(self, *records: str) -> None:
        if not records:
            return
        self.codex_rollout.parent.mkdir(parents=True, exist_ok=True)
        with self.codex_rollout.open("a") as stream:
            stream.writelines(records)


class FakeHerdr(herdr.Herdr):
    fixtures: ClassVar[dict[str, FakePane]] = {}
    calls: ClassVar[list[tuple[str, ...]]] = []

    def __init__(self, session: str) -> None:
        assert session == "fixture"
        super().__init__(session)

    @override
    def pane_row(self, pane: str) -> herdr.PaneIdentity:
        assert pane in self.fixtures
        return {"pane_id": pane, "workspace_id": "fixture", "terminal_id": f"terminal-{pane}",
                "session_id": f"native-{pane}"}

    @override
    def result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        del deadline
        match arguments:
            case ["agent", "get", pane]:
                return {"agent": {"agent_status": self.fixtures[pane].agent_status, "interactive_ready": True}}
            case _:
                raise AssertionError(arguments)

    @override
    def run(self, *arguments: str, deadline: float = math.inf) -> str:
        del deadline
        match arguments:
            case ["pane", "read", pane, *_]:
                return self.fixtures[pane].screen()
            case ["agent", "prompt", pane, text]:
                self.submit(pane, text)
                return ""
            case _:
                raise AssertionError(arguments)

    def submit(self, pane: str, text: str) -> None:
        fixture = self.fixtures[pane]
        assert not fixture.draft
        self.calls.append((pane, f"native-{pane}", text))
        fixture.conversation = fixture.next_conversation
        if fixture.family == "claude":
            fixture.history += f"\n❯ {text}"
        else:
            fixture.append_codex_records(*fixture.codex_records)
            if not fixture.refuses:
                _, model, reasoning = text.split()
                effort = reasoning.removeprefix("--reasoning=")
                fixture.append_codex_records(codex_settings_record(f"native-{pane}", model, effort))
            return
        if fixture.refuses:
            return
        match text.split():
            case ["/model", model]:
                fixture.model = fixture.model_receipt or model.removeprefix("claude-").replace("-", " ", 1).replace("-", ".").title()
                fixture.history += f"\n  ⎿  Set model to {fixture.model} for this session only"
                if fixture.status:
                    fixture.status = f"{fixture.model} medium"
            case ["/effort", effort]:
                fixture.history += f"\n  ⎿  Set effort level to {effort.title()}: fixture"
                if fixture.status:
                    fixture.status = fixture.final_status or f"{fixture.model} {effort}"
            case _:
                raise AssertionError(text)


def absent_peer(arguments: list[str], *, limit: remote.RemoteCallLimit) -> tuple[int, str]:
    assert limit == remote.STANDARD_TIME_LIMIT
    assert arguments[:2] == [set_model.LOCAL_COMMAND, "observe"]
    return 0, json.dumps({identity: ["absent", ""] for identity in arguments[2::2]})


class SetModelTests(unittest.TestCase):
    root: Path = Path()
    registry: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = self.root / "models.conf"
        _ = self.registry.write_text(REGISTRY)
        self.write_cache()
        self.write_catalog(self.root, OPUS_ID)
        binary = self.root / "claude"
        _ = binary.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then printf '%s\\n' '2.1.319'; fi\n")
        binary.chmod(0o700)
        _ = (self.root / "codex.toml").write_text('model = "gpt-6.2-sol"\n')
        (self.root / "last_success").touch()
        config = self.root / "config"
        config.mkdir()
        source = TEST_CONFIG_DIRECTORY
        _ = (config / "machines.conf").write_bytes((source / "machines.conf").read_bytes())
        _ = (config / "script_prompts.conf").write_bytes((source / "script_prompts.conf").read_bytes())
        _ = (config / "set_model.conf").write_text(
            "default_effort=high\n"
            + "# range: 0..10\nscreen_wait_seconds=0\n"
            + "# whole: 1..10\nscreen_reads=1\n"
            + "# range: 0.1..120\npane_timeout_seconds=30\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "MODELS_CONFIG_FILE": str(self.registry), "MODELS_CONFIG_PUBLISH": "0", "MODELS_CONFIG_TIER_ONLY": "0",
            "CODEX_MODELS_CACHE_FILE": str(self.root / "models-cache.json"),
            "CODEX_CONFIG_FILE": str(self.root / "codex.toml"),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(self.root / "last_success"),
            "CLAUDE_CONFIG_DIR": str(self.root),
            "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "evidence"),
            "HERDR_BIN": "/nonexistent/herdr", "CONF_DIR": str(config), "PATH": f"{self.root}{os.pathsep}{os.environ['PATH']}",
        })))
        FakeHerdr.fixtures = {}
        FakeHerdr.calls = []
        _ = self.enterContext(patch.object(herdr, "Herdr", FakeHerdr))
        _ = self.enterContext(patch.object(FakeHerdr, "native_pane_process", side_effect=self.native_pane_process))
        _ = self.enterContext(patch.object(codex_daemon, "read_daemons", return_value=[]))
        _ = self.enterContext(patch.object(remote, "other_machine", return_value="mac"))
        _ = self.enterContext(patch.object(remote, "run_remote", side_effect=absent_peer))

    def native_pane_process(self, pane: str, kind: str, timeout: float, *,
                            pane_row: object = None) -> herdr.NativeAgentProcess:
        del timeout, pane_row
        fixture = FakeHerdr.fixtures[pane]
        assert fixture.family == kind
        identity = fixture.conversation or f"native-{pane}"
        home_key = "CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR"
        return herdr.NativeAgentProcess(herdr.EnvironmentThread(identity), "fixture-start",
                                        {home_key: str(self.root)})

    def write_catalog(self, directory: Path, opus: str) -> None:
        catalog = directory / "cache/model-catalog/fixture-cc.json"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        _ = catalog.write_text(json.dumps({"fetchedAt": 1, "catalog": {"config": {
            "provider_alias_targets": {"opus": {"default": opus}, "fable": {"default": "claude-fable-5-2"}},
            "models": [{"id": opus}, {"id": "claude-fable-5-2"}],
        }}}))

    def write_cache(self, *extra: str) -> None:
        slugs = ("gpt-6.1-sol", "gpt-6.2-sol", "gpt-6-astra", "gpt-6.2-astra", *extra)
        _ = (self.root / "models-cache.json").write_text(json.dumps({"models": [
            {"slug": slug, "visibility": "list",
             "supported_reasoning_levels": [{"effort": effort} for effort in ("medium", "high")]}
            for slug in slugs
        ]}))

    def member(self, name: str, kind: staff.Kind = "producer") -> staff.StaffRow:
        return staff.StaffRow(staff.StaffId(f"staff-{name}"), kind, name, staff.StaffRoot(),
                              worktree=staff.PresentWorktree("natedev", self.root),
                              home=staff.SessionHome(self.root), current_session=staff.StaffId(f"native-{name}"),
                              unit_type=staff.UnitDetails("direct") if kind == "unit" else staff.NotUnit())

    def row(self, name: str, observed: FakePane, kind: staff.Kind = "producer"
            ) -> tuple[staff.StaffRow, unit_lookup.LabeledSession]:
        row = self.member(name, kind)
        if observed.family == "codex":
            observed.codex_rollout = self.root / "sessions" / f"rollout-{row.current_session}.jsonl"
        FakeHerdr.fixtures[name] = observed
        pid = 10 + len(FakeHerdr.fixtures)
        claude = (unit_lookup.LiveClaude(name, row.current_session, "", pid)
                  if observed.family == "claude" else unit_lookup.ClaudeNotRunning())
        process = (session_staff.ObservedProcess(pid, "claude") if observed.family == "claude"
                   else session_staff.ProvedCodexPaneProcess(pid, "codex"))
        return row, unit_lookup.LabeledSession(
            name, claude, unit_lookup.Place("fixture", name), process, unit_lookup.LiveSession(row.current_session),
            generation=session_staff.ProvedProcessGeneration(pid, "fixture-start"))

    def invoke(self, *arguments: str, panes: tuple[tuple[staff.StaffRow, unit_lookup.LabeledSession], ...] = (),
               absent: tuple[staff.StaffRow, ...] = (),
               on_wait: Callable[[float], None] = lambda _: None) -> tuple[int, str, str]:
        record = staff.StaffSnapshot((*absent, *(row for row, _ in panes)))
        observations = session_staff.SessionLabelObservations(tuple(pane for _, pane in panes))
        out, err = io.StringIO(), io.StringIO()
        active_deadline = -math.inf
        elapsed = 0.0

        def monotonic() -> float:
            nonlocal active_deadline, elapsed
            deadline = worker_lifecycle.effective_deadline()
            if deadline != active_deadline:
                active_deadline = deadline
                elapsed = 0.0
            return time.monotonic() + elapsed

        def sleep(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds
            on_wait(seconds)

        with (staff.supplied_snapshot(record),
              patch.object(unit_lookup, "observed_session_labels", return_value=observations),
              patch.object(set_model, "time", SimpleNamespace(monotonic=monotonic, sleep=sleep)),
              redirect_stdout(out), redirect_stderr(err)):
            code = shutdown.main([set_model.COMMAND, *arguments])
        return code, out.getvalue(), err.getvalue()

    def assert_panes(self, result: tuple[int, str, str], expected_code: int, expected_output: str) -> None:
        code, out, err = result
        self.assertEqual((code, out), (expected_code, expected_output))
        for label in ("role default", "producer profiles"):
            err = err.removeprefix(f"{label}: saved locally\n{label}: publication disabled\n")
        self.assertEqual(err, "")

    def test_each_harness_verifies_the_selected_model(self) -> None:
        for family, model, initial, commands in (
            ("claude", "opus", "Fable 5.2 high", (OPUS_COMMAND, "/effort high")),
            ("codex", "sol", "gpt-6.1-sol medium", ("/model gpt-6.2-sol --reasoning=high",)),
            ("codex", "astra", "gpt-6-astra medium", ("/model gpt-6.2-astra --reasoning=high",)),
        ):
            with self.subTest(model=model):
                _ = self.registry.write_text(REGISTRY)
                FakeHerdr.calls.clear()
                pane = self.row("brain", FakePane(family, "Fixture history", status=initial))
                with patch.object(models_registry, "save_registry", wraps=models_registry.save_registry) as writer:
                    for _ in range(2):
                        self.assert_panes(self.invoke("producers", model, "high", panes=(pane,)), 0, "100 percent\n")
                    writer.assert_called_once()
                self.assertEqual(models_registry.producer_defaults().value(), f"{family}:{model}:high")
                submitted = commands if family == "claude" else commands * 2
                self.assertEqual(FakeHerdr.calls, [("brain", "native-brain", command) for command in submitted])

    def test_f001_preserves_unsent_text(self) -> None:
        command = "/model gpt-6.2-sol --reasoning=high"
        for draft in (command, " "):
            with self.subTest(draft=draft):
                fixture = FakePane("codex", "Fixture history", draft=draft)
                pane = self.row("brain", fixture)
                self.assert_panes(self.invoke("producers", "sol", panes=(pane,)), 1, "brain: has unsent text\n")
                self.assertEqual(fixture.draft, draft)
                self.assertEqual(FakeHerdr.calls, [])

    def test_f012_and_f015_confirm_from_fresh_rollout_records(self) -> None:
        command = "/model gpt-6.2-sol --reasoning=high"
        match = codex_settings_record("native-brain", "gpt-6.2-sol", "high")
        conflict = codex_settings_record("native-brain", "gpt-6.2-sol", "medium")
        other = codex_settings_record("native-other", "gpt-6.2-sol", "high")
        mixed = json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": f"user text {command}"},
        }) + "\n"
        for name, before, after, problem in (
            ("fresh", (), (match,), ""),
            ("stale", (match,), (), "unconfirmed"),
            ("stale partial line", (match[:-1],), ("\n",), "unconfirmed"),
            ("wrong thread", (), (other,), "unconfirmed"),
            ("later conflict", (), (match, conflict), "unconfirmed"),
            ("partial last line", (), (match[:-1],), "unconfirmed"),
            ("partial conflict", (), (match, conflict[:-1]), "unconfirmed"),
            ("malformed complete line", (), (match, "{\n"), "unconfirmed: the Codex rollout has a malformed record"),
            ("missing rollout", (), (), "unconfirmed"),
            ("mixed input", (), (match, mixed), "input mixed with user text"),
        ):
            with self.subTest(name=name):
                FakeHerdr.calls.clear()
                fixture = FakePane("codex", "Fixture history", refuses=True, codex_records=after)
                pane = self.row("brain", fixture)
                fixture.codex_rollout.unlink(missing_ok=True)
                fixture.append_codex_records(*before)
                self.assert_panes(self.invoke("producers", "sol", panes=(pane,)),
                                  1 if problem else 0, f"brain: {problem}\n" if problem else "100 percent\n")
                self.assertEqual(FakeHerdr.calls, [("brain", "native-brain", command)])

    def test_f012_checks_for_user_input_after_the_first_settings_match(self) -> None:
        command = "/model gpt-6.2-sol --reasoning=high"
        for name, record, problem in (
            ("mixed", {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": f"user text {command}"},
            }, "input mixed with user text"),
            ("text", {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "new user prompt"},
            }, "unconfirmed"),
            ("image", {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "data:image/png;base64,abc"}],
                },
            }, "unconfirmed"),
        ):
            with self.subTest(prompt=name):
                fixture = FakePane("codex", "Fixture history")
                pane = self.row("brain", fixture)
                waits: list[float] = []

                def record_prompt(seconds: float) -> None:
                    waits.append(seconds)
                    if len(waits) == 1:
                        fixture.append_codex_records(json.dumps(record) + "\n")

                result = self.invoke("producers", "sol", panes=(pane,), on_wait=record_prompt)
                self.assert_panes(result, 1, f"brain: {problem}\n")
                self.assertEqual(waits, [herdr.PANE_POLL_SECONDS])

    def test_f017_does_not_wait_after_a_user_prompt(self) -> None:
        prompt = json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "new user prompt"},
        }) + "\n"
        fixture = FakePane("codex", "Fixture history", codex_records=(prompt,))
        pane = self.row("brain", fixture)
        waits: list[float] = []

        result = self.invoke("producers", "sol", panes=(pane,), on_wait=waits.append)

        self.assert_panes(result, 1, "brain: unconfirmed\n")
        self.assertEqual(waits, [])

    def test_f012_rejects_settings_that_change_during_the_final_target_check(self) -> None:
        fixture = FakePane("codex", "Fixture history")
        pane = self.row("brain", fixture)
        check_target = FakeHerdr.check_command_target

        def record_conflict(client: FakeHerdr, pane: str, terminal: str, *, target: str, codex: bool) -> None:
            check_target(client, pane, terminal, target=target, codex=codex)
            if FakeHerdr.calls:
                fixture.append_codex_records(codex_settings_record(target, "gpt-6.2-sol", "medium"))

        with patch.object(FakeHerdr, "check_command_target", record_conflict):
            self.assert_panes(self.invoke("producers", "sol", panes=(pane,)), 1, "brain: unconfirmed\n")

    def test_f015_ignores_screen_confirmation(self) -> None:
        receipt = "• Model changed to gpt-6.2-sol high for this session only"
        fixture = FakePane("codex", f"Fixture history\n{receipt}", refuses=True, status="gpt-6.2-sol high")
        pane = self.row("brain", fixture)
        self.assert_panes(self.invoke("producers", "sol", panes=(pane,)), 1, "brain: unconfirmed\n")
        self.assertEqual(FakeHerdr.calls, [("brain", "native-brain", "/model gpt-6.2-sol --reasoning=high")])

    def test_f002_current_status_can_disprove_a_new_receipt(self) -> None:
        for status in ("Fable 5.2 high", "Fable 5.2", "Opus 5.5 medium"):
            with self.subTest(status=status):
                FakeHerdr.calls.clear()
                fixture = FakePane("claude", "Fixture history", status="Fable 5.2 medium", final_status=status)
                pane = self.row("brain", fixture)
                self.assert_panes(self.invoke("producers", "opus", panes=(pane,)),
                                  1, "brain: the screen did not confirm the model and effort\n")
                self.assertEqual([call[-1] for call in FakeHerdr.calls], [OPUS_COMMAND, "/effort high"])

    def test_f002_the_same_terminal_can_change_conversations(self) -> None:
        fixture = FakePane("codex", "Fixture history", next_conversation="native-other")
        self.assert_panes(self.invoke("producers", "sol", panes=(self.row("brain", fixture),)),
                          1, "brain: brain: the conversation changed\n")

    def test_failed_screen_check_names_only_the_failed_session(self) -> None:
        panes = tuple(self.row(name, FakePane("claude", "Set model to Fable 5.2", refuses=refuses))
                      for name, refuses in (("brain", False), ("morty", True)))
        self.assert_panes(self.invoke("producers", "opus", "high", panes=panes), 1, "morty: the screen did not confirm the model\n")

    def test_no_running_session_changes_only_the_default(self) -> None:
        self.assert_panes(self.invoke("producers", "opus"), 0, "100 percent\n")
        self.assertEqual(models_registry.producer_defaults().value(), "claude:opus:high")
        self.assertEqual(FakeHerdr.calls, [])

    def test_f014_default_save_failure_suppresses_success_with_no_running_sessions(self) -> None:
        with patch.object(models_registry, "save_registry", side_effect=OSError("fixture save failure")):
            code, out, err = self.invoke("producers", "opus")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("role default: local save failed: fixture save failure", err)

    def test_role_defaults_reach_their_launch_readers(self) -> None:
        for role, model, section, key, kind, slug in (
            ("directors", "fable", "production", "director", "claude", "fable"),
            ("showrunners", "opus", "production", "showrunner", "claude", "opus"),
            ("impl", "sol", "direct", "impl", "codex", "gpt-6.2-sol"),
        ):
            with self.subTest(role=role):
                self.assert_panes(self.invoke(role, model, "high"), 0, "100 percent\n")
                resolved = models_registry.launch_settings(section, key)
                self.assertEqual((resolved.kind, resolved.model, resolved.effort), (kind, slug, "high"))
        self.assertEqual(models_registry.launch_settings("direct", "test").effort, "medium")

    def test_other_harness_is_named_and_receives_no_input(self) -> None:
        pane = self.row("brain", FakePane("codex", "Fixture history"))
        self.assert_panes(self.invoke("producers", "opus", "high", panes=(pane,)), 1, "brain: the session runs Codex. opus requires Claude.\n")
        self.assertEqual(models_registry.producer_defaults().value(), "claude:opus:high")
        self.assertEqual(FakeHerdr.calls, [])

    def test_f003_worktree_host_does_not_hide_a_remote_conversation(self) -> None:
        row = self.member("brain")
        with patch.object(remote, "run_remote", side_effect=[
            (0, '{"staff-brain": ["pane", ""]}'),
            (0, '{"staff-brain": ["failed", "the remote pane is blocked"]}'),
        ]) as call:
            self.assert_panes(self.invoke("producers", "opus", absent=(row,)), 1, "brain: the remote pane is blocked\n")
        self.assertEqual([entry.kwargs for entry in call.call_args_list],
                         [{"limit": remote.STANDARD_TIME_LIMIT}, {"limit": {"kind": "while link alive"}}])
        self.assertEqual([entry.args for entry in call.call_args_list], [
            ([set_model.LOCAL_COMMAND, "observe", "staff-brain", "native-brain"],),
            ([set_model.LOCAL_COMMAND, "apply", "claude", "opus", "high", "staff-brain", "native-brain"],),
        ])
        with patch.object(remote, "run_remote", return_value=(0, "{}")):
            self.assert_panes(self.invoke("producers", "opus", absent=(row,)), 1, "brain: mac did not report a result for the session\n")

    def test_f004_daemon_thread_without_a_pane_is_named(self) -> None:
        row = self.member("brain")
        threads = [codex_daemon.DaemonSession(row.current_session, str(self.root), "idle")]
        with patch.object(codex_daemon, "read_daemons", return_value=[codex_daemon.DaemonRead(DAEMON, threads)]):
            self.assert_panes(self.invoke("producers", "sol", absent=(row,)), 1, "brain: the live session has no herdr pane\n")

    def test_f005_keep_existing_overrides_and_seed_only_confirmed_rows(self) -> None:
        live = tuple(self.row(name, FakePane("claude", "Fixture history")) for name in ("brain", "new"))
        absent = (self.member("morty"), self.member("stopped"))
        self.assert_panes(self.invoke("producers", "opus", panes=live, absent=absent), 0, "100 percent\n")
        self.assertEqual(models_registry.parse_registry(self.registry.read_text())["producers"], {
            "brain": "claude:fable:high",
            "morty": "claude:opus:medium",
            "new": "claude:opus:high",
        })

    def test_f006_refresh_catalog_and_confirm_the_exact_version(self) -> None:
        self.write_cache("gpt-6.3-sol")
        (self.root / "last_success").unlink()
        pane = self.row("brain", FakePane("codex", "Fixture history"))
        self.assert_panes(self.invoke("producers", "sol", panes=(pane,)), 0, "100 percent\n")
        self.assertIn(("brain", "native-brain", "/model gpt-6.3-sol --reasoning=high"), FakeHerdr.calls)
        pane = self.row("brain", FakePane("claude", "Fixture history", model_receipt="Opus 5.4"))
        self.assert_panes(self.invoke("producers", "opus", panes=(pane,)), 1, "brain: the screen did not confirm the model\n")
        self.assertEqual(FakeHerdr.calls[-1], ("brain", "native-brain", OPUS_COMMAND))
        (self.root / "cache/model-catalog/fixture-cc.json").unlink()
        before = FakeHerdr.calls.copy()
        self.assert_panes(self.invoke("producers", "opus", panes=(pane,)), 1, "brain: could not resolve opus to one Claude model version\n")
        self.assertEqual(FakeHerdr.calls, before)

    def test_f007_update_live_codex_directors_and_showrunners(self) -> None:
        roles: tuple[tuple[str, staff.Kind, str], ...] = (
            ("directors", "unit", "director"), ("showrunners", "showrunner", "showrunner"))
        for role, kind, key in roles:
            with self.subTest(role=role):
                pane = self.row(key, FakePane("codex", "Fixture history"), kind)
                self.assert_panes(self.invoke(role, "sol", panes=(pane,)), 0, "100 percent\n")
                config = models_registry.parse_registry(self.registry.read_text())
                self.assertEqual(config["production.codex"][key], "sol:high")
                self.assertEqual(models_registry.launch_settings("production", "director").kind, "claude")
                self.assertEqual(models_registry.launch_settings("production", "showrunner", "claude").kind, "claude")
                self.assertEqual(FakeHerdr.calls[-1], (key, f"native-{key}", "/model gpt-6.2-sol --reasoning=high"))

    def test_f006_use_the_selected_process_account(self) -> None:
        other = self.root / "other-claude"
        self.write_catalog(other, "claude-opus-5-6")
        row, pane = self.row("brain", FakePane("claude", "Fixture history"))
        process = herdr.NativeAgentProcess(herdr.EnvironmentThread(row.current_session), "fixture-start", {})
        with patch.object(FakeHerdr, "native_pane_process", return_value=process):
            self.assert_panes(self.invoke("producers", "opus", panes=((row, pane),)),
                              1, "brain: the selected pane's Claude account is unresolved\n")
        self.assertEqual(FakeHerdr.calls, [])
        process = replace(process, environment={"CLAUDE_CONFIG_DIR": str(other)})
        with patch.object(FakeHerdr, "native_pane_process", return_value=process):
            self.assert_panes(self.invoke("producers", "opus", panes=((row, pane),)), 0, "100 percent\n")
        self.assertEqual(FakeHerdr.calls[0][-1], "/model claude-opus-5-6")

    def test_f008_the_ultra_composer_accepts_a_model_change(self) -> None:
        fixture = FakePane("codex", "Fixture history", marker="»", status="gpt-6.1-sol ultra")
        self.assert_panes(self.invoke("producers", "sol", panes=(self.row("brain", fixture),)), 0, "100 percent\n")
        self.assertEqual(FakeHerdr.calls, [("brain", "native-brain", "/model gpt-6.2-sol --reasoning=high")])

    def test_f009_keep_completed_results_when_the_peer_disconnects(self) -> None:
        rows = (self.member("brain"), self.member("morty"))
        with patch.object(remote, "run_remote", side_effect=[
            (0, '{"staff-brain": ["pane", ""], "staff-morty": ["pane", ""]}'),
            (255, '{"staff-brain": ["confirmed", ""]}\n{"staff-morty"'),
        ]) as peer:
            result = self.invoke("producers", "sol", absent=rows)
        self.assertEqual(result[0], 1)
        self.assertTrue(result[1].startswith("morty: invalid reply from mac:"))
        self.assertNotIn("brain:", result[1])
        self.assertEqual(peer.call_args_list[-1].kwargs, {"limit": {"kind": "while link alive"}})

    def test_f009_one_pane_deadline_keeps_the_next_pane_result(self) -> None:
        panes = tuple(self.row(name, FakePane("codex", "Fixture history")) for name in ("brain", "morty"))
        record = staff.StaffSnapshot(tuple(row for row, _ in panes))
        observations = session_staff.SessionLabelObservations(tuple(pane for _, pane in panes))
        output = io.StringIO()

        def update(place: unit_lookup.Place, identity: str, target: models_registry.LaunchSettings,
                   checks: set_model.ScreenChecks) -> str:
            del identity, target, checks
            self.assertTrue(math.isfinite(worker_lifecycle.effective_deadline()))
            if place.pane == "brain":
                raise TimeoutError("fixture deadline")
            return ""

        with (staff.supplied_snapshot(record), patch.object(unit_lookup, "observed_session_labels", return_value=observations),
              patch.object(set_model, "update_pane", side_effect=update), redirect_stdout(output)):
            self.assertEqual(set_model.on_remote(["apply", "codex", "gpt-6.2-sol", "high",
                                                 "staff-brain", "native-brain", "staff-morty", "native-morty"]), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            '{"staff-brain": ["failed", "the pane exceeded its update deadline"]}',
            '{"staff-morty": ["confirmed", ""]}',
        ])

    def test_f010_publication_failure_keeps_local_saves_and_pane_results(self) -> None:
        panes = tuple(self.row(name, FakePane("codex", "Fixture history", refuses=refuses))
                      for name, refuses in (("new", False), ("morty", True)))
        save = models_registry.save_registry

        def fail_publication(path: Path, before: str, after: str, description: str, *,
                             preserve_producer_rows: bool) -> models_registry.SavedRegistry:
            saved = save(path, before, after, description, preserve_producer_rows=preserve_producer_rows)
            return saved._replace(publication=models_registry.RegistryPublication.UNCONFIRMED, detail="fixture SSH failure")

        with patch.object(models_registry, "save_registry", side_effect=fail_publication):
            code, out, err = self.invoke("producers", "sol", panes=panes)
        self.assertEqual((code, out), (1, "morty: unconfirmed\n"))
        self.assertEqual(err.splitlines(), [
            "role default: saved locally", "role default: publication unconfirmed: fixture SSH failure",
            "producer profiles: saved locally", "producer profiles: publication unconfirmed: fixture SSH failure",
        ])
        self.assertEqual(models_registry.producer_defaults().value(), "codex:sol:high")
        self.assertEqual(models_registry.parse_registry(self.registry.read_text())["producers"]["new"], "codex:sol:high")

    def test_f011_use_the_production_resolver_for_claude_showrunners(self) -> None:
        self.assert_panes(self.invoke("showrunners", "fable"), 0, "100 percent\n")
        config = models_registry.parse_registry(self.registry.read_text())
        self.assertEqual(config["production.claude"], {"director": "opus:medium", "showrunner": "fable:high"})
        self.assertEqual(models_registry.launch_settings("production", "showrunner", "claude"),
                         models_registry.LaunchSettings("claude", "fable", "high", ""))

    def test_unknown_model_lists_names_before_any_change(self) -> None:
        code, out, error = self.invoke("producers", "unknown", "high")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Valid models:", error)
        for name in ("fable", "opus", "sol", "astra"):
            self.assertIn(name, error)
        self.assertEqual(self.registry.read_text(), REGISTRY)
        self.assertEqual(FakeHerdr.calls, [])


if __name__ == "__main__":
    _ = unittest.main()
