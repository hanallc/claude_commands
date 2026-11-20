"""Check whether the current Rust toolchain has a requested target."""

import os
import subprocess
import sys


def check_installed_target(target: str) -> int:
    try:
        result = subprocess.run(["rustup", "target", "list", "--installed"],
                                stdout=subprocess.PIPE, text=True, check=False)
    except OSError as error:
        print(f"lint: cannot read the installed Rust targets: {error}", file=sys.stderr)
        return 2
    if result.returncode != 0:
        return 2
    if target in result.stdout.splitlines():
        return 0
    print(f"lint: install {target} on this target with rustup target add {target}", file=sys.stderr)
    return os.EX_PROTOCOL


if __name__ == "__main__":
    raise SystemExit(check_installed_target(sys.argv[1]) if len(sys.argv) == 2 else 2)
