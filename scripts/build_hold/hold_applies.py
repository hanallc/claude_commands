#!/usr/bin/env python3
"""Report whether natedev's hold refuses the calling session's build."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys

from scripts.build_targets import build_targets
from scripts.build_hold import build_hold
from scripts.build_targets.targets import Held, StateUnreadable, target_state


class HoldStateReadError(ValueError):
    """The hold decision could not be read; admission must stop."""


def hold_applies() -> str:
    """Return the refusal while another session's holder file exists.

    Raise HoldStateReadError when the hold state cannot be read.
    """
    try:
        state = target_state(build_targets.local_target())
        if isinstance(state, StateUnreadable):
            raise HoldStateReadError(state.reason)
        directory = build_hold.holder_directory()
        holds = build_hold.natedev_holds(directory)
        if not build_targets.caller_waits_on_hold(holds, directory):
            return ""
        if not isinstance(state, Held):
            raise HoldStateReadError("holder file exists; target state disagrees")
        names = "; ".join(build_hold.holder_text(holder) for holder in holds.in_force)
        return (f"natedev builds are held by {names}. Keep working, writes included. "
                "Use verify.sh: eligible steps go to the Mac; other steps wait. "
                "Raw cargo and merge gates must wait for the hold to end.")
    except (OSError, UnicodeError, ValueError) as error:
        raise HoldStateReadError(str(error)) from error


def main() -> int:
    try:
        reason = hold_applies()
    except HoldStateReadError as error:
        print(f"natedev hold state cannot be read: {error}", file=sys.stderr)
        return 2
    if not reason:
        return 1
    print(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
