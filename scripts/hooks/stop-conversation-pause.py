#!/usr/bin/env python3
"""Record when a session reply finishes during an update pause."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

from scripts.hooks.hook_budget import pause_state_root, print_hook_error, run_hook_with_budget


class ScheduledWakeup(TypedDict):
    id: str
    schedule: str
    recurring: bool
    prompt: str


class StopPayload(TypedDict, total=False):
    session_id: str
    agent_id: str
    stop_hook_active: bool
    session_crons: list[ScheduledWakeup]


def pause_record_path(session_id: str) -> Path:
    return pause_state_root() / f"{session_id}.json"


def scheduled_prompts_path(session_id: str) -> Path:
    return pause_record_path(session_id).parent / "scheduled-prompts" / f"{session_id}.json"


def main(*, budgeted: bool = False) -> None:
    payload = cast(StopPayload, json.loads(sys.stdin.read()))
    if "agent_id" in payload:
        return
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    wakeups = payload.get("session_crons", [])
    records_scheduled_prompts = "session_crons" in payload and (
        bool(wakeups) or scheduled_prompts_path(session_id).exists()
    )
    marks_reply_ended = (
        not payload.get("stop_hook_active", False)
        and pause_record_path(session_id).exists()
    )
    if not records_scheduled_prompts and not marks_reply_ended:
        return

    def record_stop() -> None:
        from scripts.hooks import conversation_pause

        if records_scheduled_prompts:
            conversation_pause.record_scheduled_prompts(
                session_id,
                tuple(conversation_pause.ScheduledTaskPrompt(
                    wakeup["id"], wakeup["prompt"], wakeup["recurring"]
                ) for wakeup in wakeups),
            )
        if marks_reply_ended:
            conversation_pause.mark_reply_ended(
                session_id, conversation_pause.now_epoch()
            )

    if budgeted:
        run_hook_with_budget(record_stop)
    else:
        record_stop()


if __name__ == "__main__":
    try:
        main(budgeted=True)
    except Exception as error:
        print_hook_error(error)
    raise SystemExit(0)
