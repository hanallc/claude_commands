"""Read recorder phase state without loading the recorder command."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

STATE_FILENAME = "progress_history_state.json"
OPEN_PHASES_KEY = "open_phases"


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
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise ValueError(f"Unable to read progress state {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Invalid progress state: {path}")
    return open_phases(cast(dict[str, object], value))


def read_open_phase_ids(session_dir: Path) -> list[str]:
    return [value for phase in read_open_phases(session_dir) if isinstance(value := phase.get("id"), str)]
