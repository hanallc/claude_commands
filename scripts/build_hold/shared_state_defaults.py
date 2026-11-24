"""Paths and stop confirmations shared by build-state owners."""

import os
import re
from pathlib import Path
from typing import Literal

from scripts.lib.atomic_file import write_text_atomic


DEFAULT_HOLDER_DIRECTORY = Path(".local/state/build-hold")
DEFAULT_RELEASE_DIRECTORY = DEFAULT_HOLDER_DIRECTORY.with_name(DEFAULT_HOLDER_DIRECTORY.name + "-release")
DEFAULT_ENFORCER_DIRECTORY = Path(".local/state/build-hold-enforcer")
DEFAULT_PROC_ROOT = Path("/proc")
DEFAULT_ADMISSION_SOCKET = Path("/run/build-admission/build-admission.sock")


StopStatus = Literal["stopped", "not_stopped"]
StopConfirmation = StopStatus | Literal["absent", "unreadable"]


def holder_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / DEFAULT_HOLDER_DIRECTORY)))


def release_directory() -> Path:
    explicit = os.environ.get("BUILD_HOLD_RELEASE_DIR")
    if explicit is not None:
        return Path(explicit)
    if "BUILD_HOLD_DIR" not in os.environ:
        return Path.home() / DEFAULT_RELEASE_DIRECTORY
    holders = holder_directory().absolute()
    return holders.with_name(holders.name + "-release")


def stop_confirmation_path(request_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("invalid stop confirmation ID")
    return release_directory() / "stop-confirmations" / f"{request_id}.txt"


def write_stop_confirmation(path: Path, status: StopStatus) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, status + "\n")


def read_stop_confirmation_state(request_id: str) -> StopConfirmation:
    try:
        status = stop_confirmation_path(request_id).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "absent"
    except (OSError, UnicodeError):
        return "unreadable"
    if status == "stopped":
        return "stopped"
    return "not_stopped" if status == "not_stopped" else "unreadable"


def read_stop_confirmation(request_id: str) -> StopStatus | None:
    status = read_stop_confirmation_state(request_id)
    return status if status in ("stopped", "not_stopped") else None
