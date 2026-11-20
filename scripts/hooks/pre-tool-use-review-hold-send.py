#!/usr/bin/env python3
"""Block a native send to a session with an open review hold."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import shlex
import sys
import time
from typing import cast

from scripts.lib.transcripts import config_dirs
from scripts.message import review_hold


def matching_sessions(target: str) -> dict[str, str]:
    """Return matching session IDs and names from the Claude registry files."""
    by_socket = target.startswith("uds:")
    field = "messagingSocketPath" if by_socket else "name"
    value = target.removeprefix("uds:") if by_socket else target
    matches: dict[str, str] = {}
    if not value:
        return matches
    for config in config_dirs():
        try:
            paths = list((config / "sessions").iterdir())
        except FileNotFoundError:
            continue
        for path in paths:
            if path.suffix != ".json" or not path.stem.isdecimal():
                continue
            raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
            if not isinstance(raw, dict):
                raise ValueError("expected a session record")
            record = cast(dict[str, object], raw)
            if record.get(field) != value:
                continue
            session = record.get("sessionId")
            if not isinstance(session, str) or not session:
                raise ValueError("expected a session ID")
            name = record.get("name")
            matches[session] = name if isinstance(name, str) and name else f"session:{session}"
    return matches


def main() -> None:
    try:
        raw = cast(object, json.load(sys.stdin))
        if not isinstance(raw, dict):
            return
        payload = cast(dict[str, object], raw)
        tool_input = payload.get("tool_input")
        if payload.get("tool_name") != "SendMessage" or not isinstance(tool_input, dict):
            return
        fields = cast(dict[str, object], tool_input)
        target, message = fields.get("to"), fields.get("message")
        if not isinstance(target, str) or not isinstance(message, str) or message.startswith(review_hold.URGENT_PREFIX):
            return
        matches = matching_sessions(target)
        if len(matches) != 1:
            return
        ((session, name),) = matches.items()
        if not review_hold.has_open_hold(session, time.time()):
            return
    except Exception:
        return

    reason = ("The session is in a review. Send the same text with "
              + f"`~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to {shlex.quote(name)} "
              + '--from "<your session name>" --text "<same text>"`. '
              + "It keeps the message until the review ends and returns KEPT.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    main()
