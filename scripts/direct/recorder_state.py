"""Read recorder phase state without loading the recorder command."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

STATE_FILENAME = "progress_history_state.json"
OPEN_PHASES_KEY = "open_phases"


class OldRecorderState(RuntimeError):
    """The recorder state requires conversion before use."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"Recorder state {path} has the old shape. Run the conversion: "
            + "/tmp/claude/unit/bd19678b-c7b1-4e24-be9e-133ecfe24c7a/phases/parallelphases/deploy/run_conversion.sh"
        )


def require_current_shape(path: Path, state: dict[str, object]) -> None:
    """Check the shape before interpreting the recorder state."""
    if "phase_last_percent" in state:
        raise OldRecorderState(path)


def read_state(path: Path) -> dict[str, object]:
    """Read the current shape of the recorder state."""
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid progress state: {path}")
    state = cast(dict[str, object], value)
    require_current_shape(path, state)
    return state


def open_phases(state: dict[str, object]) -> list[dict[str, object]]:
    """Return the open phases in start order."""
    raw = state.get(OPEN_PHASES_KEY)
    records = cast(list[object], raw) if isinstance(raw, list) else []
    return [cast(dict[str, object], item) for item in records if isinstance(item, dict)]


def open_instance_ids(phases: list[dict[str, object]]) -> set[str]:
    """Return the instance ids of the open phases."""
    return {value for phase in phases if isinstance(value := phase.get("instance_id"), str)}


def open_phase_ids(state: dict[str, object]) -> list[str]:
    return [value for phase in open_phases(state) if isinstance(value := phase.get("id"), str)]


def only_open_phase(state: dict[str, object]) -> dict[str, object] | None:
    """Select the only open phase or require a name when several are open."""
    phases = open_phases(state)
    if len(phases) > 1:
        names = ", ".join(open_phase_ids(state))
        raise SystemExit(f"Several phases are open ({names}). Name one with --phase-id.")
    return phases[0] if phases else None


def read_open_phases(session_dir: Path) -> list[dict[str, object]]:
    """Read atomically published recorder state. A missing file has no open phases."""
    path = session_dir / STATE_FILENAME
    try:
        state = read_state(path)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(f"Unable to read progress state {path}: {error}") from error
    return open_phases(state)


def read_open_phase_ids(session_dir: Path) -> list[str]:
    return [value for phase in read_open_phases(session_dir) if isinstance(value := phase.get("id"), str)]
