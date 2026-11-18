"""Wait for repository slot capacity through the build queue."""
from __future__ import annotations

import os
from pathlib import Path

from scripts.lib import build_folders, conf
from scripts.build_queue import admission_wire as wire


class SlotWaitExpired(RuntimeError):
    """The repository pool has no capacity before the wait ends."""


def read_pool(repository: str, machine: str, root: Path, _locks: str) -> wire.RepositorySlotWaiting:
    """Check each slot without waiting for its lease."""
    settings = conf.read_or_repository("build_folders")
    key = f"slots.{machine}.{repository}.max"
    maximum = settings.whole(key if key in settings.values() else f"slots.{machine}.default_max")
    held = tuple(number for number in range(1, maximum + 1)
                 if build_folders.build_folder_state(root / f"{repository}-slot{number}", wait=False) == "held")
    return wire.RepositorySlotWaiting(repository, held, maximum, key)


def wait_for_slot(repository: str, machine: str, root: Path, maximum: int, *, episode: str,
                  since: float, showrunner: str, unit: str, work: str, purpose: str,
                  expires_at: float | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> None:
    """Block on one queue connection; retain episode and since after a lost race."""
    from scripts.build_queue import admission_client as client, admission_records as records
    from scripts.build_queue import admission_protocol as wire

    seconds = conf.read_or_repository("build_folders").number("slots.wait_seconds")
    deadline = since + seconds if isinstance(expires_at, conf.UseConfiguredValue) else expires_at
    start = records.process_start_time(os.getpid())
    if start is None:
        raise RuntimeError("the slot waiter has no process identity")
    identity = wire.Identity(os.environ.get("CLAUDE_CODE_SESSION_ID", "") or os.environ.get("CODEX_THREAD_ID", ""),
                             showrunner, purpose, work, unit)
    request = wire.RepositorySlotRequest(episode, wire.Owner(os.getpid(), start), since,
        repository, machine, str(root), maximum, deadline, identity)
    reply = client.repository_slot(request)
    if isinstance(reply, wire.RepositorySlotExpired):
        raise SlotWaitExpired(reply.reason)
    if isinstance(reply, wire.Refused):
        raise RuntimeError(reply.reason)
