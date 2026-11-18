"""Read recorder phase state without loading the recorder command."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

STATE_FILENAME = "progress_history_state.json"
OPEN_PHASES_KEY = "open_phases"
LEGACY_PHASE_FIELDS = (
    "activity",
    "early_review",
    "phase_last_percent",
    "phase_percent_started_at",
    "last_percent",
    "percent_started_at",
    "pending_calibration",
)


def _record(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def open_phases(state: dict[str, object]) -> list[dict[str, object]]:
    """Return the open phases in start order, including those in older records."""
    raw = state.get(OPEN_PHASES_KEY)
    records = cast(list[object], raw) if isinstance(raw, list) else []
    phases = [cast(dict[str, object], item) for item in records if isinstance(item, dict)]
    legacy = _record(state.get("phase"))
    if legacy.get("status") == "active":
        phases.append(legacy)
    return phases


def migrate_phases(state: dict[str, object]) -> None:
    """Put all open phases and their state in one list before any recorder write."""
    phases = open_phases(state)
    state[OPEN_PHASES_KEY] = phases
    legacy = _record(state.get("phase"))
    fields = {key: state.pop(key) for key in LEGACY_PHASE_FIELDS if key in state}
    legacy.update(fields)
    if legacy.get("status") == "active":
        state["phase"] = {}
    for phase in phases:
        saved = _record(phase.pop("parked", None))
        phase.update(saved)


def open_phase_ids(state: dict[str, object]) -> list[str]:
    return [value for phase in open_phases(state) if isinstance(value := phase.get("id"), str)]


def only_open_phase(state: dict[str, object]) -> dict[str, object] | None:
    """Select the only open phase or require a name when several are open."""
    phases = open_phases(state)
    if len(phases) > 1:
        names = ", ".join(open_phase_ids(state))
        raise SystemExit(f"Several phases are open ({names}). Name one with --phase-id.")
    return phases[0] if phases else None


def read_open_phase_ids(session_dir: Path) -> list[str]:
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
    return open_phase_ids(cast(dict[str, object], value))
