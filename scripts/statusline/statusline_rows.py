"""Read the action count and render the status line in one process."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
from scripts.statusline import assignment


def main() -> None:
    assignment.render_status(remote=sys.argv.pop(1) == "--for-you-console")


if __name__ == "__main__":
    main()
