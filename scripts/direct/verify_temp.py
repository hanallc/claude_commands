"""Hold the temporary folder for local Verify steps."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from contextlib import ExitStack
import os
from pathlib import Path
import signal
import sys
from types import FrameType

from scripts.lib import build_folders, testing_folders


def stop_verify_temp(signum: int, _frame: FrameType | None) -> None:
    """Stop allocation and give cleanup three seconds."""
    _ = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    _ = signal.alarm(3)
    raise SystemExit(128 + signum)


def hold_verify_temp(source: Path) -> int:
    """Hold the temporary folder until Verify and its steps close the pipe."""
    _ = signal.signal(signal.SIGTERM, stop_verify_temp)
    if os.getpgrp() != os.getpid():
        os.setsid()
    with ExitStack() as held:
        try:
            source = source.resolve(strict=True)
            target = source / "target"
            if isinstance(build_folders.parse_form(source), build_folders.BuildCopy):
                lease = held.enter_context(build_folders.numbered_copy_use_lease(source))
                if isinstance(lease, build_folders.NumberedCopyUseRefused):
                    raise ValueError(lease.reason)
                if target.is_symlink():
                    raise ValueError(f"{target}: a numbered copy requires its own target directory")
            elif not (target.is_symlink() and target.is_dir()
                      and isinstance(build_folders.parse_form(target), (build_folders.WorktreeOutput, build_folders.MainOutput))):
                raise ValueError(f"{source}: no assigned build output")
            parent = testing_folders.prepare_test_runs_parent(source)
            request = build_folders.UniqueBuildFolder("r", build_folders.ExplicitBuildParent(parent))
            temporary = held.enter_context(build_folders.temporary_build_folder(request))
        except (OSError, ValueError, RuntimeError) as error:
            held.close()
            print("fallback " + " ".join(str(error).split()), flush=True)
            _ = sys.stdin.read(1)
            return 2
        # A scope stop can signal the holder and the steps together.
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            _ = signal.signal(signum, signal.SIG_IGN)
        print(f"ready {temporary}", flush=True)
        _ = sys.stdin.read(1)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: verify_temp.py <checkout>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(hold_verify_temp(Path(sys.argv[1])))
