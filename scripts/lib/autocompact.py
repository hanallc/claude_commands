"""Claude's auto-compact window: the one validator, and the per-session record of a unit director.

`add_unit.py --resume <id> --autocompact <tokens>` records the window of a unit session.
For a unit that already has a staff row, it only records and launches nothing.
Every later Claude launch of that session passes the record as `--autocompact <tokens>`,
and the record wins over the `unit` key in `[compact_windows]` of `config/models.conf`.
`--autocompact auto` removes the record, so the session uses the role window or settings.json.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

AUTO = "auto"
LOWEST = 100_000
HIGHEST = 1_000_000
_TOKENS = re.compile(r"([1-9][0-9]*)([kKmM]?)")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class BadAutocompact(ValueError):
    pass


def state_dir() -> Path:
    return Path.home() / ".local/state/unit-launch"


def _path(session_id: str) -> Path:
    if _SESSION_ID.fullmatch(session_id) is None:
        raise BadAutocompact(f"session id {session_id!r} cannot name an auto-compact record")
    return state_dir() / f"{session_id}.autocompact"


def checked(value: str) -> str:
    """`auto`, or a token count Claude accepts (100k to 1M), spelled as Claude's help spells it: `200k`, `1M`."""
    if value == AUTO:
        return value
    match = _TOKENS.fullmatch(value)
    if match is None:
        raise BadAutocompact(f"--autocompact takes auto or a token count such as 200k, not {value!r}")
    unit = match.group(2).lower()
    scale = {"": 1, "k": 1_000, "m": 1_000_000}[unit]
    digits = match.group(1)
    if len(digits) > len(str(HIGHEST)) or not LOWEST <= int(digits) * scale <= HIGHEST:
        raise BadAutocompact(f"--autocompact {value} is outside 100k to 1M")
    return digits + {"": "", "k": "k", "m": "M"}[unit]


def recorded(session_id: str) -> str:
    """The session's recorded window, or "" when it uses the saved setting."""
    try:
        return checked(_path(session_id).read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return ""


def record(session_id: str, value: str) -> None:
    """Keep `value` for every relaunch of the session; `auto` removes the record."""
    path = _path(session_id)
    value = checked(value)
    if value == AUTO:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            _ = output.write(value + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
