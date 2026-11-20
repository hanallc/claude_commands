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
    if target.startswith("uds:"):
        field, value = "messagingSocketPath", target.removeprefix("uds:")
    elif target.startswith("session:"):
        field, value = "sessionId", target.removeprefix("session:")
    else:
        field, value = "name", target
    matches: dict[str, str] = {}
    if not value:
        return matches
    for config in config_dirs():
        try:
            paths = list((config / "sessions").iterdir())
        except OSError:
            continue
        for path in paths:
            if path.suffix != ".json" or not path.stem.isdecimal():
                continue
            try:
                raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            record = cast(dict[str, object], raw)
            if record.get(field) != value:
                continue
            session = record.get("sessionId")
            if not isinstance(session, str) or not session:
                continue
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
    except Exception:
        return

    at = time.time()
    for session, name in matches.items():
        try:
            if review_hold.has_open_hold(session, at):
                break
        except Exception:
            continue
    else:
        return

    address = shlex.quote(f"session:{session}")
    reason = (f"{name} is in a review. Send the same text with "
              + f"`~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to {address} "
              + '--from "<your session name>" --text "<same text>"`. '
              + "It keeps the message until the review ends and returns KEPT.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    main()
