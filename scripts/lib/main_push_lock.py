"""Serialize main publication with explicit child handoff.

Each hold appends one line to `<git common dir>/main-push-lock.jsonl`: the
holder, its pid, when it got the lock, how long it waited and how long it held
the lock. Lines older than `record_days` in config/main_push_lock.conf go.
"""

from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import NamedTuple, cast

from scripts.lib import conf
from scripts.production import land_queue


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


class MainPushReady(NamedTuple):
    """The settings publisher holds the gap and main push locks."""


class MainPushBusy(NamedTuple):
    """Another process holds the main push lock."""


class CheckLandLine(NamedTuple):
    """The publisher still needs a gap in the line."""


CHECK_LAND_LINE = CheckLandLine()


@contextmanager
def settings_push(checkout: Path, *, wait: bool,
                  gap: land_queue.SettingsGap | CheckLandLine = CHECK_LAND_LINE) -> Generator[
                      MainPushReady | MainPushBusy | land_queue.SettingsWaiting | land_queue.SettingsGapBusy]:
    """Get a publication gap and hold main while the caller publishes settings."""
    with ExitStack() as locks:
        admission = locks.enter_context(land_queue.settings_gap(wait=wait)) if isinstance(gap, CheckLandLine) else gap
        if not isinstance(admission, land_queue.SettingsGap):
            yield admission
            return
        try:
            locks.enter_context(main_push_lock(checkout, wait=wait))
        except BlockingIOError:
            yield MainPushBusy()
        else:
            yield MainPushReady()


def _settings_candidate(checkout: Path) -> land_queue.DirectSettingsPush | None:
    commit = os.environ.get("SETTINGS_PUSH_COMMIT", "")
    branch = os.environ.get("SETTINGS_PUSH_BRANCH", "")
    if not commit or branch != "main":
        return None
    result = subprocess.run(["git", "-C", str(checkout), "merge-base", "origin/main", commit],
                            capture_output=True, text=True, check=False)
    base = result.stdout.strip()
    if result.returncode:
        return None
    result = subprocess.run(["git", "-C", str(checkout), "log", "-m", "--format=", "--name-only",
                             "--no-renames", "-z", f"{base}..{commit}"], capture_output=True, text=True, check=False)
    paths = [name for name in result.stdout.split("\0") if name]
    if result.returncode or not land_queue.settings_only(paths):
        return None
    return {"kind": "direct", "checkout": str(checkout.resolve()), "commit": commit, "base": base, "branch": branch,
            "validated_base": os.environ.get("CI_VALIDATED_BASE", ""),
            "settings_json_only": set(paths) == {"settings.json"}}


class LockCommand(argparse.Namespace):
    checkout: Path = Path()
    command: list[str] = []


def _queue_direct(request: land_queue.DirectSettingsPush) -> int:
    script = Path(__file__).resolve().with_name("live_settings.py")
    result = subprocess.run([sys.executable, str(script), "queue-direct", json.dumps(request)], check=False)
    return result.returncode


def _live_hand_commit(checkout: Path, request: land_queue.DirectSettingsPush) -> bool:
    if not request.get("settings_json_only") or (checkout / "config").resolve() != conf.directory().resolve():
        return False
    branch = subprocess.run(["git", "-C", str(checkout), "branch", "--show-current"],
                            capture_output=True, text=True, check=False)
    return branch.returncode == 0 and branch.stdout.strip() == "main"


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
    request = _settings_candidate(args.checkout)
    if request is not None and _live_hand_commit(args.checkout, request):
        return _queue_direct(request)
    with ExitStack() as locks:
        if request is None:
            locks.enter_context(main_push_lock(args.checkout, Path(command[0]).name))
        else:
            admission = locks.enter_context(settings_push(args.checkout, wait=False))
            if not isinstance(admission, MainPushReady):
                return _queue_direct(request)
        result = subprocess.run(command, check=False, env=held_environment(args.checkout))
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
