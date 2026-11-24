"""Prepare and remove the temporary folder for local Verify steps."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from types import FrameType

from scripts.lib import build_folder_lock, build_folders, testing_folders
from scripts.lib.process_group import send_process_group


def stop_verify_temp(signum: int, _frame: FrameType | None) -> None:
    """Stop allocation and give cleanup three seconds."""
    _ = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    _ = signal.alarm(3)
    raise SystemExit(128 + signum)


def allocate(source: Path) -> int:
    """Hold the new folder until Verify takes its own lock."""
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
            temporary = held.enter_context(build_folders.leased_build_folder(request))
            fcntl.flock(build_folders.build_folder_lock_fd(temporary), fcntl.LOCK_SH)
        except (OSError, ValueError, RuntimeError) as error:
            held.close()
            print("fallback " + " ".join(str(error).split()), flush=True)
            _ = sys.stdin.read(1)
            return 2
        print(f"ready {temporary}", flush=True)
        _ = sys.stdin.read(1)
    return 0


def claim(path: Path) -> None:
    """Lock the descriptor that Verify opened."""
    with build_folder_lock.folder_parent(path, path.parent, create=False) as directory:
        fcntl.flock(0, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if (not build_folder_lock.lock_current(path, directory, 0)
                or not stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode)):
            raise ValueError(f"{path}: the temporary folder changed")


def remove(path: Path) -> None:
    """Release Verify's flock and remove the folder when its locks are free."""
    fcntl.flock(0, fcntl.LOCK_UN)
    with build_folders.folder_removal_lease(path, children=True, wait=False) as lease:
        if isinstance(lease, build_folders.HeldFolderRemovalLease):
            sidecars = lease.sidecar_identities()
            build_folders.remove_folder(path, lease.directory)
            lease.unlink_sidecars(sidecars)


def wait_step_group(group: int) -> None:
    """Wait for the remaining live members of a step group."""
    while True:
        try:
            send_process_group(group, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        try:
            rows = subprocess.check_output(["ps", "-A", "-o", "pgid=", "-o", "stat="], text=True)
        except (OSError, subprocess.CalledProcessError):
            rows = f"{group} ?"
        if not any(parts[0] == str(group) and not parts[1].startswith("Z")
                   for line in rows.splitlines() if len(parts := line.split()) == 2):
            return
        time.sleep(0.05)


def main(words: list[str]) -> int:
    try:
        match words:
            case ["allocate", checkout]:
                return allocate(Path(checkout))
            case ["claim", folder]:
                claim(Path(folder))
            case ["remove", folder]:
                remove(Path(folder))
            case ["wait-group", group]:
                wait_step_group(int(group))
            case _:
                return 2
    except (OSError, ValueError, RuntimeError) as error:
        print(f"verify_temp: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
