#!/usr/bin/env python3
"""Send a queued Mac pull as soon as the Mac answers: one watcher runs on natedev while a pull waits.

  mac_wake.py watch   probe the Mac until it answers, then run `zone.py drain`; exit when nothing waits

start() runs the watcher in its own session when a pull waits and no watcher holds the lock.
The notes timer calls it each quota pass, so a watcher comes back after a reboot.
A fixture queue (ZONE_STATE, XDG_STATE_HOME or HOME moved) starts no watcher;
MAC_WAKE_PROGRAM replaces this file in tests.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import os
from pathlib import Path
import platform
import pwd
import subprocess
import sys
import time
from typing import TextIO

from scripts.lib import conf, main_publish

ZONE_SCRIPT = Path(__file__).resolve().with_name("zone.py")


def lock_path() -> Path:
    return main_publish.queue_path().with_name("mac-wake.lock")


def _waiting() -> bool:
    pending = main_publish.read_queue()
    return isinstance(pending, main_publish.PendingMacPull) and bool(pending.pulls or pending.text)


def _live_queue() -> bool:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return main_publish.queue_path() == home / ".local/state/zone/mac.json"


def _try_lock(handle: TextIO) -> bool:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def start() -> list[str]:
    """Start the watcher when a pull waits and none runs; return the line to show, if any."""
    program = os.environ.get("MAC_WAKE_PROGRAM") or ""
    if platform.system() == "Darwin" or not _waiting() or not (program or _live_queue()):
        return []
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        if not _try_lock(handle):
            return []
    _ = subprocess.Popen([sys.executable, program or str(Path(__file__).resolve()), "watch"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    return ["mac: a watcher sends the waiting pull as soon as the Mac answers"]


def _answers() -> bool:
    done = main_publish.ssh("true", ssh_program=os.environ.get("ZONE_SSH") or "ssh",
                            time_limit=main_publish.CommandTimeLimit(_seconds("mac_wake_probe_seconds")))
    return not isinstance(done, str) and done.returncode == 0


def _seconds(key: str) -> int:
    return conf.read_or_repository("live_settings").whole(key)


def watch() -> int:
    """Hold the lock; probe the Mac, pausing between probes, and drain the queue when it answers."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        if not _try_lock(handle):
            return 0
        while _waiting():
            if _answers():
                done = subprocess.run([sys.executable, str(ZONE_SCRIPT), "drain"], capture_output=True,
                                      text=True, check=False)
                print(done.stdout, end="")
                if not _waiting():
                    return 0
            time.sleep(_seconds("mac_wake_pause_seconds"))
    return 0


def main(arguments: list[str]) -> int:
    if arguments != ["watch"]:
        print("usage: mac_wake.py watch", file=sys.stderr)
        return 2
    try:
        return watch()
    except (OSError, conf.Unusable) as error:
        print(f"mac_wake: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
