"""Add a producer's ready goal card to a typed prompt."""

from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from scripts.production import staff


def context(record: staff.StaffSnapshot, *, is_typed: Callable[[], bool]) -> str:
    """Read the ready card for an active producer's typed prompt."""
    from scripts.production import staff

    producers = staff.active(record, "producer")
    if not producers:
        return ""
    try:
        if not is_typed():
            return ""
        from scripts.production import goal

        return goal.read_card(producers[0].id)
    except (OSError, ValueError, RecursionError):
        return ""


def _producer_fields(session_id: str) -> dict[str, object] | None:
    """Find the current producer session in the local staff record."""
    path = os.path.expanduser(os.environ.get("STAFF_FILE") or "~/.local/state/staff/staff.json")
    try:
        with open(path, encoding="utf-8") as record:
            value = cast(object, json.load(record))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    document = cast(dict[str, object], value)
    schema, rows = document.get("schema"), document.get("rows")
    if type(schema) is not int or schema not in (1, 2) or not isinstance(rows, list):
        return None
    for entry in cast(list[object], rows):
        if isinstance(entry, dict):
            fields = cast(dict[str, object], entry)
            if fields.get("current_session", fields.get("id")) == session_id:
                return fields if fields.get("kind") == "producer" and "retired_producer" not in fields else None
    return None


def main() -> None:
    """Clear compaction and add any goal card in the same Codex hook."""
    try:
        value = cast(object, json.load(sys.stdin))
    except (ValueError, RecursionError):
        return
    if not isinstance(value, dict):
        return
    payload = cast(dict[str, object], value)
    session_id, prompt = payload.get("session_id"), payload.get("prompt")
    if not isinstance(session_id, str) or not session_id:
        return
    try:
        from scripts.hooks.compaction_marker import clear

        clear(session_id)
    except (OSError, ValueError):
        pass
    if (payload.get("hook_event_name") != "UserPromptSubmit" or "agent_id" in payload
            or not isinstance(prompt, str)):
        return
    fields = _producer_fields(session_id)
    if fields is None:
        return

    from scripts.hooks import conversation_pause
    from scripts.production import staff

    def is_typed() -> bool:
        source = conversation_pause.prompt_source(
            prompt,
            conversation_pause.scheduled_senders,
            lambda: conversation_pause.read_scheduled_prompts(session_id),
        )
        if source is conversation_pause.PromptSource.SCHEDULED:
            conversation_pause.consume_scheduled_prompt(session_id, prompt)
        # Codex daemon messages carry the sender stamp on the second line.
        return (source is conversation_pause.PromptSource.TYPED
                and not prompt.partition("\n")[2].startswith("Sender native id: "))

    try:
        record = staff.StaffSnapshot((staff.StaffRow.from_json(fields),))
    except (OSError, ValueError, RecursionError):
        return
    card = context(record, is_typed=is_typed)
    if card:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": card,
        }}))


if __name__ == "__main__":
    main()
