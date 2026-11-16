#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Run the targeted mode through the gate entry point."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
from collections.abc import Sequence

from scripts.production import gate


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    return gate.main(["--targeted", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
