#!/usr/bin/env python3
"""Deliver messages held for this Claude session."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import sys
from typing import cast

from scripts.message import review_hold


def emit(text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": text,
    }}), flush=True)


def main() -> None:
    payload = cast(dict[str, object], json.load(sys.stdin))
    session = payload.get("session_id")
    if isinstance(session, str) and session:
        review_hold.deliver_at_session_start(session, emit)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"review hold: {error}", file=sys.stderr)
