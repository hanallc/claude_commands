"""Read the action count and render the status line in one process."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
from scripts.statusline import assignment, rebuild_row


def main() -> None:
    """Pass the action count and shell arguments to the status line renderer."""
    source = sys.argv[1]
    if source == "--for-you-console":
        args = assignment.status_action_args(remote=True)
        rebuild = rebuild_row.read("statusline")
    else:
        rebuild = rebuild_row.read("statusline")
        args = assignment.status_action_args(remote=False, rebuild=rebuild)
    sys.argv[1:2] = args
    assignment.main(rebuild)


if __name__ == "__main__":
    main()
