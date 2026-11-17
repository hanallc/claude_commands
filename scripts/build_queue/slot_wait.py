"""Wait for repository slot capacity through the build queue."""
from __future__ import annotations

import os
from pathlib import Path

from scripts.lib import conf
from scripts.build_queue import admission_wire as wire


class SlotWaitExpired(RuntimeError):
    """The repository pool has no capacity before the wait ends."""


def read_pool(repository: str, machine: str, root: Path, locks: str) -> wire.RepositorySlotWaiting:
    """Read lock holders without taking a slot lock or changing a slot."""
    settings = conf.read_or_repository("build_folders")
    key = f"slots.{machine}.{repository}.max"
    maximum = settings.whole(key if key in settings.values() else f"slots.{machine}.default_max")
    holders: set[tuple[int, int, int]] = set()
    for line in locks.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK" or fields[3] != "WRITE":
            continue
        major, minor, inode = fields[5].split(":")
        holders.add((int(major, 16), int(minor, 16), int(inode)))
    held: list[int] = []
    for number in range(1, maximum + 1):
        try:
            info = (root / f"{repository}-slot{number}.lock").stat()
        except FileNotFoundError:
            continue
        if (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino) in holders:
            held.append(number)
    return wire.RepositorySlotWaiting(repository, tuple(held), maximum, key)


def wait_for_slot(repository: str, machine: str, root: Path, maximum: int, *, episode: str,
                  since: float, showrunner: str, unit: str, work: str, purpose: str) -> None:
    """Block on one queue connection; retain episode and since after a lost race."""
    from scripts.build_queue import admission_client as client, admission_records as records
    from scripts.build_queue import admission_protocol as wire

    seconds = conf.read_or_repository("build_folders").number("slots.wait_seconds")
    start = records.process_start_time(os.getpid())
    if start is None:
        raise RuntimeError("the slot waiter has no process identity")
    identity = wire.Identity(os.environ.get("CLAUDE_CODE_SESSION_ID", "") or os.environ.get("CODEX_THREAD_ID", ""),
                             showrunner, purpose, work, unit)
    request = wire.RepositorySlotRequest(episode, wire.Owner(os.getpid(), start), since,
        repository, machine, str(root), maximum, since + seconds, identity)
    reply = client.repository_slot(request)
    if isinstance(reply, wire.RepositorySlotExpired):
        raise SlotWaitExpired(reply.reason)
    if isinstance(reply, wire.Refused):
        raise RuntimeError(reply.reason)
