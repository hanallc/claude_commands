#!/usr/bin/env python3
# Gate rename chunk 6 deletes this folder and its configuration after deployment.
"""Move the gate state after the machine drains.

1. After the merge reaches main, set `/build:hold drain natedev`.
2. Run this program through the checkout's scripts/lib/py -I.
3. Release the drain hold.

A unit worktree with old code can create a stray `merge-gate` folder for tests-only runs until it merges main.
Nothing reads that folder, and chunk 6 deletes it.
The program scans only processes owned by the current user.
Only these processes run gates and lands.
The program skips processes whose descriptor folders or links deny access and prints their PIDs and commands once.
Gate, land, and queue processes are dumpable Python processes.
The program refuses other unreadable state for processes owned by the current user.
"""
from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BufferedReader
import json
import os
from pathlib import Path
import select
import shutil
import stat
import sys
from typing import cast

from scripts.build_hold import build_hold
from scripts.lib.session_move import open_directory, rename_exclusive

PROC_ROOT = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
SKIPPED_PIDS: set[int] = set()


@dataclass(frozen=True)
class StateHolder:
    """A process has an open file or working directory inside the old state folder."""

    pid: int
    command: str


def step(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat(timespec='milliseconds')} {message}", flush=True)


def exists(path: Path) -> bool:
    return os.path.lexists(path)


def path_identity(path: Path, *, directory: bool = False) -> tuple[int, int] | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    if directory and not stat.S_ISDIR(status.st_mode):
        raise ValueError(f"Gate path is not a directory: {path}")
    return status.st_dev, status.st_ino


