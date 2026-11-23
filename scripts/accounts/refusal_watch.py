#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Find local Claude quota refusals and start safe, account-grouped moves."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[2]
from scripts.lib import conf
from scripts.lib.transcripts import open_transcript, transcript_paths, uncompressed_path
from scripts.accounts import accounts, moves, next_moves, refusals, role_map
from scripts.agents import stop_results
from scripts.production import staff
from scripts.whoami import account
from scripts.whoami import agent_accounts
from scripts.whoami.note_file import canonical_label
from scripts.whoami import usage_store
from scripts.whoami.quota_alert import (RefusedAt, _refusal_window, blocked_next_move_line, blocked_next_move_reason_key, notify_blocked_next_move,  # pyright: ignore[reportPrivateUsage]
                         BlockedHeadReason as BlockedHeadReason, NoTargetReading as NoTargetReading,
                         TargetReadingUnavailable as TargetReadingUnavailable,
                         TargetLoginRenewalFailed as TargetLoginRenewalFailed,
                         TargetAtOrBelowThreshold as TargetAtOrBelowThreshold,
                         TargetUnresolvedRefusal as TargetUnresolvedRefusal,
                         TargetPreparationRefused,
                         TargetAccountUnavailable as TargetAccountUnavailable)

if TYPE_CHECKING:
    from scripts.agents import inventory as inventory_module
    from scripts.herdr import herdr
    from scripts.message import sessions
else:
    inventory_module = import_module("scripts.agents.inventory")
    sessions = import_module('scripts.message.sessions')
    herdr = import_module("scripts.herdr.herdr")


QUOTA_REFUSAL = "hit your usage limit"


CLAUDE_LIMIT_PHRASES = ("hit your session limit", "hit your weekly limit")


def _available_members(ids: tuple[str, ...]) -> tuple[str, ...]:
    if not ids:
        return ()
    try:
        record = staff.local_snapshot()
    except staff.StaffAbsent:
        record = staff.StaffSnapshot(())
    return tuple(identity for identity in ids if isinstance(staff.row_activity(
        row.id if (row := record.by_member_reference(identity)) is not None else moves.native_row_key(identity)),
        staff.RowAvailable))


@dataclass(frozen=True)
class NoSessionRefusal:
    """The session has no current transcript or assumed quota refusal."""


@dataclass(frozen=True)
class ContinuationDelivered:
    """The live owner accepted its continuation after the quota wait."""


@dataclass(frozen=True)
class ContinuationNotDelivered:
    reason: str


class LiveSessionMessenger(Protocol):
    def send_message(self, recipient: str, summary: str, text: str, *, key: str = "") -> Mapping[str, str]: ...
    def message_was_delivered(self, outcome: Mapping[str, object]) -> bool: ...


@dataclass(frozen=True)
class TranscriptRefusal:
    """A transcript refusal. `family` is the refusal's scope; `session_family` is the family of the session's work."""

    entry_id: str
    refused_at: RefusedAt
    window: refusals.KnownWindow | refusals.UnknownWindow
    text: str
    reset: refusals.KnownReset | refusals.NoResetKnown = field(default_factory=refusals.NoResetKnown)
    family: refusals.ModelFamily | refusals.AllModels = refusals.ALL_MODELS
    session_family: refusals.ModelFamily | refusals.UnknownFamily = refusals.UNKNOWN_FAMILY


@dataclass(frozen=True)
class ProofRefusal:
    entry_id: str
    refused_at: RefusedAt
    window: refusals.KnownWindow | refusals.UnknownWindow
    reset: refusals.KnownReset | refusals.NoResetKnown = field(default_factory=refusals.NoResetKnown)
    family: refusals.ModelFamily | refusals.AllModels = refusals.ALL_MODELS
    session_family: refusals.ModelFamily | refusals.UnknownFamily = refusals.UNKNOWN_FAMILY


_assumed_refusals: ContextVar[Mapping[str, ProofRefusal]] = ContextVar("assumed_refusals", default={})


class FirstRefusalObservation(TypedDict):
    login: str
    window: Literal["weekly", "five_hour", "unknown"]
    reset: NotRequired[str]
    at: str
    family: NotRequired[str]


WatchTransition = Literal["requested", "cancelled", "continued", "waited", "no room", "retry", "group selected"]


class WatchedSession(TypedDict):
    entry_id: str
    state: WatchTransition
    move_id: str
    refusal: NotRequired[FirstRefusalObservation]
    retry_decision: NotRequired[str]


class WatchState(TypedDict):
    sessions: dict[str, WatchedSession]
    notices: dict[str, str]
    auto_move_notices: NotRequired[dict[str, str]]
    auto_move_holds: NotRequired[dict[str, HoldStarted]]


class HoldStartedDocument(TypedDict):
    account: str
    at: str
    move_id: str
    percent: float
    minutes: NotRequired[float]
    source: NotRequired[dict[str, object]]


class WatchStateDocument(TypedDict):
    sessions: dict[str, WatchedSession]
    notices: dict[str, str]
    auto_move_notices: NotRequired[dict[str, str]]
    auto_move_holds: NotRequired[dict[str, HoldStartedDocument]]


@dataclass(frozen=True)
class LegacyHoldDuration:
    """A pre-duration hold still uses its entry's fixed minutes."""


@dataclass(frozen=True)
class GroupSelection:
    account: str | accounts.GroupNotConfigured
    move_id: str | accounts.NoGroupTransition


def _group_selection(selected: accounts.GroupAccounts, group: accounts.ClaudeGroup) -> GroupSelection:
    transition = accounts.group_transition(group)
    return GroupSelection(accounts.group_label(selected, group),
                          transition.move_id if isinstance(transition, accounts.GroupTransition) else transition)


@dataclass(frozen=True)
class HoldStarted:
    account: str
    at: datetime
    move_id: str | accounts.NoGroupTransition
    percent: float
    duration: next_moves.ResolvedHold | LegacyHoldDuration = LegacyHoldDuration()

    def __post_init__(self) -> None:
        if self.at.utcoffset() is None:
            raise ValueError("hold start needs a zone")

    def is_current(self, selection: GroupSelection, percent: float) -> bool:
        return (self.account == selection.account and self.move_id == selection.move_id
                and self.percent == percent)


@dataclass(frozen=True)
class LeftAtMost:
    percent: float
    hold_mode: next_moves.HoldMode = next_moves.FixedHold(0)


@dataclass(frozen=True)
class AutoMoveRule:
    entry_id: str
    group: accounts.ClaudeGroup
    to: str
    when: LeftAtMost
    fallback_targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnreadableNextMoveLog:
    reason: str


def read_auto_move_rules() -> tuple[AutoMoveRule | UnreadableNextMoveLog, ...]:
    try:
        return tuple(AutoMoveRule(entry.id, entry.group, entry.to,
                                  LeftAtMost(entry.at_percent, entry.hold_mode), entry.fallback_targets)
                     for entry in next_moves.read_next_moves() if isinstance(entry, next_moves.ClaudeGroupMove))
    except next_moves.NextMoveError as error:
        return (UnreadableNextMoveLog(str(error)),)


def state_root() -> Path:
    return Path.home() / ".local" / "state" / "accounts"


