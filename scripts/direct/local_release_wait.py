"""Wake a routing retry when a hold or admission state changes."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
import time

from scripts.direct.release_events import episode_is_assigned, local_release_waiter


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
    with local_release_waiter(holders, release, wake_files=wake_files) as events:
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
