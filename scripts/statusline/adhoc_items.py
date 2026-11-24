"""Store the open review count for one Claude session."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys
import time
from pathlib import Path

from scripts.message import review_hold


def row(session_id: str) -> str:
    """Read one session's count without reading a production."""
    try:
        remaining = review_hold.item_count(session_id)
    except OSError:
        return ""
    return format_count(remaining)


def read_row(path: Path) -> str:
    """Read the count and report any read error to the caller."""
    text = path.read_text(encoding="utf-8").strip()
    if not text.isdecimal():
        raise ValueError("The adhoc count must be a whole number.")
    return format_count(int(text))


def format_count(remaining: int) -> str:
    if remaining <= 0:
        return ""
    return f"{remaining} adhoc item" + ("s" if remaining != 1 else "")


def main() -> None:
    from scripts.hooks import review_hold_delivery

    review_hold.set_count(os.environ["CLAUDE_CODE_SESSION_ID"], int(sys.argv[1]), time.time(),
                          lambda text: print(text, flush=True), record_keys=review_hold_delivery.record_keys)


if __name__ == "__main__":
    main()
