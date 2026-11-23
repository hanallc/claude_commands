"""Keep each account's empty-at prediction every hour and the times of the real events, to check the prediction.

The log has three kinds of line:
- `forecast`: the prediction the Accounts page shows, with percent left and use per hour.
- `empty`: the first reading at 0% left. `reset`: a weekly reset or an early refill.
- `roles`: the account's roles changed, for example after an account move.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from scripts.lib import atomic_file, file_lock

if TYPE_CHECKING:
    from scripts.accounts import accounts
    from scripts.whoami import run_out

WINDOW = timedelta(days=35)
# One prediction per account each hour is enough to check it at 24, 12, 6 and 1 hours before the event.
INTERVAL = timedelta(hours=1)
# Percent left rises only at a refill; a smaller rise is a rounding difference between two reads.
REFILL_RISE = 1.0

type LineValue = str | float | bool | list[str] | None


class _AccountState(TypedDict):
    predicted_at: str | None
    left: float | None
    resets_at: str | None
    roles: list[str]


def log_path() -> Path:
    return Path.home() / ".local/state/accounts/forecast_log.jsonl"


def _state_path() -> Path:
    return log_path().with_suffix(".state.json")


def _lock_path() -> Path:
    return log_path().with_suffix(".lock")


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _stamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _read_state() -> dict[str, _AccountState]:
    try:
        document = cast(object, json.loads(_state_path().read_text(encoding="utf-8", errors="replace")))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    states: dict[str, _AccountState] = {}
    for label, value in cast(dict[object, object], document).items():
        if not isinstance(label, str) or not isinstance(value, dict):
            continue
        fields = cast(dict[str, object], value)
        predicted_at, left, resets_at, roles = (fields.get("predicted_at"), fields.get("left"),
                                                fields.get("resets_at"), fields.get("roles"))
        if (not (predicted_at is None or isinstance(predicted_at, str)) or not isinstance(roles, list)
                or not (left is None or (isinstance(left, (int, float)) and not isinstance(left, bool)))
                or not (resets_at is None or isinstance(resets_at, str))):
            continue
        names = [role for role in cast(list[object], roles) if isinstance(role, str)]
        states[label] = _AccountState(predicted_at=predicted_at, left=None if left is None else float(left),
                                      resets_at=resets_at, roles=names)
    return states


def _events(label: str, last: _AccountState, left: float | None, reset: datetime | None,
            read_at: datetime, roles: list[str]) -> list[dict[str, LineValue]]:
    events: list[dict[str, LineValue]] = []
    if left is not None and left <= 0 and (last["left"] is None or last["left"] > 0):
        events.append({"kind": "empty", "at": _stamp(read_at), "account": label})
    old_reset = _time(last["resets_at"])
    reset_moved = reset is not None and old_reset is not None and reset > old_reset
    refilled = left is not None and last["left"] is not None and left - last["left"] >= REFILL_RISE
    if reset_moved or refilled:
        # A scheduled reset happened at its time; an early refill shows at the read that found it.
        at = old_reset if reset_moved and old_reset is not None and old_reset <= read_at else read_at
        events.append({"kind": "reset", "at": _stamp(at), "account": label})
    if roles != last["roles"]:
        events.append({"kind": "roles", "at": _stamp(read_at), "account": label, "roles": roles})
    return events


def _forecast(row: accounts.StatusRow, left: float, reset: datetime | None, read_at: datetime,
              histories: dict[str, list[run_out.Reading]], now: datetime) -> dict[str, LineValue]:
    from scripts.whoami import quota_alert, run_out

    # The same inputs as the Accounts page, so the log keeps what the page shows.
    forecast = quota_alert.weekly_forecast(histories.get(row.label, []), now, reset,
                                           current=run_out.Reading(read_at, 100 - left))
    return {"kind": "forecast", "at": _stamp(now), "account": row.label, "left": left,
            "read_at": _stamp(read_at), "resets_at": _stamp(reset),
            "used_per_hour": forecast.used_per_hour if forecast else None,
            "empty_at": _stamp(forecast.empty_at) if forecast else None,
            "lasts_to_reset": forecast.lasts_to_deadline if forecast else None}


def _kept(text: str, now: datetime) -> list[str]:
    kept: list[str] = []
    for line in text.splitlines():
        try:
            document = cast(object, json.loads(line))
        except ValueError:
            continue
        at = _time(cast(dict[str, object], document).get("at")) if isinstance(document, dict) else None
        # A line after now stays: a clock that steps back loses no history.
        if at is not None and now - WINDOW <= at:
            kept.append(line + "\n")
    return kept


def sample(reading: accounts.StatusReading, now: datetime) -> None:
    """Write the events since the last pass, and each account's prediction once an hour."""
    from scripts.whoami import run_out

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(_lock_path()):
        states = _read_state()
        lines: list[dict[str, LineValue]] = []
        histories: dict[str, list[run_out.Reading]] | None = None
        predicted = False
        for row in reading.rows:
            left = float(row.weekly_left_percent) if isinstance(row.weekly_left_percent, (int, float)) else None
            reset = row.resets_at if isinstance(row.resets_at, datetime) else None
            read_at = row.read_at if isinstance(row.read_at, datetime) else now
            roles = sorted(row.roles)
            last = states.get(row.label)
            if last is not None:
                lines.extend(_events(row.label, last, left, reset, read_at, roles))
            last_prediction = _time(last["predicted_at"]) if last is not None else None
            predicted_at = _stamp(last_prediction)
            if left is not None and (last_prediction is None or not last_prediction <= now < last_prediction + INTERVAL):
                if histories is None:
                    histories = run_out.read_readings(run_out.READINGS_LOG)
                lines.append(_forecast(row, left, reset, read_at, histories, now))
                predicted, predicted_at = True, _stamp(now)
            states[row.label] = _AccountState(
                predicted_at=predicted_at,
                left=left if left is not None else (last["left"] if last is not None else None),
                resets_at=_stamp(reset) if reset is not None else (last["resets_at"] if last is not None else None),
                roles=roles)
        if lines or predicted:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                text = ""
            kept = _kept(text, now) if predicted else [text] if text else []
            kept.extend(json.dumps(line, sort_keys=True) + "\n" for line in lines)
            atomic_file.write_text_atomic(path, "".join(kept))
        atomic_file.write_text_atomic(_state_path(), json.dumps(states, sort_keys=True))
