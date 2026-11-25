"""Fixture checks for the agents clone command."""
from __future__ import annotations

from contextlib import nullcontext
from functools import cached_property
import json
import os
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import ANY, Mock, patch

from scripts.agents import shutdown
from scripts.herdr import herdr
from scripts.message import roster, sessions
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase
from scripts.whoami.account import named_account_environment

SOURCE = "019b3a40-4250-7000-8000-000000000201"
FORK = "019b3a40-4250-7000-8000-000000000202"
PANE = "w1:p1"
FORK_PANE = "w1:p2"


@final
class CloneCommandTests(StaffTestCase):
    folder: Path = Path()
    prepare: Mock = Mock()
    launch: Mock = Mock()
    native: Mock = Mock()
    remember: Mock = Mock()

    @cached_property
    def owner(self) -> staff.StaffRow:
        return staff.ensure("showrunner", name="alpha", runtime_id=SOURCE,
                            doc=staff.LiveDoc("natedev", self.root / "production.md"))

    @cached_property
    def client(self) -> herdr.Herdr:
        fake_herdr = Path(herdr.__file__).with_name("fake_herdr.py")
        with patch.dict(os.environ, {"HERDR_BIN": str(fake_herdr)}):
            return herdr.Herdr("fixture")

    @override
    def setUp(self) -> None:
        super().setUp()
        self.folder = self.root / "worktree"
        self.folder.mkdir()
        _ = self.owner
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.client))
        _ = self.enterContext(patch.object(herdr, "reboot_lock", side_effect=nullcontext))
        _ = self.enterContext(patch.object(roster, "roster", return_value=roster.Roster([], [])))
        _ = self.enterContext(patch.object(sessions, "transcript_locations", return_value=[]))
        _ = self.enterContext(patch.object(self.client, "tab_sessions",
                              return_value=[herdr.TabSession(PANE, "alpha", "workspace", SOURCE)]))
        _ = self.enterContext(patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("fixture", PANE)))
        self.prepare = cast(Mock, self.enterContext(patch.object(herdr, "prepare_agent_environment",
                                                               side_effect=prepare_fixture_environment)))
        self.launch = cast(Mock, self.enterContext(patch.object(herdr, "launch_prepared_agent", return_value=
            herdr.AgentLaunched(FORK_PANE, herdr.PromptSubmissionNotRequired()))))
        self.native = cast(Mock, self.enterContext(patch.object(self.client, "native_session", return_value=FORK)))
        self.remember = cast(Mock, self.enterContext(patch.object(self.client, "remember_row_tab")))

    def source(self, kind: Literal["claude", "codex"], home: Path, args: list[str]) -> None:
        key = "CLAUDE_CONFIG_DIR" if kind == "claude" else "CODEX_HOME"
        environment = {key: str(home), "STAFF_ID": SOURCE}
        process = herdr.NativeAgentProcess(herdr.PaneSessionReport(SOURCE), "source-start", environment)
        _ = self.enterContext(patch.object(self.client, "foreground_launch",
            return_value=herdr.ForegroundLaunch(process, 42, str(self.folder), args)))
        _ = self.enterContext(patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation(kind, (42,))))

    def assert_new_row(self, home: Path) -> None:
        record = staff.initial_snapshot()
        self.assertEqual(record.by_id(self.owner.id), self.owner)
        new = record.by_path("alpha-copy")
        assert new is not None
        self.assertEqual(new.id, FORK)
        self.assertEqual(new.current_session, FORK)
        self.assertEqual(new.kind, "producer")
        self.assertIsInstance(new.parent, staff.StaffRoot)
        self.assertIsInstance(new.doc, staff.DocUnfilled)
        self.assertEqual(new.home, staff.SessionHome(home))
        self.assertEqual(new.worktree, staff.PresentWorktree("natedev", self.folder))
        cast(Mock, self.remember).assert_called_once_with(FORK, FORK_PANE)

    def test_claude_clone_uses_the_source_account_and_turn_settings_and_admits_a_new_row(self) -> None:
        home = self.root / ".claude4"
        self.source("claude", home, ["claude", "--model", "sonnet", "--effort", "low"])
        transcript = home / "projects" / "worktree" / f"{SOURCE}.jsonl"
        transcript.parent.mkdir(parents=True)
        data = json.dumps({"type": "assistant", "message": {"model": "fable"}, "effort": "high"}) + "\n"
        _ = transcript.write_text(data)
        self.assertEqual(shutdown.main(["clone", "alpha", "--as", "alpha-copy"]), 0)
        environment = named_account_environment(home)
        cast(Mock, self.prepare).assert_called_once_with(str(self.folder), environment)
        cast(Mock, self.launch).assert_called_once_with("fixture", herdr.ForkTab(PANE, "alpha-copy"),
            herdr.PreparedAgentEnvironment(self.folder, environment), "claude",
            ["--resume", SOURCE, "--fork-session", "-n", "alpha-copy", "--remote-control", "alpha-copy",
             "--model", "fable", "--effort", "high", "--settings", '{"disableAgentView": true}', "--", ANY],
            herdr.NoOpeningPrompt(), 90_000, agent="alpha-copy", client=self.client)
        call = cast(Mock, self.launch).call_args
        assert call is not None
        prompt = cast(list[str], call.args[4])[-1]
        self.assertTrue(prompt.startswith("You are alpha-copy, a clone of alpha."))
        self.assertIn("Messages to alpha are for the source session.", prompt)
        self.assertEqual(transcript.read_text(), data)
        self.assert_new_row(home)

    def test_codex_clone_forks_the_native_id_with_current_settings_and_a_separate_row(self) -> None:
        home = self.root / ".codex4"
        self.source("codex", home, ["codex", "--model", "gpt-5", "-c", 'model_reasoning_effort="low"'])
        transcript = home / "sessions" / f"rollout-{SOURCE}.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(json.dumps({"type": "turn_context", "payload": {
            "model": "gpt-6", "effort": "high"}}) + "\n")
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 0)
        cast(Mock, self.launch).assert_called_once_with("fixture", herdr.ForkTab(PANE, "alpha-copy"),
            herdr.PreparedAgentEnvironment(self.folder, {"CODEX_HOME": str(home)}), "codex",
            ["fork", SOURCE, "--model", "gpt-6", "-c", 'model_reasoning_effort="high"', "--", ANY],
            herdr.NoOpeningPrompt(), 90_000, agent="alpha-copy", client=self.client)
        self.assert_new_row(home)

    def test_clone_refuses_an_existing_staff_name_before_launch(self) -> None:
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "ALPHA"]), 2)
        cast(Mock, self.launch).assert_not_called()
        self.assertEqual(staff.initial_snapshot().rows, (self.owner,))

    def test_clone_refuses_a_stopped_source_before_launch(self) -> None:
        with patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()):
            self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        cast(Mock, self.launch).assert_not_called()
        self.assertEqual(staff.initial_snapshot().rows, (self.owner,))

    def test_clone_refuses_to_attach_its_staff_row_to_the_source_id(self) -> None:
        self.source("claude", self.root / ".claude4", ["claude"])
        cast(Mock, self.native).return_value = SOURCE
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        self.assertEqual(staff.initial_snapshot().rows, (self.owner,))
        cast(Mock, self.remember).assert_not_called()


def prepare_fixture_environment(cwd: str, environment: dict[str, str]) -> herdr.PreparedAgentEnvironment:
    return herdr.PreparedAgentEnvironment(Path(cwd), environment)
