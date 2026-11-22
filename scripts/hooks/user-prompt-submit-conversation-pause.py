#!/usr/bin/env python3
"""Pause a session's automatic updates when a conversation begins, and record the user's activity."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from scripts.hooks.hook_budget import print_hook_error, run_hook_with_budget

if TYPE_CHECKING:
    from scripts.production import staff


class PromptPayload(TypedDict, total=False):
    session_id: str
    prompt: str
    agent_id: str


def _record_typed_prompt() -> None:
    try:
        from scripts.message import escalate

        escalate.typed()
    except Exception as error:
        lines = str(error).splitlines()
        detail = lines[0] if lines else ""
        print(
            f"conversation-pause: typed stamp: {type(error).__name__}: {detail}",
            file=sys.stderr,
        )


def _record_activity(session_id: str, source: str, prompt: str, record: staff.StaffSnapshot) -> None:
    """Send the session activity without blocking the prompt hook."""
    try:
        from scripts.hooks import conversation_pause

        if not conversation_pause.is_showrunner_or_unit_director(session_id, record=record):
            return
        override = os.environ.get("AUTO_SILENCE_COMMAND")
        script = Path(__file__).resolve().parent.parent / "notify" / "auto_silence.py"
        command = [override] if override else [sys.executable, str(script)]
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        _ = subprocess.Popen([*command, "active", "--source", source, "--session-id", session_id,
                              "--pane-id", os.environ.get("HERDR_PANE_ID", ""), "--prompt-hash", digest], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as error:
        lines = str(error).splitlines()
        detail = lines[0] if lines else ""
        print(f"conversation-pause: auto silence: {type(error).__name__}: {detail}", file=sys.stderr)


def main(*, budgeted: bool = False) -> None:
    payload = cast(PromptPayload, json.loads(sys.stdin.read()))
    try:
        from scripts.hooks.compaction_marker import clear

        clear(payload.get("session_id", ""))
    except Exception:
        pass
    if "agent_id" in payload:
        return
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    prompt = payload.get("prompt", "")

    def process_prompt() -> None:
        from scripts.hooks import conversation_pause

        source = conversation_pause.prompt_source(
            prompt,
            conversation_pause.scheduled_senders,
            lambda: conversation_pause.read_scheduled_prompts(session_id),
        )
        now = conversation_pause.now_epoch()
        if source in (conversation_pause.PromptSource.PEER, conversation_pause.PromptSource.SCHEDULED):
            try:
                kept = conversation_pause.keep_native_review_prompt(session_id, prompt, now)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"review hold: {error}", file=sys.stderr)
            else:
                if kept:
                    print(json.dumps({
                        "decision": "block",
                        "reason": "Kept for the end of the review.",
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "suppressOriginalPrompt": True,
                        },
                    }), flush=True)
                    return
        if source is conversation_pause.PromptSource.SCHEDULED:
            conversation_pause.consume_scheduled_prompt(session_id, prompt)
        # Read the staff pointer so long waits retain the current run.
        from scripts.lib import unit_runs

        record = conversation_pause.session_staff(session_id)
        run = unit_runs.open_run(session_id, record=record)
        if run is not None:
            from scripts.direct.progress_history import finish_turn_wait

            finish_turn_wait(run, session_id, source=source.name, prompt=prompt)
        if source is conversation_pause.PromptSource.TYPED:
            _record_typed_prompt()
        activity_source = {conversation_pause.PromptSource.TYPED: "UserTypedPrompt",
                           conversation_pause.PromptSource.PEER: "PeerPrompt",
                           conversation_pause.PromptSource.SCHEDULED: "ScriptPrompt",
                           conversation_pause.PromptSource.NOTICE: "HarnessNotice"}[source]
        if not prompt.strip():
            activity_source = "UnknownPrompt"
        _record_activity(session_id, activity_source, prompt, record)
        reply = conversation_pause.message_arrived(session_id, source, prompt, now, record=record)
        mac_context = ""
        if source is conversation_pause.PromptSource.TYPED and "/Users/" in prompt:
            try:
                from scripts.hooks import mac_files

                mac_context = mac_files.context(prompt)
            except (OSError, ValueError, KeyError):
                pass
        emitted = False

        def emit(held_messages: str = "") -> None:
            nonlocal emitted
            pause_context = "" if reply is conversation_pause.NoReply.NOTHING else reply.context
            context = "\n\n".join(filter(None, (pause_context, held_messages, mac_context)))
            if not context:
                return
            result: dict[str, object] = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                },
            }
            if isinstance(reply, conversation_pause.ShownToUser):
                result["systemMessage"] = reply.system_message
            print(json.dumps(result), flush=True)
            emitted = True

        from scripts.hooks import review_hold_delivery
        from scripts.message import review_hold

        try:
            if source is conversation_pause.PromptSource.TYPED:
                review_hold.record_user_prompt(session_id, now, emit, record_keys=review_hold_delivery.record_keys)
            else:
                review_hold.deliver_if_disabled(session_id, now, emit, record_keys=review_hold_delivery.record_keys)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"review hold: {error}", file=sys.stderr)
        if not emitted:
            emit()

    if budgeted:
        run_hook_with_budget(process_prompt)
    else:
        process_prompt()


if __name__ == "__main__":
    try:
        main(budgeted=True)
    except Exception as error:
        print_hook_error(error)
    raise SystemExit(0)
