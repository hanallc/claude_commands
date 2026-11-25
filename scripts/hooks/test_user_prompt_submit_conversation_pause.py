"""Verify goal cards through the Claude and Codex prompt hooks."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from functools import cached_property
from importlib import import_module
from io import StringIO
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

from scripts.hooks import conversation_pause, goal_card
from scripts.lib import script_prompts
from scripts.production import goal, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member


HOOK = import_module("scripts.hooks.user-prompt-submit-conversation-pause")
WORDS = "What is the main goal?"


@final
class GoalCardPromptTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(cast(AbstractContextManager[object], patch.dict(os.environ, {
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"),
            "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "pause"),
            "CONVERSATION_PAUSE_NOW_EPOCH": "1000",
            "CONF_DIR": str(Path(__file__).resolve().parents[2] / "config"),
            "HERDR_PANE_ID": "goal-fixture-pane",
        })))

    @cached_property
    def producer(self) -> staff.StaffRow:
        return fixture_member("producer", name="goal-producer")

    def publish_card(self) -> str:
        return goal.save(self.producer.id,
                         goal.GoalRecord(keeper=self.producer.id, item="L1", blocked="", faster="",
                                         noted_at=""),
                         goal.ItemStatus("Publish the panel", "Working", "2026-11-25"))

    def run_prompt(self, session_id: str, prompt: str, *, agent_id: str = "", codex: bool = False) -> object:
        payload = {"session_id": session_id, "prompt": prompt}
        if agent_id:
            payload["agent_id"] = agent_id
        if codex:
            payload["hook_event_name"] = "UserPromptSubmit"
        identity = {"CODEX_THREAD_ID": session_id, "CLAUDE_CODE_SESSION_ID": ""} if codex else {
            "CLAUDE_CODE_SESSION_ID": session_id,
        }
        output, errors = StringIO(), StringIO()
        main = goal_card.main if codex else cast(Callable[[], None], getattr(HOOK, "main"))
        with (patch.dict(os.environ, identity),
              patch("sys.stdin", StringIO(json.dumps(payload))), redirect_stdout(output), redirect_stderr(errors)):
            main()
        self.assertEqual(errors.getvalue(), "")
        return cast(object, json.loads(output.getvalue())) if output.getvalue() else {}

    def assert_context(self, result: object, context: str) -> None:
        self.assertEqual(result, {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": context,
        }})

    def test_typed_producer_prompt_reads_the_ready_card_once(self) -> None:
        card = self.publish_card()
        with (patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read,
              patch.object(conversation_pause, "session_staff", wraps=conversation_pause.session_staff) as staff_read):
            result = self.run_prompt(self.producer.current_session, WORDS)
        card_path = goal.card_path(self.producer.id)
        paths = [cast(Path, call.args[0]) for call in read.call_args_list]
        self.assertEqual([path for path in paths if path.parent == card_path.parent], [card_path])
        staff_read.assert_called_once_with(self.producer.current_session)
        self.assert_context(result, card)

    def test_producer_without_a_mark_gets_the_no_goal_line(self) -> None:
        self.assert_context(self.run_prompt(self.producer.current_session, WORDS), "No main goal set.")

    def test_non_producer_prompts_do_not_read_a_goal_card(self) -> None:
        other = fixture_member("showrunner", name="production")
        with patch.object(goal, "read_card") as read:
            for session_id in (other.current_session, "missing-session"):
                with self.subTest(session_id=session_id):
                    self.assertEqual(self.run_prompt(session_id, WORDS), {})
            read.assert_not_called()

    def test_peer_hook_and_scheduled_prompts_do_not_read_the_card(self) -> None:
        session_id = self.producer.current_session
        card = self.publish_card()
        script_prompts.record("goal-fixture-pane", "A pane command")
        script_prompts.record(f"session:{session_id}", "A session command")
        conversation_pause.write_scheduled_task_prompts(session_id, (
            conversation_pause.ScheduledTaskPrompt("one", "A task prompt", False),
        ))
        with patch.object(goal, "read_card", wraps=goal.read_card) as read:
            for prompt in (
                '<cross-session-message from-name="peer">Hello</cross-session-message>',
                '<system-reminder source="script">Run it</system-reminder>',
                "A pane command",
                "A session command",
                "A task prompt",
            ):
                with self.subTest(prompt=prompt):
                    self.assertEqual(self.run_prompt(session_id, prompt), {})
            read.assert_not_called()
            self.assert_context(self.run_prompt(session_id, "A session command"), card)
            read.assert_called_once_with(self.producer.id)

    def test_agent_prompt_skips_the_card(self) -> None:
        with patch.object(goal, "read_card") as read:
            self.assertEqual(self.run_prompt(self.producer.current_session, WORDS, agent_id="helper"), {})
        read.assert_not_called()

    def test_the_card_comes_before_idle_update_text(self) -> None:
        card = self.publish_card()
        with patch.object(HOOK, "_idle_update_context", return_value="Idle update text."):
            result = self.run_prompt(self.producer.current_session, WORDS)
        self.assert_context(result, f"{card}\n\nIdle update text.")

    def test_unreadable_card_leaves_the_other_prompt_context(self) -> None:
        path = goal.card_path(self.producer.id)
        with patch.object(HOOK, "_idle_update_context", return_value="Idle update text."):
            path.mkdir(parents=True)
            self.assert_context(self.run_prompt(self.producer.current_session, WORDS), "Idle update text.")
            path.rmdir()
            _ = path.write_bytes(b"\xff")
            self.assert_context(self.run_prompt(self.producer.current_session, WORDS), "Idle update text.")

    def test_codex_typed_producer_prompt_reads_the_ready_card_once(self) -> None:
        card = self.publish_card()
        with patch.object(goal, "read_card", wraps=goal.read_card) as read:
            self.assert_context(self.run_prompt(self.producer.current_session, WORDS, codex=True), card)
        read.assert_called_once_with(self.producer.id)

    def test_codex_producer_without_a_mark_gets_the_no_goal_line(self) -> None:
        self.assert_context(self.run_prompt(self.producer.current_session, WORDS, codex=True), "No main goal set.")

    def test_codex_non_producer_and_agent_prompts_skip_the_card(self) -> None:
        other = fixture_member("showrunner", name="production")
        with patch.object(goal, "read_card") as read:
            self.assertEqual(self.run_prompt(other.current_session, WORDS, codex=True), {})
            self.assertEqual(self.run_prompt(self.producer.current_session, WORDS, agent_id="helper", codex=True), {})
        read.assert_not_called()

    def test_codex_peer_hook_and_scheduled_prompts_skip_the_card(self) -> None:
        session_id = self.producer.current_session
        script_prompts.record("goal-fixture-pane", "A pane command")
        conversation_pause.write_scheduled_task_prompts(session_id, (
            conversation_pause.ScheduledTaskPrompt("one", "A task prompt", False),
        ))
        with patch.object(goal, "read_card", wraps=goal.read_card) as read:
            for prompt in (
                '<cross-session-message from-name="peer">Hello</cross-session-message>',
                "Review available.\nSender native id: codex:other-session\nRead the review.",
                '<system-reminder source="script">Run it</system-reminder>',
                "A pane command",
                "A task prompt",
            ):
                with self.subTest(prompt=prompt):
                    self.assertEqual(self.run_prompt(session_id, prompt, codex=True), {})
            read.assert_not_called()
            self.assert_context(self.run_prompt(session_id, "A task prompt", codex=True), "No main goal set.")
            read.assert_called_once_with(self.producer.id)

    def test_both_hooks_show_the_stale_line(self) -> None:
        _ = self.publish_card()
        path = goal.card_path(self.producer.id)
        record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        record["card"] = goal.STALE_CARD
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        for codex in (False, True):
            with self.subTest(codex=codex):
                self.assert_context(self.run_prompt(self.producer.current_session, WORDS, codex=codex),
                                    "Card is stale: run goal.py card.")
