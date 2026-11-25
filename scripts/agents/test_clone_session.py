"""Fixture checks for the agents clone command."""
from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext, redirect_stderr
from functools import cached_property
import io
import json
import os
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import ANY, Mock, patch

from scripts.agents import member_account, pane_record, shutdown, shutdown_record, stop_resume
from scripts.herdr import herdr
from scripts.lib import copy_record, scratch, session_folder
from scripts.message import roster, send, sessions
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase

SOURCE = "019b3a40-4250-7000-8000-000000000201"
FORK = "019b3a40-4250-7000-8000-000000000202"
PANE = "w1:p1"
FORK_PANE = "w1:p2"
THIRD = "019b3a40-4250-7000-8000-000000000203"


@final
class CloneCommandTests(StaffTestCase):
    clone_scratch: Path = Path()
    folder: Path = Path()
    prepare: Mock = Mock()
    launch: Mock = Mock()
    native: Mock = Mock()
    remember: Mock = Mock()
    processes: Mock = Mock()
    place: Mock = Mock()
    visible: Mock = Mock()
    client_factory: Mock = Mock()
    conversation: Mock = Mock()
    publication: Mock = Mock()

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
        _ = self.enterContext(cast(AbstractContextManager[object], patch.dict(os.environ, {
            "CONF_DIR": str(Path(scratch.__file__).resolve().parents[2] / "config"),
            copy_record.ROOT_ENV: str(self.root / "scratch"),
            session_folder.ROOT_ENV: str(self.root / "session"),
        })))
        self.clone_scratch = self.enterContext(scratch.temporary_folder("clone-fixture"))
        self.folder = self.clone_scratch / "worktree"
        self.folder.mkdir()
        _ = self.owner
        self.client_factory = cast(Mock, self.enterContext(patch.object(herdr, "Herdr", return_value=self.client)))
        _ = self.enterContext(patch.object(herdr, "shared_session", return_value="shared"))
        _ = self.enterContext(patch.object(herdr, "reboot_lock", side_effect=nullcontext))
        _ = self.enterContext(patch.object(roster, "roster", return_value=roster.Roster([], [])))
        _ = self.enterContext(patch.object(sessions, "transcript_locations", return_value=[]))
        _ = self.enterContext(patch.object(self.client, "tab_sessions",
                              return_value=[herdr.TabSession(PANE, "alpha", "workspace", SOURCE)]))
        self.place = cast(Mock, self.enterContext(patch.object(herdr, "herdr_place_of", return_value=herdr.HerdrPlace("fixture", PANE))))
        self.processes = cast(Mock, self.enterContext(patch.object(sessions, "session_processes", return_value=sessions.SessionProcesses(()))))
        self.conversation = cast(Mock, self.enterContext(patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation())))
        self.visible = cast(Mock, self.enterContext(patch.object(self.client, "run")))
        _ = self.enterContext(patch.object(self.client, "read", return_value=""))
        _ = self.enterContext(patch.object(self.client, "command_terminal", return_value="fixture-terminal"))
        self.publication = cast(Mock, self.enterContext(patch.object(pane_record, "write", wraps=pane_record.write)))
        self.prepare = cast(Mock, self.enterContext(patch.object(herdr, "prepare_agent_environment",
                                                               side_effect=prepare_fixture_environment)))
        self.launch = cast(Mock, self.enterContext(patch.object(herdr, "launch_prepared_agent", return_value=
            herdr.ForkLaunched(FORK_PANE, herdr.PromptSubmissionNotRequired(), "fixture-terminal"))))

        def native_session(pane: str, _kind: str, _timeout: float) -> str:
            return SOURCE if pane == PANE else FORK

        self.native = cast(Mock, self.enterContext(patch.object(self.client, "native_session", side_effect=native_session)))
        self.remember = cast(Mock, self.enterContext(patch.object(self.client, "remember_row_tab")))
        _ = self.enterContext(patch.object(self.client, "close_terminal_pane", return_value=True))

    def source(self, kind: Literal["claude", "codex"], home: Path, args: list[str], *,
               footer: str = "", clone_home: Path | None = None, implicit_account: bool = False,
               clone_uses_explicit_account: bool = False) -> None:
        key = "CLAUDE_CONFIG_DIR" if kind == "claude" else "CODEX_HOME"
        account = {"HOME": str(home.parent)} if implicit_account else {key: str(home)}
        clone_account = {key: str(clone_home or home)} if clone_home or clone_uses_explicit_account else account
        origin = herdr.NativeAgentProcess(herdr.PaneSessionReport(SOURCE), "source-start", {**account, "STAFF_ID": SOURCE})
        fork = herdr.NativeAgentProcess(herdr.PaneSessionReport(FORK), "fork-start", clone_account)

        def foreground_launch(pane: str, _kind: str) -> herdr.ForegroundLaunch:
            if pane == PANE:
                return herdr.ForegroundLaunch(origin, 42, str(self.folder), args)
            return herdr.ForegroundLaunch(fork, 43, str(self.folder), [kind])

        _ = self.enterContext(patch.object(self.client, "foreground_launch", side_effect=foreground_launch))
        cast(Mock, self.processes).return_value = sessions.SessionProcesses((42,) if kind == "claude" else ())
        cast(Mock, self.conversation).return_value = herdr.LiveConversation(kind, (42,))
        cast(Mock, self.visible).return_value = (f"───\n❯ \n───\n  {footer}" if kind == "claude" else f"› \n\n  {footer}")

    def assert_new_row(self, home: Path) -> None:
        record = staff.initial_snapshot()
        self.assertEqual(record.by_id(self.owner.id), self.owner)
        new = record.by_path("alpha-copy")
        assert new is not None
        self.assertEqual(new.id, FORK)
        self.assertEqual(new.current_session, FORK)
        self.assertEqual(new.kind, "session")
        self.assertIsInstance(new.parent, staff.StaffRoot)
        self.assertIsInstance(new.doc, staff.DocUnfilled)
        self.assertEqual(new.home, staff.SessionHome(home))
        self.assertIsInstance(new.worktree, staff.WorktreeUnfilled)
        self.assertEqual(new.launch_folder, staff.SessionLaunchFolder("natedev", self.folder))
        self.assertEqual(stop_resume.member_machine(new), "natedev")
        self.assertEqual(shutdown_record.row_folder(new, record), ("natedev", self.folder))
        cast(Mock, self.remember).assert_called_once_with(FORK, FORK_PANE)

    def launched_environment(self) -> herdr.PreparedAgentEnvironment:
        call = cast(Mock, self.launch).call_args
        assert call is not None
        return cast(herdr.PreparedAgentEnvironment, call.args[2])

    def test_claude_clone_uses_live_settings_and_admits_an_ordinary_session(self) -> None:
        home = self.clone_scratch / ".claude4"
        unit = staff.ensure("unit", name="unit", parent=staff.ReportsTo(self.owner.id), runtime_id=THIRD,
                            worktree=staff.PresentWorktree("natedev", self.folder))
        self.source("claude", home, ["claude", "--model", "sonnet", "--effort", "low"], footer="Opus 4.8 xhigh")
        transcript = home / "projects" / "worktree" / f"{SOURCE}.jsonl"
        transcript.parent.mkdir(parents=True)
        data = json.dumps({"type": "assistant", "message": {"model": "fable"}, "effort": "high"}) + "\n"
        _ = transcript.write_text(data)
        self.assertEqual(shutdown.main(["clone", "alpha", "--as", "alpha-copy"]), 0)
        environment = {"CLAUDE_CONFIG_DIR": str(home)}
        cast(Mock, self.prepare).assert_called_once_with(str(self.folder), environment, ANY)
        prepared = self.launched_environment()
        self.assertEqual(prepared.env, environment)
        cast(Mock, self.launch).assert_called_once_with("fixture", herdr.ForkTab(PANE, "alpha-copy"),
            prepared, "claude",
            ["--resume", SOURCE, "--fork-session", "-n", "alpha-copy", "--remote-control", "alpha-copy",
             "--model", "claude-opus-4-8", "--effort", "xhigh", "--settings", '{"disableAgentView": true}', "--", ANY],
            herdr.NoOpeningPrompt(), 90_000, agent="alpha-copy", client=self.client)
        call = cast(Mock, self.launch).call_args
        assert call is not None
        prompt = cast(list[str], call.args[4])[-1]
        self.assertTrue(prompt.startswith("You are alpha-copy, a clone of alpha."))
        self.assertIn("Messages to alpha are for the source session.", prompt)
        self.assertEqual(transcript.read_text(), data)
        self.assert_new_row(home)
        snapshot = staff.initial_snapshot()
        new = snapshot.by_current_session(FORK)
        assert new is not None
        self.assertEqual(staff.active(snapshot, "producer"), ())
        self.assertEqual(snapshot.by_worktree("natedev", self.folder), unit)
        saved = pane_record.publication_for_session(FORK, record=snapshot)
        assert saved is not None
        self.assertEqual(saved.entry["pane_id"], FORK_PANE)
        selected = member_account.resume_account("claude", new)
        self.assertEqual(getattr(selected, "dir", None), home)
        cast(Mock, self.publication).assert_called_once()

    def test_codex_clone_forks_the_native_id_with_current_settings_and_a_separate_row(self) -> None:
        home = self.clone_scratch / ".codex4"
        self.source("codex", home, ["codex", "--model", "gpt-5", "-c", 'model_reasoning_effort="low"'],
                    footer="GPT-5.6 Luna high · Standard")
        home.mkdir()
        _ = (home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6 Luna"}
        ]}))
        transcript = home / "sessions" / f"rollout-{SOURCE}.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text(json.dumps({"type": "turn_context", "payload": {
            "model": "gpt-6", "effort": "high"}}) + "\n")
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 0)
        prepared = self.launched_environment()
        self.assertEqual(prepared.env, {"CODEX_HOME": str(home)})
        cast(Mock, self.launch).assert_called_once_with("fixture", herdr.ForkTab(PANE, "alpha-copy"),
            prepared, "codex",
            ["fork", SOURCE, "--model", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"', "--", ANY],
            herdr.NoOpeningPrompt(), 90_000, agent="alpha-copy", client=self.client)
        self.assert_new_row(home)

    def test_codex_clone_passes_the_live_ultra_effort(self) -> None:
        self.source("codex", self.clone_scratch / ".codex", ["codex"], footer="gpt-6.1-sol ultra")
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 0)
        call = cast(Mock, self.launch).call_args
        assert call is not None
        self.assertIn('model_reasoning_effort="ultra"', cast(list[str], call.args[4]))

    def test_clone_refuses_an_unstaffed_label_and_names_the_native_id_route(self) -> None:
        self.source("claude", self.clone_scratch / ".claude", ["claude"], footer="Opus 4.8 high")
        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic):
            self.assertEqual(shutdown.main(["clone", "only-a-tab-label", "--as", "alpha-copy"]), 2)
        self.assertIn("Use the native session ID", diagnostic.getvalue())
        cast(Mock, self.launch).assert_not_called()
        cast(Mock, self.client_factory).assert_not_called()

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
        self.source("claude", self.clone_scratch / ".claude4", ["claude"], footer="Opus 4.8 high")
        cast(Mock, self.native).side_effect = None
        cast(Mock, self.native).return_value = SOURCE
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        self.assertEqual(staff.initial_snapshot().rows, (self.owner,))
        cast(Mock, self.remember).assert_not_called()

    def test_clone_refuses_multiple_live_panes_for_one_claude_session(self) -> None:
        self.source("claude", self.clone_scratch / ".claude4", ["claude"], footer="Opus 4.8 high")
        cast(Mock, self.processes).return_value = sessions.SessionProcesses((42, 44))
        cast(Mock, self.place).side_effect = [herdr.HerdrPlace("fixture", PANE), herdr.HerdrPlace("fixture", "w1:p9")]
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        cast(Mock, self.launch).assert_not_called()

    def test_clone_uses_the_proved_server_instead_of_the_shared_server(self) -> None:
        self.source("claude", self.clone_scratch / ".claude4", ["claude"], footer="Opus 4.8 high")
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 0)
        cast(Mock, self.client_factory).assert_called_once_with("fixture")

    def test_clone_refuses_unproved_source_placement(self) -> None:
        self.source("claude", self.clone_scratch / ".claude4", ["claude"], footer="Opus 4.8 high")
        cast(Mock, self.place).return_value = herdr.UnprovedHerdrPlacement("cannot prove the source placement")
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        cast(Mock, self.client_factory).assert_not_called()
        cast(Mock, self.launch).assert_not_called()

    def test_clone_refuses_a_missing_live_effort_even_with_launch_flags(self) -> None:
        self.source("claude", self.clone_scratch / ".claude4", ["claude", "--effort", "high"], footer="Opus 4.8")
        diagnostic = io.StringIO()
        with redirect_stderr(diagnostic):
            self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        self.assertIn("cannot prove the current effort", diagnostic.getvalue())
        cast(Mock, self.launch).assert_not_called()

    def test_clone_restores_account_environment_and_checks_the_started_account(self) -> None:
        home = self.clone_scratch / ".claude4"
        wrong = self.clone_scratch / ".claude5"
        self.source("claude", home, ["claude"], footer="Opus 4.8 high", clone_home=wrong)
        cast(Mock, self.prepare).side_effect = None
        cast(Mock, self.prepare).return_value = herdr.PreparedAgentEnvironment(self.folder, {"CLAUDE_CONFIG_DIR": str(wrong)})
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        self.assertEqual(self.launched_environment().env["CLAUDE_CONFIG_DIR"], str(home))
        self.assertEqual(staff.initial_snapshot().rows, (self.owner,))
        cast(Mock, self.publication).assert_not_called()

    def test_clone_preserves_an_implicit_claude_account_after_environment_loading(self) -> None:
        home = self.clone_scratch / ".claude"
        self.source("claude", home, ["claude"], footer="Opus 4.8 high", implicit_account=True)
        cast(Mock, self.prepare).side_effect = None
        cast(Mock, self.prepare).return_value = herdr.PreparedAgentEnvironment(
            self.folder, {"HOME": "/another/home", "CLAUDE_CONFIG_DIR": str(home), "PATH": "/bin"})
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 0)
        prepared = self.launched_environment()
        self.assertEqual(prepared.env, {"HOME": str(home.parent), "PATH": "/bin"})
        self.assertIn("CLAUDE_CONFIG_DIR", prepared.unset_environment)
        self.assert_new_row(home)

    def test_clone_refuses_an_explicit_account_for_an_implicit_source(self) -> None:
        home = self.clone_scratch / ".claude"
        self.source("claude", home, ["claude"], footer="Opus 4.8 high", implicit_account=True,
                    clone_uses_explicit_account=True)
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        self.assertEqual(staff.initial_snapshot().rows, (self.owner,))
        cast(Mock, self.publication).assert_not_called()
        cast(Mock, self.client.close_terminal_pane).assert_called_once_with(FORK_PANE, "fixture-terminal")

    def test_clone_refuses_reserved_message_addresses(self) -> None:
        for name in ("user", "USER", "staff:alpha", "session:alpha", "codex:alpha", "uds:alpha", "worker:alpha"):
            with self.subTest(name=name):
                self.assertEqual(shutdown.main(["clone", SOURCE, "--as", name]), 2)
        cast(Mock, self.launch).assert_not_called()

    def test_source_and_clone_names_resolve_to_separate_native_addresses(self) -> None:
        self.source("claude", self.clone_scratch / ".claude4", ["claude"], footer="Opus 4.8 high")
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 0)
        entries = [
            roster.RosterEntry("natedev", "claude", "freestanding", name, "idle",
                               roster.Messageable(f"session:{identity}"), str(self.folder),
                               roster.NoShowrunner(), roster.NoDirector(), roster.NotAWorker(),
                               session=roster.ClaudeSession(identity))
            for name, identity in (("alpha", SOURCE), ("alpha-copy", FORK))
        ]
        record = staff.initial_snapshot()
        for name, identity in (("alpha", SOURCE), ("alpha-copy", FORK)):
            result = send.resolve_recipient(name, send.LocalOnly(), roster.Roster(entries, []), membership=record)
            self.assertEqual(result, send.ClaudeRecipient(f"session:{identity}"))

    def test_clone_does_not_replace_a_row_admitted_during_launch(self) -> None:
        home = self.clone_scratch / ".claude4"
        self.source("claude", home, ["claude"], footer="Opus 4.8 high")

        def competing_writer(*_args: object, **_kwargs: object) -> herdr.AgentLaunched:
            _ = staff.create_session("Alpha-Copy", THIRD, staff.SessionLaunchFolder("natedev", self.folder),
                                     staff.SessionHome(home))
            return herdr.ForkLaunched(FORK_PANE, herdr.PromptSubmissionNotRequired(), "fixture-terminal")

        cast(Mock, self.launch).side_effect = competing_writer
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        record = staff.initial_snapshot()
        self.assertIsNotNone(record.by_current_session(THIRD))
        self.assertIsNone(record.by_current_session(FORK))
        cast(Mock, self.publication).assert_not_called()
        cast(Mock, self.client.close_terminal_pane).assert_called_once_with(FORK_PANE, "fixture-terminal")

    def test_staff_rejects_later_casefold_name_collisions_with_a_session(self) -> None:
        _ = staff.create_session("alpha-copy", FORK, staff.SessionLaunchFolder("natedev", self.folder),
                                 staff.SessionHome(self.clone_scratch / ".claude"))
        with self.assertRaisesRegex(staff.StaffRefused, "another staff row"):
            _ = staff.ensure("showrunner", name="Alpha-Copy", runtime_id=THIRD)
        with self.assertRaisesRegex(staff.StaffRefused, "another staff row"):
            _ = staff.rename(self.owner.id, "ALPHA-COPY")
        self.assertEqual(staff.initial_snapshot().by_id(self.owner.id), self.owner)

    def test_clone_removes_its_launch_and_terminal_when_staff_rejects_admission(self) -> None:
        self.source("claude", self.clone_scratch / ".claude", ["claude"], footer="Opus 4.8 high")
        with patch.object(staff, "create_session", side_effect=staff.StaffRefused("name taken")):
            self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        cast(Mock, self.publication).assert_called_once()
        self.assertIsNone(pane_record.publication_for_session(FORK))
        cast(Mock, self.client.close_terminal_pane).assert_called_once_with(FORK_PANE, "fixture-terminal")

    def test_clone_does_not_admit_staff_if_the_launch_cannot_be_saved(self) -> None:
        self.source("claude", self.clone_scratch / ".claude", ["claude"], footer="Opus 4.8 high")
        cast(Mock, self.publication).side_effect = OSError("launch file unavailable")
        self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        self.assertEqual(staff.initial_snapshot().rows, (self.owner,))
        cast(Mock, self.client.close_terminal_pane).assert_called_once_with(FORK_PANE, "fixture-terminal")

    def test_clone_keeps_the_launch_if_staff_commits_before_its_reply_fails(self) -> None:
        self.source("claude", self.clone_scratch / ".claude", ["claude"], footer="Opus 4.8 high")
        create = staff.create_session

        def lose_reply(name: str, identity: str, folder: staff.SessionLaunchFolder, home: staff.SessionHome) -> staff.StaffRow:
            _ = create(name, identity, folder, home)
            raise staff.StaffUnreadable("lost staff reply")

        diagnostic = io.StringIO()
        with patch.object(staff, "create_session", side_effect=lose_reply), redirect_stderr(diagnostic):
            self.assertEqual(shutdown.main(["clone", SOURCE, "--as", "alpha-copy"]), 2)
        self.assertIn("staff admission is unconfirmed", diagnostic.getvalue())
        self.assertIsNotNone(staff.initial_snapshot().by_current_session(FORK))
        self.assertIsNotNone(pane_record.publication_for_session(FORK))
        cast(Mock, self.client.close_terminal_pane).assert_not_called()

    def test_create_session_validates_name_and_identity_on_the_staff_owner(self) -> None:
        with patch.object(staff, "is_owner", return_value=False):
            folder = staff.SessionLaunchFolder("mac", self.folder)
            home = staff.SessionHome(self.clone_scratch / ".claude4")
            row = staff.create_session("alpha-copy", FORK, folder, home)
            self.assertEqual(row.kind, "session")
            self.assertEqual(row.home, home)
            with self.assertRaisesRegex(staff.StaffRefused, "another staff row"):
                _ = staff.create_session("ALPHA-COPY", THIRD, folder, home)
            with self.assertRaisesRegex(staff.StaffRefused, "runtime identity"):
                _ = staff.create_session("another-name", FORK, folder, home)

    def test_probe_cleanup_removes_only_the_scratch_folder_after_failure(self) -> None:
        projects = self.clone_scratch / ".claude" / "projects"
        project = projects / "worktree"
        project.mkdir(parents=True)
        transcript = project / f"{SOURCE}.jsonl"
        _ = transcript.write_text("source transcript\n")

        with (patch.object(scratch, "remove", wraps=scratch.remove) as remove,
              self.assertRaisesRegex(ValueError, "probe failed"),
              scratch.temporary_folder("clone-probe") as probe):
            self.assertEqual(probe.parent, self.root / "scratch")
            fixture_projects = probe / ".claude" / "projects"
            fixture_projects.mkdir(parents=True)
            _ = (fixture_projects / f"{FORK}.jsonl").write_text("fork transcript\n")
            alias = probe / ".claude6"
            alias.mkdir()
            (alias / "projects").symlink_to(projects, target_is_directory=True)
            raise ValueError("probe failed")

        remove.assert_called_once_with(probe)
        self.assertFalse(probe.exists())
        self.assertEqual(list(projects.iterdir()), [project])
        self.assertEqual(list(project.iterdir()), [transcript])
        self.assertEqual(transcript.read_text(), "source transcript\n")


def prepare_fixture_environment(cwd: str, environment: dict[str, str],
                                unset: frozenset[str]) -> herdr.PreparedAgentEnvironment:
    return herdr.PreparedAgentEnvironment(Path(cwd), environment, unset)
