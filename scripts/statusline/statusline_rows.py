"""Read the action count and render the status line in one process."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
import traceback

from scripts.statusline import assignment, rebuild_row

try:
    from scripts.review_links import for_you
except Exception:
    for_you = None


def _action_count(source: str, rebuild: rebuild_row.RebuildAnswer | None = None) -> str:
    """Keep the other rows available if the action count fails."""
    if for_you is None:
        return "unavailable"
    try:
        return for_you.action_count(source, rebuild)
    except Exception:
        traceback.print_exc()
        return "unavailable"


def main() -> None:
    """Pass the action count and shell arguments to the status line renderer."""
    source = sys.argv[1]
    if source == "--for-you-console":
        count = _action_count(source)
        rebuild = rebuild_row.read(rebuild_row.DEFAULT_CONFIG)
    else:
        rebuild = rebuild_row.read(rebuild_row.DEFAULT_CONFIG)
        count = _action_count(source, rebuild)
    sys.argv[1:2] = ["--arg", "for_you", count]
    assignment.main(rebuild)


if __name__ == "__main__":
    main()
