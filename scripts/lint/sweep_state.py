"""Shared paths for sweep records."""
from __future__ import annotations

import os
from pathlib import Path


STATE_ENV = "LINT_SWEEP_STATE_DIR"
DEFAULT_STATE_DIR = os.path.join("~", ".local", "state", "lint-sweep")
TARGET_DELETES_NAME = "target-deletes.json"


def state_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get(STATE_ENV) or DEFAULT_STATE_DIR))
