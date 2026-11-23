"""Refused work outranks a last good usage reading until allowance returns."""
from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast


from scripts.lib import conf, models_registry, registry_rows
from scripts.whoami import usage_store  # noqa: E402

@dataclass(frozen=True)
class KnownReset:
    at: datetime


@dataclass(frozen=True)
class NoResetKnown:
    pass


@dataclass(frozen=True)
class NoWindow:
    pass


@dataclass(frozen=True)
class ReadingWindow:
    left: float
    reset: KnownReset | NoResetKnown
    at: datetime


@dataclass(frozen=True)
class FreshReading:
    weekly: ReadingWindow
    five_hour: ReadingWindow | NoWindow
    credits: str


@dataclass(frozen=True)
class StaleReading:
    weekly: ReadingWindow | NoWindow = field(default_factory=NoWindow)
    five_hour: ReadingWindow | NoWindow = field(default_factory=NoWindow)


@dataclass(frozen=True)
class NoReading:
    pass


def _reading_window(window: usage_store.StoredWindow) -> ReadingWindow:
    at = datetime.fromisoformat(window["at"])
    if at.tzinfo is None or not math.isfinite(window["left"]):
        raise ValueError("invalid usage reading")
    reset = NoResetKnown() if not window["resets"] else KnownReset(datetime.fromisoformat(window["resets"]))
    if isinstance(reset, KnownReset) and reset.at.tzinfo is None:
        raise ValueError("reset time needs an offset")
    return ReadingWindow(window["left"], reset, at)


def stored_reading(tool: Literal["claude", "codex"], login: str, now: datetime) -> FreshReading | StaleReading | NoReading:
    """Read saved usage with the configured age limit."""
    stale_after = timedelta(minutes=conf.read("accounts").whole("usage_stale_minutes"))
    return stored_reading_with_age(tool, login, now, stale_after)


def stored_reading_with_age(tool: Literal["claude", "codex"], login: str, now: datetime,
                            stale_after: timedelta) -> FreshReading | StaleReading | NoReading:
    """Read saved usage with the caller's age limit."""
    held = usage_store.load().get(usage_store.key(tool.capitalize(), login))
    if held is None or "weekly" not in held:
        return NoReading()
    try:
        weekly = _reading_window(held["weekly"])
        five = _reading_window(held["five_hour"]) if "five_hour" in held else NoWindow()
        if now - weekly.at > stale_after or weekly.at > now:
            return StaleReading(weekly, five)
        credits = held["credits"]["balance"] if "credits" in held else ""
        return FreshReading(weekly, five, credits)
    except (ValueError, TypeError):
        return NoReading()


@dataclass(frozen=True)
class KnownWindow:
    name: Literal["weekly", "five_hour"]


@dataclass(frozen=True)
class UnknownWindow:
    pass


@dataclass(frozen=True)
class ModelFamily:
    """One Claude model family, such as fable or opus, in lower case."""

    name: str


@dataclass(frozen=True)
class AllModels:
    """The refusal blocks every model on the account."""


@dataclass(frozen=True)
class UnknownFamily:
    """The caller does not know the model family of its work."""


UNKNOWN_FAMILY = UnknownFamily()
ALL_MODELS = AllModels()
# The provider resets a model family's allowance at most one week after a refusal.
FAMILY_WINDOW = timedelta(days=7)


def claude_families() -> frozenset[str]:
    """Read the family names in [claude.families] of the model registry; an unreadable registry names none."""
    try:
        # Resolve the registry from this call's HOME, as a launch does.
        sections = registry_rows.parse_registry(models_registry.registry_text(models_registry.registry_path(os.environ)))
    except (OSError, ValueError):
        return frozenset()
    return registry_families(sections)


def registry_families(sections: Mapping[str, Mapping[str, str]]) -> frozenset[str]:
    """Take the family names from the keys of [claude.families] in parsed registry sections."""
    return frozenset(name.casefold() for name in sections.get("claude.families", {}))


def model_family(model: str, families: frozenset[str]) -> ModelFamily | UnknownFamily:
    """Name the family of a Claude model ID or alias, such as claude-fable-5-2 or opus.

    Only a name in `families` is a family; any other model, such as opusplan or default, has an unknown family.
    """
    words = model.casefold().partition("[")[0].split("-")
    return next((ModelFamily(word) for word in words if word in families), UNKNOWN_FAMILY)


@dataclass(frozen=True)
class AccountRefusal:
    tool: Literal["claude", "codex"]
    login: str
    window: KnownWindow | UnknownWindow
    reset: KnownReset | NoResetKnown
    at: datetime
    family: ModelFamily | AllModels = ALL_MODELS


class RefusalWire(TypedDict):
    tool: str
    login: str
    window: str
    reset: str
    at: str
    family: NotRequired[str]


class RefusalsError(ValueError):
    pass


def store_file() -> Path:
    return Path.home() / ".local" / "state" / "accounts" / "refusals.json"


def key(tool: str, login: str) -> str:
    """The store key of an all-model refusal."""
    return f"{tool.casefold()}:{login.casefold()}"


