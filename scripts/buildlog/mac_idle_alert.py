"""The saved Mac idle reading and the one line shown by build views."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast
from zoneinfo import ZoneInfo

from scripts.buildlog import store
from scripts.lib import conf


@dataclass(frozen=True)
class MacIdleConfig:
    """Validated timing policy shared by the idle watch and its views."""

    alert_seconds: float
    push_seconds: float
    stale_seconds: float
    repeat_minutes: float
    send_timeout_s: float


# Keep the public error name for callers that report idle configuration failures.
MacIdleConfigError = conf.Unusable


def idle_config(config: conf.ConfigRead) -> MacIdleConfig:
    """Read the idle policy from one validated configuration file."""
    return MacIdleConfig(config.number("alert_seconds"), config.number("push_seconds"),
                         config.number("stale_seconds"), config.number("repeat_minutes"),
                         config.number("send_timeout_s"))


def read_idle_config() -> MacIdleConfig:
    return idle_config(conf.read("mac_test"))


def nonnegative_time(value: object) -> bool:
    """JSON numbers used as times must also fit a finite float."""
    try:
        return type(value) in (int, float) and math.isfinite(cast(float, value)) and cast(float, value) >= 0
    except OverflowError:
        return False


def state_dir() -> Path:
    return Path(os.environ.get("MAC_IDLE_STATE_DIR", str(store.root().parent / "mac-idle"))).expanduser()


class SavedAlert(TypedDict):
    version: int
    written: float
    state: Literal["busy", "idle", "unreachable", "unreadable"]
    since: float
    changed: float
    waiting: int
    cause: Literal["blocked", "claim_idle", "free", "disk", ""]
    holder: str
    detail: str
    until: float
    messaged: bool
    pushed: bool
    admission_error: NotRequired[str]
    kept_claims: NotRequired[int]


@dataclass(frozen=True)
class NoSavedAlert:
    """The watch has not written a reading."""


@dataclass(frozen=True)
class UnreadableAlert:
    """The reason explains why the idle alert cannot be read."""
    reason: str


def read_alert(path: Path) -> SavedAlert | NoSavedAlert | UnreadableAlert:
    try:
        raw = cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return NoSavedAlert()
    except (OSError, ValueError, UnicodeError) as error:
        return UnreadableAlert(str(error))
    if not isinstance(raw, dict):
        return UnreadableAlert("saved reading is not an object")
    row = cast(dict[str, object], raw)
    if type(row.get("version")) is not int or row["version"] != 1:
        return UnreadableAlert("invalid version")
    for field in ("written", "since", "changed", "until"):
        if not nonnegative_time(row.get(field)):
            return UnreadableAlert(f"invalid {field}")
    if row.get("state") not in ("busy", "idle", "unreachable", "unreadable"):
        return UnreadableAlert("invalid state")
    if row.get("cause") not in ("blocked", "claim_idle", "free", "disk", ""):
        return UnreadableAlert("invalid cause")
    waiting = row.get("waiting")
    if type(waiting) is not int or waiting < 0:
        return UnreadableAlert("invalid waiting")
    kept_claims = row.get("kept_claims", 0)
    if type(kept_claims) is not int or kept_claims < 0:
        return UnreadableAlert("invalid kept_claims")
    for field in ("holder", "detail"):
        if not isinstance(row.get(field), str):
            return UnreadableAlert(f"invalid {field}")
    if "admission_error" in row and not isinstance(row["admission_error"], str):
        return UnreadableAlert("invalid admission_error")
    for field in ("messaged", "pushed"):
        if type(row.get(field)) is not bool:
            return UnreadableAlert(f"invalid {field}")
    return cast(SavedAlert, cast(object, row))


def display_field(value: str) -> str:
    return " ".join("".join(" " if character.isspace() else character
                            for character in value if character.isprintable() or character.isspace()).split())


def clock_text(at: float, zone: ZoneInfo, *, include_zone: bool = False) -> str:
    try:
        return datetime.fromtimestamp(at, zone).strftime("%H:%M %Z" if include_zone else "%H:%M")
    except (OSError, OverflowError, ValueError):
        return "unknown time"


class BlockText(Enum):
    """The Builds page shows a Mac block in its own card, so its alert keeps only the diagnostics."""

    SHOWN = auto()
    OMITTED = auto()


def kept_claims_text(count: int) -> str:
    """Name retained claims without reporting running work."""
    noun = "claim" if count == 1 else "claims"
    return f"{count} {noun} kept for cleanup"


def waiting_steps_text(count: int) -> str:
    """Use the count to select the noun and verb."""
    return f"{count} step waits" if count == 1 else f"{count} steps wait"


def _live_state_agrees(saved: SavedAlert, now: float) -> bool:
    """Compare the saved state and cause with the live Mac state.

    The saved reading can be one sample old. In that time a block can end or a claim can start work.
    The live read uses only local files and never contacts the Mac.
    """
    from scripts.buildlog import mac_idle

    live = mac_idle.mac_work(mac_idle.watch_paths(), now)
    if saved["state"] == "unreachable":
        return isinstance(live, mac_idle.MacUnreachable)
    if saved["cause"] in ("blocked", "claim_idle"):
        return isinstance(live, mac_idle.IdleReservation) and live.cause == saved["cause"]
    return isinstance(live, mac_idle.FreeMac)


def alert_line(path: Path, now: float, zone: ZoneInfo, *, block: BlockText = BlockText.SHOWN) -> str | None:
    """Show the saved Mac condition only when the live Mac state agrees with it."""
    saved = read_alert(path)
    if isinstance(saved, NoSavedAlert):
        return None
    if isinstance(saved, UnreadableAlert):
        return f"MAC IDLE WATCH: Mac state unreadable: {display_field(saved.reason)}."
    try:
        settings = read_idle_config()
    except MacIdleConfigError as error:
        return f"MAC IDLE WATCH: config unreadable: {display_field(str(error))}."
    if now - saved["written"] > settings.stale_seconds:
        return f"MAC IDLE WATCH: no reading since {clock_text(saved['written'], zone)}; check buildlog-sample.timer."
    if saved["state"] == "unreadable":
        return f"MAC IDLE WATCH: Mac state unreadable: {display_field(saved['detail'])}."
    admission_error = saved.get("admission_error", "")
    query_reason = f"admission query failed: {display_field(admission_error)}" if admission_error else ""
    quiet = f"MAC IDLE WATCH: {query_reason}." if query_reason else None
    if saved["state"] == "busy" or saved["waiting"] == 0:
        return quiet
    waiting = saved["waiting"]
    if saved["state"] == "unreachable":
        if not _live_state_agrees(saved, now):
            return quiet
        return f"MAC UNREACHABLE until {clock_text(saved['until'], zone)}: {waiting_steps_text(waiting)} for the Mac."
    elapsed = now - saved["since"]
    if elapsed < settings.alert_seconds or saved["cause"] == "blocked" and block is BlockText.OMITTED:
        return quiet
    if not _live_state_agrees(saved, now):
        return quiet
    holder, detail = display_field(saved["holder"]), display_field(saved["detail"])
    suffix = f"; {query_reason}" if query_reason else ""
    if saved["cause"] == "blocked":
        return (f"MAC BLOCKED by {holder} since {clock_text(saved['since'], zone)} ({detail}): {waiting_steps_text(waiting)} "
                f"for the Mac, idle {int(elapsed // 60)} min; /build:mac unblock clears it{suffix}.")
    if saved["cause"] == "claim_idle":
        cause = f"Claim of {holder} ({detail}) runs no build or test on the Mac"
    elif saved["cause"] == "disk":
        cause = "Mac disk exclusion keeps these steps on natedev"
    else:
        kept_claims = saved.get("kept_claims", 0)
        cause = (f"No block; {kept_claims_text(kept_claims)}. Kept claims hold no work; "
                 if kept_claims else "No block and no claim; ")
        cause += "the waiting steps do not take the Mac"
    return f"MAC IDLE {int(elapsed // 60)} min: {waiting_steps_text(waiting)} for the Mac. {cause}{suffix}."
