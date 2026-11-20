"""Store the open review count for one Claude session."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys
import time

from scripts.message import review_hold


def row(session_id: str) -> str:
    """Read one session's count without reading a production."""
    try:
        remaining = review_hold.item_count(session_id)
    except OSError:
        return ""
    if remaining <= 0:
        return ""
    return f"{remaining} adhoc item" + ("s" if remaining != 1 else "")


def main() -> None:
    from scripts.hooks import review_hold_delivery

    review_hold.set_count(os.environ["CLAUDE_CODE_SESSION_ID"], int(sys.argv[1]), time.time(),
                          lambda text: print(text, flush=True), record_keys=review_hold_delivery.record_keys)


if __name__ == "__main__":
    main()
