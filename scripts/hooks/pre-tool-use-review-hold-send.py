#!/usr/bin/env python3
"""Require send.py for messages to another Claude session."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import re
import shlex
import sys
from typing import cast

from scripts.lib.transcripts import config_dirs
from scripts.message.session_records import UnreadableSessionRecord, parse_session, running_session


def matches_session_name(name: str) -> bool:
    """Check the registered name of a running session."""
    for config in config_dirs():
        try:
            paths = list((config / "sessions").iterdir())
        except OSError:
            continue
        for path in paths:
            if path.suffix != ".json" or not path.stem.isdecimal():
                continue
            try:
                record = parse_session(cast(object, json.loads(path.read_text(encoding="utf-8"))))
                if (not isinstance(record, UnreadableSessionRecord) and record["sessionId"]
                        and record["name"] == name and running_session(record)):
                    return True
            except (OSError, ValueError, RecursionError):
                continue
    return False


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
        target = fields.get("to")
        if not isinstance(target, str) or not target or not isinstance(fields.get("message"), str):
            return
        if not target.startswith(("uds:", "session:")):
            target = re.sub(r" \[[^\[\]\r\n]+\]\Z", "", target)
            if not target or not matches_session_name(target):
                return
    except (OSError, ValueError, RecursionError):
        return

    reason = ("Use send.py for messages to another session. Send the same text with "
              + f"`~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to {shlex.quote(target)} "
              + '--from "<your session name>" --text "<same text>"`. '
              + "Add `--urgent` for an URGENT message.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    main()
