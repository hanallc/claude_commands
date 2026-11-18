"""Read the Cargo build folder for the local placement candidate."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys

from scripts.buildlog.step_context import step_output_roots
from scripts.lib.cargo_output import CargoOutputRoots


def main(argv: list[str]) -> int:
    roots = step_output_roots(argv, os.getcwd())
    if isinstance(roots, CargoOutputRoots):
        print(roots.build_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
