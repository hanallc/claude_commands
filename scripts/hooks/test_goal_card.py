"""Verify the registered Codex prompt command and its role gate."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import cast, final, override

from scripts.hooks import codex_hooks, compaction_marker, conversation_pause
from scripts.production import goal, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id


CHECKOUT = Path(__file__).resolve().parents[2]
COMMAND = next(hook.command for hook in codex_hooks.HOOKS if hook.event == "UserPromptSubmit")


@final
class CodexPromptHookTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.installation.parent.mkdir(parents=True, exist_ok=True)

    @property
    def installation(self) -> Path:
        return Path.home() / ".claude"

    def invoke(self, session: staff.StaffId, prompt: str = "What is the main goal?", *,
               profile: bool = False, worker_prompt: bool = False
               ) -> subprocess.CompletedProcess[str]:
        command = ["sh", "-c", COMMAND]
        if profile:
            command = [str(self.installation / "scripts/lib/py"), str(CHECKOUT / "scripts/hooks/goal_card.py")]
        environment = {**os.environ, "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
                       "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "pause"),
                       "PYTHONPROFILEIMPORTTIME": "1" if profile else "",
                       "UNIT_TEAM_ROLE": "fixture-worker" if worker_prompt else "", "STAFF_ID": ""}
        return subprocess.run(command, env=environment, text=True, capture_output=True,
                              input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt,
                                                "session_id": session}), timeout=5, check=False)

    def worker(self) -> staff.StaffRow:
        showrunner = fixture_member("showrunner", name="fixture-production")
        unit = fixture_member("unit", name="fixture-team", parent=showrunner.id)
        return fixture_member("worker", name="fixture-worker", parent=unit.id)

    def marker(self, session: str) -> Path:
        path = compaction_marker.marker_folder() / f"{session}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("{}", encoding="utf-8")
        return path

    def compact_staff_record(self) -> None:
        document = cast(dict[str, object], json.loads(self.staff_file.read_text(encoding="utf-8")))
        _ = self.staff_file.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")

    def test_worker_without_a_marker_leaves_other_markers(self) -> None:
        row = self.worker()
        self.installation.symlink_to(CHECKOUT, target_is_directory=True)
        other_marker = self.marker("another-session")

        result = self.invoke(row.current_session)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue(other_marker.is_file())

    def test_worker_with_a_marker_clears_it_without_importing_staff(self) -> None:
        row = self.worker()
        self.installation.symlink_to(CHECKOUT, target_is_directory=True)
        marker = self.marker(row.current_session)

        result = self.invoke(row.current_session, profile=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(marker.exists())
        self.assertIn("scripts.hooks.compaction_marker", result.stderr)
        self.assertNotIn("scripts.production.staff", result.stderr)
        self.assertNotIn("scripts.hooks.conversation_pause", result.stderr)

    def test_worker_with_a_compact_staff_record_does_not_import_staff(self) -> None:
        row = self.worker()
        self.installation.symlink_to(CHECKOUT, target_is_directory=True)
        self.compact_staff_record()

        result = self.invoke(row.current_session, profile=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("import time:", result.stderr)
        self.assertNotIn("scripts.production.staff", result.stderr)
        self.assertNotIn("scripts.hooks.conversation_pause", result.stderr)

    def test_producer_with_a_compact_staff_record_gets_the_card_and_clears_the_marker(self) -> None:
        producer = fixture_member("producer", name="fixture-producer")
        worker = self.worker()
        self.installation.symlink_to(CHECKOUT, target_is_directory=True)
        self.compact_staff_record()
        marker = self.marker(producer.current_session)
        prompt = 'Read this text. {"session_id": "' + worker.current_session + '"}'

        result = self.invoke(producer.current_session, prompt)

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertFalse(marker.exists())
        self.assertEqual(cast(object, json.loads(result.stdout)), {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": goal.NoGoal.UNSET.value,
        }})

    def test_restarted_producer_with_nested_worker_kind_gets_the_card(self) -> None:
        producer = fixture_member("producer", name="fixture-producer")
        session = staff.StaffId(fixture_runtime_id("restarted-goal-producer"))
        _ = staff.update_current_session(producer.current_session, session)
        self.installation.symlink_to(CHECKOUT, target_is_directory=True)

        text = self.staff_file.read_text(encoding="utf-8")
        session_line = f'      "current_session": "{session}",\n'
        self.assertIn(session_line, text)
        nested = '      "details": {\n      "kind": "worker",\n      "fixture": true\n      },\n'
        _ = self.staff_file.write_text(text.replace(session_line, session_line + nested), encoding="utf-8")
        card_file = self.root / "showrunner" / producer.id / "main-goal-card.json"
        card_file.parent.mkdir(parents=True)
        _ = card_file.write_text(json.dumps({"card": "Publish the panel"}), encoding="utf-8")

        result = self.invoke(session)

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(cast(object, json.loads(result.stdout)), {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": "Publish the panel",
        }})

    def test_worker_without_markers_exits_before_the_launcher(self) -> None:
        row = self.worker()
        launcher = self.installation / "scripts/lib/py"
        launcher.parent.mkdir(parents=True)
        ran = Path.home() / "launcher-ran"
        _ = launcher.write_text('#!/bin/sh\n: > "$HOME/launcher-ran"\n', encoding="utf-8")
        launcher.chmod(0o755)

        result = self.invoke(row.current_session, worker_prompt=True)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(ran.exists())

    def test_worker_with_a_marker_clears_it_through_the_registered_hook(self) -> None:
        row = self.worker()
        self.installation.symlink_to(CHECKOUT, target_is_directory=True)
        marker = self.marker(row.current_session)
        other_marker = self.marker("another-session")

        result = self.invoke(row.current_session, worker_prompt=True)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse(marker.exists())
        self.assertTrue(other_marker.is_file())

    def test_paused_director_gets_hold_context_and_clears_the_marker(self) -> None:
        owner = fixture_member("showrunner", name="fixture-production")
        director = fixture_member("unit", name="fixture-team", parent=owner.id)
        _ = staff.pause(str(director.id), "maintenance", keep_workers=True)
        self.installation.symlink_to(CHECKOUT, target_is_directory=True)
        marker = self.marker(director.current_session)

        result = self.invoke(director.current_session)

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertFalse(marker.exists())
        self.assertEqual(cast(object, json.loads(result.stdout)), {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": conversation_pause.DIRECTOR_HOLD_CONTEXT,
        }})

        _ = staff.resume(str(director.id))
        result = self.invoke(director.current_session)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_launcher_failure_is_silent(self) -> None:
        row = self.worker()
        _ = self.marker(row.current_session)
        launcher = self.installation / "scripts/lib/py"
        launcher.parent.mkdir(parents=True)
        _ = launcher.write_text(
            '#!/bin/sh\n: > "$HOME/launcher-ran"\nprintf "launcher failed\\n" >&2\nexit 77\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)

        result = self.invoke(row.current_session, worker_prompt=True)

        self.assertTrue((Path.home() / "launcher-ran").is_file())
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
