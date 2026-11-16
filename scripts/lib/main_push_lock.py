"""Serialize main publication with explicit child handoff.

Each hold appends one line to `<git common dir>/main-push-lock.jsonl`: the
holder, its pid, when it got the lock, how long it waited and how long it held
the lock. Lines older than `record_days` in config/main_push_lock.conf go.
"""

from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import cast

from scripts.lib import conf


_HELD_LOCKS: set[str] = set()
_INHERITED_LOCK = os.environ.get("MAIN_PUSH_LOCK_HELD", "")
RECORD_NAME = "main-push-lock.jsonl"


def _lock_path(checkout: Path) -> str:
    common = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=False,
    )
    if common.returncode:
        raise OSError(common.stderr.strip())
    return str((Path(common.stdout.strip()) / "main-push.lock").resolve())


def held_environment(checkout: Path) -> dict[str, str]:
    """Give a named child access to this process's active repository hold."""
    path = _lock_path(checkout)
    if path not in _HELD_LOCKS:
        raise RuntimeError(f"this process does not hold {path}")
    return {**os.environ, "MAIN_PUSH_LOCK_HELD": path}


def _record_hold(path: str, holder: str, acquired_at: float, waited: float, held: float) -> None:
    """Append one hold to the record beside the lock and drop lines past the retention."""
    line = json.dumps({"holder": holder, "pid": os.getpid(), "acquired_at": round(acquired_at, 3),
                       "waited_seconds": round(waited, 3), "held_seconds": round(held, 3)})
    oldest = acquired_at - conf.read_or_repository("main_push_lock").whole("record_days") * 86400
    with Path(path).with_name(RECORD_NAME).open("a+", encoding="utf-8") as record:
        _ = record.seek(0)
        first = record.readline()
        if first and _acquired_at(first) < oldest:
            kept = [kept for kept in [first, *record] if _acquired_at(kept) >= oldest]
            _ = record.seek(0)
            _ = record.truncate()
            record.writelines(kept)
        _ = record.write(line + "\n")


def _acquired_at(line: str) -> float:
    try:
        value = cast(object, cast(dict[str, object], json.loads(line)).get("acquired_at"))
    except (ValueError, AttributeError):
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0


@contextmanager
def main_push_lock(checkout: Path, holder: str = "", *, wait: bool = True) -> Generator[None]:
    """Hold the repository lock or use an active hold for this process.

    `holder` names the hold in the record; the default is the program name.
    With wait=False, a lock held by another process raises BlockingIOError.
    """
    path = _lock_path(checkout)
    if path in _HELD_LOCKS:
        yield
        return
    if _INHERITED_LOCK == path:
        _HELD_LOCKS.add(path)
        try:
            yield
        finally:
            _HELD_LOCKS.remove(path)
        return
    with Path(path).open("a") as lock:
        asked = time.monotonic()
        fcntl.flock(lock, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        _HELD_LOCKS.add(path)
        started = time.monotonic()
        acquired_at = time.time()
        try:
            yield
        finally:
            try:
                _record_hold(path, holder or Path(sys.argv[0]).name, acquired_at, started - asked,
                             time.monotonic() - started)
            except (OSError, ValueError) as error:
                print(f"main_push_lock: hold record not written: {error}", file=sys.stderr)
            finally:
                _HELD_LOCKS.remove(path)
                fcntl.flock(lock, fcntl.LOCK_UN)


class LockCommand(argparse.Namespace):
    checkout: Path = Path()
    command: list[str] = []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--checkout", type=Path, required=True)
    _ = parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(namespace=LockCommand())
    command: list[str] = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("supply a command after --")
    with main_push_lock(args.checkout, Path(command[0]).name):
        result = subprocess.run(command, check=False, env=held_environment(args.checkout))
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
