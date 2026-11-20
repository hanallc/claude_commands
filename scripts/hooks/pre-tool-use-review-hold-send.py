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


def normalized_name(name: str) -> str:
    return " ".join(name.casefold().split())


def matching_sessions(name: str) -> dict[str, str]:
    """Return the registered names by session ID."""
    matches: dict[str, str] = {}
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
                        and normalized_name(record["name"]) == name and running_session(record)):
                    matches[record["sessionId"]] = record["name"]
            except (OSError, ValueError, RecursionError):
                continue
    return matches


def send_reason(target: str) -> str:
    return ("Use send.py for messages to another session. Send the same text with "
            + f"`~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to {shlex.quote(target)} "
            + '--from "<your session name>" --text "<same text>"`. '
            + "Add `--urgent` for an URGENT message.")


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
        if target.startswith(("uds:", "session:")):
            reason = send_reason(target)
        elif target.endswith(".sock"):
            reason = send_reason(f"uds:{target}")
        else:
            target = target.strip()
            name = re.sub(r"\s+\[[^\[\]\r\n]+\]\Z", "", target)
            normalized = normalized_name(name)
            matches = matching_sessions(normalized) if normalized else {}
            if not matches:
                return
            if len(matches) == 1:
                session, registered_name = next(iter(matches.items()))
                reason = send_reason(f"session:{session}" if name != target else registered_name)
            else:
                choices = ", ".join(f"`session:{session}`" for session in sorted(matches))
                reason = (f"Choose one session address from {choices}. "
                          + "Use it with `send.py --to`. Add `--urgent` for an URGENT message.")
    except (OSError, ValueError, RecursionError):
        return

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    main()
