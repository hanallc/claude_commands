"""Test producer lifecycle commands with private records and a fake herdr."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.agents import pane_record, producer_lifecycle, remote, restart, restart_support, shutdown, shutdown_record, stop_resume
from scripts.agents.test_stop_resume import producer_first_fixture
from scripts.herdr import herdr, pane_launch
from scripts.lib import models_registry
from scripts.lib.test_models_registry import write_registry
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.message import roster, sessions
from scripts.production import staff, the_list, update_registration
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id


def command(*arguments: str) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = shutdown.main(list(arguments))
    return code, output.getvalue(), errors.getvalue()


def retain_producer_pane(row: staff.StaffRow) -> None:
    entry: pane_launch.StaffPaneLaunch = {"pane_id": "producer-pane", "staff_id": row.id,
        "model": "fixture", "remote_control": False, "environment": {}, "flags_read": True, "written_at": "fixture-start"}
    pane_record.save("fixture", {entry["pane_id"]: entry})


class CreateProducerTests(unittest.TestCase):
    def test_create_uses_default_or_override_and_passes_only_its_opening_prompt(self) -> None:
        for arguments, expected in (((), "claude:fable:high"),
                                    (("--model", "opus", "--effort", "max"), "claude:opus:max")):
            with self.subTest(arguments=arguments), producer_first_fixture("claude") as world:
                registry = world.case.root / "configuration/models.conf"
                _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"},
                                              "producers": {"dwight": None}}, update=True)
                template = world.case.root / "template.md"
                _ = template.write_text("# {name}\n\n{job}\n")
                readme = world.folder / "README.md"
                readme.unlink()
                with (patch.object(producer_lifecycle, "TEMPLATE", template),
                      patch.object(roster, "roster", return_value=roster.Roster([], []))):
                    code, output, errors = command("add-producer", "dwight", *arguments, "--job", "Plan the next work")
                    self.assertEqual(code, 0, errors)
                    self.assertEqual(command("add-producer", "dwight", *arguments,
                                             "--job", "Plan the next work"), (0, output, ""))
                row = staff.initial_snapshot().by_path("dwight")
                assert row is not None
                self.assertIn(f"staff {row.id}, account claude3, tab {world.pane}", output)
                self.assertEqual(models_registry.parse_registry(registry.read_text())["producers"]["dwight"], expected)
                self.assertEqual(readme.read_text(), "# dwight\n\nPlan the next work\n")
                self.assertEqual(len(world.openings), 1)
                opening = world.openings[0]
                assert isinstance(opening, herdr.OpeningPrompt)
                self.assertEqual(opening.text, "Read ~/rust/hanadocs/dwight/README.md, then run /producer:produce.")

    def test_create_retry_reuses_the_profile_readme_and_reserved_tab(self) -> None:
        with producer_first_fixture("claude") as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:sonnet:high"},
                                          "producers": {"dwight": None}}, update=True)
            template = world.case.root / "template.md"
            _ = template.write_text("# {name}\n{job}\n")
            readme = world.folder / "README.md"
            previous = readme.read_bytes(), readme.stat().st_mtime_ns
            with (patch.object(producer_lifecycle, "TEMPLATE", template),
                  patch.object(roster, "roster", return_value=roster.Roster([], []))):
                with patch.object(herdr, "start_agent", return_value=herdr.AgentNotStarted("fixture start failed")):
                    code, _, errors = command("add-producer", "dwight")
                self.assertNotEqual(code, 0)
                self.assertIn("fixture start failed", errors)
                self.assertEqual(staff.initial_snapshot().rows, ())
                written = registry.read_bytes(), registry.stat().st_mtime_ns
                template.unlink()
                code, output, errors = command("add-producer", "dwight")
                self.assertEqual(code, 0, errors)
                self.assertIn(world.identity, output)
            self.assertEqual((registry.read_bytes(), registry.stat().st_mtime_ns), written)
            self.assertEqual((readme.read_bytes(), readme.stat().st_mtime_ns), previous)
            self.assertEqual(len(world.case.client.opened), 1)
            self.assertEqual(len(world.openings), 1)

    def test_create_retry_uses_proof_from_the_existing_pane(self) -> None:
        with producer_first_fixture("claude") as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"},
                                          "producers": {"dwight": None}}, update=True)
            template = world.case.root / "template.md"
            _ = template.write_text("# {name}\n{job}\n")

            def starts_without_confirmation(name: str, pane: str, kind: str, arguments: list[str],
                                            opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt,
                                            timeout_ms: int, *, agent: str) -> herdr.PromptNotSubmitted:
                self.assertEqual(agent, "dwight")
                _ = world.start_agent(name, pane, kind, arguments, opening, timeout_ms)
                return herdr.PromptNotSubmitted("fixture did not confirm the prompt")

            proved = herdr.PromptSubmitted(datetime.now(timezone.utc))
            with (patch.object(producer_lifecycle, "TEMPLATE", template),
                  patch.object(roster, "roster", return_value=roster.Roster([], []))):
                with patch.object(herdr, "start_agent", side_effect=starts_without_confirmation):
                    self.assertIn("did not confirm", command("add-producer", "dwight")[2])
                self.assertIn("agents add-producer dwight", command("resume", "dwight")[2])
                with (patch.object(herdr.TranscriptCursor, "submitted_prompt", return_value=proved) as proof,
                      patch.object(herdr.Herdr, "submit_opening_prompt",
                                   side_effect=AssertionError("prompt was already present"))):
                    code, output, errors = command("add-producer", "dwight")
                self.assertEqual(code, 0, errors)
                self.assertIn(world.identity, output)
                proof.assert_called_once()
                self.assertEqual(len(world.openings), 1)
                self.assertEqual(len(world.case.client.opened), 1)


class ProducerLifecycleTests(unittest.TestCase):
    root: Path = Path()
    config: Path = Path()
    registry: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "configuration"
        _ = write_config_fixture(self.config / "staff.conf", f"PRODUCER_FOLDER_ROOT={self.root / 'documents'}\n")
        self.registry = self.config / "models.conf"
        _ = write_registry(self.registry, {"producer_defaults": {"model": "claude:sonnet:high"},
                                          "producers": {"morty": "claude:opus:high", "dwight": "claude:sonnet:high"}})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {**config_environment(self.config),
            "HOME": str(self.root), "STAFF_FILE": str(self.root / "staff.json"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "MODELS_CONFIG_FILE": str(self.registry),
            "MODELS_CONFIG_PUBLISH": "0", "MODELS_CONFIG_TIER_ONLY": "1"})))
        _ = cast(object, self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes")))
        _ = cast(object, self.enterContext(patch.object(roster, "roster", return_value=roster.Roster([], []))))
        _ = cast(object, self.enterContext(patch.object(restart_support, "send_message",
            side_effect=AssertionError("fixture did not declare a message"))))
        _ = cast(object, self.enterContext(patch.object(remote, "local_machine", return_value="natedev")))

    def profile(self, name: str) -> str | None:
        return models_registry.parse_registry(self.registry.read_text())["producers"].get(name)

    def producer(self, name: str) -> staff.StaffRow:
        folder = self.root / "documents" / name
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / "README.md").write_text(f"# {name}\n")
        return fixture_member("producer", name=name, doc=staff.LiveDoc("natedev", folder),
                              worktree=staff.PresentWorktree("natedev", folder))

    def test_create_refuses_the_staff_row_key_live_name_and_missing_template(self) -> None:
        taken = fixture_member("showrunner", name="taken")
        before = self.registry.read_bytes()
        self.assertIn("staff row", command("add-producer", taken.name)[2])
        self.assertIn("producer key", command("add-producer", "dwight")[2])
        live = roster.RosterEntry("natedev", "claude", "freestanding", "live", "running",
            roster.Messageable("session:live"), "", roster.NoShowrunner(), roster.NoDirector(), roster.NotAWorker())
        with patch.object(roster, "roster", return_value=roster.Roster([live], [])):
            self.assertIn("live session", command("add-producer", "live")[2])
        missing = self.root / "absent-template"
        with patch.object(producer_lifecycle, "TEMPLATE", missing):
            self.assertIn(str(missing), command("add-producer", "new")[2])
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse((self.root / "documents").exists())

    def test_rename_moves_the_key_and_keeps_identity_and_doc(self) -> None:
        old = self.producer("dwight")
        _ = self.producer("morty")
        taken = fixture_member("showrunner", name="taken")
        self.assertIn("morty", command("rename", "morty", "new")[2])
        self.assertIn("another staff row", command("rename", old.name, taken.name)[2])
        for name in (old.name, "new"):
            with self.subTest(pending_admission=name):
                admission = stop_resume.producer_admission_record(name)
                admission.parent.mkdir(exist_ok=True)
                _ = admission.write_text("{}")
                before = self.registry.read_bytes()
                self.assertIn(f"Run agents resume {name} first", command("rename", old.name, "new")[2])
                self.assertEqual(self.registry.read_bytes(), before)
                self.assertEqual(staff.snapshot().by_id(old.id), old)
                self.assertFalse(producer_lifecycle.pending_rename(old.name))
                admission.unlink()
        with patch.object(shutdown, "rename_repair_displays", return_value=True) as displays:
            code, output, errors = command("rename", old.name, "new")
        self.assertEqual(code, 0, errors)
        row = staff.snapshot().by_id(old.id)
        assert row is not None
        self.assertEqual((row.name, row.current_session, row.doc, row.worktree), ("new", old.current_session, old.doc, old.worktree))
        self.assertIsNone(self.profile(old.name))
        self.assertEqual(self.profile(row.name), "claude:sonnet:high")
        self.assertIn(str(self.root / "documents/dwight"), output)
        self.assertFalse((self.root / "documents/new").exists())
        displays.assert_called_once_with(row)

        with (patch.object(sessions, "running_sessions", return_value=[]),
              patch.object(herdr, "shared_session", return_value="fixture"),
              patch.object(herdr.Herdr, "panes", side_effect=OSError("fixture pane read failed"))):
            self.assertIn("cannot read producer panes: fixture pane read failed", command("rename", row.name, "next")[2])
            self.assertTrue(producer_lifecycle.pending_rename(row.name))
            self.assertEqual(self.profile("next"), "claude:sonnet:high")
        with patch.object(shutdown, "rename_repair_displays", return_value=True) as displays:
            self.assertEqual(command("rename", "next", "final")[0], 0)
            before = self.registry.read_bytes()
            self.assertIn("saved rename no longer matches its producer", command("rename", row.name, "next")[2])
            displays.assert_called_once()
        self.assertTrue(producer_lifecycle.pending_rename(row.name))
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertIsNotNone(staff.snapshot().by_path("final"))

        source = self.producer("alpha")
        replacement = self.producer("gamma")
        for producer in (source, replacement):
            _ = models_registry.store_producer(producer.name, "claude", "sonnet", "high")
        pane = {"pane_id": "producer-pane", "terminal_id": "terminal"}
        retain_producer_pane(source)
        with (patch.object(sessions, "running_sessions", return_value=[]),
              patch.object(herdr, "shared_session", return_value="fixture"),
              patch.object(herdr.Herdr, "panes", return_value=[pane]),
              patch.object(herdr.Herdr, "scan_row_panes", return_value=herdr.RowPaneScan({}, [])),
              patch.object(herdr.Herdr, "result", side_effect=OSError("fixture process read failed"))):
            errors = command("rename", source.name, "beta")[2]
            self.assertIn("cannot read producer panes: producer-pane: fixture process read failed", errors)
            self.assertTrue(producer_lifecycle.pending_rename(source.name))
        with patch.object(shutdown, "rename_repair_displays", return_value=True):
            self.assertEqual(command("rename", replacement.name, source.name)[0], 0)
            code, output, errors = command("rename", source.name, "beta")
        self.assertEqual(code, 0, errors)
        self.assertIn("alpha: kept the producer key", output)
        self.assertEqual(self.profile("alpha"), "claude:sonnet:high")
        self.assertEqual(self.profile("beta"), "claude:sonnet:high")
        self.assertEqual(staff.snapshot().by_path("alpha"), replace(replacement, name="alpha"))
        self.assertEqual(staff.snapshot().by_path("beta"), replace(source, name="beta"))
        self.assertFalse(producer_lifecycle.pending_rename(source.name))

    def test_retirement_requests_handoff_once_moves_open_items_and_restores_the_same_session(self) -> None:
        row, morty = self.producer("dwight"), self.producer("morty")
        owner = fixture_member("showrunner", name="show")
        destination = self.producer("director")
        opened = the_list.add(morty.id, "Open work", owner=owner.id, source="fixture", changes={"producer": row.id})
        closed = the_list.add(morty.id, "Closed work", owner=owner.id, source="fixture", changes={"producer": row.id})
        closed = the_list.close(morty.id, closed.id, "Done", source="fixture")
        self.assertIn("morty", command("retire", "morty")[2])
        self.assertIn("active producer", command("retire", row.name, "--to", owner.name)[2])
        admission = stop_resume.producer_admission_record(row.name)
        admission.parent.mkdir(exist_ok=True)
        _ = admission.write_text("{}")
        self.assertIn(f"Run agents resume {row.name} first", command("retire", row.name)[2])
        self.assertEqual(the_list.load(morty.id, opened.id), opened)
        self.assertEqual(staff.snapshot().by_path(row.name), row)
        admission.unlink()
        with patch.object(update_registration, "question_records_for_document", return_value=[object()]):
            self.assertIn("open questions", command("retire", row.name)[2])
        with patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()) as stopped:
            self.assertIn("live destination producer", command("retire", row.name, "--to", destination.name)[2])
            stopped.assert_called_once_with(destination.current_session)

        peer = roster.RosterEntry("mac", "codex", "freestanding", destination.name, "unknown",
            roster.Messageable(f"codex:{destination.current_session}"), "", roster.NoShowrunner(),
            roster.NoDirector(), roster.NotAWorker(), thread=roster.CodexThread(destination.current_session))

        def destination_on_peer(member: staff.StaffRow) -> str:
            return "mac" if member.id == destination.id else "natedev"

        with (patch.object(stop_resume, "member_machine", side_effect=destination_on_peer),
              patch.object(roster, "peer_roster", return_value=roster.Roster([peer], []))):
            self.assertIn("live destination producer", command("retire", row.name, "--to", destination.name)[2])

        during_stop: list[the_list.ListItem] = []
        after_move: list[the_list.ListItem] = []

        def stop_with_assignment(_row: staff.StaffRow, _client: herdr.Herdr) -> bool:
            if stop.call_count == 1:
                return False
            if stop.call_count == 2:
                during_stop.append(the_list.add(morty.id, "Assigned during stop", owner=owner.id,
                                                source="fixture", changes={"producer": row.id}))
            return True

        def send_with_assignment(*_arguments: object, **_options: object) -> restart_support.MessageSent:
            if not after_move:
                after_move.append(the_list.add(morty.id, "Assigned during notice", owner=owner.id,
                                               source="fixture", changes={"producer": row.id}))
            return {"kind": "sent"}

        pane: dict[str, object] = {"pane_id": "producer-pane", "terminal_id": "terminal"}
        with (patch.object(restart_support, "send_message", return_value={"kind": "sent"}) as send,
              patch.object(restart, "stop_row", side_effect=stop_with_assignment) as stop,
              patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("claude", (456,))),
              patch.object(herdr.Herdr, "panes", return_value=[pane]) as panes,
              patch.object(herdr.Herdr, "scan_row_panes", return_value=herdr.RowPaneScan({row.id: [pane]}, [])) as scan,
              patch.object(herdr, "shared_session", return_value="fixture"),
              patch.object(herdr.Herdr, "close_tab_confirmed", return_value=True) as close_tab,
              patch.object(shutdown_record, "remove_started") as forget):
            send.side_effect = RuntimeError("fixture refused the handoff notice")
            self.assertIn("refused the handoff notice", command("retire", row.name, "--to", destination.name)[2])
            send.side_effect = None
            with (patch.object(stop_resume, "member_machine", side_effect=destination_on_peer),
                  patch.object(roster, "peer_roster", return_value=roster.Roster([replace(peer, status="idle")], []))):
                for _ in range(2):
                    code, output, errors = command("retire", row.name, "--to", destination.name)
                    self.assertEqual(code, 0, errors)
                    self.assertIn("handoff.md", output)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args_list[0], send.call_args_list[1])
            self.assertEqual(staff.snapshot().by_path(row.name), row)
            self.assertEqual(the_list.load(morty.id, opened.id), opened)
            self.assertEqual(self.profile(row.name), "claude:sonnet:high")
            stop.assert_not_called()
            handoff = self.root / "documents/dwight/handoff.md"
            _ = handoff.write_text("Open work. Review the scope.\n")
            self.assertIn("conversation did not stop", command("retire", row.name, "--to", destination.name)[2])
            self.assertEqual(the_list.load(morty.id, opened.id), opened)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(self.profile(row.name), "claude:sonnet:high")
            self.assertEqual(staff.snapshot().by_path(row.name), row)
            retain_producer_pane(row)
            scan.return_value = herdr.RowPaneScan({}, [herdr.UnknownPane("producer-pane", "", "fixture unreadable")])
            with patch.object(herdr.Herdr, "result", side_effect=OSError("fixture process read failed")):
                self.assertIn("cannot read producer panes: producer-pane: fixture process read failed",
                              command("retire", row.name, "--to", destination.name)[2])
            stop.assert_called_once()
            close_tab.assert_not_called()
            self.assertEqual(self.profile(row.name), "claude:sonnet:high")
            replacement = staff.native_id(fixture_runtime_id("retirement replacement"))
            _ = staff.update_current_session(row.current_session, replacement)
            errors = command("retire", row.name, "--to", destination.name)[2]
            self.assertIn(f"retirement belongs to session {row.current_session}", errors)
            self.assertIn(f"Current session is {replacement}", errors)
            stop.assert_called_once()
            self.assertEqual(the_list.load(morty.id, opened.id), opened)
            _ = staff.update_current_session(replacement, row.current_session)

            scan.return_value = herdr.RowPaneScan({row.id: [pane]}, [])
            send.side_effect = RuntimeError("fixture refused the List notice")
            self.assertIn("refused the List notice", command("retire", row.name, "--to", destination.name)[2])
            before_retry = the_list.load(morty.id, opened.id)
            self.assertEqual(len(before_retry.log), len(opened.log) + 1)
            self.assertEqual(the_list.load(morty.id, during_stop[0].id), during_stop[0])
            close_tab.assert_called_once_with("producer-pane", target=row.current_session, terminal="terminal")
            forget.assert_called_once_with([row.id])

            panes.return_value = []
            scan.return_value = herdr.RowPaneScan({}, [])
            send.side_effect = send_with_assignment
            self.assertIn("new open items belong to the producer", command("retire", row.name, "--to", destination.name)[2])
            self.assertEqual(send.call_args_list[2], send.call_args_list[3])
            self.assertEqual(staff.snapshot().by_path(row.name), row)
            self.assertEqual(self.profile(row.name), "claude:sonnet:high")
            send.side_effect = None
            self.assertEqual(command("retire", row.name, "--to", destination.name)[0], 0)
            self.assertEqual(command("retire", row.name, "--to", destination.name)[0], 0)
            self.assertEqual(the_list.load(morty.id, opened.id), before_retry)
            self.assertEqual(stop.call_count, 4)
            self.assertEqual(close_tab.call_count, 1)
            self.assertEqual(send.call_count, 6)
        retired = staff.snapshot()
        self.assertEqual(retired.by_id(row.id), staff.StaffRow.from_json({
            **row.to_json(), "retired_producer": "claude:sonnet:high"}))
        self.assertIsNone(retired.by_path(row.name))
        self.assertIsNone(retired.by_member_reference(row.id))
        self.assertIsNone(staff.local_by_current_session(row.current_session))
        self.assertIsInstance(sessions.staff_recipient(row.name, retired), sessions.StaffNameMissing)
        self.assertNotIn(row.name, {member.name for member in retired.find(kind="producer")})
        self.assertIsNone(self.profile(row.name))
        self.assertTrue(handoff.is_file())
        moved = the_list.load(morty.id, opened.id)
        self.assertEqual((moved.producer, moved.log[-1].source), (destination.id, "agents retire"))
        for late in (*during_stop, *after_move):
            moved_late = the_list.load(morty.id, late.id)
            self.assertEqual((moved_late.producer, moved_late.log[-1].source), (destination.id, "agents retire"))
        self.assertEqual(the_list.load(morty.id, closed.id), closed)
        self.assertIn("dwight | Closed work", the_list.table(morty.id, include_closed=True))

        def resume(identity: str, *, why: str, to: str | None) -> stop_resume.ClaudeUp:
            del why, to
            self.assertEqual(staff.snapshot().by_path(row.name), row)
            self.assertEqual(self.profile(row.name), "claude:sonnet:high")
            return stop_resume.ClaudeUp(identity, "restored-tab", "claude3")

        with patch.object(stop_resume, "resume", side_effect=resume) as launch:
            self.assertEqual(command("resume", row.name, "--retired")[0], 0)
        launch.assert_called_once_with(row.id, why="resumed by name", to=None)
