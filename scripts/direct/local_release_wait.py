"""Wake a routing retry when a hold or admission state changes."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
from pathlib import Path
import sys
import time
from typing import cast

from scripts.lib import local_release_events


def episode_is_assigned(state_file: Path, episode: str) -> bool:
    """Read an assignment as a wake signal without granting permission."""
    try:
        state = cast(object, json.loads(state_file.read_text()))
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict):
        return False
    fields = cast(dict[str, object], state)
    current = fields.get("current_assignments")
    assignments = fields.get("assignments")
    if not isinstance(current, dict) or not isinstance(assignments, dict):
        return False
    key = cast(dict[str, object], current).get(episode)
    saved = cast(dict[str, object], assignments).get(key) if isinstance(key, str) else None
    if not isinstance(saved, dict):
        return False
    attempt = cast(dict[str, object], saved)
    permission = attempt.get("permission")
    if isinstance(permission, dict) and cast(dict[str, object], permission).get("kind") == "PermissionAvailable":
        return True
    assignment = attempt.get("assignment")
    destination = cast(dict[str, object], assignment).get("destination") if isinstance(assignment, dict) else None
    return isinstance(destination, dict) and cast(dict[str, object], destination).get("provider") == "local"


def wait_for_local_release(seconds: float, call_id: str, episode: str = "",
                           *, provider_evidence_pending: bool = False,
                           wait_for_assignment: bool = False) -> None:
    _ = call_id
    from scripts.build_targets import build_targets
    from scripts.build_targets.targets import Held, target_state
    from scripts.build_hold import build_hold
    from scripts.buildlog import store

    target = build_targets.local_target()
    holders = build_hold.holder_directory()
    release = build_hold.release_directory()
    state_file = store.root() / "admission/state.json"
    wake_files = (state_file,) if episode else ()
    if provider_evidence_pending:
        wake_files += (state_file.with_name("queue.json"),)
    deadline = time.monotonic() + seconds
    with local_release_events.local_release_waiter(holders, release, wake_files=wake_files) as events:
        while True:
            events.arm()
            # Arm before reading state: a release before this
            # helper's startup is observed here, and a later release queues an event.
            state = target_state(target)
            if wait_for_assignment:
                if isinstance(state, Held):
                    return
            elif not isinstance(state, Held):
                return
            if episode and episode_is_assigned(state_file, episode):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            events.wait(remaining)
            # A completed pass can supply the first blocked provider evidence without an assignment.
            if provider_evidence_pending:
                return


if __name__ == "__main__":
    wait_for_local_release(float(sys.argv[1]), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "",
                           provider_evidence_pending=len(sys.argv) > 4 and sys.argv[4] == "evidence",
                           wait_for_assignment=len(sys.argv) > 5 and sys.argv[5] == "assignment")