def _object(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".watch-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            _ = output.write(json.dumps(value, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load_state() -> WatchState:
    held = _object(state_root() / "refusal_watch.json")
    result = WatchState(sessions={}, notices={})
    raw_sessions = held.get("sessions")
    if isinstance(raw_sessions, dict):
        for session_id, raw in cast(dict[str, object], raw_sessions).items():
            if isinstance(raw, dict):
                value = cast(dict[str, object], raw)
                if (all(isinstance(value.get(key), str) for key in ("entry_id", "move_id"))
                        and value.get("state") in ("requested", "cancelled", "continued", "waited", "no room", "retry", "group selected")):
                    result["sessions"][session_id] = cast(WatchedSession, cast(object, value))
    raw_notices = held.get("notices")
    if isinstance(raw_notices, dict):
        result["notices"] = {key: value for key, value in cast(dict[str, object], raw_notices).items() if isinstance(value, str)}
    raw_auto_notices = held.get("auto_move_notices")
    if isinstance(raw_auto_notices, dict):
        result["auto_move_notices"] = {key: value for key, value in cast(dict[str, object], raw_auto_notices).items()
                                       if isinstance(value, str)}
    raw_holds = held.get("auto_move_holds")
    if isinstance(raw_holds, dict):
        holds: dict[str, HoldStarted] = {}
        for name, raw in cast(dict[str, object], raw_holds).items():
            if not isinstance(raw, dict):
                continue
            fields = cast(dict[str, object], raw)
            label, at = fields.get("account"), fields.get("at")
            move_id, percent = fields.get("move_id"), fields.get("percent")
            if (isinstance(label, str) and isinstance(at, str) and isinstance(move_id, str)
                    and isinstance(percent, (int, float)) and not isinstance(percent, bool)
                    and 0 <= percent <= 100 and math.isfinite(percent)):
                try:
                    holds[name] = HoldStarted(label, datetime.fromisoformat(at),
                                             move_id if move_id else accounts.NoGroupTransition(), float(percent),
                                             _decode_hold_duration(fields))
                except ValueError:
                    continue
        result["auto_move_holds"] = holds
    return result


def _decode_hold_duration(fields: dict[str, object]) -> next_moves.ResolvedHold | LegacyHoldDuration:
    if "minutes" not in fields:
        return LegacyHoldDuration()
    minutes, source = fields.get("minutes"), fields.get("source")
    if (not isinstance(minutes, (int, float)) or isinstance(minutes, bool)
            or minutes < 0 or not math.isfinite(minutes) or not isinstance(source, dict)):
        raise ValueError("invalid hold duration")
    details = cast(dict[str, object], source)
    kind = details.get("kind")
    if kind == "fixed":
        return next_moves.ResolvedHold(float(minutes), next_moves.FixedHold(float(minutes)))
    if kind == "no usable pace":
        return next_moves.ResolvedHold(float(minutes), next_moves.NoUsablePace())
    pace = details.get("points_per_hour")
    if (kind == "pace" and isinstance(pace, (int, float)) and not isinstance(pace, bool)
            and pace > 0 and math.isfinite(pace)):
        return next_moves.ResolvedHold(float(minutes), next_moves.PaceEstimate(float(pace)))
    raise ValueError("invalid hold source")


def _hold_document(hold: HoldStarted) -> HoldStartedDocument:
    document = HoldStartedDocument(account=hold.account, at=hold.at.isoformat(),
                                   move_id=hold.move_id if isinstance(hold.move_id, str) else "", percent=hold.percent)
    if isinstance(hold.duration, next_moves.ResolvedHold):
        document["minutes"] = hold.duration.minutes
        source = hold.duration.source
        document["source"] = ({"kind": "pace", "points_per_hour": source.points_per_hour}
                              if isinstance(source, next_moves.PaceEstimate) else
                              {"kind": "no usable pace"} if isinstance(source, next_moves.NoUsablePace)
                              else {"kind": "fixed"})
    return document


def _state_document(state: WatchState) -> WatchStateDocument:
    document = WatchStateDocument(sessions=state["sessions"], notices=state["notices"])
    if "auto_move_notices" in state:
        document["auto_move_notices"] = state["auto_move_notices"]
    if "auto_move_holds" in state:
        document["auto_move_holds"] = {
            name: _hold_document(hold)
            for name, hold in state["auto_move_holds"].items()
        }
    return document


def _save_state(state: WatchState) -> None:
    _atomic_json(state_root() / "refusal_watch.json", _state_document(state))


def started_next_move_holds(entries: tuple[next_moves.NextMove, ...]) -> dict[str, next_moves.StartedHold] | next_moves.WatchStateUnreadable:
    """Read current heads' resolved holds without reacquiring the editor's watch lock."""
    if not entries:
        return {}
    try:
        state = _load_state()
    except (OSError, ValueError, OverflowError) as error:
        print(f"accounts: anomaly: cannot read started holds: {error}", file=sys.stderr)
        return {}
    holds = state.get("auto_move_holds", {})
    if not holds:
        return {}
    selected = accounts.group_accounts()
    heads: set[accounts.ClaudeGroup] = set()
    started: dict[str, next_moves.StartedHold] = {}
    for entry in entries:
        if not isinstance(entry, next_moves.ClaudeGroupMove) or entry.group in heads:
            continue
        heads.add(entry.group)
        if isinstance(entry.hold_mode, next_moves.FixedHold) and entry.hold_mode.minutes == 0:
            continue
        hold = holds.get(entry.id)
        if hold is None or not isinstance(hold.duration, next_moves.ResolvedHold):
            continue
        selection = _group_selection(selected, entry.group)
        if hold.is_current(selection, entry.at_percent):
            started[entry.id] = next_moves.StartedHold(hold.at, hold.duration)
    return started


@contextmanager
def _watch_lock() -> Generator[None]:
    # The synchronization file does not initialize account state during a refused preparation.
    identity = hashlib.sha256(str(state_root()).encode()).hexdigest()
    lock = Path(tempfile.gettempdir()) / ("claude-refusal-watch-" + identity + ".lock")
    with lock.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


@dataclass(frozen=True)
class LockedNextMoveEdit:
    """An edit whose watch and log locks remain held through cleanup and commit."""

    before: tuple[next_moves.NextMove, ...]

    def commit(self, entries: tuple[next_moves.NextMove, ...]) -> None:
        heads: dict[next_moves.NextMoveGroup, str] = {}
        for entry in self.before:
            _ = heads.setdefault(entry.group, entry.id)
        new_heads: dict[next_moves.NextMoveGroup, str] = {}
        for entry in entries:
            _ = new_heads.setdefault(entry.group, entry.id)
        discarded = {entry.id for entry in self.before} - {entry.id for entry in entries}
        discarded.update(entry_id for group, entry_id in heads.items() if new_heads.get(group) != entry_id)
        prior_modes = {entry.id: entry.hold_mode for entry in self.before if isinstance(entry, next_moves.ClaudeGroupMove)}
        changed_modes = {entry.id for entry in entries if isinstance(entry, next_moves.ClaudeGroupMove)
                         and entry.id in prior_modes and prior_modes[entry.id] != entry.hold_mode}
        if discarded or changed_modes:
            state = _load_state()
            original = deepcopy(state)
            for entry_id in discarded | changed_modes:
                _ = state.get("auto_move_holds", {}).pop(entry_id, None)
            for entry_id in discarded:
                _ = state.get("auto_move_notices", {}).pop(entry_id, None)
            if state != original:
                _save_state(state)
            if discarded:
                from scripts.whoami import quota_alert
                with quota_alert.state_file() as quota_state:
                    for entry_id in discarded:
                        _ = quota_state.get("workers_move_notices", {}).pop(entry_id, None)
        next_moves.write_next_moves(entries)


@contextmanager
def editing_next_moves() -> Generator[LockedNextMoveEdit]:
    """Resolve edits under watch then log locks; cleanup must precede the log write."""
    with _watch_lock(), next_moves.next_moves_lock():
        yield LockedNextMoveEdit(next_moves.read_next_moves())


class TranscriptEntry(TypedDict, total=False):
    type: str
    isApiErrorMessage: bool
    error: str
    uuid: str
    timestamp: str
    message: object
    quotaLimits: object
    payload: object


@dataclass(frozen=True)
class NewestExchange:
    entry: TranscriptEntry


@dataclass(frozen=True)
class NoExchange:
    """The transcript has no readable exchange or turn start."""


def _entries_newest_first(path: Path, chunk_bytes: int) -> Generator[dict[str, object]]:
    """Read decoded transcript entries from the end; an unreadable file ends the read."""
    try:
        with open_transcript(path) as source:
            position = source.seek(0, os.SEEK_END)
            pending = b""
            while position:
                size = min(position, chunk_bytes)
                position -= size
                _ = source.seek(position)
                lines = (source.read(size) + pending).split(b"\n")
                pending = lines[0] if position else b""
                for line in reversed(lines[1:] if position else lines):
                    try:
                        value = cast(object, json.loads(line))
                    except (ValueError, TypeError, UnicodeError):
                        continue
                    if isinstance(value, dict):
                        yield cast(dict[str, object], value)
    except OSError:
        return


def _newest_exchange(path: Path, chunk_bytes: int) -> NewestExchange | NoExchange:
    """Read the newest exchange or turn start and ignore later bookkeeping."""
    for entry in _entries_newest_first(path, chunk_bytes):
        if entry.get("type") in ("user", "assistant"):
            return NewestExchange(cast(TranscriptEntry, cast(object, entry)))
        payload = entry.get("payload")
        if isinstance(payload, dict):
            message = cast(dict[str, object], payload)
            if (entry.get("type") == "event_msg" and (message.get("type") in (
                    "user_message", "agent_message", "error", "task_started")
                    or message.get("type") == "task_complete" and isinstance(message.get("error"), dict)) or
                    entry.get("type") == "response_item" and message.get("type") == "message"
                    and message.get("role") in ("user", "assistant")):
                return NewestExchange(cast(TranscriptEntry, cast(object, entry)))
    return NoExchange()


SYNTHETIC_MODEL = "<synthetic>"


def entry_model_family(entry: Mapping[str, object], families: frozenset[str]) -> refusals.ModelFamily | refusals.UnknownFamily | None:
    """Name the model family of one real assistant entry; give None for any other entry.

    A real entry whose model is not in `families` has an unknown family.
    """
    if entry.get("type") != "assistant" or entry.get("isApiErrorMessage") is True:
        return None
    message = entry.get("message")
    model = cast(dict[str, object], message).get("model") if isinstance(message, dict) else None
    candidates = [candidate for candidate in (model, entry.get("requestedModel")) if isinstance(candidate, str)]
    if model == SYNTHETIC_MODEL or not candidates:
        return None
    return next((family for candidate in candidates
                 if isinstance(family := refusals.model_family(candidate, families), refusals.ModelFamily)),
                refusals.UNKNOWN_FAMILY)


def _session_family(path: Path, chunk_bytes: int, families: frozenset[str]) -> refusals.ModelFamily | refusals.UnknownFamily:
    """Name the family of the newest real assistant entry before the refusal, even when it is unknown."""
    for entry in _entries_newest_first(path, chunk_bytes):
        if (family := entry_model_family(entry, families)) is not None:
            return family
    return refusals.UNKNOWN_FAMILY


def transcript_refusal(session_id: str, *, config_dir: Path | None = None, screen_text: str = "", account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> TranscriptRefusal | NoSessionRefusal:
    """Read transcript evidence first. Use supplied pane text only without transcript evidence."""
    account_config = accounts.read_account_settings(account_config)
    root = config_dir if config_dir is not None else Path.home() / ".claude"
    try:
        paths = transcript_paths(root / "projects", "*/" + session_id + ".jsonl")
        if not paths:
            homes = [config_dir] if config_dir is not None else agent_accounts.codex_homes(agent_accounts.codex_home())
            paths = [path for home in homes for folder in ("sessions", "archived_sessions")
                     for path in transcript_paths(home / folder, f"**/*{session_id}.jsonl")]
    except OSError:
        paths = []
    return _transcript_refusal(paths, account_config.whole("transcript_chunk_bytes"), screen_text=screen_text)


def _refusal_time(entry_id: object, timestamp: object) -> datetime | None:
    if not isinstance(entry_id, str) or not entry_id or not isinstance(timestamp, str):
        return None
    try:
        at = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _text_window(text: str, codex_error: bool) -> refusals.KnownWindow | refusals.UnknownWindow:
    folded = text.casefold()
    return (refusals.KnownWindow("weekly") if "weekly limit" in folded or codex_error and "weekly" in folded else
            refusals.KnownWindow("five_hour") if CLAUDE_LIMIT_PHRASES[0] in folded or codex_error and (
                "5-hour" in folded or "five-hour" in folded) else refusals.UnknownWindow())


# The provider names a model family limit in text such as "You've reached your Fable limit."
FAMILY_LIMIT_TEXT = re.compile(r"reached your ([A-Za-z]+) limit")
OVERAGE_LIMIT = "seven_day_overage_included"
FAMILY_LIMIT_PREFIX = "seven_day_"


def _text_family(text: str, families: frozenset[str]) -> refusals.ModelFamily | refusals.UnknownFamily:
    found = FAMILY_LIMIT_TEXT.search(text)
    name = found[1].casefold() if found else ""
    return refusals.ModelFamily(name) if name in families else refusals.UNKNOWN_FAMILY


def _rate_limit_fields(entry: Mapping[str, object]) -> tuple[object, object]:
    """Read the limit type and reset from apiErrorParams.rate_limit_info, then from quotaLimits."""
    sources: list[dict[str, object]] = []
    params = entry.get("apiErrorParams")
    if isinstance(params, dict):
        info = cast(dict[str, object], params).get("rate_limit_info")
        if isinstance(info, dict):
            sources.append(cast(dict[str, object], info))
    quota = entry.get("quotaLimits")
    if isinstance(quota, dict):
        sources.append(cast(dict[str, object], quota))
    return (next((source["rateLimitType"] for source in sources if "rateLimitType" in source), None),
            next((source["resetsAt"] for source in sources if "resetsAt" in source), None))


def _limit_reset(raw_reset: object) -> refusals.KnownReset | refusals.NoResetKnown:
    try:
        if isinstance(raw_reset, (int, float)) and not isinstance(raw_reset, bool):
            return refusals.KnownReset(datetime.fromtimestamp(raw_reset, timezone.utc))
        if isinstance(raw_reset, str):
            parsed = datetime.fromisoformat(raw_reset)
            if parsed.tzinfo is not None:
                return refusals.KnownReset(parsed)
    except (ValueError, OverflowError, OSError):
        pass
    return refusals.NoResetKnown()


def claude_refusal_shape(entry: Mapping[str, object]) -> bool:
    """Tell whether one Claude transcript entry is a rate limit API error."""
    return entry.get("type") == "assistant" and entry.get("isApiErrorMessage") is True and entry.get("error") == "rate_limit"


def claude_entry_refusal(entry: Mapping[str, object],
                         session_family: Callable[[], refusals.ModelFamily | refusals.UnknownFamily],
                         families: frozenset[str]) -> TranscriptRefusal | NoSessionRefusal:
    """Read one Claude transcript entry as a refusal with its window, reset and model family scope.

    The family that the refusal text names wins; `session_family` gives the prior model's family as the fallback.
    Only a name in `families` is a family.
    """
    if not claude_refusal_shape(entry):
        return NoSessionRefusal()
    entry_id = entry.get("uuid")
    at = _refusal_time(entry_id, entry.get("timestamp"))
    if at is None or not isinstance(entry_id, str):
        return NoSessionRefusal()
    message = entry.get("message")
    content = cast(dict[str, object], message).get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(str(cast(dict[str, object], block).get("text", ""))
                         for block in cast(list[object], content) if isinstance(block, dict))
    else:
        text = ""
    window = _text_window(text, False)
    rate_type, raw_reset = _rate_limit_fields(entry)
    # The text describes the refused request; the prior model describes an earlier one.
    family = _text_family(text, families)
    if isinstance(family, refusals.UnknownFamily):
        family = session_family()
    scope: refusals.ModelFamily | refusals.AllModels = refusals.ALL_MODELS
    if rate_type in ("seven_day", "weekly"):
        window = refusals.KnownWindow("weekly")
    elif rate_type == "five_hour":
        window = refusals.KnownWindow("five_hour")
    elif rate_type == OVERAGE_LIMIT:
        window = refusals.KnownWindow("weekly")
        scope = family if isinstance(family, refusals.ModelFamily) else refusals.ALL_MODELS
    elif isinstance(rate_type, str) and rate_type.startswith(FAMILY_LIMIT_PREFIX):
        window = refusals.KnownWindow("weekly")
        suffix = rate_type.removeprefix(FAMILY_LIMIT_PREFIX).casefold()
        scope = refusals.ModelFamily(suffix) if suffix in families else refusals.ALL_MODELS
    return TranscriptRefusal(entry_id, RefusedAt(at), window, text, _limit_reset(raw_reset), scope, family)


def _transcript_refusal(paths: list[Path], chunk_bytes: int, *, screen_text: str = "") -> TranscriptRefusal | NoSessionRefusal:
    try:
        newest = max(paths, key=lambda path: path.stat().st_mtime) if paths else None
        latest = _newest_exchange(newest, chunk_bytes) if newest is not None else NoExchange()
    except OSError:
        newest, latest = None, NoExchange()
    if isinstance(latest, NewestExchange) and latest.entry.get("type") != "event_msg":
        source = newest
        entry = cast(Mapping[str, object], latest.entry)
        if not claude_refusal_shape(entry):
            return NoSessionRefusal()
        # Read the registry only for a refusal; most passes see none.
        families = refusals.claude_families()
        return claude_entry_refusal(entry,
            lambda: _session_family(source, chunk_bytes, families) if source is not None else refusals.UNKNOWN_FAMILY,
            families)
    if isinstance(latest, NoExchange):
        text = screen_text
        timestamp: object = datetime.now(timezone.utc).isoformat()
        entry_id = hashlib.sha256(text.encode()).hexdigest()
    else:
        entry = latest.entry
        timestamp = entry.get("timestamp")
        payload_value = entry.get("payload")
        if not isinstance(payload_value, dict):
            return NoSessionRefusal()
        payload = cast(dict[str, object], payload_value)
        error = payload.get("error")
        if payload.get("type") == "task_complete" and isinstance(error, dict):
            text = str(cast(dict[str, object], error).get("message", ""))
        elif payload.get("type") == "error":
            text = str(payload.get("message", ""))
        else:
            return NoSessionRefusal()
        entry_id = entry.get("uuid") or hashlib.sha256((str(timestamp or "") + text).encode()).hexdigest()
    if QUOTA_REFUSAL not in text.casefold():
        if not isinstance(latest, NoExchange) or not any(phrase in text.casefold() for phrase in CLAUDE_LIMIT_PHRASES):
            return NoSessionRefusal()
    at = _refusal_time(entry_id, timestamp)
    if at is None:
        return NoSessionRefusal()
    return TranscriptRefusal(entry_id, RefusedAt(at), _text_window(text, True), text)


def assumed_refusal(session_id: str) -> ProofRefusal | NoSessionRefusal:
    return _assumed_refusals.get().get(session_id, NoSessionRefusal())


def session_refusal(session_id: str, *, account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> TranscriptRefusal | ProofRefusal | NoSessionRefusal:
    account_config = accounts.read_account_settings(account_config)
    assumed = assumed_refusal(session_id)
    return transcript_refusal(session_id, account_config=account_config) if isinstance(assumed, NoSessionRefusal) else assumed


def session_refusals(session_ids: list[str], *, account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> dict[str, TranscriptRefusal | ProofRefusal | NoSessionRefusal]:
    """Find transcripts for this pass without a persistent cache."""
    account_config = accounts.read_account_settings(account_config)
    detected: dict[str, TranscriptRefusal | ProofRefusal | NoSessionRefusal] = {
        session_id: assumed_refusal(session_id) for session_id in session_ids}
    wanted = {session_id + ".jsonl": session_id for session_id, refusal in detected.items()
              if isinstance(refusal, NoSessionRefusal)}
    if not wanted:
        return detected
    paths: dict[str, list[Path]] = {session_id: [] for session_id in wanted.values()}
    # Keep the index within this pass. New transcripts and exchanges are read next pass.
    try:
        with os.scandir(Path.home() / ".claude/projects") as projects:
            for project in projects:
                try:
                    if not project.is_dir():
                        continue
                    transcripts = transcript_paths(Path(project.path), "*.jsonl")
                except OSError:
                    transcripts = []
                if not transcripts:
                    # A project can permit known-file reads without directory listing.
                    for name, session_id in wanted.items():
                        try:
                            paths[session_id].extend(transcript_paths(Path(project.path), name))
                        except OSError:
                            continue
                for transcript in transcripts:
                    name = uncompressed_path(transcript).name
                    if name in wanted:
                        paths[wanted[name]].append(transcript)
    except OSError:
        pass
    chunk_bytes = account_config.whole("transcript_chunk_bytes")
    for session_id, candidates in paths.items():
        detected[session_id] = _transcript_refusal(candidates, chunk_bytes)
    return detected


def clear_assumed(session_id: str) -> None:
    _ = _assumed_refusals.set({key: value for key, value in _assumed_refusals.get().items() if key != session_id})


def cancelled(session_id: str, *, account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> None:
    """Suppress this unchanged refusal, including after its assumption is removed."""
    account_config = accounts.read_account_settings(account_config)
    try:
        refusal = session_refusal(session_id, account_config=account_config)
    except (OSError, ValueError):
        # Cancellation must also remove an incomplete live-proof file.
        clear_assumed(session_id)
        refusal = transcript_refusal(session_id, account_config=account_config)
    if isinstance(refusal, NoSessionRefusal):
        clear_assumed(session_id)
        return
    with _watch_lock():
        state = _load_state()
        state["sessions"][session_id] = WatchedSession(entry_id=refusal.entry_id, state="cancelled", move_id="")
        # An assumption can cover a real transcript refusal; neither may retrigger cancellation.
        transcript = transcript_refusal(session_id, account_config=account_config)
        if isinstance(transcript, TranscriptRefusal):
            state["sessions"][session_id] = WatchedSession(entry_id=transcript.entry_id, state="cancelled", move_id="")
        _save_state(state)
        clear_assumed(session_id)


NO_SESSION_FILTER: frozenset[str] = frozenset()
NO_MOVED_GROUPS: frozenset[accounts.ClaudeGroup] = frozenset()


@dataclass(frozen=True)
class NoSessionObservation:
    """No earlier report can prove that completion work is unchanged."""


@dataclass(frozen=True)
class ReportSessionIdentities:
    identities: frozenset[tuple[str, int, str]]


_report_sessions: ContextVar[NoSessionObservation | ReportSessionIdentities] = ContextVar(
    "refusal_report_sessions", default=NoSessionObservation())


def _session_identities() -> ReportSessionIdentities:
    return ReportSessionIdentities(frozenset((row["sessionId"], row["pid"], row.get("procStart", ""))
                                           for row in sessions.live_sessions()))


@contextmanager
def session_observation() -> Generator[None]:
    """Capture registry identities before the timer's first report scan."""
    token = _report_sessions.set(_session_identities())
    try:
        yield
    finally:
        _report_sessions.reset(token)


@herdr.process_action()
def _action_group_members() -> dict[accounts.ClaudeGroup, list[accounts.LiveGroupMember]]:
    """Read members fresh before refusal, queued or committed group moves."""
    return accounts.live_group_members()


@herdr.process_action()
def live_owners(*, only: frozenset[str] = NO_SESSION_FILTER) -> list[inventory_module.Session]:
    owners: dict[str, inventory_module.Session] = {}
    home_state = account.home_account_state()
    logins = {identity.login for session in sessions.live_sessions()
              if not only or session["sessionId"] in only
              if (identity := account.account_of(session["pid"], home_state=home_state)) is not None}
    for login in sorted(logins):
        for session in inventory_module.inventory(login, only)["sessions"]:
            if session["kind"] != "worker":
                owners[session["session_id"]] = session
    return list(owners.values())


def _notify_no_room(text: str, now: datetime) -> bool:
    from scripts.whoami import quota_alert
    config = quota_alert.load_config()
    return quota_alert.deliver_notice([quota_alert.Job("", recipient, text, "refusal")
        for recipient in quota_alert.recipients(config)], now.isoformat())


def _blocked_head_line(rule: AutoMoveRule, reason: BlockedHeadReason) -> str:
    return blocked_next_move_line(rule.group, rule.to, rule.entry_id, reason)


def _notify_blocked_head(rule: AutoMoveRule, reason: BlockedHeadReason, now: datetime) -> bool:
    return notify_blocked_next_move(rule.group, rule.to, rule.entry_id, reason, now)


def _record_blocked_head(state: WatchState, rule: AutoMoveRule, reason: BlockedHeadReason,
                         now: datetime, dry_run: bool, *,
                         other_targets: tuple[tuple[AutoMoveRule, BlockedHeadReason], ...] = ()) -> str:
    blocked = ((rule, reason), *other_targets)
    line = "; ".join(_blocked_head_line(candidate, cause) for candidate, cause in blocked)
    if not dry_run:
        notices = state.setdefault("auto_move_notices", {})
        fingerprint = "|".join(candidate.to + blocked_next_move_reason_key(cause) for candidate, cause in blocked) if other_targets else blocked_next_move_reason_key(reason)
        if notices.get(rule.entry_id) != fingerprint:
            _ = notices.pop(rule.entry_id, None)
            head = next((entry for entry in next_moves.read_next_moves() if entry.group == rule.group), None)
            if (isinstance(head, next_moves.ClaudeGroupMove) and head.id == rule.entry_id and head.to == rule.to
                    and head.fallback_targets == rule.fallback_targets):
                if (_notify_no_room(line, now) if other_targets else _notify_blocked_head(rule, reason, now)):
                    notices[rule.entry_id] = fingerprint
    return line


def _first_refusal(held: FirstRefusalObservation) -> refusals.AccountRefusal:
    window = refusals.UnknownWindow() if held["window"] == "unknown" else refusals.KnownWindow(held["window"])
    reset = refusals.KnownReset(datetime.fromisoformat(held["reset"])) if "reset" in held else refusals.NoResetKnown()
    family = refusals.ModelFamily(held["family"]) if "family" in held else refusals.ALL_MODELS
    return refusals.AccountRefusal("claude", held["login"], window, reset, datetime.fromisoformat(held["at"]), family)


def _set_transition(state: WatchState, session_id: str, entry_id: str, transition: WatchTransition, move_id: str) -> None:
    previous = state["sessions"].get(session_id)
    current = WatchedSession(entry_id=entry_id, state=transition, move_id=move_id)
    if previous and previous["entry_id"] == entry_id and "refusal" in previous:
        current["refusal"] = previous["refusal"]
    state["sessions"][session_id] = current


def _continue_session(session_id: str, entry_id: str) -> ContinuationDelivered | ContinuationNotDelivered:
    # Shutdown imports this watcher for its hold and cancellation; resolve its delivery boundary at use.
    messenger = cast(LiveSessionMessenger, cast(object, import_module("scripts.agents.restart_support")))
    try:
        delivery = messenger.send_message(f"session:{session_id}", "Quota wait ended", "Continue where you stopped.",
            key=f"quota-continue-{session_id}-{entry_id}")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        return ContinuationNotDelivered(str(error).splitlines()[0] if str(error) else type(error).__name__)
    if messenger.message_was_delivered(delivery):
        return ContinuationDelivered()
    return ContinuationNotDelivered((delivery.get("reason", "send not delivered").splitlines() or ["queued"])[0])


def _continue_wait(state: WatchState, session_id: str, name: str, previous: WatchedSession, *, dry_run: bool) -> str:
    if dry_run:
        return f"{name}: refused; continue on its current account"
    delivery = _continue_session(session_id, previous["entry_id"])
    if isinstance(delivery, ContinuationNotDelivered):
        return f"{name}: continuation not delivered: {delivery.reason}"
    clear_assumed(session_id)
    _set_transition(state, session_id, previous["entry_id"], "continued", previous["move_id"])
    return f"{name}: continued on its current account"


def _reading_values_fingerprint() -> str:
    values: dict[str, dict[str, object]] = {}
    for identity, reading in usage_store.load().items():
        values[identity] = {}
        for name, window in (("weekly", reading.get("weekly")), ("five_hour", reading.get("five_hour"))):
            if window is not None:
                values[identity][name] = {"left": window["left"], "resets": window["resets"]}
        if "credits" in reading:
            values[identity]["credits"] = reading["credits"]["balance"]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _latest_claude_requests(entries: list[moves.MoveEntry]) -> dict[str, tuple[datetime, moves.RecordedMoveRequest]]:
    latest: dict[str, tuple[datetime, moves.RecordedMoveRequest]] = {}
    for entry in entries:
        if entry["kind"] != "requested" or entry["tool"] != "claude":
            continue
        identity = entry["subject"].split(": ", 1)[0]
        at = datetime.fromisoformat(entry["at"])
        previous = latest.get(identity)
        if previous is None or at >= previous[0]:
            latest[identity] = at, entry
    return latest


def _newer_by_hand_move(latest: tuple[datetime, moves.RecordedMoveRequest] | None,
                       target: str, selected_at: datetime | None) -> bool:
    if latest is None:
        return False
    at, request = latest
    destination = request["to"]
    return (request.get("cause") == "by hand" and request.get("scope", "session") == "session"
            and isinstance(destination, str) and destination != target
            and (selected_at is None or at > selected_at))


def _run(now: datetime, dry_run: bool, proof_group: frozenset[str] = NO_SESSION_FILTER, *,
         refresh_usage: Callable[[], None] = lambda: None, account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> list[str]:
    account_config = accounts.read_account_settings(account_config)
    reset_wait = timedelta(minutes=account_config.whole("refusal_reset_wait_minutes"))
    stale_minutes = account_config.whole("usage_stale_minutes")
    stale_after = timedelta(minutes=stale_minutes)
    state = _load_state()
    original_state = deepcopy(state)
    configured = read_auto_move_rules() if not proof_group else ()
    pending_groups = {rule.group for rule in configured if isinstance(rule, AutoMoveRule)}
    entries = moves.read_moves()
    ended = {entry["move_id"] for entry in entries if entry["kind"] == "wait ended"}
    for entry in entries:
        if (entry["tool"] == "claude" and entry["kind"] == "waited" and entry["move_id"] not in ended
                and (not proof_group or entry["subject"].split(": ", 1)[0] in proof_group)
                and datetime.fromisoformat(entry["reset"]) <= now and not dry_run):
            moves.append_move(moves.WaitEndedMove(kind="wait ended", at=now.isoformat(), tool="claude",
                subject=entry["subject"], move_id=entry["move_id"]))
    groups: dict[str, list[tuple[inventory_module.Session, TranscriptRefusal | ProofRefusal, refusals.AccountRefusal]]] = {}
    lines: list[str] = []
    selected_groups = accounts.GroupAccounts() if proof_group else accounts.group_accounts()
    unconfigured = {group for group in (() if proof_group else accounts.CLAUDE_GROUPS) if isinstance(accounts.group_label(selected_groups, group), accounts.GroupNotConfigured)}
    reported_unconfigured: set[accounts.ClaudeGroup] = set()
    live = [session for session in sessions.live_sessions()
        if not proof_group or session["sessionId"] in proof_group]
    detected = session_refusals([session["sessionId"] for session in live], account_config=account_config)
    for session in live:
        session_id = session["sessionId"]
        previous = state["sessions"].get(session_id)
        if not previous or previous["state"] != "waited" or not isinstance(detected[session_id], NoSessionRefusal):
            continue
        waited = next((entry for entry in entries if entry["kind"] == "waited"
            and entry["move_id"] == previous["move_id"]), None)
        if waited is not None and datetime.fromisoformat(waited["reset"]) <= now:
            lines.append(_continue_wait(state, session_id, session["name"], previous, dry_run=dry_run))
    refused_ids = frozenset(session_id for session_id, refusal in detected.items()
        if isinstance(refusal, (TranscriptRefusal, ProofRefusal))
        and not ((previous := state["sessions"].get(session_id)) and previous["entry_id"] == refusal.entry_id
                 and previous["state"] in ("requested", "cancelled", "continued", "group selected")))
    refusing_logins: set[str] = set()
    owners = live_owners(only=refused_ids) if refused_ids else []
    all_members: dict[accounts.ClaudeGroup, list[accounts.LiveGroupMember]] = (
        _action_group_members() if owners else {group: [] for group in accounts.CLAUDE_GROUPS})
    owner_groups: dict[str, accounts.ClaudeGroup | Literal["ungrouped"]] = {
        member.session_id: member.group for members in all_members.values() for member in members}
    owners = [session for session in owners if not isinstance(detected[session["session_id"]], ProofRefusal)
              or session["session_id"] not in owner_groups]
    if owners and not dry_run:
        refresh_usage()
        now = max(now, datetime.now(timezone.utc))
    waiting_groups: set[accounts.ClaudeGroup] = set()
    tick_logins = frozenset(identity.login.casefold() for session in owners
        if isinstance(detected[session["session_id"]], TranscriptRefusal)
        if isinstance(identity := accounts.account_identity(Path(session.get("config_dir", str(accounts.main_config_dir())))),
                      agent_accounts.NumberedAccount))
    tick_excluded = accounts.account_labels_for_logins(tick_logins)
    journaled_waits: dict[str, moves.WaitedMove] = {}
    for session in owners:
        session_id = session["session_id"]
        refusal = detected[session_id]
        previous = state["sessions"].get(session_id)
        if (isinstance(refusal, NoSessionRefusal) or not previous or previous["entry_id"] != refusal.entry_id
                or previous["state"] not in ("retry", "no room", "waited")):
            continue
        wait_reset = refusal.reset
        if isinstance(wait_reset, refusals.NoResetKnown) and "refusal" in previous and "reset" in previous["refusal"]:
            wait_reset = refusals.KnownReset(datetime.fromisoformat(previous["refusal"]["reset"]))
        if not isinstance(wait_reset, refusals.KnownReset):
            continue
        journaled_wait = next((entry for entry in entries if entry["tool"] == "claude"
            and entry["kind"] == "waited" and entry["subject"].split(": ", 1)[0] == session_id
            and entry["reset"] == wait_reset.at.isoformat()
            and datetime.fromisoformat(entry["at"]) >= refusal.refused_at.at), None)
        if journaled_wait is not None:
            journaled_waits[session_id] = journaled_wait
            if previous["state"] in ("retry", "no room") and not dry_run:
                # Restore the saved wait before destination preparation can block its continuation.
                _set_transition(state, session_id, refusal.entry_id, "waited", journaled_wait["move_id"])
    # A short reset wait applies to the whole group.
    for session in owners:
        group_name = owner_groups.get(session["session_id"], "ungrouped")
        if group_name == "ungrouped" or group_name in pending_groups:
            continue
        if group_name in unconfigured:
            if group_name not in reported_unconfigured:
                lines.append(accounts.group_configuration_line(group_name))
                reported_unconfigured.add(group_name)
            continue
        refusal = detected[session["session_id"]]
        if isinstance(refusal, NoSessionRefusal):
            continue
        previous = state["sessions"].get(session["session_id"])
        if previous and previous["entry_id"] == refusal.entry_id and previous["state"] in ("requested", "cancelled", "continued", "group selected"):
            continue
        login = accounts.claude_login(Path(session.get("config_dir", str(accounts.main_config_dir()))))
        if previous and previous["entry_id"] == refusal.entry_id and "refusal" in previous:
            first = _first_refusal(previous["refusal"])
            window = first.window
            preflight_reset = first.reset
        else:
            reading = refusals.stored_reading_with_age("claude", login, now, stale_after)
            window = _refusal_window(refusal.window, reading, refusal.refused_at.at)
            preflight_reset: refusals.KnownReset | refusals.NoResetKnown = refusals.NoResetKnown()
            if isinstance(window, refusals.KnownWindow) and isinstance(reading, (refusals.FreshReading, refusals.StaleReading)):
                value = reading.weekly if window.name == "weekly" else reading.five_hour
                if isinstance(value, refusals.ReadingWindow):
                    preflight_reset = value.reset
        if isinstance(refusal.reset, refusals.KnownReset):
            preflight_reset = refusal.reset
        if previous and previous["entry_id"] == refusal.entry_id and previous["state"] == "waited":
            waiting_groups.add(group_name)
            continue
        if isinstance(window, refusals.KnownWindow) and window.name == "five_hour" and isinstance(preflight_reset, refusals.KnownReset) and timedelta(0) < preflight_reset.at - now < reset_wait:
            waiting_groups.add(group_name)
            continue
    for session in owners:
        session_id = session["session_id"]
        refusal = detected[session_id]
        if isinstance(refusal, NoSessionRefusal):
            continue
        previous = state["sessions"].get(session_id)
        if previous and previous["entry_id"] == refusal.entry_id and previous["state"] in ("requested", "cancelled", "continued", "group selected"):
            continue
        if (owner_groups.get(session["session_id"], "ungrouped") in unconfigured
                and not (previous and previous["entry_id"] == refusal.entry_id and previous["state"] == "waited")):
            continue
        login = accounts.claude_login(Path(session.get("config_dir", str(accounts.main_config_dir()))))
        if previous and previous["entry_id"] == refusal.entry_id and "refusal" in previous:
            account_refusal = _first_refusal(previous["refusal"])
        else:
            reading = refusals.stored_reading_with_age("claude", login, now, stale_after)
            window = _refusal_window(refusal.window, reading, refusal.refused_at.at)
            reset: refusals.KnownReset | refusals.NoResetKnown = refusals.NoResetKnown()
            # A reading holds no family window, so a family refusal keeps only its own reset.
            if (isinstance(window, refusals.KnownWindow) and isinstance(refusal.family, refusals.AllModels)
                    and isinstance(reading, (refusals.FreshReading, refusals.StaleReading))):
                value = reading.weekly if window.name == "weekly" else reading.five_hour
                if isinstance(value, refusals.ReadingWindow):
                    reset = value.reset
            if isinstance(refusal.reset, refusals.KnownReset):
                reset = refusal.reset
            account_refusal = refusals.AccountRefusal("claude", login, window, reset, refusal.refused_at.at, refusal.family)
            if not dry_run:
                observation = FirstRefusalObservation(login=login,
                    window=account_refusal.window.name if isinstance(account_refusal.window, refusals.KnownWindow) else "unknown",
                    at=account_refusal.at.isoformat())
                if isinstance(account_refusal.reset, refusals.KnownReset):
                    observation["reset"] = account_refusal.reset.at.isoformat()
                if isinstance(account_refusal.family, refusals.ModelFamily):
                    observation["family"] = account_refusal.family.name
                if not previous or previous["entry_id"] != refusal.entry_id:
                    if isinstance(refusal, TranscriptRefusal):
                        refusals.write_refusal(account_refusal)
                    _set_transition(state, session_id, refusal.entry_id, "retry", "")
                state["sessions"][session_id]["refusal"] = observation
                _save_state(state)
        window, reset = account_refusal.window, account_refusal.reset
        wait_reset = refusal.reset if isinstance(refusal.reset, refusals.KnownReset) else reset
        journaled_wait = journaled_waits.get(session_id)
        if previous and previous["entry_id"] == refusal.entry_id and previous["state"] == "waited":
            waited = next((entry for entry in entries if entry["kind"] == "waited" and entry["move_id"] == previous["move_id"]), None)
            if waited is not None and datetime.fromisoformat(waited["reset"]) <= now:
                lines.append(_continue_wait(state, session_id, session["name"], previous, dry_run=dry_run))
                continue
        if isinstance(refusal, TranscriptRefusal):
            refusing_logins.add(login.casefold())
        subject = session_id + ": " + session["name"]
        pending_head = owner_groups.get(session["session_id"], "ungrouped") in pending_groups
        if (isinstance(wait_reset, refusals.KnownReset) and (pending_head
                or isinstance(window, refusals.KnownWindow) and window.name == "five_hour"
                and timedelta(0) < wait_reset.at - now < reset_wait)):
            lines.append(f"{session['name']}: refused; wait until {wait_reset.at.astimezone(agent_accounts.user_zone()).isoformat()}")
            if not dry_run and journaled_wait is None:
                move_id = uuid4().hex
                moves.append_move(moves.WaitedMove(kind="waited", at=now.isoformat(), tool="claude",
                    subject=subject, move_id=move_id,
                    window=window.name if isinstance(window, refusals.KnownWindow) else "unknown",
                    reset=wait_reset.at.isoformat()))
                _set_transition(state, session_id, refusal.entry_id, "waited", move_id)
            continue
        if pending_head:
            continue
        group_name = owner_groups.get(session["session_id"], "ungrouped")
        key = ("proof group" if proof_group else "proof:" + session_id if isinstance(refusal, ProofRefusal)
               else login.casefold()) if group_name == "ungrouped" else "group:" + group_name
        groups.setdefault(key, []).append((session, refusal, account_refusal))
    fingerprint = _reading_values_fingerprint()
    if dry_run:
        lines.insert(0, accounts.group_status_line(selected_groups))
    if not proof_group and any(key.startswith("group:") for key in groups):
        all_members = _action_group_members()

    moved_groups: set[accounts.ClaudeGroup] = set()
    no_room_groups: set[accounts.ClaudeGroup] = set()
    for login, group in groups.items():
        group_name = cast(accounts.ClaudeGroup, login.removeprefix("group:")) if login.startswith("group:") else "ungrouped"
        if group_name in waiting_groups:
            continue
        source_dir = Path(group[0][0].get("config_dir", str(accounts.main_config_dir())))
        source_login = accounts.claude_login(source_dir)
        default_identity = accounts.account_identity(accounts.main_config_dir())
        source = accounts.claude_config_label(source_dir)
        excluded = {source, *tick_excluded}
        for member, _, _ in group:
            member_identity = accounts.account_identity(Path(member.get("config_dir", str(accounts.main_config_dir()))))
            if isinstance(member_identity, agent_accounts.NumberedAccount):
                excluded.add(member_identity.label)
        if isinstance(default_identity, agent_accounts.NumberedAccount) and default_identity.login.casefold() in refusing_logins:
            excluded.add(default_identity.label)
        for named in accounts.checked_named_accounts():
            identity = accounts.account_identity(named.dir)
            if isinstance(identity, agent_accounts.NumberedAccount) and identity.login.casefold() in refusing_logins:
                excluded.add(canonical_label(named.label))
        if group_name == "ungrouped":
            families = {refusal.session_family for _, refusal, _ in group}
            chosen = accounts.pick("claude", now, exclude=frozenset(excluded), keep=accounts.NoKeep(),
                                   reading_age_minutes=stale_minutes,
                                   family=families.pop() if len(families) == 1 else refusals.UNKNOWN_FAMILY)
        else:
            family = accounts.group_family(group_name)
            # A member whose own session runs another family makes the group's family unknown.
            if any(isinstance(refusal.session_family, refusals.ModelFamily) and refusal.session_family != family
                   for _, refusal, _ in group):
                family = refusals.UNKNOWN_FAMILY
            placed = accounts.placement(group_name, now, exclude=frozenset(excluded), reading_age_minutes=stale_minutes,
                                        family=family)
            if isinstance(placed.reason, accounts.GroupNoRoom):
                chosen = placed.reason.outcome
            else:
                assert isinstance(placed.label, str) and isinstance(placed.dir, Path)
                chosen = accounts.PickedAccount(placed.label, placed.dir, 0, accounts.NoResetKnown())
        if isinstance(chosen, accounts.NoAccountWithRoom):
            if group_name != "ungrouped":
                no_room_groups.add(group_name)
            text = source + " refused; " + accounts.no_room_line("claude", chosen)
            lines.extend(f"{session['name']}: refused; {text}" for session, _, _ in group)
            if not dry_run:
                for session, refusal, _ in group:
                    prior = state["sessions"].get(session["session_id"])
                    if not prior or prior["entry_id"] != refusal.entry_id or prior["state"] != "no room":
                        move_id = uuid4().hex
                        moves.append_move(moves.NoRoomMove(kind="no room", at=now.isoformat(), tool="claude",
                            subject=session["session_id"] + ": " + session["name"], move_id=move_id,
                            earliest_reset=chosen.earliest_reset.at.isoformat() if isinstance(chosen.earliest_reset, accounts.EarliestReset) else ""))
                        _set_transition(state, session["session_id"], refusal.entry_id, "no room", move_id)
                        if prior and prior["entry_id"] == refusal.entry_id and prior["state"] == "retry":
                            state["sessions"][session["session_id"]]["retry_decision"] = prior["move_id"]
                notice_key = login if isinstance(group[0][1], ProofRefusal) else source_login.casefold()
                if state["notices"].get(notice_key) != fingerprint:
                    _ = state["notices"].pop(notice_key, None)
                    if _notify_no_room(text, now):
                        state["notices"][notice_key] = fingerprint
            continue
        by_id = {session["session_id"]: refusal for session, refusal, _ in group}
        batches: dict[str, list[str]] = {}
        if group_name != "ungrouped":
            lines.extend(f"{session.name}: refused group {group_name}; move to {chosen.label}" for session in all_members[group_name]
                         if session.session_id not in {item[0]["session_id"] for item in group})
        for session, _, _ in group:
            lines.append(f"{session['name']}: refused; move from {source} to {chosen.label}")
            prior = state["sessions"].get(session["session_id"])
            if not dry_run and prior and prior["state"] == "no room":
                moves.append_move(moves.RoomAgainMove(kind="room again", at=now.isoformat(), tool="claude",
                    subject=session["session_id"] + ": " + session["name"], move_id=prior["move_id"], label=chosen.label))
        if dry_run:
            if group_name != "ungrouped":
                selected_groups[group_name] = chosen.label
                moved_groups.add(group_name)
            continue
        first_refusal = group[0][2]
        window_name = first_refusal.window.name if isinstance(first_refusal.window, refusals.KnownWindow) else "unknown"
        reset_text = first_refusal.reset.at.astimezone(agent_accounts.user_zone()).isoformat() if isinstance(first_refusal.reset, refusals.KnownReset) else "unknown reset"
        why = f"{source} refused: {window_name} limit until {reset_text}"
        move_origin = accounts.RefusalOrigin(window_name, first_refusal.reset)
        refusal_ids = sorted((session["session_id"], refusal.entry_id) for session, refusal, _ in group)
        transition = accounts.GroupTransition(uuid5(NAMESPACE_URL, json.dumps(refusal_ids)).hex,
                                              move_origin, accounts.RecordedTransitionReason(why))
        if group_name != "ungrouped":
            transition = accounts.set_group_account(group_name, chosen.label, why=why, now=now, origin=move_origin)
            selected_groups[group_name] = chosen.label
            moved_groups.add(group_name)
            for session, refusal, _ in group:
                _set_transition(state, session["session_id"], refusal.entry_id, "group selected", "")
            # The chosen group account survives a member's failed request or interrupted delivery.
            _save_state(state)
            batches[transition.move_id] = [session.session_id for session in all_members[group_name]]
        else:
            for identity, refusal in by_id.items():
                prior = state["sessions"].get(identity)
                decision = transition.move_id
                if prior and prior["entry_id"] == refusal.entry_id:
                    decision = (prior.get("retry_decision") or
                                (prior["move_id"] if prior["state"] == "retry" else "")) or decision
                batches.setdefault(decision, []).append(identity)
                _set_transition(state, identity, refusal.entry_id, "retry", decision)
            # The decision survives an interruption before a move returns.
            _save_state(state)
        for decision, members in batches.items():
            available = _available_members(tuple(members)) if group_name == "ungrouped" else tuple(members)
            intent = (moves.ExplicitMoveTarget.REQUESTED_ACCOUNT if group_name == "ungrouped"
                      else moves.FollowGroupAccount(group_name))
            outcomes = accounts.move_sessions(available, chosen.label, why=why, cause="refusal",
                                              intent=intent) if available else ()
            for outcome in outcomes:
                lines.append(accounts.move_result_line(outcome))
                if (refusal := by_id.get(outcome.session_id)) is None:
                    continue
                accepted = (isinstance(outcome, (stop_results.ClaudeUp, stop_results.MoveHandedOff))
                            and not isinstance(outcome, moves.MoveInProgress))
                _set_transition(state, outcome.session_id, refusal.entry_id,
                    "requested" if accepted else "group selected" if group_name != "ungrouped" else "retry",
                    decision if accepted or group_name == "ungrouped" else "")
    if not proof_group:
        if not dry_run and state != original_state:
            _save_state(state)
        before_rules = dict(selected_groups)
        lines.extend(run_auto_moves(now, dry_run=dry_run, moved_groups=frozenset(moved_groups), account_config=account_config))
        if not dry_run:
            selected_groups = accounts.group_accounts()
            moved_groups.update(group for group in accounts.CLAUDE_GROUPS
                                if selected_groups.get(group) != before_rules.get(group))
            state = _load_state()
            original_state = deepcopy(state)
    if not proof_group:
        skipped = frozenset(moved_groups | waiting_groups | no_room_groups)
        if not dry_run:
            # The Mac age rule covers a plan that is not saved, so the save never stops this pass.
            try:
                _save_completion_plan(now, selected_groups, skipped, stale_after)
            except (OSError, ValueError) as error:
                lines.append(f"group completion: the completion plan is not saved: {error}")
        lines.extend(_complete_groups(now, dry_run=dry_run, selected_groups=selected_groups, skipped=skipped, state=state,
                                      refusals_of=lambda ids: {session_id: detected[session_id] for session_id in ids
                                                               if session_id in detected},
                                      stale_after=stale_after))
    if not dry_run and state != original_state:
        _save_state(state)
    return lines


SessionRefusals = Mapping[str, TranscriptRefusal | ProofRefusal | NoSessionRefusal]


def _eligible_groups(selected_groups: accounts.GroupAccounts,
                     skipped: frozenset[accounts.ClaudeGroup]) -> dict[accounts.ClaudeGroup, str]:
    """Map each committed group that this pass may complete to its selected account."""
    return {group: target for group in accounts.CLAUDE_GROUPS
            if group not in skipped and (group != "producers" or group in selected_groups)
            and isinstance(target := accounts.group_label(selected_groups, group), str)}


def _completion_members(observed: Mapping[accounts.ClaudeGroup, list[accounts.LiveGroupMember]],
                        group_name: accounts.ClaudeGroup, target: str, state: WatchState,
                        refusals_of: Callable[[list[str]], SessionRefusals]) -> list[accounts.LiveGroupMember]:
    off_target = [session for session in observed.get(group_name, ())
                  if not accounts.session_on_account(session, target)]
    # A cancelled refusal stays on its account until a new refusal entry.
    cancelled = {session.session_id for session in off_target
                 if (watched := state["sessions"].get(session.session_id)) is not None and watched["state"] == "cancelled"}
    current: SessionRefusals = refusals_of(sorted(cancelled)) if cancelled else {}
    return [session for session in off_target
            if not (session.session_id in cancelled
                    and isinstance(refusal := current.get(session.session_id), TranscriptRefusal)
                    and state["sessions"][session.session_id]["entry_id"] == refusal.entry_id)]


def _available_group_members(members: list[accounts.LiveGroupMember]) -> list[accounts.LiveGroupMember]:
    available = set(_available_members(tuple(session.session_id for session in members)))
    return [session for session in members if session.session_id in available]


def _target_refused(target: str, group_name: accounts.ClaudeGroup, now: datetime, stale_after: timedelta) -> bool:
    login = accounts.claude_login(accounts.claude_account_dir(target))
    return refusals.blocking(refusals.load(), "claude", login, accounts.group_family(group_name),
                             refusals.stored_reading_with_age("claude", login, now, stale_after), now) is not None


def _completion_why(group_name: accounts.ClaudeGroup, target: str, transition: accounts.GroupTransition) -> str:
    if isinstance(transition.origin, accounts.AutoOrigin):
        return transition.why.text if isinstance(transition.why, accounts.RecordedTransitionReason) else ""
    return f"complete {group_name} move to {target}"


def _complete_groups(now: datetime, *, dry_run: bool, selected_groups: accounts.GroupAccounts,
                     skipped: frozenset[accounts.ClaudeGroup], state: WatchState,
                     refusals_of: Callable[[list[str]], SessionRefusals], stale_after: timedelta) -> list[str]:
    """A committed group choice is stable. Complete its off-target local members without choosing again."""
    lines: list[str] = []
    eligible = _eligible_groups(selected_groups, skipped)
    if not eligible:
        return lines
    # Reports can suggest work. A changed registry also finds late arrivals.
    cached = accounts.live_group_members()
    previous = _report_sessions.get()
    changed = isinstance(previous, NoSessionObservation) or previous != _session_identities()
    if not changed and not any(_completion_members(cached, group, target, state, refusals_of)
                               for group, target in eligible.items()):
        return lines
    latest_requests = None
    with herdr.process_action():
        all_members = accounts.live_group_members()
        for group_name, target in eligible.items():
            members = _available_group_members(_completion_members(all_members, group_name, target, state, refusals_of))
            if not members:
                continue
            if latest_requests is None:
                # A move can reach its target after the first journal read.
                latest_requests = _latest_claude_requests(moves.read_moves())
            transition = accounts.group_transition(group_name)
            selected_at = transition.at if isinstance(transition, accounts.GroupTransition) else None
            members = [session for session in members if not _newer_by_hand_move(
                latest_requests.get(session.session_id), target, selected_at)]
            if not members:
                continue
            if _target_refused(target, group_name, now, stale_after):
                lines.append(f"{group_name}: move refused: {target} holds an unresolved refusal")
                continue
            if dry_run:
                lines.extend(accounts.move_preview_line(session, target) for session in members)
            else:
                if isinstance(transition, accounts.NoGroupTransition):
                    transition = accounts.set_group_account(group_name, target, why=f"complete {group_name} move to {target}", now=now)
                outcomes = accounts.move_sessions(tuple(session.session_id for session in members), target,
                    why=_completion_why(group_name, target, transition),
                    cause=accounts.move_origin_fields(transition.origin)["cause"],
                    intent=moves.FollowGroupAccount(group_name))
                lines.extend(accounts.move_result_line(outcome) for outcome in outcomes)
    return lines


@dataclass(frozen=True)
class PlannedCompletion:
    """One group that natedev permits another machine to complete."""

    target: str
    cause: moves.MoveCause
    why: str
    selected_at: datetime | None


@dataclass(frozen=True)
class CompletionPlan:
    at: datetime
    groups: Mapping[accounts.ClaudeGroup, PlannedCompletion]


class PlannedCompletionDocument(TypedDict):
    target: str
    cause: moves.MoveCause
    why: str
    selected_at: NotRequired[str]


class CompletionPlanDocument(TypedDict):
    at: str
    groups: dict[str, PlannedCompletionDocument]


def _save_completion_plan(now: datetime, selected_groups: accounts.GroupAccounts,
                          skipped: frozenset[accounts.ClaudeGroup], stale_after: timedelta) -> None:
    """Save the groups that this pass would complete, for the machines that natedev does not run."""
    groups: dict[str, PlannedCompletionDocument] = {}
    for group_name, target in _eligible_groups(selected_groups, skipped).items():
        transition = accounts.group_transition(group_name)
        # This pass records a transition for a legacy selection before it moves a local member.
        if isinstance(transition, accounts.NoGroupTransition):
            continue
        # An unreadable refusal store leaves the group out, so the Mac does not move to a refused account.
        try:
            if _target_refused(target, group_name, now, stale_after):
                continue
        except (accounts.AccountsError, OSError, ValueError):
            continue
        entry = PlannedCompletionDocument(target=target, cause=accounts.move_origin_fields(transition.origin)["cause"],
                                          why=_completion_why(group_name, target, transition))
        if transition.at is not None:
            entry["selected_at"] = transition.at.isoformat()
        groups[group_name] = entry
    _atomic_json(role_map.completion_plan_file(), CompletionPlanDocument(at=now.isoformat(), groups=groups))


def decode_completion_plan(text: str) -> CompletionPlan:
    value = cast(object, json.loads(text))
    if not isinstance(value, dict):
        raise ValueError("completion plan must contain an object")
    fields = cast(dict[str, object], value)
    at, raw_groups = fields.get("at"), fields.get("groups")
    if not isinstance(at, str) or not isinstance(raw_groups, dict):
        raise ValueError("invalid completion plan")
    groups: dict[accounts.ClaudeGroup, PlannedCompletion] = {}
    for group_name, raw in cast(dict[str, object], raw_groups).items():
        group: accounts.ClaudeGroup | None = next((known for known in accounts.CLAUDE_GROUPS if known == group_name), None)
        if group is None or not isinstance(raw, dict):
            raise ValueError("invalid completion plan group")
        entry = cast(dict[str, object], raw)
        target, cause, why, selected_at = entry.get("target"), entry.get("cause"), entry.get("why"), entry.get("selected_at")
        if (not isinstance(target, str) or cause not in ("refusal", "credits off", "by hand", "auto")
                or not isinstance(why, str) or not isinstance(selected_at, (str, type(None)))):
            raise ValueError("invalid completion plan group")
        groups[group] = PlannedCompletion(target, cause, why,
                                          _aware_time(selected_at) if selected_at is not None else None)
    return CompletionPlan(_aware_time(at), groups)


def _aware_time(text: str) -> datetime:
    at = datetime.fromisoformat(text)
    if at.tzinfo is None:
        raise ValueError("completion plan time needs a zone")
    return at


def complete_local_groups(now: datetime, *, max_age: timedelta,
                          account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> list[str]:
    """Complete this machine's off-target group members from natedev's current completion plan.

    Each machine reads only its own registry, so it moves only the members that it runs.
    It writes no group selection. The move batch reads the current group account again under the row lock.
    """
    account_config = accounts.read_account_settings(account_config)
    with herdr.process_action():
        observed = accounts.live_group_members()
        if not any(observed.values()):
            return []
        try:
            plan = decode_completion_plan(role_map.ssh_role_map(("completion-plan-read",)))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            reason = str(error).splitlines()[0] if str(error) else type(error).__name__
            return [f"group completion: the completion plan is unavailable: {reason}"]
        if now - plan.at > max_age:
            return [f"group completion: the completion plan from {plan.at.isoformat()} is too old"]
        lines: list[str] = []
        with _watch_lock():
            state = _load_state()
            latest_requests = None
            for group_name, planned in plan.groups.items():
                members = _available_group_members(_completion_members(
                    observed, group_name, planned.target, state,
                    lambda ids: session_refusals(ids, account_config=account_config)))
                if not members:
                    continue
                if latest_requests is None:
                    latest_requests = _latest_claude_requests(moves.read_moves())
                members = [session for session in members if not _newer_by_hand_move(
                    latest_requests.get(session.session_id), planned.target, planned.selected_at)]
                if not members:
                    continue
                outcomes = accounts.move_sessions(tuple(session.session_id for session in members), planned.target,
                    why=planned.why, cause=planned.cause, intent=moves.FollowGroupAccount(group_name))
                lines.extend(accounts.move_result_line(outcome) for outcome in outcomes)
        return lines


# Preserve the watcher API while sharing replay evidence with quota_alert.
RecordedAutoMoves = role_map.RecordedAutoMoves
UnreadableAutoMoveHistory = role_map.UnreadableAutoMoveHistory


def recorded_auto_moves() -> RecordedAutoMoves | UnreadableAutoMoveHistory:
    return role_map.recorded_auto_moves(accounts.roles_history_file())


@dataclass(frozen=True)
class BlockedAutoMoveTargets:
    reasons: tuple[tuple[AutoMoveRule, BlockedHeadReason], ...]


def _choose_auto_move_target(rule: AutoMoveRule, current_account: str, now: datetime,
                             account_config: conf.ConfigRead) -> AutoMoveRule | BlockedAutoMoveTargets:
    held_refusals = refusals.load()
    family = accounts.group_family(rule.group)
    stale_after = timedelta(minutes=account_config.whole("usage_stale_minutes"))
    blocked: list[tuple[AutoMoveRule, BlockedHeadReason]] = []
    for label in (rule.to, *rule.fallback_targets):
        candidate = replace(rule, to=label)
        if current_account == label:
            return candidate
        try:
            destination = accounts.NamedAccount(label, accounts.claude_account_dir(label))
        except accounts.AccountsError as error:
            blocked.append((candidate, TargetAccountUnavailable(str(error).splitlines()[0])))
            continue
        sign_in = accounts.move_target_sign_in(destination, now)
        if isinstance(sign_in, accounts.MoveRefused):
            blocked.append((candidate, TargetPreparationRefused(sign_in.reason)))
            continue
        target_login = sign_in.login
        target = refusals.stored_reading_with_age("claude", target_login, now, stale_after)
        last_read = (target.weekly.at if isinstance(target, (refusals.FreshReading, refusals.StaleReading))
                     and isinstance(target.weekly, refusals.ReadingWindow) else NoTargetReading())
        target_usage = usage_store.load().get(usage_store.key("Claude", target_login), {})
        reason: BlockedHeadReason
        if usage_store.latest_read_outcome(target_usage) in (agent_accounts.UsageReadOutcome.STALE_TOKEN,
                                                          agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN):
            attempt = accounts.login_renewal_attempts().get(label)
            reason = (TargetLoginRenewalFailed(attempt.result.detail)
                      if attempt is not None and isinstance(attempt.result, accounts.LoginRenewalFailed)
                      else TargetReadingUnavailable(last_read))
        elif not isinstance(target, refusals.FreshReading):
            reason = TargetReadingUnavailable(last_read)
        elif target.weekly.left <= rule.when.percent:
            reason = TargetAtOrBelowThreshold(target.weekly.left, rule.when.percent)
        else:
            if refusals.blocking(held_refusals, "claude", target_login, family, target, now) is None:
                return candidate
            reason = TargetUnresolvedRefusal()
        blocked.append((candidate, reason))
    return BlockedAutoMoveTargets(tuple(blocked))


def _remove_auto_move_entries(entry_ids: frozenset[str]) -> None:
    positions: Counter[accounts.ClaudeGroup] = Counter()
    remaining: list[next_moves.NextMove] = []
    lines: list[str] = []
    for entry in next_moves.read_next_moves():
        if isinstance(entry, next_moves.ClaudeGroupMove):
            positions[entry.group] += 1
            if entry.id in entry_ids:
                percent = next_moves.format_next_move_percent(entry.at_percent)
                lines.append(f"Removed next move {entry.group} {positions[entry.group]} at {percent}% to {entry.to}.")
                continue
        remaining.append(entry)
    if lines:
        next_moves.write_next_moves(tuple(remaining))
        for line in lines:
            try:
                print(line, flush=True)
            except OSError:
                pass


@dataclass(frozen=True)
class AutoMoveTargetSelected:
    """The selected account already satisfies this queue entry."""


@dataclass(frozen=True)
class ObservedGroupSelection:
    selection: GroupSelection
    observed_at: datetime


def _remove_head_if_selected(rule: AutoMoveRule, lines: list[str], *,
                             dry_run: bool) -> ObservedGroupSelection | AutoMoveTargetSelected:
    # The caller holds the queue lock during a real pass.
    with nullcontext() if dry_run else role_map._groups_lock():  # pyright: ignore[reportPrivateUsage]
        with role_map.supplied_role_map(accounts.read_role_map(fresh=True)):
            selection = _group_selection(accounts.group_accounts(), rule.group)
        if selection.account != rule.to:
            return ObservedGroupSelection(selection, datetime.now(timezone.utc))
        if dry_run:
            lines.append(f"{rule.group}: unchanged")
        else:
            _remove_auto_move_entries(frozenset({rule.entry_id}))
        return AutoMoveTargetSelected()


def _run_auto_moves_locked(now: datetime, *, dry_run: bool = False,
                   moved_groups: frozenset[accounts.ClaudeGroup] = NO_MOVED_GROUPS, account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> list[str]:
    """Apply log entries after the refusal pass while holding the log lock."""
    account_config = accounts.read_account_settings(account_config)
    if socket.gethostname().split(".", 1)[0] != role_map.ROLE_MAP_HOST:
        return []
    configured = read_auto_move_rules()
    rules = [rule for rule in configured if isinstance(rule, AutoMoveRule)]
    lines = [rule.reason for rule in configured if isinstance(rule, UnreadableNextMoveLog)]
    state = _load_state()
    original = deepcopy(state)
    holds = state.get("auto_move_holds", {})
    notices = state.get("auto_move_notices", {})
    heads: dict[accounts.ClaudeGroup, AutoMoveRule] = {}
    for rule in rules:
        _ = heads.setdefault(rule.group, rule)
    head_ids = {rule.entry_id for rule in heads.values()}
    for entry_id in set(holds) - head_ids:
        del holds[entry_id]
    for entry_id in set(notices) - head_ids:
        del notices[entry_id]
    recorded = role_map.repair_selected_auto_moves(tuple(role_map.AutoMoveTarget(rule.entry_id, rule.group, target)
        for rule in rules for target in (rule.to, *rule.fallback_targets)), now, dry_run=dry_run)
    consumed: frozenset[str] = (recorded.entry_ids & {rule.entry_id for rule in rules}
                if isinstance(recorded, RecordedAutoMoves) else frozenset())
    if consumed:
        for entry_id in consumed:
            _ = holds.pop(entry_id, None)
            _ = notices.pop(entry_id, None)
        if not dry_run:
            _remove_auto_move_entries(consumed)
    # A successor starts on a later pass, even when replay consumes its head here.
    rules = [rule for rule in heads.values() if rule.entry_id not in consumed]
    if not rules:
        if not dry_run and state != original:
            _save_state(state)
        return lines
    moved = set(moved_groups)
    stale_after = timedelta(minutes=account_config.whole("usage_stale_minutes"))
    for rule in rules:
        observed = _remove_head_if_selected(rule, lines, dry_run=dry_run)
        if isinstance(observed, AutoMoveTargetSelected):
            _ = holds.pop(rule.entry_id, None)
            _ = notices.pop(rule.entry_id, None)
            continue
        selection = observed.selection
        hold = holds.get(rule.entry_id)
        if hold is not None and not hold.is_current(selection, rule.when.percent):
            del holds[rule.entry_id]
        if (isinstance(selection.account, accounts.GroupNotConfigured)
                or rule.group in moved):
            continue
        source_login = accounts.claude_login(accounts.claude_account_dir(selection.account))
        source = refusals.stored_reading_with_age("claude", source_login, now, stale_after)
        if not isinstance(source, refusals.FreshReading):
            continue
        if source.weekly.left > rule.when.percent:
            _ = holds.pop(rule.entry_id, None)
            continue
        if isinstance(rule.when.hold_mode, next_moves.HalfPointHold) or rule.when.hold_mode.minutes > 0:
            if rule.entry_id not in holds:
                holds[rule.entry_id] = HoldStarted(selection.account, observed.observed_at, selection.move_id, rule.when.percent,
                                                  next_moves.resolve_hold(rule.when.hold_mode, selection.account, now, account_config=account_config))
                state["auto_move_holds"] = holds
            started = holds[rule.entry_id]
            if isinstance(started.duration, LegacyHoldDuration):
                started = HoldStarted(started.account, started.at, started.move_id, started.percent,
                                      next_moves.resolve_hold(rule.when.hold_mode, selection.account, now, account_config=account_config))
                holds[rule.entry_id] = started
                state["auto_move_holds"] = holds
            if (isinstance(started.duration, next_moves.ResolvedHold)
                    and (now - started.at).total_seconds() / 60 < started.duration.minutes):
                continue
        chosen = _choose_auto_move_target(rule, selection.account, now, account_config)
        if isinstance(chosen, BlockedAutoMoveTargets):
            blocked_rule, reason = chosen.reasons[0]
            if not rule.fallback_targets and isinstance(reason, TargetAccountUnavailable):
                lines.append(reason.reason)
            else:
                lines.append(_record_blocked_head(state, blocked_rule, reason, now, dry_run,
                                                  other_targets=chosen.reasons[1:]))
            continue
        if selection.account == chosen.to:
            if isinstance(_remove_head_if_selected(chosen, lines, dry_run=dry_run), AutoMoveTargetSelected):
                _ = holds.pop(rule.entry_id, None)
                _ = notices.pop(rule.entry_id, None)
            continue
        why = f"auto: {rule.entry_id}"
        if dry_run:
            lines.append(f"{rule.group}: {why}; would move from {selection.account} to {chosen.to}")
            moved.add(rule.group)
            continue
        members = _action_group_members()
        destination = accounts.NamedAccount(chosen.to, accounts.account_dir(chosen.to))
        checked_at = datetime.now(timezone.utc)
        sign_in = accounts.move_target_sign_in(destination, checked_at)
        if isinstance(sign_in, accounts.MoveRefused):
            lines.append(_record_blocked_head(state, rule, TargetPreparationRefused(sign_in.reason), checked_at, dry_run))
            continue
        rule = chosen
        _ = accounts.set_group_account(rule.group, rule.to, why=why, now=now, origin=accounts.AutoOrigin())
        _ = notices.pop(rule.entry_id, None)
        lines.append(f"{rule.group}: {why}; move from {selection.account} to {rule.to}")
        moved.add(rule.group)
        _ = holds.pop(rule.entry_id, None)
        # Remove the entry after the role writer records the selection.
        _remove_auto_move_entries(frozenset({rule.entry_id}))
        _save_state(state)
        ids = tuple(member.session_id for member in members.get(rule.group, ()))
        outcomes = accounts.move_sessions(ids, rule.to, why=why, cause="auto",
                                         intent=moves.FollowGroupAccount(rule.group)) if ids else ()
        lines.extend(accounts.move_result_line(outcome) for outcome in outcomes)
    if not dry_run and state != original:
        _save_state(state)
    return lines


@role_map.role_map_command
def run_auto_moves(now: datetime, *, dry_run: bool = False,
                   moved_groups: frozenset[accounts.ClaudeGroup] = NO_MOVED_GROUPS, account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> list[str]:
    """Read and consume one-shot entries under the same lock as log edits."""
    account_config = accounts.read_account_settings(account_config)
    if socket.gethostname().split(".", 1)[0] != role_map.ROLE_MAP_HOST:
        return []
    if dry_run:
        return _run_auto_moves_locked(now, dry_run=True, moved_groups=moved_groups, account_config=account_config)
    with next_moves.next_moves_lock():
        return _run_auto_moves_locked(now, moved_groups=moved_groups, account_config=account_config)


def renew_expired_logins(now: datetime, dry_run: bool, *, account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> list[str]:
    """Probe local noted accounts before any move takes the queue lock."""
    account_config = accounts.read_account_settings(account_config)
    stored = usage_store.load()
    attempts = accounts.login_renewal_attempts()
    lines: list[str] = []
    renewal_interval = timedelta(minutes=account_config.whole("login_renewal_interval_minutes"))
    for named in accounts.checked_named_accounts():
        identity = accounts.account_identity(named.dir)
        if not isinstance(identity, agent_accounts.NumberedAccount):
            continue
        usage = stored.get(usage_store.key("Claude", identity.login), {})
        if usage_store.latest_read_outcome(usage) not in (agent_accounts.UsageReadOutcome.STALE_TOKEN,
                                                       agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN):
            continue
        previous = attempts.get(named.label)
        if previous is not None and now - previous.at < renewal_interval:
            continue
        if dry_run:
            lines.append(f"would renew {named.label}")
            continue
        result = accounts.renew_login(named.label, account_config=account_config)
        attempts[named.label] = accounts.LoginRenewalAttempt(now, result)
        _atomic_json(accounts.login_renewals_file(), {
            label: {"at": attempt.at.isoformat(),
                    "outcome": "renewed" if isinstance(attempt.result, accounts.LoginRenewed) else "failed",
                    "detail": attempt.result.detail if isinstance(attempt.result, accounts.LoginRenewalFailed) else ""}
            for label, attempt in attempts.items()})
        lines.append(f"{named.label} login renewed" if isinstance(result, accounts.LoginRenewed)
                     else f"{named.label} login renewal failed: {result.detail}")
    return lines


@role_map.role_map_command
def run(now: datetime | None = None, *, dry_run: bool = False, proof_group: frozenset[str] = NO_SESSION_FILTER,
        refresh_usage: Callable[[], None] = lambda: None, recover_units: bool = True, account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> list[str]:
    account_config = accounts.read_account_settings(account_config)
    from scripts.accounts import unit_watchdog

    at = now or datetime.now(timezone.utc)
    if dry_run:
        return [*renew_expired_logins(at, True, account_config=account_config), *_run(at, True, proof_group, account_config=account_config),
                *(unit_watchdog.run(at, dry_run=True) if not proof_group and recover_units else [])]
    with _watch_lock():
        renewed = renew_expired_logins(at, False, account_config=account_config)
        # A probe can save a reading after the pass started.
        after_renewal = max(at, datetime.now(timezone.utc)) if renewed else at
        return [*renewed, *_run(after_renewal, False, proof_group, refresh_usage=refresh_usage, account_config=account_config),
                *(unit_watchdog.run(after_renewal, dry_run=False) if not proof_group and recover_units else [])]


class WatchCommandLine(argparse.Namespace):
    dry_run: bool = False
    assume_refused: list[str] | None = None
    reset_in: float | None = None
    proof_group: list[str] | None = None


def main(argv: list[str] | None = None, *, account_config: conf.ConfigRead | accounts.ReadCurrentAccountSettings = accounts.CURRENT_ACCOUNT_SETTINGS) -> int:
    account_config = accounts.read_account_settings(account_config)
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--assume-refused", nargs=2, metavar=("SESSION", "WINDOW"))
    _ = parser.add_argument("--reset-in", type=float)
    _ = parser.add_argument("--proof-group", nargs="+")
    args = parser.parse_args(argv, namespace=WatchCommandLine())
    if args.reset_in is not None and not args.assume_refused:
        parser.error("--reset-in requires --assume-refused")
    if args.proof_group and not args.assume_refused:
        parser.error("--proof-group requires --assume-refused")
    proof_ids: set[str] = set()
    if args.assume_refused:
        name, window = args.assume_refused
        if window not in ("weekly", "five-hour"):
            parser.error("window must be weekly or five-hour")
        from scripts.production import staff
        record = staff.initial_snapshot()
        owners = live_owners()
        selected: dict[str, inventory_module.Session] = {}
        for probe in [name, *(args.proof_group or [])]:
            matching = [session for session in owners if session["session_id"] == probe
                        or (member.name if (member := record.by_current_session(session["session_id"]))
                            is not None else "") == probe]
            if len(matching) != 1:
                parser.error("name or id must identify one local live owner")
            selected[matching[0]["session_id"]] = matching[0]
        if args.proof_group:
            proof_ids.update(selected)
        now = datetime.now(timezone.utc)
        held = ProofRefusal(uuid4().hex, RefusedAt(now), refusals.KnownWindow(
            "weekly" if window == "weekly" else "five_hour"),
            refusals.KnownReset(now + timedelta(minutes=float(args.reset_in)))
            if args.reset_in is not None else refusals.NoResetKnown())
        assumed = {session_id: held for session_id in selected} if not args.dry_run else {}
    else:
        assumed = {}
    token = _assumed_refusals.set(assumed)
    try:
        for line in run(dry_run=bool(args.dry_run), proof_group=frozenset(proof_ids), account_config=account_config):
            print(line)
    finally:
        _assumed_refusals.reset(token)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"refusal watcher: {str(error).splitlines()[0]}", file=sys.stderr)
        sys.exit(1)
