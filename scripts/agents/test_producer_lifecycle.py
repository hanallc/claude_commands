"""Test producer lifecycle commands with private records and a fake herdr."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.agents import member_account, pane_record, producer_creation, producer_lifecycle, remote, restart, restart_support, shutdown, shutdown_record, stop_resume
from scripts.agents.test_stop_resume import ProducerAdmissionWorld, producer_first_fixture
from scripts.herdr import herdr, pane_launch
from scripts.lib import atomic_file, models_registry, session_move, transcripts
from scripts.lib.script_envelope import script_prompt
from scripts.lib.test_models_registry import write_registry
from scripts.lib.transcript_test_support import compressed_copy
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.message import roster, sessions
from scripts.production import staff, the_list, unit_move, update_registration
from scripts.production.staff_test_support import fixture_legacy_staff_name, fixture_member, fixture_runtime_id

SUBMIT_OPENING = herdr.Herdr.submit_opening_prompt
ZERO_PANE_PROCESS_IDS: dict[str, object] = {
    "process_info": {"shell_pid": 0, "foreground_process_group_id": 0, "foreground_processes": []},
}
type Rollout = Literal["clean", "malformed", "malformed-only", "no-timestamp", "partial"]


def opening_text(world: ProducerAdmissionWorld) -> str:
    return script_prompt(f"Read {world.folder / 'README.md'}, then run /producer:produce.")


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
    def test_remote_creation_retry_names_the_creation_machine(self) -> None:
        with producer_first_fixture("claude", pending_start=False) as world:
            registry = world.case.root / "configuration/models.conf"
            marker = producer_creation.Marker("fixture-creation-machine", "1" * 32)
            _ = write_registry(registry, {"producer_creations": {"fixture-planner": marker.value()}}, update=True)
            with patch.object(remote, "this_machine", return_value="fixture-other-machine"):
                code, output, errors = command("add-producer", "fixture-planner")
            self.assertEqual((code, output), (2, ""))
            self.assertEqual(errors.splitlines(), [
                "shutdown: the creation record is on fixture-creation-machine. Run agents add-producer fixture-planner there.",
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
            with self.subTest(arguments=arguments, rollout=rollout), producer_first_fixture(kind, pending_start=False) as world:
                registry = world.case.root / "configuration/models.conf"
                _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"}}, update=True)
                template = world.case.root / "template.md"
                _ = template.write_text("# {name}\n\n{job}\n")
                readme = world.folder / "README.md"
                readme.unlink()
                sent: list[str] = []
                with (patch.object(producer_lifecycle, "TEMPLATE", template),
                      patch.object(roster, "roster", return_value=roster.Roster([], [])),
                      opening_transcript(world, sent, rollout=rollout) as transcript):
                    add = ("add-producer", "fixture-planner", *arguments, "--job", "Plan the next work")
                    if rollout in ("partial", "malformed-only", "no-timestamp"):
                        self.assertIn("fixture interrupted prompt verification", command(*add)[2])
                    if rollout == "partial":
                        code, output, errors = command(*add)
                        self.assertEqual((code, output), (2, ""))
                        self.assertEqual([line for line in errors.splitlines() if line != "unchanged"],
                                         ["shutdown: the first turn is still being written"])
                        self.assertEqual(command("resume", "fixture-planner", "--resend-opening-prompt")[0], 1)
                        self.assertEqual(staff.initial_snapshot().rows, ())
                        self.assertEqual(sent, [opening_text(world)])
                        with transcript.open("a", encoding="utf-8") as stream:
                            _ = stream.write("\n")
                    if rollout == "malformed-only":
                        code, _, errors = command("resume", "fixture-planner")
                        self.assertEqual(code, 1, errors)
                        self.assertIn("Check the pane, then run agents resume fixture-planner --resend-opening-prompt", errors)
                        self.assertEqual(sent, [opening_text(world)])
                        with patch.object(world.case.client, "composer", return_value=herdr.ComposerInput("draft")):
                            code, _, errors = command("resume", "fixture-planner", "--resend-opening-prompt")
                        self.assertEqual(code, 1, errors)
                        self.assertIn("check that the destination pane is idle and its input is empty", errors)
                        self.assertEqual(sent, [opening_text(world)])
                        code, _, errors = command("resume", "fixture-planner", "--resend-opening-prompt")
                        self.assertEqual(code, 0, errors)
                        self.assertEqual(command("resume", "fixture-planner", "--resend-opening-prompt")[0], 1)
                    code, output, errors = command(*add)
                    self.assertEqual(code, 0, errors)
                    self.assertEqual(command(*add), (0, output, ""))
                row = staff.initial_snapshot().by_path("fixture-planner")
                assert row is not None
                self.assertIn(f"staff {row.id}, account {account}, tab {world.pane}", output)
                self.assertEqual(models_registry.parse_registry(registry.read_text())["producers"][row.id], expected)
                self.assertEqual(readme.read_text(), "# fixture-planner\n\nPlan the next work\n")
                self.assertEqual(sent, [opening_text(world)] * (2 if rollout == "malformed-only" else 1))

    def test_create_retry_reuses_the_profile_readme_and_reserved_tab(self) -> None:
        with producer_first_fixture("claude", pending_start=False) as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:sonnet:high"}}, update=True)
            template = world.case.root / "template.md"
            _ = template.write_text("# {name}\n{job}\n")
            readme = world.folder / "README.md"
            previous = readme.read_bytes(), readme.stat().st_mtime_ns
            sent: list[str] = []
            with (patch.object(producer_lifecycle, "TEMPLATE", template),
                  patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, sent)):
                path = producer_creation.record_path("fixture-planner")
                legacy = {"profile": "claude:sonnet:high", "job": "", "readme": "# fixture-planner", "folder": str(world.folder)}
                producer_creation.write_record(path, legacy)
                before = registry.read_bytes()
                code, _, errors = command("add-producer", "fixture-planner")
                self.assertEqual(code, 2, errors)
                self.assertIn("the creation record has no creation ID", errors)
                self.assertEqual(registry.read_bytes(), before)
                self.assertEqual(producer_creation.read_record(path), legacy)
                path.unlink()
                with patch.object(herdr, "start_agent", return_value=herdr.AgentNotStarted("fixture start failed")):
                    code, _, errors = command("add-producer", "fixture-planner")
                self.assertNotEqual(code, 0)
                self.assertIn("fixture start failed", errors)
                self.assertEqual(staff.initial_snapshot().rows, ())
                source = remote.this_machine()
                profiles = models_registry.parse_registry(registry.read_text())
                saved = producer_creation.read_record(path)
                marker = producer_creation.marker_for(saved)
                self.assertEqual(profiles["producer_creations"], {"fixture-planner": marker.value()})
                self.assertNotIn("fixture-planner", profiles.get("producers", {}))
                self.assertNotIn(world.identity, profiles.get("producers", {}))
                profile = "claude:sonnet:high"
                with self.assertRaisesRegex(staff.StaffRefused, "already has a creation marker"):
                    _ = staff.producer_creation_registry("claim", "fixture-planner", marker.value(), profile)
                for rival in (producer_creation.Marker(source, "1" * 32), producer_creation.Marker("other", marker.creation_id)):
                    with self.subTest(rival=rival):
                        for action in ("claim", "finish"):
                            with self.assertRaisesRegex(staff.StaffRefused, f"the creation record is on {source}"):
                                _ = staff.producer_creation_registry(action, "fixture-planner", rival.value(), profile)
                        self.assertEqual(models_registry.parse_registry(registry.read_text()), profiles)
                other_panes = world.case.root / "other-panes"
                with (patch.object(remote, "this_machine", return_value="fixture-other-machine"),
                      patch.object(pane_record, "RECORD_DIR", other_panes),
                      patch.dict(os.environ, {"HERDR_PANE_RECORD_DIR": str(other_panes)})):
                    self.assertFalse(producer_creation.record_path("fixture-planner").exists())
                    code, output, errors = command("resume", "fixture-planner")
                    self.assertEqual((code, output), (1, ""))
                    self.assertEqual(errors.splitlines(), [f"fixture-planner: the creation record is on {source}. Run agents add-producer fixture-planner there."])
                    self.assertEqual(staff.initial_snapshot().rows, ())
                    self.assertEqual(len(world.case.client.opened), 1)
                    self.assertEqual(sent, [])
                readme_copy = readme.rename(readme.with_suffix(".saved"))
                code, _, errors = command("resume", "fixture-planner")
                self.assertEqual(code, 1, errors)
                self.assertIn("producer README is unavailable. Run agents add-producer fixture-planner.", errors)
                self.assertEqual(len(world.case.client.opened), 1)
                _ = readme_copy.rename(readme)
                template.unlink()
                code, _, errors = command("resume", "fixture-planner")
                self.assertEqual(code, 0, errors)
                self.assertEqual(sent, [opening_text(world)])
                completed = models_registry.parse_registry(registry.read_text())
                self.assertEqual(completed["producers"], {**profiles.get("producers", {}), world.identity: profile})
                self.assertNotIn("fixture-planner", completed.get("producer_creations", {}))
                written = registry.read_bytes(), registry.stat().st_mtime_ns
                with self.assertRaisesRegex(staff.StaffRefused, "a staff row already has this name"):
                    _ = staff.producer_creation_registry("claim", "fixture-planner", f"other:{'2' * 32}", profile)
                code, output, errors = command("add-producer", "fixture-planner")
                self.assertEqual(code, 0, errors)
                self.assertIn(world.identity, output)
            self.assertEqual((registry.read_bytes(), registry.stat().st_mtime_ns), written)
            self.assertEqual((readme.read_bytes(), readme.stat().st_mtime_ns), previous)
            self.assertEqual(len(world.case.client.opened), 1)
            self.assertEqual(sent, [opening_text(world)])

    def test_create_retry_uses_proof_from_the_existing_pane(self) -> None:
        with producer_first_fixture("claude", pending_start=False) as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"}}, update=True)
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
                    self.assertIn("did not confirm", command("add-producer", "fixture-planner")[2])
                with patch.object(world.case.client, "submit_opening_prompt",
                                  side_effect=AssertionError("prompt was already present")):
                    code, output, errors = command("add-producer", "fixture-planner")
                self.assertEqual(code, 0, errors)
                self.assertIn(world.identity, output)
                self.assertEqual(sent, [opening_text(world)])
                self.assertEqual(len(world.case.client.opened), 1)

    def test_create_retry_keeps_prompt_proof_if_staff_admission_stops(self) -> None:
        with producer_first_fixture("claude", pending_start=False) as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"}}, update=True)
            sent: list[str] = []
            with (patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, sent) as transcript):
                with patch.object(staff, "ensure", side_effect=OSError("fixture interrupted staff admission")):
                    self.assertIn("interrupted staff admission", command("add-producer", "fixture-planner")[2])
                    self.assertEqual(staff.initial_snapshot().rows, ())
                saved = stop_resume.producer_admission_record("fixture-planner")
                self.assertIn(f'"opening_confirmed": "{world.identity}"', saved.read_text())
                transcript.unlink()
                world.active = False
                world.case.launched.discard(world.identity)
                with patch.object(world.case.client, "submit_opening_prompt",
                                  side_effect=AssertionError("the saved mark confirms the prompt")):
                    code, _, errors = command("add-producer", "fixture-planner")
                self.assertEqual(code, 0, errors)
                self.assertEqual(sent, [opening_text(world)])
                self.assertEqual(world.arguments[-1][world.arguments[-1].index("--resume") + 1], world.identity)

    def test_create_retries_profile_publication_before_binding_the_pane(self) -> None:
        with producer_first_fixture("claude", pending_start=False) as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"}}, update=True)
            sent: list[str] = []
            with (patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, sent)):
                with (patch.object(producer_creation, "publish_profile", side_effect=OSError("fixture profile write failed")),
                      patch.object(pane_launch, "staff_admitted", wraps=pane_launch.staff_admitted) as bind):
                    self.assertIn("fixture profile write failed", command("add-producer", "fixture-planner")[2])
                    bind.assert_not_called()
                row = staff.initial_snapshot().by_current_session(world.identity)
                assert row is not None
                self.assertTrue(stop_resume.producer_admission_record(row.name).is_file())
                code, _, errors = command("resume", row.name)
                self.assertEqual(code, 0, errors)
                self.assertEqual(models_registry.producer_profile(row.id).value(), "claude:fable:high")
                self.assertTrue(pane_record.read("agents")[world.pane].get("producer_binding", False))
                self.assertEqual(sent, [opening_text(world)])

    def test_create_adopts_the_pane_after_the_reservation_disappears(self) -> None:
        with producer_first_fixture("claude", pending_start=False) as world:
            name = "fixture-planner"
            profile = "claude:fable:high"
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": profile}}, update=True)
            sent: list[str] = []
            with (patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, sent)):
                with patch.object(producer_creation, "publish_profile", side_effect=OSError("fixture profile write failed")):
                    self.assertIn("fixture profile write failed", command("add-producer", name)[2])
                row = staff.initial_snapshot().by_current_session(world.identity)
                assert row is not None
                receipt = producer_creation.read_record(producer_creation.record_path(name))
                self.assertEqual(receipt.get("opening_confirmed"), world.identity)
                self.assertNotIn(row.id, models_registry.parse_registry(registry.read_text()).get("producers", {}))
                reservation = stop_resume.producer_admission_record(name)
                reservation.unlink()
                with patch.object(stop_resume, "producer_conflicts", return_value=[(world.identity, world.pane)]):
                    code, output, errors = command("add-producer", name)
                self.assertEqual(code, 0, errors)
                self.assertIn(f"staff {row.id}, account claude3, tab {world.pane}", output)
                self.assertEqual(models_registry.producer_profile(row.id).value(), profile)
                binding = pane_record.read("agents")[world.pane]
                self.assertEqual(binding.get("staff_id"), row.id)
                self.assertTrue(binding.get("producer_binding", False))
                self.assertEqual(world.started_panes, [world.pane])
                self.assertEqual(sent, [opening_text(world)])
                self.assertFalse(reservation.exists())

    def test_create_retry_sends_the_missing_prompt_to_an_admitted_producer(self) -> None:
        with producer_first_fixture("claude", pending_start=False) as world:
            registry = world.case.root / "configuration/models.conf"
            _ = write_registry(registry, {"producer_defaults": {"model": "claude:fable:high"}}, update=True)
            sent: list[str] = []
            with (patch.object(roster, "roster", return_value=roster.Roster([], [])),
                  opening_transcript(world, sent)):
                with patch.object(world.case.client, "submit_opening_prompt",
                                  return_value=herdr.PromptNotSubmitted("fixture did not send the prompt")):
                    self.assertIn("did not send", command("add-producer", "fixture-planner")[2])
                admitted = stop_resume.adopt_named_producer(
                    stop_resume.ProducerFirstStart("fixture-planner", world.folder), world.identity, world.pane, world.case.client,
                    profile=producer_creation.saved_profile(producer_creation.read_record(producer_creation.record_path("fixture-planner"))))
                self.assertEqual(sent, [])
                path = producer_creation.record_path("fixture-planner")
                receipt = cast(dict[str, object], json.loads(path.read_text()))
                _ = path.write_text(json.dumps({**receipt, "staff": admitted.session_id,
                                                "account": admitted.account, "tab": admitted.pane_id}))
                result = command("add-producer", "fixture-planner")
                self.assertEqual(result[0], 0, result[2])
                self.assertEqual(command("add-producer", "fixture-planner"), (0, result[1], ""))
                self.assertEqual(sent, [opening_text(world)])
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
        _ = write_registry(self.registry, {"producer_defaults": {"model": "claude:sonnet:high"}})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {**config_environment(self.config),
            "HOME": str(self.root), "STAFF_FILE": str(self.root / "staff.json"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "MODELS_CONFIG_FILE": str(self.registry),
            "MODELS_CONFIG_PUBLISH": "0", "MODELS_CONFIG_TIER_ONLY": "1"})))
        _ = cast(object, self.enterContext(patch.object(pane_record, "RECORD_DIR", self.root / "panes")))
        _ = cast(object, self.enterContext(patch.object(roster, "roster", return_value=roster.Roster([], []))))
        _ = cast(object, self.enterContext(patch.object(restart_support, "send_message",
            side_effect=AssertionError("fixture did not declare a message"))))
        _ = cast(object, self.enterContext(patch.object(remote, "local_machine", return_value="natedev")))
        _ = cast(object, self.enterContext(patch.object(remote, "this_machine", return_value="natedev")))

    def profile(self, name: str) -> str | None:
        return models_registry.parse_registry(self.registry.read_text()).get("producers", {}).get(name)

    def producer(self, name: str) -> staff.StaffRow:
        folder = self.root / "documents" / name
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / "README.md").write_text(f"# {name}\n")
        row = fixture_member("producer", name=name, doc=staff.LiveDoc("natedev", folder),
                             worktree=staff.PresentWorktree("natedev", folder))
        _ = write_registry(self.registry, {"producers": {row.id: "claude:sonnet:high"}}, update=True)
        return row

    def list_owner(self) -> staff.StaffRow:
        row = self.producer("fixture-keeper")
        _ = write_config_fixture(self.config / "console.conf", f"page.list.owner={row.id}\n")
        return row

    def test_create_refuses_the_staff_row_live_name_and_missing_template(self) -> None:
        taken = fixture_member("showrunner", name="taken")
        before = self.registry.read_bytes()
        self.assertIn("staff row", command("add-producer", taken.name)[2])
        live = roster.RosterEntry("natedev", "claude", "freestanding", "live", "running",
            roster.Messageable("session:live"), "", roster.NoShowrunner(), roster.NoDirector(), roster.NotAWorker())
        with patch.object(roster, "roster", return_value=roster.Roster([live], [])):
            self.assertIn("live session", command("add-producer", "live")[2])
        missing = self.root / "absent-template"
        with patch.object(producer_lifecycle, "TEMPLATE", missing):
            self.assertIn(str(missing), command("add-producer", "new")[2])
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse((self.root / "documents").exists())

    def rename_fixture(self, name: str) -> tuple[staff.StaffRow, Path]:
        row = self.producer(name)
        folder = self.root / "documents" / name
        transcript = self.root / ".claude3/projects" / transcripts.claude_project_key(folder) / f"{row.current_session}.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text('{"type":"user","message":{"content":"old turn"}}\n')
        return row, transcript

    def resume_turns(self, row: staff.StaffRow, kind: Literal["claude", "codex"], home: Path,
                     transcript: Path) -> None:
        assert isinstance(row.worktree, staff.PresentWorktree)
        folder = row.worktree.path
        account = (accounts.NamedAccount("claude3", home) if kind == "claude"
                   else accounts.CodexWorkersAccount("codex3", home))

        def prepare(_identity: str, cwd: str, environment: dict[str, str],
                    _removals: frozenset[str]) -> herdr.PreparedAgentEnvironment:
            return herdr.PreparedAgentEnvironment(Path(cwd), environment)

        def launch(_server: str, _target: object, prepared: herdr.PreparedAgentEnvironment,
                   backend: str, arguments: list[str], *_args: object, **_kwargs: object) -> herdr.AgentLaunched:
            self.assertEqual((backend, prepared.directory), (kind, folder))
            flag = "--resume" if kind == "claude" else "resume"
            self.assertEqual(arguments[arguments.index(flag) + 1], row.current_session)
            self.assertIn("old turn", transcript.read_text())
            with transcript.open("a", encoding="utf-8") as output:
                _ = output.write('{"type":"user","message":{"content":"new turn"}}\n')
            return herdr.AgentLaunched("w1:p0", herdr.PromptSubmissionNotRequired())

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(herdr.Herdr, "panes", return_value=[]),
              patch.object(herdr.Herdr, "producer_row_tab", return_value=herdr.NewTab("producers", row.name)),
              patch.object(herdr.Herdr, "remember_row_tab"),
              patch.object(pane_record, "publication_for_session", return_value=None),
              patch.object(member_account, "resume_account", return_value=account),
              patch.object(herdr, "prepare_session_environment", side_effect=prepare),
              patch.object(herdr, "launch_prepared_agent", side_effect=launch)):
            self.assertEqual(restart.start_row(row, staff.initial_snapshot(), herdr.Herdr("fixture")), "w1:p0")
        self.assertEqual(
            [json.loads(line)["message"]["content"] for line in transcript.read_text().splitlines()],
            ["old turn", "new turn"])

    def test_rename_moves_the_folder_and_resumes_its_archived_claude_transcript(self) -> None:
        row, transcript = self.rename_fixture("fixture-old")
        archived = compressed_copy(transcript)
        sessions_dir = transcript.with_name(str(row.current_session))
        sessions_dir.mkdir()
        _ = (sessions_dir / "state.json").write_text("{}")
        shared = self.root / ".claude4"
        shared.mkdir()
        (shared / "projects").symlink_to(transcript.parent.parent, target_is_directory=True)
        alias_root = self.root / ".claude5/projects"
        alias_root.mkdir(parents=True)
        (alias_root / transcript.parent.name).symlink_to(transcript.parent, target_is_directory=True)
        other = transcript.parent.parent / "another-project"
        other.mkdir()
        duplicate = other / transcript.name
        _ = duplicate.write_text("other conversation copy")
        link = other / archived.name
        link.symlink_to(archived)
        vault = self.root / "documents"
        source, destination = vault / row.name, vault / "fixture-new"
        doc = staff.LiveDoc("natedev", source / "missing.md")
        row = staff.set_doc(row.id, doc)
        ignore_rules = b"*.png\nhanadocs/fixture-old/media/\n"
        _ = (source / ".gitignore").write_bytes(ignore_rules)
        references = vault / "links.md"
        _ = references.write_text(
            "hanadocs/fixture-old/a hanadocs/fixture-old/b\n" +
            "![[fixture-old/note#part|Example]] [note](fixture-old/note%20title.md#part) fixture-old\n")
        workspace = vault / ".obsidian/workspace.json"
        workspace.parent.mkdir()
        workspace_bytes = b'{"lastOpenFiles":["hanadocs/fixture-old/README.md"],"file":"hanadocs/fixture-old/notes.md"}\n'
        _ = workspace.write_bytes(workspace_bytes)
        creation = {"staff": row.id, "opening_confirmed": row.current_session}
        producer_creation.write_record(producer_creation.record_path(row.name), creation)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            code, output, errors = command("rename", row.name, "fixture-new")
        self.assertEqual(code, 0, errors)
        self.assertIn("changed 2 vault line(s)", output)
        moved = staff.by_id(row.id)
        assert moved is not None
        self.assertEqual(moved, replace(row, name="fixture-new", doc=replace(doc, path=destination / "missing.md"),
                                       worktree=staff.PresentWorktree("natedev", destination)))
        self.assertFalse(source.exists())
        self.assertEqual((destination / ".gitignore").read_bytes(), ignore_rules)
        self.assertIn(f"{destination / '.gitignore'}:2: old producer path remains", output)
        self.assertEqual(references.read_text(),
            "hanadocs/fixture-new/a hanadocs/fixture-new/b\n" +
            "![[fixture-new/note#part|Example]] [note](fixture-new/note%20title.md#part) fixture-old\n")
        self.assertEqual(workspace.read_bytes(), workspace_bytes)
        self.assertIn(f"{workspace}:1: old producer path remains", output)
        project = transcript.parent.parent / transcripts.claude_project_key(destination)
        self.assertEqual(link.resolve(), project / archived.name)
        self.assertEqual((project / sessions_dir.name / "state.json").read_text(), "{}")
        self.assertEqual(duplicate.read_text(), "other conversation copy")
        self.assertFalse(producer_creation.record_path(row.name).exists())
        receipt = producer_creation.record_path(row.name).with_name(f".producer-created-{row.id}")
        self.assertEqual(producer_creation.read_record(receipt), creation)
        self.assertTrue((shared / "projects" / project.name / archived.name).is_file())
        self.assertTrue((alias_root / project.name / archived.name).is_file())
        self.resume_turns(moved, "claude", self.root / ".claude3", project / transcript.name)
        self.assertEqual(self.profile(moved.id), "claude:sonnet:high")

    def test_rename_keeps_the_codex_thread_in_its_store(self) -> None:
        row = self.producer("fixture-codex-old")
        _ = write_registry(self.registry, {"producers": {row.id: "codex:gpt-test:high"}}, update=True)
        home = self.root / ".codex3"
        transcript = home / "sessions" / f"rollout-fixture-{row.current_session}.jsonl"
        atomic_file.write_text_atomic(transcript, '{"type":"user","message":{"content":"old turn"}}\n')
        before = transcript.stat().st_ino
        with (patch.object(pane_launch, "current_kind", return_value="codex"),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            code, _, errors = command("rename", row.name, "fixture-codex-new")
        self.assertEqual(code, 0, errors)
        moved = staff.by_id(row.id)
        assert moved is not None
        self.assertEqual(moved.current_session, row.current_session)
        self.assertEqual(moved.worktree, staff.PresentWorktree("natedev", self.root / "documents/fixture-codex-new"))
        self.assertEqual(transcript.stat().st_ino, before)
        self.resume_turns(moved, "codex", home, transcript)

    def test_same_project_key_requires_the_transcript_after_claim(self) -> None:
        row, transcript = self.rename_fixture("fixture-same")
        source = self.root / "documents/fixture.same"
        assert isinstance(row.worktree, staff.PresentWorktree)
        _ = row.worktree.path.rename(source)
        row = staff.set_worktree(row.id, replace(row.worktree, path=source), expected=row.worktree)
        row = staff.set_doc(row.id, staff.LiveDoc("natedev", source))
        row = fixture_legacy_staff_name(row, source.name)
        reserve = staff.reserve_producer_rename

        def remove_transcript(plan: staff.ProducerRename) -> None:
            reserve(plan)
            transcript.unlink()

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(staff, "reserve_producer_rename", side_effect=remove_transcript)):
            code, _, errors = command("rename", row.name, "fixture-same")
        self.assertEqual(code, 2)
        self.assertIn("current Claude transcript changed", errors)
        self.assertFalse(producer_lifecycle.pending_rename(row.name))
        staff.require_no_producer_rename(row.name)
        self.assertEqual(staff.by_id(row.id), row)
        assert isinstance(row.worktree, staff.PresentWorktree)
        with self.assertRaisesRegex(FileNotFoundError, "absent from its project"):
            transcripts.prepare_claude_resume(self.root / ".claude3", row.worktree.path, row.current_session)

    def test_producer_commands_refuse_unsafe_new_names_before_changes(self) -> None:
        row, transcript = self.rename_fixture("fixture-old")
        before = transcript.read_bytes()
        for name in ("Fixture-new", "fixture_name", 'fixture"new', "fixture|new", "fixture new"):
            with self.subTest(name=name):
                for arguments in (("add-producer", name), ("rename", row.name, name)):
                    code, output, errors = command(*arguments)
                    self.assertEqual((code, output), (2, ""))
                    self.assertIn("use lower-case letters, digits, and hyphens", errors)
                self.assertFalse(producer_creation.record_path(name).exists())
                self.assertFalse((self.root / "documents" / name).exists())
        self.assertFalse(producer_lifecycle.pending_rename(row.name))
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(transcript.read_bytes(), before)

    def test_confirmed_creation_refusal_clears_its_new_lookup(self) -> None:
        name = "fixture-refused-creation"
        with patch.object(producer_creation, "claim", side_effect=staff.StaffRefused("fixture refused the claim")):
            self.assertIn("refused the claim", command("add-producer", name)[2])
        self.assertFalse(producer_creation.record_path(name).exists())
        with patch.object(producer_creation, "claim", side_effect=staff.StaffUnreadable("fixture lost the reply")):
            self.assertIn("lost the reply", command("add-producer", name)[2])
        self.assertTrue(producer_creation.record_path(name).is_file())

    def test_rename_retries_each_interrupted_write(self) -> None:
        reserve, commit = staff.reserve_producer_rename, staff.rename
        retain = producer_creation.retain_completed_record
        write = producer_lifecycle._write  # pyright: ignore[reportPrivateUsage]
        sync = atomic_file.sync_folder
        rename_exclusive = session_move.rename_exclusive
        vault = self.root / "documents"
        for stage in ("claim", "started", "transcript", "receipt", "vault", "row", "completion"):
            with self.subTest(stage=stage), ExitStack() as stack:
                old, new = f"fixture-{stage}-old", f"fixture-{stage}-new"
                row, transcript = self.rename_fixture(old)
                session = transcript.with_name(str(row.current_session))
                session.mkdir()
                references = vault / f"{stage}.md"
                before, after = f"hanadocs/{old}/README.md\n", f"hanadocs/{new}/README.md\n"
                _ = references.write_text(before)
                creation = {"staff": row.id, "opening_confirmed": row.current_session}
                producer_creation.write_record(producer_creation.record_path(old), creation)

                def claim(plan: staff.ProducerRename) -> None:
                    reserve(plan)
                    raise OSError("fixture interrupted after claim")

                def sync_write(folder: Path) -> None:
                    sync(folder)
                    if stage == "vault" and folder == vault and references.read_text() == after:
                        raise OSError("fixture interrupted after vault")

                def move(source_parent: int, source: str, target_parent: int, target: str) -> None:
                    rename_exclusive(source_parent, source, target_parent, target)
                    if source == transcript.name:
                        raise OSError("fixture interrupted after transcript")

                def receipt(name: str, member: staff.StaffRow, expected: dict[str, object]) -> None:
                    retain(name, member, expected)
                    raise OSError("fixture interrupted after receipt")

                def row_write(identity: staff.StaffId, name: str, *, operation: str = "") -> staff.StaffRow:
                    _ = commit(identity, name, operation=operation)
                    raise OSError("fixture interrupted after row")

                def fail_journal_sync(path: Path, values: Mapping[str, object]) -> None:
                    boundary = ("completion" if path.name.startswith(".producer-renamed-") else
                                "started" if path.name.startswith(".producer-rename-") and values.get("started") is True
                                else "")
                    if stage != boundary:
                        write(path, values)
                        return
                    with patch.object(atomic_file, "sync_folder", side_effect=OSError(f"fixture interrupted after {stage}")):
                        write(path, values)

                faults = {
                    "claim": patch.object(staff, "reserve_producer_rename", side_effect=claim),
                    "started": patch.object(producer_lifecycle, "_write", side_effect=fail_journal_sync),
                    "transcript": patch.object(session_move, "rename_exclusive", side_effect=move),
                    "receipt": patch.object(producer_creation, "retain_completed_record", side_effect=receipt),
                    "vault": patch.object(atomic_file, "sync_folder", side_effect=sync_write),
                    "row": patch.object(staff, "rename", side_effect=row_write),
                    "completion": patch.object(producer_lifecycle, "_write", side_effect=fail_journal_sync),
                }
                _ = stack.enter_context(patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()))
                _ = stack.enter_context(patch.object(shutdown, "rename_repair_displays", return_value=True))
                with faults[stage]:
                    self.assertIn(f"interrupted after {stage}", command("rename", old, new)[2])
                self.assertIn("finish the producer rename", command("add-producer", old)[2])
                if stage in ("receipt", "vault", "row", "completion"):
                    self.assertFalse(producer_creation.record_path(old).exists())
                if stage == "transcript":
                    self.assertTrue(session.is_dir())
                if stage in ("started", "completion"):
                    with patch.object(atomic_file, "sync_folder", side_effect=OSError("fixture journal not durable")):
                        self.assertIn("fixture journal not durable", command("rename", old, new)[2])
                    self.assertIn("finish the producer rename", command("resume", row.id)[2])
                    if stage == "started":
                        self.assertTrue(transcript.is_file())
                        self.assertTrue((vault / old).is_dir())
                        self.assertFalse((vault / new).exists())
                if stage == "receipt":
                    saved = producer_creation.record_path(old).with_name(f".producer-created-{row.id}")
                    producer_creation.write_record(saved, {**creation, "extra": "changed evidence"})
                    self.assertIn("completed creation evidence differs", command("rename", old, new)[2])
                    producer_creation.write_record(saved, creation)
                code, output, errors = command("rename", old, new)
                self.assertEqual(code, 0, errors)
                self.assertIn("changed 1 vault line(s)", output)
                self.assertEqual(references.read_text(), after)
                moved = staff.by_id(row.id)
                assert moved is not None
                self.assertEqual((moved.name, moved.current_session), (new, row.current_session))
                staff.require_no_producer_rename(old)

    def test_rename_refuses_a_transcript_destination_created_during_the_move(self) -> None:
        row, transcript = self.rename_fixture("fixture-old")
        old_content = transcript.read_bytes()
        new = "fixture-new"
        destination = transcript.parent.parent / transcripts.claude_project_key(self.root / "documents" / new) / transcript.name
        rename_exclusive = session_move.rename_exclusive

        def occupy_destination(source_parent: int, source: str, target_parent: int, target: str) -> None:
            if source == transcript.name:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=target_parent)
                with os.fdopen(fd, "wb") as stream:
                    _ = stream.write(b"other transcript")
            rename_exclusive(source_parent, source, target_parent, target)

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            with patch.object(session_move, "rename_exclusive", side_effect=occupy_destination):
                code, _, errors = command("rename", row.name, new)
            self.assertEqual(code, 2, errors)
            self.assertIn("File exists", errors)
            self.assertEqual(transcript.read_bytes(), old_content)
            self.assertEqual(destination.read_bytes(), b"other transcript")
            self.assertEqual(staff.by_id(row.id), row)
            self.assertIn("finish the producer rename", command("resume", row.name)[2])
            destination.unlink()
            code, _, errors = command("rename", row.name, new)
        self.assertEqual(code, 0, errors)
        self.assertEqual(destination.read_bytes(), old_content)

    def test_rename_syncs_the_project_directory_entry_before_moving_its_transcript(self) -> None:
        row, transcript = self.rename_fixture("fixture-old")
        new = "fixture-new"
        projects = transcript.parent.parent
        new_project = projects / transcripts.claude_project_key(self.root / "documents" / new)
        sync = atomic_file.sync_folder
        rename_exclusive = session_move.rename_exclusive

        def fail_project_sync(folder: Path) -> None:
            if folder == projects and new_project.is_dir():
                raise OSError("fixture interrupted before project directory sync")
            sync(folder)

        directory_synced = False

        def record_project_sync(folder: Path) -> None:
            nonlocal directory_synced
            sync(folder)
            if folder == projects and new_project.is_dir():
                directory_synced = True

        def move(source_parent: int, source: str, target_parent: int, target: str) -> None:
            if source == transcript.name:
                self.assertTrue(directory_synced)
            rename_exclusive(source_parent, source, target_parent, target)

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            with patch.object(atomic_file, "sync_folder", side_effect=fail_project_sync):
                code, _, errors = command("rename", row.name, new)
            self.assertEqual(code, 2, errors)
            self.assertIn("interrupted before project directory sync", errors)
            self.assertTrue(new_project.is_dir())
            self.assertTrue(transcript.is_file())
            self.assertFalse((new_project / transcript.name).exists())
            with (patch.object(atomic_file, "sync_folder", side_effect=record_project_sync),
                  patch.object(session_move, "rename_exclusive", side_effect=move)):
                code, _, errors = command("rename", row.name, new)
        self.assertEqual(code, 0, errors)
        self.assertFalse(transcript.exists())
        self.assertTrue((new_project / transcript.name).is_file())

    def test_rename_releases_a_claim_when_a_conflict_appears_during_preflight(self) -> None:
        row, transcript = self.rename_fixture("fixture-old")
        new = "fixture-new"
        destination = self.root / "documents" / new
        reserve = staff.reserve_producer_rename

        def claim(plan: staff.ProducerRename) -> None:
            reserve(plan)
            _ = destination.write_text("competing file")

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True),
              patch.object(pane_launch, "current_kind", return_value="claude") as backend):
            with (patch.object(staff, "reserve_producer_rename", side_effect=claim),
                  patch.object(session_move, "rename_exclusive") as move):
                code, _, errors = command("rename", row.name, new)
            self.assertEqual(code, 2, errors)
            self.assertIn("folder move state changed", errors)
            move.assert_not_called()
            self.assertEqual(destination.read_text(), "competing file")
            self.assertTrue(transcript.is_file())
            self.assertEqual(staff.by_id(row.id), row)
            self.assertFalse(producer_lifecycle.pending_rename(row.name))
            staff.require_no_producer_rename(row.id)
            destination.unlink()
            code, _, errors = command("rename", row.name, new)
        self.assertEqual(code, 0, errors)
        self.assertEqual(backend.call_count, 2)
        self.assertTrue(destination.is_dir())

    def test_rename_refuses_a_file_created_at_the_link_publication_boundary(self) -> None:
        row, transcript = self.rename_fixture("fixture-old")
        alias_projects = self.root / ".claude4/projects"
        alias_projects.mkdir(parents=True)
        (alias_projects / transcript.parent.name).symlink_to(transcript.parent)
        new = "fixture-new"
        new_project = transcripts.claude_project_key(self.root / "documents" / new)
        destination = alias_projects / new_project
        symlink = os.symlink

        def occupy_destination(target: str, name: str, *, dir_fd: int) -> None:
            self.assertEqual(name, destination.name)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
            with os.fdopen(fd, "wb") as stream:
                _ = stream.write(b"competing file")
            symlink(target, name, dir_fd=dir_fd)

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            with patch.object(os, "symlink", side_effect=occupy_destination):
                code, _, errors = command("rename", row.name, new)
            self.assertEqual(code, 2, errors)
            self.assertIn("File exists", errors)
            self.assertEqual(destination.read_bytes(), b"competing file")
            self.assertEqual(len(list(alias_projects.glob(".producer-link-*"))), 1)
            self.assertIn("finish the producer rename", command("resume", row.id)[2])
            destination.unlink()
            code, _, errors = command("rename", row.name, new)
        self.assertEqual(code, 0, errors)
        self.assertEqual(destination.resolve(), transcript.parent.parent / new_project)
        self.assertFalse(list(alias_projects.glob(".producer-link-*")))

    def test_rename_refuses_a_vault_edit_during_replacement_preparation(self) -> None:
        row, _ = self.rename_fixture("fixture-old")
        note = self.root / "documents/links.md"
        before = "[Note](fixture-old/notes.md)\n"
        competing = before + "Another edit.\n"
        _ = note.write_text(before)
        prepare = atomic_file.prepared_replacement

        @contextmanager
        def edit_during_prepare(path: Path, content: str | bytes, *, mode: int) -> Generator[Path]:
            with prepare(path, content, mode=mode) as temporary:
                if path == note:
                    _ = note.write_text(competing)
                yield temporary

        new = "fixture-new"
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            for _attempt in range(2):
                with patch.object(atomic_file, "prepared_replacement", side_effect=edit_during_prepare):
                    code, _, errors = command("rename", row.name, new)
                self.assertEqual(code, 2, errors)
                self.assertIn("vault file changed during rename", errors)
                self.assertEqual(note.read_text(), competing)
                self.assertIn("finish the producer rename", command("resume", row.id)[2])
                _ = note.write_text(before)
            code, output, errors = command("rename", row.name, new)
        self.assertEqual(code, 0, errors)
        self.assertIn("changed 1 vault line(s)", output)
        self.assertEqual(note.read_text(), "[Note](fixture-new/notes.md)\n")

    def test_rename_syncs_new_journal_ancestors_before_claim(self) -> None:
        row, _ = self.rename_fixture("fixture-old")
        vault = self.root / "documents"
        journal_directory = self.root.resolve() / "new-state/nested/panes"
        reserve = staff.reserve_producer_rename
        sync = atomic_file.sync_folder
        synced: list[Path] = []

        def interrupt_parent_sync(folder: Path) -> None:
            if folder == journal_directory.parent:
                raise OSError("fixture interrupted directory sync")
            sync(folder)

        def claim(plan: staff.ProducerRename) -> None:
            self.assertTrue(set(journal_directory.parents).issubset(synced))
            reserve(plan)

        def record_sync(folder: Path) -> None:
            sync(folder)
            synced.append(folder)

        with (patch.object(pane_record, "RECORD_DIR", journal_directory),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True),
              patch.object(staff, "reserve_producer_rename", side_effect=claim) as reservation):
            with patch.object(atomic_file, "sync_folder", side_effect=interrupt_parent_sync):
                code, _, errors = command("rename", row.name, "fixture-new")
            self.assertEqual(code, 2, errors)
            self.assertIn("interrupted directory sync", errors)
            reservation.assert_not_called()
            self.assertTrue((vault / row.name).is_dir())
            self.assertTrue((journal_directory / f".producer-rename-{row.name}").is_file())
            with patch.object(atomic_file, "sync_folder", side_effect=record_sync):
                code, _, errors = command("rename", row.name, "fixture-new")
            self.assertEqual(code, 0, errors)
            reservation.assert_called_once()

    def test_rename_resolves_its_own_document_alias(self) -> None:
        row, _ = self.rename_fixture("fixture-old")
        vault = self.root / "documents"
        alias = self.root / "document-alias"
        alias.symlink_to(vault / row.name, target_is_directory=True)
        doc = staff.LiveDoc("natedev", alias / "README.md")
        row = staff.set_doc(row.id, doc)
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            code, _, errors = command("rename", row.name, "fixture-new")
        self.assertEqual(code, 0, errors)
        moved = staff.by_id(row.id)
        assert moved is not None
        self.assertEqual(moved.doc, replace(doc, path=vault / "fixture-new/README.md"))
        self.assertFalse(alias.exists())

    def test_rename_refuses_its_document_alias_that_leaves_the_folder(self) -> None:
        row, _ = self.rename_fixture("fixture-old")
        vault = self.root / "documents"
        outside = self.root / "current.md"
        _ = outside.write_text("current")
        (vault / row.name / "exit").symlink_to(outside)
        alias = self.root / "document-alias"
        alias.symlink_to(vault / row.name / "exit")
        row = staff.set_doc(row.id, staff.LiveDoc("natedev", alias))
        with patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()):
            code, _, errors = command("rename", row.name, "fixture-new")
        self.assertEqual(code, 2, errors)
        self.assertIn("the producer document path leaves the moved folder", errors)
        self.assertEqual(alias.read_text(), "current")
        self.assertEqual(staff.by_id(row.id), row)
        staff.require_no_producer_rename(row.id)

    def test_rename_preserves_markdown_destination_and_title_forms(self) -> None:
        row, _ = self.rename_fixture("fixture-old")
        note = self.root / "documents/links.md"
        links = (
            "[Note](fixture-old/note(a(b(c))).md)",
            '[Note](<fixture-old/a note).md#part> "double")',
            "[Note](fixture-old/note.md 'single')",
            r"[Escaped](fixture-old/note\(draft\).md 'a\'b')",
            r"[Note](fixture-old/note.md (escaped \(title\)))",
            '[Note](fixture-old/note.md "a\r\nb")',
            "[**[label]**](\n fixture-old/note.md 'title'\n)",
            '[Read <span title="]">this</span>](fixture-old/note.md)',
            "[outer [inner](other.md)](fixture-old/note.md)",
            "Text ](fixture-old/note(open.md)",
        )
        _ = note.write_bytes("\n".join(links).encode())
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            code, output, errors = command("rename", row.name, "fixture-new")
        self.assertEqual(code, 0, errors)
        self.assertIn("changed 10 vault line(s)", output)
        expected = tuple(link.replace("fixture-old/", "fixture-new/") for link in links)
        self.assertEqual(note.read_bytes(), "\n".join(expected).encode())

    def test_rename_respects_markdown_code_boundaries(self) -> None:
        row, _ = self.rename_fixture("fixture-old")
        note = self.root / "documents/links.md"
        rewritable = (
            "[See `]`](fixture-old/note.md)",
            "[See ``[`]``](fixture-old/note.md)",
            "[See `one\r\n]`](fixture-old/note.md)",
            "An unmatched ` precedes [a link](fixture-old/note.md).",
            "A paragraph continues on the next line.\n    [Continuation](fixture-old/note.md)",
            "> [See `]`](fixture-old/note.md)",
            "- [See `]`](fixture-old/note.md)",
            "[[fixture-old/note|Example]] `[[fixture-old/code]]`",
        )
        literal = (
            "`[Example](fixture-old/note.md)`",
            "``[Example](fixture-old/note.md) with ` inside``",
            "[Example `continues](fixture-old/note.md`)",
            "```md\n[Backtick fence](fixture-old/note.md) [[fixture-old/note]] hanadocs/fixture-old/note.md\n```",
            "   ~~~~md\n[Tilde fence](fixture-old/note.md)\n~~~\n[Still fenced](fixture-old/note.md)\n~~~~~",
            "    [Indented](fixture-old/note.md) [[fixture-old/note]]\n\n\t[Tab indented](fixture-old/note.md)",
            "# Heading\n    [Indented after heading](fixture-old/note.md)",
            "> ```\n> [Quoted fence](fixture-old/note.md)\n> ```",
            "- ~~~\n  [List fence](fixture-old/note.md)\n  ~~~",
            "- Paragraph.\n\n      [Indented in a list](fixture-old/note.md)",
        )
        _ = note.write_bytes("\n\n".join((*rewritable, *literal)).encode())
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            code, output, errors = command("rename", row.name, "fixture-new")
        self.assertEqual(code, 0, errors)
        self.assertIn("changed 8 vault line(s)", output)
        expected = tuple(link.replace("fixture-old/", "fixture-new/", 1) for link in rewritable)
        self.assertEqual(note.read_bytes(), "\n\n".join((*expected, *literal)).encode())

    def test_rename_final_scan_reports_remaining_paths_and_completes(self) -> None:
        row, _ = self.rename_fixture("fixture-old")
        vault = self.root / "documents"
        note, leftover = vault / "links.md", vault / "added.md"
        link = "[Note](fixture-old/note(a).md 'a title')\n"
        remaining = link + "`[[fixture-old/example]]`\npath /fixture-old/fixture-old\nother-fixture-old/example\nfixture-old"
        _ = note.write_text(link)
        publish = producer_lifecycle._publish_vault_edit  # pyright: ignore[reportPrivateUsage]

        def add_link(path: Path, before: str, after: str) -> None:
            publish(path, before, after)
            _ = leftover.write_text(remaining)

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            with patch.object(producer_lifecycle, "_publish_vault_edit", side_effect=add_link):
                code, output, errors = command("rename", row.name, "fixture-new")
        self.assertEqual(code, 0, errors)
        self.assertIn("changed 1 vault line(s)", output)
        self.assertEqual(output.splitlines()[1:], [f"{leftover}:{line}: old producer path remains" for line in (1, 2, 3, 3)])
        self.assertEqual(leftover.read_text(), remaining)
        self.assertEqual(note.read_text(), link.replace("fixture-old/", "fixture-new/"))
        staff.require_no_producer_rename(row.id)

    def test_rename_refuses_backend_mismatch_and_missing_exact_project(self) -> None:
        row, transcript = self.rename_fixture("fixture-old")
        before = self.registry.read_bytes()
        with patch.object(pane_launch, "current_kind", return_value="codex"):
            code, _, errors = command("rename", row.name, "fixture-new")
        self.assertEqual(code, 2)
        self.assertIn("launch profile uses claude, native session uses codex", errors)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(staff.by_id(row.id), row)
        other = transcript.parent.parent / "another-project"
        other.mkdir()
        _ = transcript.rename(other / transcript.name)
        with patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()):
            self.assertIn("current Claude transcript is absent from its project", command("rename", row.name, "fixture-new")[2])
        self.assertFalse(producer_lifecycle.pending_rename(row.name))

    def test_unit_move_refuses_both_producer_folder_endpoints(self) -> None:
        root = self.root / "documents"
        runner = fixture_member("showrunner", name="fixture-runner")
        for source, destination in ((root / "unit-src", self.root / "other"),
                                    (self.root / "unit-src", root / "other")):
            unit = fixture_member("unit", name="fixture-unit-" + source.parent.name, parent=runner.id,
                                  worktree=staff.PresentWorktree("natedev", source))
            with self.assertRaisesRegex(unit_move.RenameRefused, "cannot use the producer folder root"):
                _ = unit_move.move_folder(unit.id, destination)

    def test_completed_retry_skips_the_stopped_session_check(self) -> None:
        row, transcript = self.rename_fixture("fixture-old")
        release = staff.release_producer_rename

        def fail_after_release(operation: str) -> None:
            release(operation)
            raise OSError("fixture interrupted lookup removal")

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True),
              patch.object(staff, "release_producer_rename", side_effect=fail_after_release)):
            self.assertIn("interrupted lookup removal", command("rename", row.name, "fixture-new")[2])
        moved = staff.by_id(row.id)
        assert moved is not None and isinstance(moved.worktree, staff.PresentWorktree)
        project = transcript.parent.parent / transcripts.claude_project_key(moved.worktree.path)
        self.resume_turns(moved, "claude", self.root / ".claude3", project / transcript.name)
        with patch.object(herdr, "read_conversation", side_effect=AssertionError("completed rename checked the session")):
            code, _, errors = command("rename", row.name, "fixture-new")
        self.assertEqual(code, 0, errors)
        self.assertFalse(producer_lifecycle.pending_rename(row.name))

    def test_completed_retry_keeps_the_next_rename_reservation(self) -> None:
        row, _ = self.rename_fixture("fixture-old")
        middle, final = "fixture-new", "fixture-final"
        release = staff.release_producer_rename

        def fail_after_release(operation: str) -> None:
            release(operation)
            raise OSError("fixture interrupted lookup removal")

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            with patch.object(staff, "release_producer_rename", side_effect=fail_after_release):
                self.assertIn("interrupted lookup removal", command("rename", row.name, middle)[2])
            self.assertTrue(producer_lifecycle.pending_rename(row.name))
            with patch.object(session_move, "rename_exclusive", side_effect=OSError("fixture interrupted next move")):
                self.assertIn("interrupted next move", command("rename", middle, final)[2])

        reservation_error = f"{middle}: finish the producer rename to {final}"
        with self.assertRaisesRegex(staff.StaffRefused, reservation_error):
            staff.require_no_producer_rename(middle)
        with patch.object(herdr, "read_conversation", side_effect=AssertionError("completed rename checked the session")):
            code, output, errors = command("rename", row.name, middle)
        self.assertEqual(code, 0, errors)
        self.assertEqual(output, f"{middle}: document folder {self.root / 'documents' / middle}, changed 0 vault line(s)\n")
        self.assertFalse(producer_lifecycle.pending_rename(row.name))
        self.assertTrue(producer_lifecycle.pending_rename(middle))
        with self.assertRaisesRegex(staff.StaffRefused, reservation_error):
            staff.require_no_producer_rename(middle)

    def test_rename_retries_after_a_move_and_allows_old_name_reuse_after_release(self) -> None:
        row, transcript = self.rename_fixture("fixture-old")
        destination = self.root / "documents/fixture-new"
        release = staff.release_producer_rename
        rename_exclusive = session_move.rename_exclusive

        def fail_move(source_parent: int, source: str, target_parent: int, target: str) -> None:
            rename_exclusive(source_parent, source, target_parent, target)
            if target == destination.name:
                raise OSError("fixture interrupted the folder move")

        def fail_after_release(operation: str) -> None:
            release(operation)
            raise OSError("fixture interrupted lookup removal")

        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(shutdown, "rename_repair_displays", return_value=True)):
            with patch.object(session_move, "rename_exclusive", side_effect=fail_move):
                self.assertIn("interrupted the folder move", command("rename", row.name, "fixture-new")[2])
            self.assertTrue(destination.is_dir())
            self.assertTrue(transcript.is_file())
            self.assertIn("finish the producer rename", command("resume", row.name)[2])
            with patch.object(staff, "release_producer_rename", side_effect=fail_after_release):
                self.assertIn("interrupted lookup removal", command("rename", row.name, "fixture-new")[2])
            self.assertTrue(producer_lifecycle.pending_rename(row.name))
            replacement = fixture_member("producer", name=row.name, runtime_id=fixture_runtime_id("replacement-producer"),
                                         worktree=staff.PresentWorktree("natedev", self.root))
            _ = write_registry(self.registry, {"producers": {replacement.id: "claude:sonnet:high"}}, update=True)
            with patch.object(pane_launch, "current_kind", return_value="claude"):
                code, _, errors = command("rename", replacement.name, "fixture-final")
            self.assertEqual(code, 0, errors)
        self.assertFalse(producer_lifecycle.pending_rename(row.name))
        original = staff.by_id(row.id)
        assert original is not None
        self.assertEqual(original.name, "fixture-new")
        self.assertEqual(staff.by_id(replacement.id), replace(replacement, name="fixture-final"))

    def test_retirement_records_a_kept_handoff_request(self) -> None:
        row = self.producer("fixture-planner")
        _ = self.list_owner()
        with patch.object(restart_support, "send_message",
                          return_value=restart_support.MessageKept(kind="kept", reason="in a review")) as send:
            for _ in range(2):
                code, output, errors = command("retire", row.name)
                self.assertEqual((code, errors), (0, ""), output)
                self.assertIn("handoff.md exists", output)
            self.assertEqual(send.call_count, 1)

    def test_retirement_requests_handoff_once_moves_open_items_and_restores_the_same_session(self) -> None:
        row, keeper = self.producer("fixture-planner"), self.list_owner()
        owner = fixture_member("showrunner", name="show")
        destination = self.producer("director")
        opened = the_list.add(keeper.id, "Open work", owner=owner.id, source="fixture", changes={"producer": row.id})
        closed = the_list.add(keeper.id, "Closed work", owner=owner.id, source="fixture", changes={"producer": row.id})
        closed = the_list.close(keeper.id, closed.id, "Done", source="fixture")
        self.assertIn("the List producer cannot retire", command("retire", keeper.name)[2])
        self.assertIn("active producer", command("retire", row.name, "--to", owner.name)[2])
        admission = stop_resume.producer_admission_record(row.name)
        admission.parent.mkdir(exist_ok=True)
        _ = admission.write_text("{}")
        self.assertIn(f"Run agents resume {row.name} first", command("retire", row.name)[2])
        self.assertEqual(the_list.load(keeper.id, opened.id), opened)
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
                during_stop.append(the_list.add(keeper.id, "Assigned during stop", owner=owner.id,
                                                source="fixture", changes={"producer": row.id}))
            return True

        def send_with_assignment(*_arguments: object, **_options: object) -> restart_support.MessageSent:
            if not after_move:
                after_move.append(the_list.add(keeper.id, "Assigned during notice", owner=owner.id,
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
            self.assertEqual(the_list.load(keeper.id, opened.id), opened)
            self.assertEqual(self.profile(row.id), "claude:sonnet:high")
            stop.assert_not_called()
            handoff = self.root / "documents/fixture-planner/handoff.md"
            _ = handoff.write_text("Open work. Review the scope.\n")
            self.assertIn("conversation did not stop", command("retire", row.name, "--to", destination.name)[2])
            self.assertEqual(the_list.load(keeper.id, opened.id), opened)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(self.profile(row.id), "claude:sonnet:high")
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
            self.assertEqual(self.profile(row.id), "claude:sonnet:high")
            replacement = staff.native_id(fixture_runtime_id("retirement replacement"))
            _ = staff.update_current_session(row.current_session, replacement)
            errors = command("retire", row.name, "--to", destination.name)[2]
            self.assertIn(f"retirement belongs to session {row.current_session}", errors)
            self.assertIn(f"Current session is {replacement}", errors)
            stop.assert_called_once()
            self.assertEqual(the_list.load(keeper.id, opened.id), opened)
            _ = staff.update_current_session(replacement, row.current_session)

            scan.return_value = herdr.RowPaneScan({row.id: [pane]}, [])
            send.side_effect = RuntimeError("fixture refused the List notice")
            self.assertIn("refused the List notice", command("retire", row.name, "--to", destination.name)[2])
            before_retry = the_list.load(keeper.id, opened.id)
            self.assertEqual(len(before_retry.log), len(opened.log) + 1)
            self.assertEqual(the_list.load(keeper.id, during_stop[0].id), during_stop[0])
            close_tab.assert_called_once_with("producer-pane", target=row.current_session, terminal="terminal")
            forget.assert_called_once_with([row.id])

            panes.return_value = []
            scan.return_value = herdr.RowPaneScan({}, [])
            send.side_effect = send_with_assignment
            self.assertIn("new open items belong to the producer", command("retire", row.name, "--to", destination.name)[2])
            self.assertEqual(send.call_args_list[2], send.call_args_list[3])
            self.assertEqual(staff.snapshot().by_path(row.name), row)
            self.assertEqual(self.profile(row.id), "claude:sonnet:high")
            send.side_effect = None
            self.assertEqual(command("retire", row.name, "--to", destination.name)[0], 0)
            self.assertEqual(command("retire", row.name, "--to", destination.name)[0], 0)
            self.assertEqual(the_list.load(keeper.id, opened.id), before_retry)
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
        self.assertIsNone(self.profile(row.id))
        self.assertTrue(handoff.is_file())
        moved = the_list.load(keeper.id, opened.id)
        self.assertEqual((moved.producer, moved.log[-1].source), (destination.id, "agents retire"))
        for late in (*during_stop, *after_move):
            moved_late = the_list.load(keeper.id, late.id)
            self.assertEqual((moved_late.producer, moved_late.log[-1].source), (destination.id, "agents retire"))
        self.assertEqual(the_list.load(keeper.id, closed.id), closed)
        self.assertIn("fixture-planner | Closed work", the_list.table(keeper.id, include_closed=True))

        def resume(identity: str, *, why: str, to: str | None) -> stop_resume.ClaudeUp:
            del why, to
            self.assertEqual(staff.snapshot().by_path(row.name), row)
            self.assertEqual(self.profile(row.id), "claude:sonnet:high")
            return stop_resume.ClaudeUp(identity, "restored-tab", "claude3")

        with patch.object(stop_resume, "resume", side_effect=resume) as launch:
            self.assertEqual(command("resume", row.name, "--retired")[0], 0)
        launch.assert_called_once_with(row.id, why="resumed by name", to=None)
