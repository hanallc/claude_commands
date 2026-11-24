"""Find the shared showrunner state root."""
import os
from pathlib import Path


def state_root() -> Path:
    """Return the shared root for records owned by staff."""
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
