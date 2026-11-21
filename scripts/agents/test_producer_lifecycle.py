"""Test producer lifecycle commands with private records and a fake herdr."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import patch

from scripts.agents import pane_record, producer_creation, producer_lifecycle, remote, restart, restart_support, shutdown, shutdown_record, stop_resume
from scripts.agents.test_stop_resume import ProducerAdmissionWorld, producer_first_fixture
from scripts.herdr import herdr, pane_launch
from scripts.lib import models_registry
from scripts.lib.git_test_support import fixture_git_environment
from scripts.lib.script_envelope import script_prompt
from scripts.lib.test_models_registry import write_registry
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.message import roster, sessions
from scripts.production import staff, the_list, update_registration
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id

SUBMIT_OPENING = herdr.Herdr.submit_opening_prompt
OPENING_TEXT = script_prompt("Read ~/rust/hanadocs/dwight/README.md, then run /producer:produce.")
ZERO_PANE_PROCESS_IDS: dict[str, object] = {
    "process_info": {"shell_pid": 0, "foreground_process_group_id": 0, "foreground_processes": []},
}
type Rollout = Literal["clean", "malformed", "malformed-only", "no-timestamp", "partial"]


@contextmanager
def opening_transcript(world: ProducerAdmissionWorld, sent: list[str], *,
                       rollout: Rollout = "clean") -> Generator[Path]:
    client = world.case.client
    path = (world.home / f"projects/producer/{world.identity}.jsonl" if world.kind == "claude"
            else world.home / f"sessions/2026/11/18/rollout-2026-11-18T00-00-00-{world.identity}.jsonl")

    def write_turn(pane: str, text: str) -> None:
        if pane != world.pane:
            raise AssertionError("unexpected opening prompt pane")
        if rollout in ("partial", "no-timestamp") and sent or rollout == "malformed-only" and len(sent) > 1:
            raise AssertionError("the opening prompt was already sent")
        sent.append(text)
        stamp = datetime.now(timezone.utc).isoformat()
        turn: dict[str, object]
        if world.kind == "claude":
            turn = {"type": "user", "timestamp": stamp, "message": {"content": text}}
        else:
            turn = {"type": "response_item", "timestamp": stamp, "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}
        if rollout == "no-timestamp":
            _ = turn.pop("timestamp")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            if rollout == "malformed" or rollout == "malformed-only" and len(sent) == 1:
                _ = stream.write('{"type":broken}\n{"timestamp":"invalid"}\n')
            if rollout != "malformed-only" or len(sent) == 2:
                _ = stream.write(json.dumps(turn) + ("" if rollout == "partial" else "\n"))

    def submit(pane: str, opening: herdr.OpeningPrompt, since: datetime, timeout_ms: int) -> herdr.PromptSubmitOutcome:
        if rollout in ("partial", "malformed-only", "no-timestamp") and not sent:
            write_turn(pane, script_prompt(opening.text))
            return herdr.PromptNotSubmitted("fixture interrupted prompt verification")
        return SUBMIT_OPENING(client, pane, opening, since, timeout_ms)

    with (patch.object(client, "submit_opening_prompt", side_effect=submit),
          patch.object(client, "wait_for_prompt_input", return_value=herdr.PromptInputReady(idle=True)),
          patch.object(client, "composer", return_value=herdr.ComposerInput("")),
          patch.object(herdr, "prompt_recipient_process", return_value=herdr.PromptRecipientUnavailable("fixture")),
          patch.object(herdr, "check_target", return_value=True),
          patch.object(client, "_prompt", side_effect=write_turn)):
        yield path


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
    def test_add_producer_writes_media_rules(self) -> None:
        with producer_first_fixture("claude") as world:
            root = world.folder.parent
            environment = fixture_git_environment(world.case.root)
            _ = subprocess.run(["git", "-C", str(root), "init", "-q"], env=environment, check=True)
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producers": {"dwight": None}}, update=True)
            ignore = world.folder / ".gitignore"
            _ = ignore.write_bytes(b"# Keep this user rule.\r\nprivate.md\r\n")
            with (patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, [])):
                code, _, errors = command("add-producer", "dwight", "--job", "Fixture job")
                self.assertEqual(code, 0, errors)
                first = ignore.read_bytes()
                self.assertEqual(command("add-producer", "dwight", "--job", "Fixture job")[0], 0)
            self.assertEqual(ignore.read_bytes(), first)
            self.assertTrue(first.startswith(b"# Keep this user rule.\r\nprivate.md\r\n"))
            result = subprocess.run(
                ["git", "-C", str(root), "check-ignore",
                 "dwight/nested/shot.PNG", "dwight/nested/demo.mp4", "outside/nested/demo.mp4", "dwight/note.md"],
                env=environment, check=True, text=True, capture_output=True,
            )
            self.assertEqual(result.stdout.splitlines(), ["dwight/nested/shot.PNG", "dwight/nested/demo.mp4"])

    def test_remote_creation_retry_uses_the_saved_name(self) -> None:
        with producer_first_fixture("claude") as world:
            registry = world.case.root / "configuration/models.conf"
            marker = producer_creation.Marker("fixture-creation-machine", "1" * 32)
            _ = write_registry(registry, {"producer_creations": {"dwight": marker.value()}}, update=True)
            with patch.object(remote, "this_machine", return_value="fixture-other-machine"):
                code, output, errors = command("add-producer", "Dwight")
            self.assertEqual((code, output), (2, ""))
            self.assertEqual(errors.splitlines(), [
                "shutdown: the creation record is on fixture-creation-machine. Run agents add-producer dwight there.",
            ])

    def test_create_uses_default_or_override_and_passes_only_its_opening_prompt(self) -> None:
        codex_arguments = ("--model", "codex:gpt-test")
        cases: tuple[tuple[Literal["claude", "codex"], tuple[str, ...], str, str, Rollout], ...] = (
            ("claude", (), "claude:fable:high", "claude3", "clean"),
            ("claude", ("--model", "opus", "--effort", "max"), "claude:opus:max", "claude3", "clean"),
            ("codex", codex_arguments, "codex:gpt-test:high", "~/.codex3", "clean"),
            ("codex", codex_arguments, "codex:gpt-test:high", "~/.codex3", "malformed"),
            ("codex", codex_arguments, "codex:gpt-test:high", "~/.codex3", "malformed-only"),
            ("codex", codex_arguments, "codex:gpt-test:high", "~/.codex3", "no-timestamp"),
            ("codex", codex_arguments, "codex:gpt-test:high", "~/.codex3", "partial"),
        )
        for kind, arguments, expected, account, rollout in cases:
            with self.subTest(arguments=arguments, rollout=rollout), producer_first_fixture(kind) as world:
                registry = world.case.root / "configuration/models.conf"
                _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"},
                                              "producers": {"dwight": None}}, update=True)
                template = world.case.root / "template.md"
                _ = template.write_text("# {name}\n\n{job}\n")
                readme = world.folder / "README.md"
                readme.unlink()
                sent: list[str] = []
                with (patch.object(producer_lifecycle, "TEMPLATE", template),
                      patch.object(roster, "roster", return_value=roster.Roster([], [])),
                      opening_transcript(world, sent, rollout=rollout) as transcript):
                    add = ("add-producer", "dwight", *arguments, "--job", "Plan the next work")
                    if rollout in ("partial", "malformed-only", "no-timestamp"):
                        self.assertIn("fixture interrupted prompt verification", command(*add)[2])
                    if rollout == "partial":
                        code, output, errors = command(*add)
                        self.assertEqual((code, output), (2, ""))
                        self.assertEqual([line for line in errors.splitlines() if line != "unchanged"],
                                         ["shutdown: the first turn is still being written"])
                        self.assertEqual(command("resume", "dwight", "--resend-opening-prompt")[0], 1)
                        self.assertEqual(staff.initial_snapshot().rows, ())
                        self.assertEqual(sent, [OPENING_TEXT])
                        with transcript.open("a", encoding="utf-8") as stream:
                            _ = stream.write("\n")
                    if rollout == "malformed-only":
                        code, _, errors = command("resume", "dwight")
                        self.assertEqual(code, 1, errors)
                        self.assertIn("Check the pane, then run agents resume dwight --resend-opening-prompt", errors)
                        self.assertEqual(sent, [OPENING_TEXT])
                        with patch.object(world.case.client, "composer", return_value=herdr.ComposerInput("draft")):
                            code, _, errors = command("resume", "dwight", "--resend-opening-prompt")
                        self.assertEqual(code, 1, errors)
                        self.assertIn("check that the destination pane is idle and its input is empty", errors)
                        self.assertEqual(sent, [OPENING_TEXT])
                        code, _, errors = command("resume", "dwight", "--resend-opening-prompt")
                        self.assertEqual(code, 0, errors)
                        self.assertEqual(command("resume", "dwight", "--resend-opening-prompt")[0], 1)
                    code, output, errors = command(*add)
                    self.assertEqual(code, 0, errors)
                    self.assertEqual(command(*add), (0, output, ""))
                row = staff.initial_snapshot().by_path("dwight")
                assert row is not None
                self.assertIn(f"staff {row.id}, account {account}, tab {world.pane}", output)
                self.assertEqual(models_registry.parse_registry(registry.read_text())["producers"]["dwight"], expected)
                self.assertEqual(readme.read_text(), "# dwight\n\nPlan the next work\n")
                self.assertEqual(sent, [OPENING_TEXT] * (2 if rollout == "malformed-only" else 1))

    def test_create_retry_reuses_the_profile_readme_and_reserved_tab(self) -> None:
        with producer_first_fixture("claude") as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:sonnet:high"},
                                          "producers": {"dwight": None}}, update=True)
            template = world.case.root / "template.md"
            _ = template.write_text("# {name}\n{job}\n")
            readme = world.folder / "README.md"
            previous = readme.read_bytes(), readme.stat().st_mtime_ns
            sent: list[str] = []
            with (patch.object(producer_lifecycle, "TEMPLATE", template),
                  patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, sent)):
                path = producer_creation.record_path("dwight")
                legacy = {"profile": "claude:sonnet:high", "job": "", "readme": "# dwight", "folder": str(world.folder)}
                producer_creation.write_record(path, legacy)
                before = registry.read_bytes()
                code, _, errors = command("add-producer", "dwight")
                self.assertEqual(code, 2, errors)
                self.assertIn("the creation record has no creation ID", errors)
                self.assertEqual(registry.read_bytes(), before)
                self.assertEqual(producer_creation.read_record(path), legacy)
                path.unlink()
                with patch.object(herdr, "start_agent", return_value=herdr.AgentNotStarted("fixture start failed")):
                    code, _, errors = command("add-producer", "dwight")
                self.assertNotEqual(code, 0)
                self.assertIn("fixture start failed", errors)
                self.assertEqual(staff.initial_snapshot().rows, ())
                source = remote.this_machine()
                profiles = models_registry.parse_registry(registry.read_text())
                saved = producer_creation.read_record(path)
                marker = producer_creation.marker_for(saved)
                self.assertEqual(profiles["producer_creations"], {"dwight": marker.value()})
                with self.assertRaisesRegex(staff.StaffRefused, "already has a creation marker"):
                    _ = staff.producer_creation_registry("claim", "dwight", marker.value(), profiles["producers"]["dwight"])
                for rival in (producer_creation.Marker(source, "1" * 32), producer_creation.Marker("other", marker.creation_id)):
                    with self.subTest(rival=rival):
                        for action in ("claim", "finish"):
                            with self.assertRaisesRegex(staff.StaffRefused, f"the creation record is on {source}"):
                                _ = staff.producer_creation_registry(action, "dwight", rival.value(), profiles["producers"]["dwight"])
                        self.assertEqual(models_registry.parse_registry(registry.read_text()), profiles)
                other_panes = world.case.root / "other-panes"
                with (patch.object(remote, "this_machine", return_value="fixture-other-machine"),
                      patch.object(pane_record, "RECORD_DIR", other_panes),
                      patch.dict(os.environ, {"HERDR_PANE_RECORD_DIR": str(other_panes)})):
                    self.assertFalse(producer_creation.record_path("dwight").exists())
                    code, output, errors = command("resume", "dwight")
                    self.assertEqual((code, output), (1, ""))
                    self.assertEqual(errors.splitlines(), [f"dwight: the creation record is on {source}. Run agents add-producer dwight there."])
                    self.assertEqual(staff.initial_snapshot().rows, ())
                    self.assertEqual(len(world.case.client.opened), 1)
                    self.assertEqual(sent, [])
                readme_copy = readme.rename(readme.with_suffix(".saved"))
                code, _, errors = command("resume", "dwight")
                self.assertEqual(code, 1, errors)
                self.assertIn("producer README is unavailable. Run agents add-producer dwight.", errors)
                self.assertEqual(len(world.case.client.opened), 1)
                _ = readme_copy.rename(readme)
                template.unlink()
                code, _, errors = command("resume", "dwight")
                self.assertEqual(code, 0, errors)
                self.assertEqual(sent, [OPENING_TEXT])
                completed = models_registry.parse_registry(registry.read_text())
                self.assertEqual(completed["producers"], profiles["producers"])
                self.assertNotIn("dwight", completed.get("producer_creations", {}))
                written = registry.read_bytes(), registry.stat().st_mtime_ns
                for name in ("dwight", "Dwight"):
                    with self.subTest(delayed_claim=name), self.assertRaisesRegex(staff.StaffRefused, "a staff row already has this name"):
                        _ = staff.producer_creation_registry("claim", name, f"other:{'2' * 32}", profiles["producers"]["dwight"])
                code, output, errors = command("add-producer", "dwight")
                self.assertEqual(code, 0, errors)
                self.assertIn(world.identity, output)
            self.assertEqual((registry.read_bytes(), registry.stat().st_mtime_ns), written)
            self.assertEqual((readme.read_bytes(), readme.stat().st_mtime_ns), previous)
            self.assertEqual(len(world.case.client.opened), 1)
            self.assertEqual(sent, [OPENING_TEXT])

    def test_create_retry_uses_proof_from_the_existing_pane(self) -> None:
        with producer_first_fixture("claude") as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"},
                                          "producers": {"dwight": None}}, update=True)
            template = world.case.root / "template.md"
            _ = template.write_text("# {name}\n{job}\n")

            def submits_without_confirmation(pane: str, opening: herdr.OpeningPrompt, since: datetime,
                                             timeout_ms: int) -> herdr.PromptNotSubmitted:
                self.assertIsInstance(SUBMIT_OPENING(world.case.client, pane, opening, since, timeout_ms),
                                      herdr.PromptSubmitted)
                return herdr.PromptNotSubmitted("fixture did not confirm the prompt")

            sent: list[str] = []
            with (patch.object(producer_lifecycle, "TEMPLATE", template),
                  patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, sent)):
                with patch.object(world.case.client, "submit_opening_prompt", side_effect=submits_without_confirmation):
                    self.assertIn("did not confirm", command("add-producer", "dwight")[2])
                with patch.object(world.case.client, "submit_opening_prompt",
                                  side_effect=AssertionError("prompt was already present")):
                    code, output, errors = command("add-producer", "dwight")
                self.assertEqual(code, 0, errors)
                self.assertIn(world.identity, output)
                self.assertEqual(sent, [OPENING_TEXT])
                self.assertEqual(len(world.case.client.opened), 1)

    def test_create_retry_keeps_prompt_proof_if_staff_admission_stops(self) -> None:
        with producer_first_fixture("claude") as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"},
                                          "producers": {"dwight": None}}, update=True)
            sent: list[str] = []
            with (patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, sent) as transcript):
                with patch.object(staff, "ensure", side_effect=OSError("fixture interrupted staff admission")):
                    self.assertIn("interrupted staff admission", command("add-producer", "dwight")[2])
                    self.assertEqual(staff.initial_snapshot().rows, ())
                saved = stop_resume.producer_admission_record("dwight")
                self.assertIn(f'"opening_confirmed": "{world.identity}"', saved.read_text())
                transcript.unlink()
                world.active = False
                world.case.launched.discard(world.identity)
                with patch.object(world.case.client, "submit_opening_prompt",
                                  side_effect=AssertionError("the saved mark confirms the prompt")):
                    code, _, errors = command("add-producer", "dwight")
                self.assertEqual(code, 0, errors)
                self.assertEqual(sent, [OPENING_TEXT])
                self.assertEqual(world.arguments[-1][world.arguments[-1].index("--resume") + 1], world.identity)

    def test_create_retry_sends_the_missing_prompt_to_an_admitted_producer(self) -> None:
        with producer_first_fixture("claude") as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"},
                                          "producers": {"dwight": None}}, update=True)
            sent: list[str] = []
            with (patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, sent)):
                with patch.object(world.case.client, "submit_opening_prompt",
                                  return_value=herdr.PromptNotSubmitted("fixture did not send the prompt")):
                    self.assertIn("did not send", command("add-producer", "dwight")[2])
                admitted = stop_resume.adopt_named_producer(
                    stop_resume.ProducerFirstStart("dwight", world.folder), world.identity, world.pane, world.case.client)
                self.assertEqual(sent, [])
                path = producer_creation.record_path("dwight")
                receipt = cast(dict[str, object], json.loads(path.read_text()))
                _ = path.write_text(json.dumps({**receipt, "staff": admitted.session_id,
                                                "account": admitted.account, "tab": admitted.pane_id}))
                result = command("add-producer", "dwight")
                self.assertEqual(result[0], 0, result[2])
                self.assertEqual(command("add-producer", "dwight"), (0, result[1], ""))
                self.assertEqual(sent, [OPENING_TEXT])
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
        for name in ("morty", "Morty"):
            self.assertIn("reserved producer name", command("add-producer", name)[2])
            self.assertFalse(producer_creation.record_path(name).exists())
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
              patch.object(herdr.Herdr, "scan_row_panes", return_value=herdr.RowPaneScan({}, []))):
            with patch.object(herdr.Herdr, "result", side_effect=OSError("fixture process read failed")):
                errors = command("rename", source.name, "beta")[2]
                self.assertIn("cannot read producer panes: producer-pane: fixture process read failed", errors)
                self.assertTrue(producer_lifecycle.pending_rename(source.name))
            with patch.object(herdr.Herdr, "result", return_value=ZERO_PANE_PROCESS_IDS):
                code, _, errors = command("rename", source.name, "beta")
                self.assertEqual(code, 2)
                self.assertIn("cannot read producer panes: producer-pane: expected positive pane process ids", errors)
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

    def test_retirement_records_a_kept_handoff_request(self) -> None:
        row = self.producer("dwight")
        _ = self.producer("morty")
        with patch.object(restart_support, "send_message",
                          return_value=restart_support.MessageKept(kind="kept", reason="in a review")) as send:
            for _ in range(2):
                code, output, errors = command("retire", row.name)
                self.assertEqual((code, errors), (0, ""), output)
                self.assertIn("handoff.md exists", output)
            self.assertEqual(send.call_count, 1)

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
            with patch.object(herdr.Herdr, "result", return_value=ZERO_PANE_PROCESS_IDS):
                code, _, errors = command("retire", row.name, "--to", destination.name)
                self.assertEqual(code, 2)
                self.assertIn("cannot read producer panes: producer-pane: expected positive pane process ids", errors)
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
