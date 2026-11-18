"""Read the Cargo paths for the local placement candidate."""
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
        if "\n" in str(roots.target_directory) or "\n" in str(roots.build_directory):
            print("verify.sh: a Cargo output path contains a newline", file=sys.stderr)
            return 2
        print(roots.target_directory, roots.build_directory, sep="\n")
        return 0
    print(f"verify.sh: cannot resolve the Cargo build folder: {roots.reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
