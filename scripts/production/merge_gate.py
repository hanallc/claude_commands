#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Use the gate module through the former entry point."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys

from scripts.production import gate


if __name__ == "__main__":
    raise SystemExit(gate.main())
else:
    sys.modules[__name__] = gate
