"""The quota state file and the no-worker mode it records.

A leaf module: restart, resume and worker code read the mode here, so they never
import quota_alert and its account, message and pane modules.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypedDict, cast

from scripts.lib import conf


# Each command that starts a worker refuses with this text while no-worker mode is on.
NO_WORKER_START = "no-worker mode is on, so no worker starts; do the work directly"


class _SavedMode(TypedDict, total=False):
    no_worker_mode: bool


def state_path() -> Path:
    """The quota state file under the current XDG_STATE_HOME."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "quota_alert.json"


def no_worker_mode() -> bool:
    """Whether Codex workers have no capacity now; a missing or unreadable state reads as False.

    The path is read at each call, so a test with its own XDG_STATE_HOME never reads the live state.
    """
    try:
        saved = cast(_SavedMode, json.loads(state_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False
    return saved.get("no_worker_mode", False)


def no_worker_notice() -> str:
    """The notice that starts no-worker mode."""
    return conf.read("accounts").text("no_worker_mode_on")
