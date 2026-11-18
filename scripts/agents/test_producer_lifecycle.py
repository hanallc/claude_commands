"""Test producer creation with private records and a fake herdr."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import patch

from scripts.agents import pane_record, producer_lifecycle, remote, shutdown, stop_resume
from scripts.agents.test_stop_resume import ProducerAdmissionWorld, producer_first_fixture
from scripts.herdr import herdr
from scripts.lib import models_registry
from scripts.lib.script_envelope import script_prompt
from scripts.lib.test_models_registry import write_registry
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.message import roster
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member

SUBMIT_OPENING = herdr.Herdr.submit_opening_prompt
OPENING_TEXT = script_prompt("Read ~/rust/hanadocs/dwight/README.md, then run /producer:produce.")


@contextmanager
def opening_transcript(world: ProducerAdmissionWorld, sent: list[str], *,
                       rollout: Literal["clean", "malformed", "partial"] = "clean") -> Generator[Path]:
    client = world.case.client
    path = (world.home / f"projects/producer/{world.identity}.jsonl" if world.kind == "claude"
            else world.home / f"sessions/2026/11/18/rollout-2026-11-18T00-00-00-{world.identity}.jsonl")

    def write_turn(pane: str, text: str) -> None:
        if pane != world.pane:
            raise AssertionError("unexpected opening prompt pane")
        if rollout == "partial" and sent:
            raise AssertionError("the opening prompt was already sent")
        sent.append(text)
        stamp = datetime.now(timezone.utc).isoformat()
        if world.kind == "claude":
            turn = {"type": "user", "timestamp": stamp, "message": {"content": text}}
        else:
            turn = {"type": "response_item", "timestamp": stamp, "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            if rollout == "malformed":
                _ = stream.write('{"type":broken}\n{"timestamp":"invalid"}\n')
            _ = stream.write(json.dumps(turn) + ("" if rollout == "partial" else "\n"))

    def submit(pane: str, opening: herdr.OpeningPrompt, since: datetime, timeout_ms: int) -> herdr.PromptSubmitOutcome:
        if rollout == "partial" and not sent:
            write_turn(pane, script_prompt(opening.text))
            return herdr.PromptNotSubmitted("fixture interrupted prompt verification")
        return SUBMIT_OPENING(client, pane, opening, since, timeout_ms)

    with (patch.object(client, "submit_opening_prompt", side_effect=submit),
          patch.object(client, "wait_for_prompt_input", return_value=herdr.PromptInputReady(idle=True)),
          patch.object(herdr, "prompt_recipient_process", return_value=herdr.PromptRecipientUnavailable("fixture")),
          patch.object(herdr, "check_target", return_value=True),
          patch.object(client, "_prompt", side_effect=write_turn)):
        yield path


def command(*arguments: str) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = shutdown.main(list(arguments))
    return code, output.getvalue(), errors.getvalue()


class CreateProducerTests(unittest.TestCase):
    def test_create_uses_default_or_override_and_passes_only_its_opening_prompt(self) -> None:
        codex_arguments = ("--model", "codex:gpt-test")
        cases: tuple[tuple[Literal["claude", "codex"], tuple[str, ...], str, str,
                           Literal["clean", "malformed", "partial"]], ...] = (
            ("claude", (), "claude:fable:high", "claude3", "clean"),
            ("claude", ("--model", "opus", "--effort", "max"), "claude:opus:max", "claude3", "clean"),
            ("codex", codex_arguments, "codex:gpt-test:high", "~/.codex3", "clean"),
            ("codex", codex_arguments, "codex:gpt-test:high", "~/.codex3", "malformed"),
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
                    if rollout == "partial":
                        self.assertIn("fixture interrupted prompt verification", command(*add)[2])
                        code, output, errors = command(*add)
                        self.assertEqual((code, output), (2, ""))
                        self.assertEqual([line for line in errors.splitlines() if line != "unchanged"],
                                         ["shutdown: the first turn is still being written"])
                        self.assertEqual(staff.initial_snapshot().rows, ())
                        self.assertEqual(sent, [OPENING_TEXT])
                        with transcript.open("a", encoding="utf-8") as stream:
                            _ = stream.write("\n")
                    code, output, errors = command(*add)
                    self.assertEqual(code, 0, errors)
                    self.assertEqual(command(*add), (0, output, ""))
                row = staff.initial_snapshot().by_path("dwight")
                assert row is not None
                self.assertIn(f"staff {row.id}, account {account}, tab {world.pane}", output)
                self.assertEqual(models_registry.parse_registry(registry.read_text())["producers"]["dwight"], expected)
                self.assertEqual(readme.read_text(), "# dwight\n\nPlan the next work\n")
                self.assertEqual(sent, [OPENING_TEXT])

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
                with patch.object(herdr, "start_agent", return_value=herdr.AgentNotStarted("fixture start failed")):
                    code, _, errors = command("add-producer", "dwight")
                self.assertNotEqual(code, 0)
                self.assertIn("fixture start failed", errors)
                self.assertEqual(staff.initial_snapshot().rows, ())
                written = registry.read_bytes(), registry.stat().st_mtime_ns
                template.unlink()
                code, _, errors = command("resume", "dwight")
                self.assertEqual(code, 0, errors)
                self.assertEqual(sent, [OPENING_TEXT])
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
                path = stop_resume.producer_creation_path("dwight")
                receipt = cast(dict[str, object], json.loads(path.read_text()))
                _ = path.write_text(json.dumps({**receipt, "staff": admitted.session_id,
                                                "account": admitted.account, "tab": admitted.pane_id}))
                result = command("add-producer", "dwight")
                self.assertEqual(result[0], 0, result[2])
                self.assertEqual(command("add-producer", "dwight"), (0, result[1], ""))
                self.assertEqual(sent, [OPENING_TEXT])
                self.assertEqual(len(world.case.client.opened), 1)


class CreateProducerRefusalTests(unittest.TestCase):
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
                                          "producers": {"dwight": "claude:sonnet:high"}})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {**config_environment(self.config),
            "HOME": str(self.root), "STAFF_FILE": str(self.root / "staff.json"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "MODELS_CONFIG_FILE": str(self.registry),
            "MODELS_CONFIG_PUBLISH": "0", "MODELS_CONFIG_TIER_ONLY": "1"})))
        _ = cast(object, self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes")))
        _ = cast(object, self.enterContext(patch.object(roster, "roster", return_value=roster.Roster([], []))))
        _ = cast(object, self.enterContext(patch.object(remote, "local_machine", return_value="natedev")))

    def test_create_refuses_the_staff_row_key_live_name_and_missing_template(self) -> None:
        taken = fixture_member("showrunner", name="taken")
        before = self.registry.read_bytes()
        for name in ("morty", "Morty"):
            self.assertIn("reserved producer name", command("add-producer", name)[2])
            self.assertFalse(stop_resume.producer_creation_path(name).exists())
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
