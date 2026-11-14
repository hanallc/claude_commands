"""Read no-worker mode from the quota state.

quota_alert.py writes the state. This module imports no other script module,
so each command that starts a worker can read the mode with no import cycle.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

# Each command that starts a worker refuses with this text while no-worker mode is on.
NO_WORKER_START = "no-worker mode is on, so no worker starts; do the work directly"


def state_path() -> Path:
    """The quota state file under the current XDG_STATE_HOME."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "quota_alert.json"


def no_worker_mode() -> bool:
    """Whether workers have no capacity now; a missing or unreadable state reads as False.

    The path is read at each call, so a test with its own XDG_STATE_HOME never reads the live state.
    """
    try:
        saved = cast(object, json.loads(state_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False
    return isinstance(saved, dict) and cast(dict[str, object], saved).get("no_worker_mode") is True