def refusal_key(refusal: AccountRefusal) -> str:
    """A family refusal has its own key, so it never replaces the all-model record."""
    identity = key(refusal.tool, refusal.login)
    return identity + ":" + refusal.family.name if isinstance(refusal.family, ModelFamily) else identity


def _time(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError as error:
        raise RefusalsError("invalid refusal time") from error
    if value.tzinfo is None:
        raise RefusalsError("refusal time needs an offset")
    return value


def load() -> dict[str, AccountRefusal]:
    path = store_file()
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise RefusalsError(f"refusal store unreadable: {error}") from error
    if not isinstance(raw, dict):
        raise RefusalsError("refusal store must be an object")
    result: dict[str, AccountRefusal] = {}
    for value in cast(dict[str, object], raw).values():
        if not isinstance(value, dict):
            raise RefusalsError("invalid account refusal")
        held = cast(dict[str, object], value)
        if not all(isinstance(held.get(name), str) for name in ("tool", "login", "window", "reset", "at")):
            raise RefusalsError("invalid account refusal fields")
        wire = cast(RefusalWire, cast(object, held))
        if wire["tool"] not in {"claude", "codex"} or not wire["login"] or wire["window"] not in {"weekly", "five_hour", "unknown"}:
            raise RefusalsError("invalid account refusal identity or window")
        window = (UnknownWindow() if wire["window"] == "unknown" else KnownWindow(cast(Literal["weekly", "five_hour"], wire["window"])))
        reset = KnownReset(_time(wire["reset"])) if wire["reset"] else NoResetKnown()
        family = held.get("family", "")
        if not isinstance(family, str) or family and not family.isalpha():
            raise RefusalsError("invalid account refusal family")
        refusal = AccountRefusal(cast(Literal["claude", "codex"], wire["tool"]), wire["login"], window, reset, _time(wire["at"]),
                                 ModelFamily(family.casefold()) if family else ALL_MODELS)
        result[refusal_key(refusal)] = refusal
    return result


def _wire(refusal: AccountRefusal) -> RefusalWire:
    wire = RefusalWire(tool=refusal.tool, login=refusal.login,
        window=refusal.window.name if isinstance(refusal.window, KnownWindow) else "unknown",
        reset=refusal.reset.at.isoformat() if isinstance(refusal.reset, KnownReset) else "", at=refusal.at.isoformat())
    if isinstance(refusal.family, ModelFamily):
        wire["family"] = refusal.family.name
    return wire


def write_refusal(refusal: AccountRefusal) -> None:
    """Merge one account under a lock and replace the store atomically."""
    path = store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        held = load()
        identity = refusal_key(refusal)
        previous = held.get(identity)
        if previous is not None and previous.at > refusal.at:
            return
        held[identity] = refusal
        wire = {identity: _wire(item) for identity, item in held.items()}
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".refusals-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                _ = output.write(json.dumps(wire, indent=2, sort_keys=True) + "\n")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def resolved(refusal: AccountRefusal, reading: FreshReading | StaleReading | NoReading, now: datetime) -> bool:
    if isinstance(refusal.family, ModelFamily):
        # Saved readings hold no family window, so only time resolves a family refusal.
        return now >= (refusal.reset.at if isinstance(refusal.reset, KnownReset) else refusal.at + FAMILY_WINDOW)
    if isinstance(refusal.window, KnownWindow) and isinstance(refusal.reset, KnownReset) and refusal.reset.at <= now:
        return True
    if not isinstance(reading, FreshReading):
        return False
    if isinstance(refusal.window, KnownWindow):
        window = reading.weekly if refusal.window.name == "weekly" else reading.five_hour
        return (isinstance(window, ReadingWindow) and window.at > refusal.at
                and (window.left > 0 and (isinstance(window.reset, NoResetKnown) or window.reset != refusal.reset)
                     or isinstance(window.reset, KnownReset)
                     and isinstance(refusal.reset, KnownReset) and window.reset.at > refusal.reset.at))
    windows = ((reading.weekly,) if isinstance(reading.five_hour, NoWindow)
               else (reading.weekly, reading.five_hour))
    return all(window.at > refusal.at and window.left > 0 for window in windows)


def applicable(held: Mapping[str, AccountRefusal], tool: str, login: str,
               family: ModelFamily | UnknownFamily) -> tuple[AccountRefusal, ...]:
    """The account's refusals that apply to work of this family; an unknown family takes every refusal."""
    return tuple(refusal for refusal in held.values()
                 if key(refusal.tool, refusal.login) == key(tool, login)
                 and (isinstance(refusal.family, AllModels) or isinstance(family, UnknownFamily) or refusal.family == family))


def blocking(held: Mapping[str, AccountRefusal], tool: str, login: str, family: ModelFamily | UnknownFamily,
             reading: FreshReading | StaleReading | NoReading, now: datetime) -> AccountRefusal | None:
    """The first unresolved refusal that keeps work of this family off the account."""
    return next((refusal for refusal in applicable(held, tool, login, family)
                 if not resolved(refusal, reading, now)), None)