@contextmanager
def open_record(path: Path) -> Generator[BufferedReader]:
    """Open a regular record without following its final link or waiting on a FIFO."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Record is not a regular file: {path}")
        with open(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def read_record(path: Path) -> bytes:
    with open_record(path) as stream:
        return stream.read()


def rename_folder(old: Path, new: Path) -> None:
    """Move a folder only when the destination is absent."""
    with ExitStack() as parents:
        source = parents.enter_context(open_directory(old.parent.resolve()))
        target = source if old.parent == new.parent else parents.enter_context(open_directory(new.parent.resolve()))
        rename_exclusive(source, old.name, target, new.name)


def remove_typecheck_environment(folder: Path) -> None:
    venv = folder / "typecheck-venv"
    if venv.is_symlink():
        venv.unlink()
    elif venv.exists():
        shutil.rmtree(venv)
    step(f"Removed the type-check environment: {venv}")


def require_hold() -> None:
    directory = build_hold.holder_directory()
    holds = build_hold.natedev_holds(directory)
    if not holds.in_force:
        raise ValueError("a natedev drain hold must be in force before the move")


def under_old(link: str, old: Path) -> bool:
    target = link.removesuffix(" (deleted)")
    return target == str(old) or target.startswith(str(old) + "/")


def process_ended(pid: int) -> bool:
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    try:
        readable, _, _ = select.select([descriptor], [], [], 0)
        return bool(readable)
    finally:
        os.close(descriptor)


def state_holders(old: Path) -> list[StateHolder]:
    holders: list[StateHolder] = []
    physical = old.resolve(strict=True)
    for process in sorted(PROC_ROOT.iterdir()):
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            descriptors = list((process / "fd").iterdir())
            held = False
            for entry in (*descriptors, process / "cwd"):
                try:
                    held = under_old(os.readlink(entry), physical) or held
                except (FileNotFoundError, ProcessLookupError):
                    continue
            if held:
                command = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
                holders.append(StateHolder(int(process.name), command or "[empty command]"))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            if process_ended(int(process.name)):
                continue
            filename = cast(str | None, error.filename)
            if isinstance(error, PermissionError) and filename is not None and Path(filename).is_relative_to(process / "fd"):
                pid = int(process.name)
                if pid not in SKIPPED_PIDS:
                    command = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
                    step(f"Skip unreadable process: pid={pid} command={command or '[empty command]'}")
                    SKIPPED_PIDS.add(pid)
                continue
            raise ValueError(f"cannot prove drained state for pid {process.name}: {error}") from error
    return holders


def drain(old: Path) -> None:
    reported: set[int] = set()
    while True:
        require_hold()
        holders = state_holders(old)
        if not holders:
            step(f"No process holds the old folder: {old}")
            return
        with ExitStack() as stack:
            descriptors: list[int] = []
            for holder in holders:
                if holder.pid not in reported:
                    step(f"Wait for holder: pid={holder.pid} command={holder.command}")
                    reported.add(holder.pid)
                try:
                    descriptor = os.pidfd_open(holder.pid)
                except ProcessLookupError:
                    continue
                _ = stack.callback(os.close, descriptor)
                descriptors.append(descriptor)
            while descriptors:
                ended, _, _ = select.select(descriptors, [], [])
                descriptors = [descriptor for descriptor in descriptors if descriptor not in ended]


def contains_path(value: object, old: str) -> bool:
    if isinstance(value, str):
        return old in value
    if isinstance(value, list):
        return any(contains_path(item, old) for item in cast(list[object], value))
    if isinstance(value, dict):
        return any(old in key or contains_path(item, old) for key, item in cast(dict[str, object], value).items())
    return False


def refuse_record_paths(old: Path) -> None:
    found: list[Path] = []
    prefixes = {str(old), str(old.resolve(strict=True))}
    for directory, names, files in os.walk(old, followlinks=False):
        if Path(directory) == old:
            names[:] = [name for name in names if name != "typecheck-venv"]
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix not in (".json", ".jsonl"):
                continue
            try:
                text = read_record(path).decode("utf-8")
                values = ([cast(object, json.loads(line)) for line in text.splitlines() if line.strip()]
                          if path.suffix == ".jsonl" else [cast(object, json.loads(text))])
            except UnicodeDecodeError as error:
                raise ValueError(f"Record must be UTF-8: {path}") from error
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON record {path}: {error}") from error
            if any(contains_path(value, prefix) for value in values for prefix in prefixes):
                found.append(path)
    if found:
        for path in found:
            step(f"Record holds the old folder path: {path}")
        raise ValueError("records hold the old folder path; no folder moved")
    step("No record holds the old folder path")


def check_move(old: Path, new: Path) -> int:
    """Check each move refusal condition and change nothing; a holder refuses instead of a wait."""
    step(f"Found folders: old={old} exists={exists(old)}; new={new} exists={exists(new)}")
    if exists(old) and exists(new):
        step("Refused: both folders exist")
        return 2
    if not exists(old):
        step("Check passed: no move is required")
        return 0
    require_hold()
    if old.is_symlink() or not old.is_dir():
        raise ValueError(f"the old folder must be a directory: {old}")
    if old.stat().st_dev != new.parent.stat().st_dev:
        raise ValueError("the folders must use the same file system")
    holders = state_holders(old)
    if holders:
        detail = "; ".join(f"pid={holder.pid} command={holder.command}" for holder in holders)
        raise ValueError(f"processes hold the old folder: {detail}")
    refuse_record_paths(old)
    step("Check passed: the move can run")
    return 0


def move_state(old: Path, new: Path, *, defer_typecheck_cleanup: bool = False) -> int:
    step(f"Found folders: old={old} exists={exists(old)}; new={new} exists={exists(new)}")
    if exists(old) and exists(new):
        step("Refused: both folders exist")
        return 2
    if not exists(old):
        step("No move: only the new folder exists" if exists(new) else "No move: neither folder exists")
        return 0
    require_hold()
    step("A natedev hold is in force")
    if old.is_symlink() or not old.is_dir():
        raise ValueError(f"the old folder must be a directory: {old}")
    if old.stat().st_dev != new.parent.stat().st_dev:
        raise ValueError("the folders must use the same file system")
    drain(old)
    refuse_record_paths(old)
    drain(old)
    try:
        rename_folder(old, new)
    except FileExistsError:
        step("Refused: the new folder appeared before the rename")
        return 2
    step(f"Renamed folder: {old} -> {new}")
    if not defer_typecheck_cleanup:
        remove_typecheck_environment(new)
    step(f"Move complete: {new}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--old", type=Path, default=Path.home() / ".local/state/merge-gate")
    _ = parser.add_argument("--new", type=Path, default=Path.home() / ".local/state/gate")
    _ = parser.add_argument("--check", action="store_true", help="check the refusal conditions and move nothing")
    _ = parser.add_argument("--defer-typecheck-cleanup", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    old = Path(os.path.abspath(cast(Path, args.old).expanduser()))
    new = Path(os.path.abspath(cast(Path, args.new).expanduser()))
    try:
        if cast(bool, args.check):
            return check_move(old, new)
        return move_state(old, new, defer_typecheck_cleanup=cast(bool, args.defer_typecheck_cleanup))
    except (OSError, ValueError) as error:
        step(f"Refused: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
