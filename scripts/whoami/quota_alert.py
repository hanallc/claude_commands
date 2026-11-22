#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Claude gets one phone reminder when a low account lacks an account-move entry.

Codex refusal broadcasts no-worker mode when no account has capacity.
Failed phone delivery stays due until the next timer pass. Claude recovery closes
its episode silently; /quota_refresh still refreshes readings.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import asyncio
import copy
import hashlib
import json
import math
import os
import signal
import socket
import subprocess
import sys
import uuid
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, NotRequired, Protocol, TypedDict, cast

from scripts.lib import process_group

from scripts.lib.process_failure import failure_detail
from scripts.message import send_result, send_budget, session_send
from scripts.agents import remote
from scripts.whoami import mac_fallback_token, note_file, token_expiry

from scripts.lib import conf, models_registry  # noqa: E402

from scripts.lib.user_zone import user_zone
from scripts.whoami import agent_accounts
from scripts.accounts import moves, next_moves, refusals, role_map
from scripts.whoami import quota_state, run_out, usage_store

from scripts.notify import review, silence

from scripts.whoami.note_file import reset_time
from scripts.whoami.agent_accounts import (ClaudeAccountsInUse, CodexHomeSource, Report,
                            codex_can_work, CodexWorkAvailable, codex_email_on_disk,
                            codex_homes, codex_live, observe_workers_home, NoWorkersHome)

if TYPE_CHECKING:
    from scripts.notify.push_texts import RenderedPush
    from scripts.accounts import accounts
    from scripts.accounts.accounts import CodexWorkersAccount


@dataclass(frozen=True)
class CodexHomeReading:
    home: Path
    report: Report
    observed_at: datetime


@dataclass(frozen=True)
class CodexAlertPassReadings:
    workers: CodexWorkersAccount | NoWorkersHome
    selected_account: agent_accounts.RefusingCodexAccount | NoWorkersHome
    checked_homes: tuple[CodexHomeReading, ...]


@dataclass(frozen=True)
class ReadCurrentCodexReadings:
    """A standalone call collects one selection's live readings."""


READ_CURRENT_CODEX_READINGS = ReadCurrentCodexReadings()
UNKNOWN_REFUSAL_WINDOW = refusals.UnknownWindow()


def codex_alert_pass(home: Path | NoWorkersHome, reports: list[Report], observed_at: datetime) -> CodexAlertPassReadings:
    from scripts.accounts.accounts import CodexWorkersAccount

    workers = (home if isinstance(home, NoWorkersHome)
               else CodexWorkersAccount(agent_accounts.codex_home_role_label(home), home))
    identity = home if isinstance(home, NoWorkersHome) else agent_accounts.codex_account(home)
    return CodexAlertPassReadings(
        workers, identity,
        tuple(CodexHomeReading(report.source.home, copy.deepcopy(report), observed_at)
              for report in reports if isinstance(report.source, CodexHomeSource)),
    )


def resolve_codex_readings(value: CodexAlertPassReadings | ReadCurrentCodexReadings, *,
                           now: datetime) -> CodexAlertPassReadings:
    if isinstance(value, CodexAlertPassReadings):
        return value
    home = observe_workers_home()
    async def collect() -> list[Report]:
        return list(await asyncio.gather(*(codex_live(folder) for folder in codex_homes(home)
                                          if folder.is_dir() and codex_email_on_disk(folder))))
    return codex_alert_pass(home, asyncio.run(collect()), now)


def selected_codex_report(readings: CodexAlertPassReadings, now: datetime) -> Report:
    identity = readings.selected_account
    if isinstance(identity, agent_accounts.KnownCodexAccount) and not isinstance(readings.workers, NoWorkersHome):
        max_age = timedelta(minutes=conf.read("accounts").whole("usage_stale_minutes"))
        for checked in readings.checked_homes:
            if (checked.home == readings.workers.home and checked.home == identity.home
                    and checked.report.email and checked.report.email.casefold() == identity.login.casefold()
                    and timedelta(0) <= now - checked.observed_at <= max_age):
                return checked.report
    return Report("Codex", problem="the selected account's current reading is unknown")


def codex_swap_line(readings: CodexAlertPassReadings, now: datetime) -> str:
    """Describe the refusal destination without asking the user to swap workers."""
    from scripts.accounts import accounts

    if isinstance(readings.workers, NoWorkersHome):
        return readings.workers.reason
    chosen = accounts.pick("codex", now, exclude=frozenset({readings.workers.label}), keep=accounts.NoKeep())
    if isinstance(chosen, accounts.PickedAccount):
        return f"workers move to {chosen.label} when Codex refuses work"
    return "no other Codex account has room until " + reset_display(chosen.earliest_reset)


def reset_display(reset: refusals.KnownReset | refusals.NoResetKnown | accounts.EarliestReset) -> str:
    from scripts.accounts import accounts

    return (reset.at.astimezone(user_zone()).strftime("%a %Y-%m-%d %H:%M %Z")
            if isinstance(reset, (refusals.KnownReset, accounts.EarliestReset)) else "an unknown reset")


def _save_codex_readings(readings: CodexAlertPassReadings, now: datetime) -> None:
    max_age = timedelta(minutes=conf.read("accounts").whole("usage_stale_minutes"))
    try:
        with usage_store.read_lock():
            held = usage_store.load()
            changed = False
            for checked in readings.checked_homes:
                if (not checked.report.problem and not checked.report.quota_problem
                        and timedelta(0) <= now - checked.observed_at <= max_age):
                    if checked.report.email:
                        previous = held.get(usage_store.key("Codex", checked.report.email), {})
                        if "weekly" in previous:
                            try:
                                if datetime.fromisoformat(previous["weekly"]["at"]) > checked.observed_at:
                                    continue
                            except ValueError:
                                pass
                    changed = usage_store.merge(held, [checked.report], checked.observed_at) or changed
            if changed:
                usage_store.write(held)
    except (OSError, ValueError) as error:
        print(f"usage store: {error}", file=sys.stderr)


def _reading_for_account(readings: CodexAlertPassReadings, identity: agent_accounts.KnownCodexAccount,
                         now: datetime) -> refusals.FreshReading | refusals.StaleReading | refusals.NoReading:
    reading = refusals.stored_reading("codex", identity.login, now)
    if isinstance(reading, refusals.FreshReading):
        max_age = timedelta(minutes=conf.read("accounts").whole("usage_stale_minutes"))
        for checked in readings.checked_homes:
            if (checked.home == identity.home and checked.report.email
                    and checked.report.email.casefold() == identity.login.casefold()
                    and not checked.report.problem and not checked.report.quota_problem
                    and timedelta(0) <= now - checked.observed_at <= max_age
                    # The store drops fractions of a second; recover this pass's exact observation time.
                    and reading.weekly.at == checked.observed_at.replace(microsecond=0)):
                return replace(reading, weekly=replace(reading.weekly, at=checked.observed_at),
                    five_hour=replace(reading.five_hour, at=checked.observed_at)
                    if isinstance(reading.five_hour, refusals.ReadingWindow) else reading.five_hour)
    return reading


def _credits_pay_after(refusal: refusals.AccountRefusal, readings: CodexAlertPassReadings,
                       selected: agent_accounts.KnownCodexAccount) -> bool:
    """Credits are on, and a reading taken after the refusal shows a credit balance."""
    if not isinstance(agent_accounts.codex_credits_setting(selected.label), agent_accounts.CreditsOn):
        return False
    for checked in readings.checked_homes:
        balance = checked.report.credit_balance
        if (checked.home != selected.home or checked.observed_at <= refusal.at or checked.report.problem
                or not checked.report.email or checked.report.email.casefold() != selected.login.casefold()
                or balance is None):
            continue
        if balance == "unlimited":
            return True
        try:
            amount = float(balance)
        except ValueError:
            continue
        if math.isfinite(amount) and amount > 0:
            return True
    return False


def _refusal_blocks_return(readings: CodexAlertPassReadings, selected: agent_accounts.KnownCodexAccount,
                           now: datetime) -> bool:
    """An unresolved refusal keeps work off the account until allowance returns or credits pay."""
    refusal = refusals.load().get(refusals.key("codex", selected.login))
    return (refusal is not None
            and not refusals.resolved(refusal, _reading_for_account(readings, selected, now), now)
            and not _credits_pay_after(refusal, readings, selected))


def _refusal_window(window: refusals.KnownWindow | refusals.UnknownWindow,
                    reading: refusals.FreshReading | refusals.StaleReading | refusals.NoReading,
                    at: datetime) -> refusals.KnownWindow | refusals.UnknownWindow:
    if isinstance(window, refusals.KnownWindow):
        return window
    if isinstance(reading, refusals.FreshReading):
        exhausted = [name for name, value in (("weekly", reading.weekly), ("five_hour", reading.five_hour))
                     if isinstance(value, refusals.ReadingWindow) and value.at > at and value.left == 0]
        if len(exhausted) == 1:
            return refusals.KnownWindow(cast(Literal["weekly", "five_hour"], exhausted[0]))
    return refusals.UnknownWindow()


def _finish_workers_transitions(
    now: datetime, journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL,
) -> None:
    """The timer or a resumed worker closes each expired wait once."""
    entries = moves.read_moves() if isinstance(journal, moves.ReadCurrentMoveJournal) else journal
    ended = {entry["move_id"] for entry in entries if entry["kind"] == "wait ended"}
    for entry in entries:
        if entry["tool"] != "codex" or entry["subject"] != "workers" or entry["kind"] != "waited" or entry["move_id"] in ended:
            continue
        reset = datetime.fromisoformat(entry["reset"])
        if reset <= now:
            moves.append_move(moves.WaitEndedMove(kind="wait ended", at=now.isoformat(), tool="codex",
                                                 subject="workers", move_id=entry["move_id"]))
            ended.add(entry["move_id"])


def _room_again(
    now: datetime, journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL,
) -> None:
    from scripts.accounts import accounts

    entries = moves.read_moves() if isinstance(journal, moves.ReadCurrentMoveJournal) else journal
    closed = {entry["move_id"] for entry in entries if entry["kind"] == "room again"}
    pending = [entry for entry in entries if entry["tool"] == "codex" and entry["subject"] == "workers"
               and entry["kind"] == "no room" and entry["move_id"] not in closed]
    if pending:
        chosen = accounts.pick("codex", now, keep=accounts.NoKeep())
        if isinstance(chosen, accounts.PickedAccount):
            for entry in pending:
                moves.append_move(moves.RoomAgainMove(kind="room again", at=now.isoformat(), tool="codex",
                    subject="workers", move_id=entry["move_id"], label=chosen.label))


@dataclass(frozen=True)
class ReadCurrentClaudeSelection:
    """A standalone alert call resolves its account selection before doing any work."""


READ_CURRENT_CLAUDE_SELECTION = ReadCurrentClaudeSelection()


def _selection(value: ClaudeAccountsInUse | ReadCurrentClaudeSelection) -> ClaudeAccountsInUse:
    if isinstance(value, ReadCurrentClaudeSelection):
        # Resolve only standalone calls; injected selections are already fixed.
        from scripts.accounts import accounts

        return accounts.claude_account_selection()
    return value


from scripts.production.showrunners import (
    CONFIG as CONFIG,
    Showrunner as Showrunner,
    ShowrunnerSettings as ShowrunnerSettings,
    load_settings_from as load_settings_from,
    registered_showrunners as registered_showrunners,
    require_complete,
    socket_for as socket_for,
)


Config = ShowrunnerSettings
STATE = quota_state.state_path()
SESSIONS = Path.home() / ".claude" / "sessions"
PROTOCOL = Path.home() / ".claude" / "docs" / "quota_alerts.md"
AGENTS = Path(__file__).resolve().parent.parent / "agents" / "models_config.sh"
# The same override models_config.sh reads; edit_registry() passes it on explicitly.
REGISTRY = models_registry.registry_path()
# The session whose user is shown a switch on their next prompt.
OWNER = "natedev"
NO_WORKER_MODE_SENDER = "quota-alert"
NO_WORKER_MODE_FYI = "FYI, no action:"
KILL_GRACE = 10


class Episode(TypedDict):
    since: str
    weekly_reset: NotRequired[str]
    reached_zero: NotRequired[bool]
    # "due" until the first low-quota user message succeeds, then its UTC ISO time.
    push: NotRequired[str]
    # UTC ISO time of the successful Codex-refusal user message.
    refusal_push: NotRequired[str]
    # Why the editor refused the switch to Claude this Codex episode opened with.
    switch_failed: NotRequired[str]
    # Recipient -> UTC ISO time of the last attempt, delivered or not.
    last: dict[str, str]


@dataclass(frozen=True)
class NoCodexQuotaFallback:
    """No assignments are owed back to Codex."""


@dataclass(frozen=True)
class NoFallbackHold:
    """Ordinary refusal recovery follows account readiness immediately."""


@dataclass(frozen=True)
class CreditsOffHold:
    """Keep future work on Claude until a known refill or explicit account change."""
    until: datetime


@dataclass(frozen=True)
class ScheduledMoveHold:
    """A requested Claude route lasts until this source account's weekly reset."""
    until: datetime

    def __post_init__(self) -> None:
        if self.until.utcoffset() is None:
            raise ValueError("scheduled hold needs a zone")


NO_FALLBACK_HOLD = NoFallbackHold()
DEFAULT_CLAUDE_ACCOUNT = next_moves.DefaultClaudeAccount()


@dataclass(frozen=True)
class AccountCodexQuotaFallback:
    owner: agent_accounts.KnownCodexAccount
    at: str
    moved: tuple[str, ...]
    hold: CreditsOffHold | ScheduledMoveHold | NoFallbackHold = NO_FALLBACK_HOLD
    claude_account: next_moves.ClaudeWorkersTarget | next_moves.DefaultClaudeAccount = DEFAULT_CLAUDE_ACCOUNT


@dataclass(frozen=True)
class PendingClaudeSwitch:
    """Assignments selected for a switch whose registry outcome is not yet saved."""

    owner: agent_accounts.KnownCodexAccount
    at: str
    assignment_keys: tuple[str, ...]
    hold: CreditsOffHold | ScheduledMoveHold | NoFallbackHold
    claude_account: next_moves.ClaudeWorkersTarget | next_moves.DefaultClaudeAccount
    cause: Literal["credits off", "refusal", "auto"]
    why: str
    description: str


@dataclass(frozen=True)
class UnknownOwnerCodexQuotaFallback:
    """A fallback saved before its refusing account was recorded."""

    at: str
    moved: tuple[str, ...]


CodexQuotaFallback = NoCodexQuotaFallback | AccountCodexQuotaFallback | UnknownOwnerCodexQuotaFallback
NO_CODEX_QUOTA_FALLBACK = NoCodexQuotaFallback()


class KnownRefusalNotice(TypedDict):
    kind: Literal["known"]
    login: str
    label: str
    push: NotRequired[str]
    switch_failed: NotRequired[str]
    failure_pushes: NotRequired[list[str]]


class UnknownRefusalNotice(TypedDict):
    kind: Literal["unknown"]


RefusalNotice = KnownRefusalNotice | UnknownRefusalNotice


class RestorationNoticeIdentity(TypedDict):
    fallback_at: str
    kind: Literal["routed back", "routing not restored"]


@dataclass(frozen=True)
class RestorationOutcomeOwed:
    """A finished restore whose journal row and OWNER notice are not yet saved."""

    routing: moves.RoutedBack | moves.RoutingNotRestored
    notice: str


class StoredRestorationOutcome(TypedDict):
    routing: moves.RoutedBack | moves.RoutingNotRestored
    notice: str


class ZeroEpisodeNotice(TypedDict):
    id: str
    values: dict[str, str]


class CodexZeroEpisode(TypedDict):
    since: str
    weekly_reset: str
    credits: Literal["on", "off"]
    phone: str
    forecast_at: str
    forecast_phone: NotRequired[str]
    # Save empty-queue facts; queued advice is applied only when rendering a retry.
    notice: ZeroEpisodeNotice
    forecast_notice: NotRequired[ZeroEpisodeNotice]
    credits_off_fallback: NotRequired[Literal["waiting_for_next_entry"]]


class ClaudeExpiryAttempt(TypedDict):
    weekly_reset: str
    attempted_at: str


class PreviousClaudeExpiryDelivery(TypedDict):
    weekly_reset: str
    delivered_at: str


class TokenDeliveryAttempt(TypedDict):
    attempted_at: str
    delivered: bool


class TokenExpiryAttempt(TokenDeliveryAttempt):
    event: str


class PreviousMacTokenLoginClaim(TypedDict):
    made: str
    failure: str


class MacTokenLoginClaim(PreviousMacTokenLoginClaim, TokenDeliveryAttempt):
    pass


class State(TypedDict):
    # Account label ("codex1") -> its open episode.
    episodes: dict[str, Episode]
    claude_expiry: NotRequired[dict[str, ClaudeExpiryAttempt]]
    token_expiry_alerts: NotRequired[dict[str, TokenExpiryAttempt]]
    mac_token_login_claim: NotRequired[MacTokenLoginClaim]
    codex_zero: NotRequired[dict[str, CodexZeroEpisode]]
    refusal_notices: NotRequired[dict[str, RefusalNotice]]
    fallback: CodexQuotaFallback
    pending_switch: NotRequired[PendingClaudeSwitch]
    restoration_owed: NotRequired[RestorationOutcomeOwed]
    restoration_notice: NotRequired[RestorationNoticeIdentity]
    # One line per switch or switch-back the user has not been shown in OWNER.
    unseen: NotRequired[list[str]]
    pending_routing: NotRequired[moves.RoutedToClaude]
    pending_move_failures: NotRequired[list[moves.FailedMove]]
    workers_move_notices: NotRequired[dict[str, str]]
    no_worker_mode: NotRequired[bool]


class StoredFallback(TypedDict):
    at: str
    moved: list[str]
    owner: NotRequired[agent_accounts.StoredCodexAccount]
    credits_off_until: NotRequired[str]
    scheduled_move_until: NotRequired[str]
    claude_account: NotRequired[str]


class StoredState(TypedDict):
    episodes: dict[str, Episode]
    claude_expiry: NotRequired[dict[str, ClaudeExpiryAttempt | PreviousClaudeExpiryDelivery]]
    token_expiry_alerts: NotRequired[dict[str, TokenExpiryAttempt]]
    mac_token_alerts: NotRequired[dict[str, TokenExpiryAttempt]]
    mac_token_login_claim: NotRequired[MacTokenLoginClaim | PreviousMacTokenLoginClaim]
    codex_zero: NotRequired[dict[str, CodexZeroEpisode]]
    refusal_notices: NotRequired[dict[str, RefusalNotice]]
    switch: NotRequired[StoredFallback]
    pending_switch: NotRequired[StoredPendingClaudeSwitch]
    restoration_owed: NotRequired[StoredRestorationOutcome]
    restoration_notice: NotRequired[RestorationNoticeIdentity]
    unseen: NotRequired[list[str]]
    pending_routing: NotRequired[moves.RoutedToClaude]
    pending_move_failures: NotRequired[list[moves.FailedMove]]
    workers_move_notices: NotRequired[dict[str, str]]
    no_worker_mode: NotRequired[bool]


class StoredPendingClaudeSwitch(TypedDict):
    switch: StoredFallback
    cause: Literal["credits off", "refusal", "auto"]
    why: str
    description: NotRequired[str]


def _read_fallback(value: object) -> CodexQuotaFallback:
    if not isinstance(value, dict):
        return NoCodexQuotaFallback()
    record = cast(dict[str, object], value)
    at, moved = record.get("at"), record.get("moved")
    if not isinstance(at, str) or not isinstance(moved, list):
        return NoCodexQuotaFallback()
    entries = tuple(item for item in cast(list[object], moved) if isinstance(item, str))
    owner = record.get("owner")
    if isinstance(owner, dict):
        fields = cast(dict[str, object], owner)
        home, login, label = fields.get("home"), fields.get("login"), fields.get("label")
        if isinstance(home, str) and isinstance(login, str) and isinstance(label, str):
            hold: CreditsOffHold | ScheduledMoveHold | NoFallbackHold = NO_FALLBACK_HOLD
            until = record.get("credits_off_until")
            if isinstance(until, str):
                parsed = datetime.fromisoformat(until)
                if parsed.tzinfo is not None:
                    hold = CreditsOffHold(parsed)
            scheduled = record.get("scheduled_move_until")
            if isinstance(scheduled, str):
                hold = ScheduledMoveHold(datetime.fromisoformat(scheduled))
            target = record.get("claude_account")
            destination = (next_moves.ClaudeWorkersTarget(target) if isinstance(target, str)
                           else next_moves.DefaultClaudeAccount())
            return AccountCodexQuotaFallback(agent_accounts.KnownCodexAccount(Path(home), login, note_file.canonical_label(label)), at, entries, hold, destination)
    return UnknownOwnerCodexQuotaFallback(at, entries)


def _stored_fallback(fallback: AccountCodexQuotaFallback | UnknownOwnerCodexQuotaFallback) -> StoredFallback:
    record = StoredFallback(at=fallback.at, moved=list(fallback.moved))
    if isinstance(fallback, AccountCodexQuotaFallback):
        if isinstance(fallback.claude_account, next_moves.ClaudeWorkersTarget):
            record["claude_account"] = fallback.claude_account.label
        if isinstance(fallback.hold, CreditsOffHold):
            record["credits_off_until"] = fallback.hold.until.isoformat()
        if isinstance(fallback.hold, ScheduledMoveHold):
            record["scheduled_move_until"] = fallback.hold.until.isoformat()
        record["owner"] = agent_accounts.StoredCodexAccount(home=str(fallback.owner.home),
            login=fallback.owner.login, label=fallback.owner.label)
    return record


def _stored_state(state: State) -> StoredState:
    saved = StoredState(episodes=state["episodes"])
    if token_attempts := state.get("token_expiry_alerts"):
        saved["token_expiry_alerts"] = token_attempts
    if "mac_token_login_claim" in state:
        saved["mac_token_login_claim"] = state["mac_token_login_claim"]
    if expiry_attempts := state.get("claude_expiry"):
        saved["claude_expiry"] = {label: attempt for label, attempt in expiry_attempts.items()}
    if "no_worker_mode" in state:
        saved["no_worker_mode"] = state["no_worker_mode"]
    if "codex_zero" in state:
        saved["codex_zero"] = state["codex_zero"]
    if "refusal_notices" in state:
        saved["refusal_notices"] = state["refusal_notices"]
    if "unseen" in state:
        saved["unseen"] = state["unseen"]
    if "pending_routing" in state:
        saved["pending_routing"] = state["pending_routing"]
    if "pending_move_failures" in state:
        saved["pending_move_failures"] = state["pending_move_failures"]
    if "workers_move_notices" in state:
        saved["workers_move_notices"] = state["workers_move_notices"]
    if "restoration_owed" in state:
        owed = state["restoration_owed"]
        routing = owed.routing.copy()
        if isinstance(routing["owner"], moves.UnknownFallbackOwner):
            routing["owner"] = "unknown"
        saved["restoration_owed"] = StoredRestorationOutcome(routing=routing, notice=owed.notice)
    if "restoration_notice" in state:
        saved["restoration_notice"] = state["restoration_notice"]
    fallback = state["fallback"]
    if not isinstance(fallback, NoCodexQuotaFallback):
        saved["switch"] = _stored_fallback(fallback)
    if "pending_switch" in state:
        pending = state["pending_switch"]
        saved["pending_switch"] = StoredPendingClaudeSwitch(switch=_stored_fallback(AccountCodexQuotaFallback(
            pending.owner, pending.at, pending.assignment_keys, pending.hold, pending.claude_account)),
            cause=pending.cause, why=pending.why, description=pending.description)
    return saved


@dataclass(frozen=True)
class NoRestorationAccount:
    """A description assembled outside an actual Codex account restoration."""


NO_RESTORATION_ACCOUNT = NoRestorationAccount()


class SwitchBack(NamedTuple):
    moved: list[str]
    # Re-assigned by someone since the switch, so left alone.
    kept: list[str]
    # "key (why)" for each the editor refused.
    failed: list[str]
    restored_account: agent_accounts.KnownCodexAccount | NoRestorationAccount = NO_RESTORATION_ACCOUNT

    def describe(self) -> str:
        parts = [f"moved back from Claude to Codex: {', '.join(self.moved)}" if self.moved else "",
                 f"left alone, re-assigned since the switch: {', '.join(self.kept)}" if self.kept else "",
                 f"could not move back: {', '.join(self.failed)}" if self.failed else ""]
        return "; ".join(part for part in parts if part)


@dataclass(frozen=True)
class NoCodexFallbackRestoration:
    """Nothing is owed, or the selected account cannot yet work."""


CodexFallbackRestoration = NoCodexFallbackRestoration | SwitchBack


class AgentNote(Protocol):
    path: Path
    tool: str
    label: str

    def get(self, key: str) -> str | None: ...
    def get_list(self, key: str) -> tuple[str, ...]: ...


class Job(NamedTuple):
    name: str
    recipient: str
    text: str
    kind: str


class Ask(NamedTuple):
    """What a direct user message asks: `action`, which the user decides while work goes on or takes before it can."""
    need: Literal["decision", "blocked"]
    action: str


class UserPush(NamedTuple):
    """An actionable quota notice delivered to the user's phone."""
    name: str
    rendered: RenderedPush

    @property
    def summary(self) -> str:
        return self.rendered.title

    @property
    def text(self) -> str:
        return self.rendered.body

    @property
    def ask(self) -> Ask:
        from scripts.notify.user_action import ActionRequired
        assert isinstance(self.rendered.action, ActionRequired)
        assert self.rendered.need in ("decision", "blocked")
        return Ask(self.rendered.need, self.rendered.action.text)


class QuotaStatusNote(NamedTuple):
    """Quota information recorded in the log without a phone delivery."""
    name: str
    text: str


QuotaNotice = UserPush | QuotaStatusNote


def load_config() -> Config:
    return load_settings_from(CONFIG)


def showrunners_now() -> list[Showrunner]:
    """Every showrunner with an update timer. None when the session records cannot say who runs."""
    try:
        return require_complete(registered_showrunners())
    except OSError as error:
        print(f"quota-alert: showrunners unknown: {error}", file=sys.stderr)
        return []


def recipients(config: Config, here: str | None = None) -> list[str]:
    """Recipients for five-hour, Claude no-room and blocked next-head notices."""
    from scripts.message import send
    from scripts.production import staff

    membership = staff.snapshot()
    names = [*config["always"], *(runner["name"] for runner in showrunners_now() if runner["session"])]
    excluded = send.staff_named(here, membership) if here and not here.startswith("uds:") else None
    found: list[str] = []
    identities: set[str] = set()
    for target in names:
        member = send.staff_named(target, membership) if not target.startswith("uds:") else None
        if isinstance(member, staff.StaffRow):
            if isinstance(excluded, staff.StaffRow) and member.id == excluded.id:
                continue
            if member.id in identities:
                continue
            identities.add(member.id)
            address = target if target.startswith("session:") else str(member.id)
        elif target.startswith(("session:", "uds:")):
            address = target
        else:
            raise ValueError(f"{target}: no staff recipient")
        if address != here and address not in found:
            found.append(address)
    return found


def _normalize_notice_text(notice: ZeroEpisodeNotice) -> None:
    notice["values"] = {key: note_file.canonical_label_text(value)
                        for key, value in notice["values"].items()}


def _write_state(state: State) -> None:
    """Save under the caller's state lock, including before a registry edit."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f".{STATE.name}.tmp")
    _ = tmp.write_text(json.dumps(_stored_state(state), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE)


def _start_detached(what: str, script: str, arguments: Callable[[], list[str]]) -> None:
    """Start one transient unit for a script; a failed start prints one line."""
    try:
        scripts = Path(__file__).resolve().parents[1]
        command = [os.environ.get("SHUTDOWN_SYSTEMD_RUN", "systemd-run"), "--user", "--collect", "--quiet", "--no-block",
                   "--working-directory=" + str(scripts.parent), "--", str(scripts / "lib/py"),
                   str(scripts / script), *arguments()]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"quota-alert: {what} failed: {' '.join(str(error).split())}")
    else:
        if result.returncode:
            print(f"quota-alert: {what} failed: exit {result.returncode}")


def _worker_mode_text(active: bool) -> str:
    return quota_state.no_worker_notice() if active else conf.read("accounts").text("no_worker_mode_off")


def _broadcast_worker_mode(active: bool) -> None:
    """Announce the mode after the state lock closes; when it goes on, also stop the Codex workers.

    Each producer in `no_worker_mode_fyi` gets one copy marked as information only.
    """
    def notice() -> list[str]:
        text = _worker_mode_text(active)
        return ["--from", NO_WORKER_MODE_SENDER, "--showrunners", text, "--units", text]

    _start_detached("no-worker mode broadcast", "production/broadcast.py", notice)
    try:
        producers = conf.read("accounts").text("no_worker_mode_fyi").split()
    except conf.Unusable as error:
        print(f"quota-alert: no-worker mode FYI failed: {' '.join(str(error).split())}")
        producers = []
    for producer in producers:
        _start_detached(f"no-worker mode FYI to {producer}", "message/send.py", _fyi(producer, active))
    if active:
        _start_detached("Codex worker stop", "production/worker_capacity.py", lambda: ["stop"])


def _fyi(producer: str, active: bool) -> Callable[[], list[str]]:
    def arguments() -> list[str]:
        return ["--to", producer, "--from", NO_WORKER_MODE_SENDER,
                "--text", f"{NO_WORKER_MODE_FYI} {_worker_mode_text(active)}"]
    return arguments


def _start_no_worker_mode(state: State, readiness: agent_accounts.CodexWorkReadiness,
                          destination: accounts.PickedAccount | accounts.NoAccountWithRoom) -> bool:
    """Enter only when the workers account is exhausted and no other account can work."""
    from scripts.accounts import accounts

    if (not isinstance(readiness, agent_accounts.CodexWorkExhausted)
            or not isinstance(destination, accounts.NoAccountWithRoom)):
        return False
    if socket.gethostname().split(".", 1)[0] != role_map.ROLE_MAP_HOST:
        return False
    state["no_worker_mode"] = True
    return True


@contextmanager
def state_file() -> Generator[State]:
    """The state under an exclusive lock, written back on exit.

    alert() holds it only around its reads, writes and registry edits, never across
    a relay. Holding it across an edit is what makes a switch happen once.
    """
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE.with_name(f".{STATE.name}.lock"), "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state: State
        try:
            saved = cast(StoredState, json.loads(STATE.read_text(encoding="utf-8")))
            state = State(episodes={note_file.canonical_label(label): episode for label, episode in saved.get("episodes", {}).items()}, fallback=_read_fallback(saved.get("switch")))
            old_mac_prefix = token_expiry.LEGACY_MAC_EXPIRY_ID + "/"
            token_attempts = dict(saved.get("token_expiry_alerts", {}))
            for slot, attempt in saved.get("mac_token_alerts", {}).items():
                if not slot.startswith(old_mac_prefix):
                    continue
                key = f"{token_expiry.MAC_TOKEN}/{slot.removeprefix(old_mac_prefix)}"
                current = token_attempts.get(key)
                if current is None or (current["event"] == attempt["event"]
                                       and attempt["delivered"] and not current["delivered"]):
                    token_attempts[key] = attempt
            if token_attempts:
                state["token_expiry_alerts"] = token_attempts
            if "mac_token_login_claim" in saved:
                claim = saved["mac_token_login_claim"]
                state["mac_token_login_claim"] = (
                    claim if "attempted_at" in claim and "delivered" in claim else
                    MacTokenLoginClaim(made=claim["made"], failure=claim["failure"],
                                       attempted_at=claim["failure"], delivered=False))
            if "claude_expiry" in saved:
                state["claude_expiry"] = {
                    note_file.canonical_label(label): ClaudeExpiryAttempt(
                        weekly_reset=attempt["weekly_reset"],
                        attempted_at=attempt["attempted_at"] if "attempted_at" in attempt else attempt["delivered_at"])
                    for label, attempt in saved["claude_expiry"].items()
                }
            if "codex_zero" in saved:
                state["codex_zero"] = {note_file.canonical_label(label): episode for label, episode in saved["codex_zero"].items()}
                for episode in state["codex_zero"].values():
                    _normalize_notice_text(episode["notice"])
                    if "forecast_notice" in episode:
                        _normalize_notice_text(episode["forecast_notice"])
            if "refusal_notices" in saved:
                state["refusal_notices"] = {note_file.canonical_label(label): notice for label, notice in saved["refusal_notices"].items()}
                for notice in state["refusal_notices"].values():
                    if notice["kind"] == "known":
                        notice["label"] = note_file.canonical_label(notice["label"])
            if "unseen" in saved:
                state["unseen"] = saved["unseen"]
            if "workers_move_notices" in saved:
                state["workers_move_notices"] = saved["workers_move_notices"]
            if "no_worker_mode" in saved:
                state["no_worker_mode"] = saved["no_worker_mode"]
            if "pending_routing" in saved:
                state["pending_routing"] = saved["pending_routing"]
            if "pending_move_failures" in saved:
                state["pending_move_failures"] = saved["pending_move_failures"]
            if "restoration_owed" in saved:
                owed = saved["restoration_owed"]
                routing = owed["routing"].copy()
                if routing["owner"] == "unknown":
                    routing["owner"] = moves.UnknownFallbackOwner()
                state["restoration_owed"] = RestorationOutcomeOwed(routing, owed["notice"])
            if "restoration_notice" in saved:
                state["restoration_notice"] = saved["restoration_notice"]
            if "pending_switch" in saved:
                pending = saved["pending_switch"]
                intent = _read_fallback(pending["switch"])
                if isinstance(intent, AccountCodexQuotaFallback):
                    state["pending_switch"] = PendingClaudeSwitch(intent.owner, intent.at, intent.moved,
                        intent.hold, intent.claude_account, pending["cause"], pending["why"],
                        pending.get("description", f"{pending['why']} ({intent.owner.label})"))
        except (OSError, ValueError):
            state = State(episodes={}, fallback=NoCodexQuotaFallback())
        before = json.dumps(_stored_state(state), sort_keys=True)
        mode_before = state.get("no_worker_mode", False)
        for episode in state["episodes"].values():
            _ = cast(dict[str, object], cast(object, episode)).pop("acknowledged", None)
        yield state
        if json.dumps(_stored_state(state), sort_keys=True) != before:
            _write_state(state)
        mode_after = state.get("no_worker_mode", False)
    if mode_after != mode_before:
        _broadcast_worker_mode(mode_after)



def percent(note: AgentNote) -> str:
    value = note_file.remaining(note)
    return "unknown" if value is None else f"{value:g}%"


def low(notes: list[AgentNote], threshold: float, *, now: datetime) -> list[AgentNote]:
    """Notes in use at or under the threshold in a window that has not reset."""
    return [note for note in notes
            if note_file.in_use(note) and note_file.ahead(note.get("resets"), now=now)
            and (value := note_file.remaining(note)) is not None and value <= threshold]


def recovered(name: str, notes: list[AgentNote], threshold: float,
              selection: ClaudeAccountsInUse, now: datetime,
              codex_readings: CodexAlertPassReadings,
              episode: Episode) -> bool:
    """Known to be out of the low state. An unknown reading is not a recovery."""
    del selection
    note = next((note for note in notes if note.label == name), None)
    parsed_label = note_file.parse_account_label(name)
    if isinstance(parsed_label, note_file.NumberedAccountLabel) and parsed_label.tool == "codex":
        reset_at = episode.get("weekly_reset")
        if reset_at:
            try:
                if datetime.fromisoformat(reset_at) <= now:
                    return True
            except ValueError:
                pass
        if episode.get("reached_zero"):
            return False
        if note is None:
            return False
        max_age = timedelta(minutes=conf.read("accounts").whole("usage_stale_minutes"))
        for checked in codex_readings.checked_homes:
            report = checked.report
            if (report.email and report.email.casefold() == (note.get("login") or "").casefold()
                    and not report.problem
                    and timedelta(0) <= now - checked.observed_at <= max_age):
                value, reset = report.weekly_remaining, report.weekly_reset
                if value is not None and reset is not None and reset > now and value > threshold:
                    return True
        reset = reset_time(note.get("resets"))
        return isinstance(reset, note_file.KnownReset) and reset.value <= now
    if isinstance(parsed_label, note_file.NumberedAccountLabel) and parsed_label.tool == "claude":
        reset_at = episode.get("weekly_reset")
        if reset_at:
            try:
                if datetime.fromisoformat(reset_at) <= now:
                    return True
            except ValueError:
                pass
        if note is None:
            return False
        value = note_file.remaining(note)
        if value is not None and math.isfinite(value) and value > threshold:
            return True
        reset = reset_time(note.get("resets"))
        return isinstance(reset, note_file.KnownReset) and reset.value <= now
    if note is None or not note_file.in_use(note):
        return True
    value = note_file.remaining(note)
    return value is not None and value > threshold


def close_recovered(state: State, notes: list[AgentNote], threshold: float,
                    selection: ClaudeAccountsInUse, now: datetime,
                    codex_readings: CodexAlertPassReadings) -> list[str]:
    """Remove the episodes whose account recovered and return their names."""
    episodes = state["episodes"]
    closed = [name for name in episodes if recovered(name, notes, threshold, selection, now, codex_readings, episodes[name])]
    for name in closed:
        del episodes[name]
        notices = state.get("refusal_notices", {})
        for home, notice in list(notices.items()):
            if notice["kind"] == "known" and notice["label"] == name:
                del notices[home]
    return closed


def local_time(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone(user_zone()).strftime("%Y-%m-%d %H:%M")


def registry_assignments() -> dict[str, str]:
    """[assignments] in the registry: function, or exact-task override, -> family."""
    rows = models_registry.parse_registry(models_registry.registry_text(REGISTRY))
    return rows.get("assignments", {})


def edit_registry(editor: str, *args: str) -> str | None:
    """Run a validated registry editor and return its failure cause.

    The edit is live at once and never waits for main; publish_registry() pushes it later.
    A timeout stops the process group before the caller reads the result.
    """
    command = ["bash", "-c", f'source "$0" && {editor} "$@"', str(AGENTS), *args]
    timeout = conf.read("accounts").whole("quota_edit_timeout_seconds")
    publish = "0" if os.environ.get("MODELS_CONFIG_PUBLISH") == "0" else "later"
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, "MODELS_CONFIG_FILE": str(REGISTRY),
                                        "MODELS_CONFIG_PUBLISH": publish}, start_new_session=True)
    except OSError as error:
        return str(error)
    with process:
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process_group.send_process_group(process.pid, signal.SIGKILL)
            _ = process.communicate()
            return f"{editor} took longer than {timeout} s and was stopped"
    if process.returncode == 0:
        return None
    return " ".join((err.strip() or out.strip() or f"exit {process.returncode}").split())


@dataclass(frozen=True)
class CodexWorkMovedToClaude:
    account: str
    assignments: tuple[str, ...]

    def log_line(self) -> str:
        return f"switched to claude for {self.account}: {', '.join(self.assignments)}"


@dataclass(frozen=True)
class NoWorkOnCodex:
    account: str

    def log_line(self) -> str:
        return f"switch to claude for {self.account}: nothing was on codex"


@dataclass(frozen=True)
class CodexWorkSwitchFailed:
    account: str
    cause: str

    def log_line(self) -> str:
        return f"switch to claude for {self.account} failed: {self.cause}"


CodexWorkSwitch = CodexWorkMovedToClaude | NoWorkOnCodex | CodexWorkSwitchFailed


def _complete_pending_claude_switch(state: State, pending: PendingClaudeSwitch) -> CodexWorkSwitch:
    try:
        current = registry_assignments()
    except OSError as error:
        return CodexWorkSwitchFailed(pending.owner.label, str(error))
    moved = tuple(key for key in pending.assignment_keys if current.get(key) == "claude")
    return _record_claude_switch(state, pending, moved)


def _record_claude_switch(state: State, pending: PendingClaudeSwitch, moved: tuple[str, ...]) -> CodexWorkSwitch:
    if moved:
        previous = state["fallback"]
        owed_keys = previous.moved if not isinstance(previous, NoCodexQuotaFallback) else ()
        state["fallback"] = AccountCodexQuotaFallback(pending.owner, pending.at, tuple(dict.fromkeys((*moved, *owed_keys))),
            pending.hold, pending.claude_account)
        state["pending_routing"] = moves.RoutedToClaude(kind="routed to claude", at=pending.at,
            tool="codex", fallback_at=pending.at, owner=pending.owner.label, moved=list(moved),
            cause=pending.cause, why=pending.why)
        if isinstance(pending.claude_account, next_moves.ClaudeWorkersTarget):
            state["pending_routing"]["claude_account"] = pending.claude_account.label
        restoration = (f"and stay there until the source weekly reset at {local_time(pending.hold.until.isoformat())}."
                       if isinstance(pending.hold, ScheduledMoveHold) else "and move back when Codex recovers.")
        state.setdefault("unseen", []).append(
            f"{pending.description}: {', '.join(moved)} moved to Claude at {local_time(pending.at)}, " + restoration)
    del state["pending_switch"]
    _write_state(state)
    _ = _retry_pending_move_journal(state)
    return (CodexWorkMovedToClaude(pending.owner.label, moved) if moved
            else NoWorkOnCodex(pending.owner.label))


def switch_to_claude(state: State, note: AgentNote, stamp: str, owner: agent_accounts.KnownCodexAccount, *,
                     hold: CreditsOffHold | ScheduledMoveHold | NoFallbackHold = NO_FALLBACK_HOLD,
                     why: str = "Codex refused work for quota",
                     claude_account: next_moves.ClaudeWorkersTarget | next_moves.DefaultClaudeAccount = DEFAULT_CLAUDE_ACCOUNT) -> CodexWorkSwitch:
    """Move Codex assignments to Claude and report what the registry confirms."""
    if "pending_switch" in state:
        return _complete_pending_claude_switch(state, state["pending_switch"])
    episode = state["episodes"].get(note.label)
    account = f"{note.label} at {percent(note)}, resets {note.get('resets')}"
    cause: Literal["credits off", "refusal", "auto"] = (
        "auto" if isinstance(hold, ScheduledMoveHold) else
        "credits off" if isinstance(hold, CreditsOffHold) else "refusal")
    reason = why if cause == "auto" else "credits off" if cause == "credits off" else "Codex refused work for quota"
    trigger = reason
    moved: list[str] = []
    previous_route: role_map.WorkersClaudeAccount = role_map.DEFAULT_WORKERS_CLAUDE_ACCOUNT
    route_written = False
    confirmed = False
    try:
        if isinstance(claude_account, next_moves.ClaudeWorkersTarget):
            if not isinstance(hold, ScheduledMoveHold):
                raise ValueError("named Claude workers route needs a weekly reset")
            route = role_map.NamedWorkersClaudeAccount(claude_account.label, hold.until)
        else:
            route = role_map.DEFAULT_WORKERS_CLAUDE_ACCOUNT
        on_codex = [key for key, family in registry_assignments().items() if family == "codex"]
        if on_codex:
            previous_route = role_map.read_role_map().workers_claude
            role_map.write_role_map(role_map.WorkersClaudeRouteChange(route, reason, stamp))
            route_written = True
            state["pending_switch"] = PendingClaudeSwitch(owner, stamp, tuple(on_codex), hold,
                claude_account, cause, reason, f"{trigger} ({account})")
            _write_state(state)
        error = edit_registry("agents_set_all_assignments", "claude") if on_codex else None
        # The registry, not the exit status, says what moved: one mv writes the whole switch.
        moved = [key for key, family in registry_assignments().items() if key in on_codex and family == "claude"]
        confirmed = True
    except (OSError, ValueError) as failure:
        error = str(failure)
    if confirmed and not moved and route_written:
        try:
            role_map.write_role_map(role_map.WorkersClaudeRouteChange(previous_route, "Claude switch did not move work", stamp))
        except (OSError, ValueError) as failure:
            rollback_error = f"route rollback failed: {failure}"
            error = f"{error}; {rollback_error}" if error else rollback_error
    if not moved and error:
        if confirmed:
            _ = state.pop("pending_switch", None)
            _write_state(state)
        if episode is not None:
            episode["switch_failed"] = error
        notice = state.get("refusal_notices", {}).get(str(owner.home))
        if notice is not None and notice["kind"] == "known":
            notice["switch_failed"] = error
        state.setdefault("unseen", []).append(
            f"{trigger} ({account}), and the automatic switch to Claude failed at "
            + f"{local_time(stamp)}: {error}")
        return CodexWorkSwitchFailed(note.label, error)
    if not moved:
        _ = state.pop("pending_switch", None)
        return NoWorkOnCodex(note.label)
    if "pending_switch" in state:
        _ = _record_claude_switch(state, state["pending_switch"], tuple(moved))
    return CodexWorkMovedToClaude(note.label, tuple(moved))


def _complete_restoration_outcome(state: State) -> bool:
    if "restoration_owed" not in state:
        return True
    owed = state["restoration_owed"]
    try:
        moves.append_routing(owed.routing)
    except OSError as error:
        print(f"account moves: cannot record restored workers routing: {error}")
        return False
    identity = RestorationNoticeIdentity(fallback_at=owed.routing["fallback_at"], kind=owed.routing["kind"])
    if state.get("restoration_notice") != identity:
        state.setdefault("unseen", []).append(owed.notice)
        state["restoration_notice"] = identity
    del state["restoration_owed"]
    _write_state(state)
    return True


def switch_back(
    state: State, now: datetime, readings: CodexAlertPassReadings, *,
    journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL,
) -> CodexFallbackRestoration:
    """Restore owed assignments when the selected account can work on allowance or credits."""
    if not _complete_restoration_outcome(state):
        return NoCodexFallbackRestoration()
    if "pending_switch" in state:
        _ = _complete_pending_claude_switch(state, state["pending_switch"])
        if "pending_switch" in state:
            return NoCodexFallbackRestoration()
    if isinstance(readings.workers, NoWorkersHome):
        return NoCodexFallbackRestoration()
    entries = moves.read_moves() if isinstance(journal, moves.ReadCurrentMoveJournal) else journal
    _finish_workers_transitions(now, entries)
    _room_again(now, entries)
    fallback = state["fallback"]
    if isinstance(fallback, NoCodexQuotaFallback) and not state.get("no_worker_mode", False):
        return NoCodexFallbackRestoration()
    if (isinstance(fallback, AccountCodexQuotaFallback) and isinstance(fallback.hold, ScheduledMoveHold)
            and now < fallback.hold.until):
        return NoCodexFallbackRestoration()
    selected = readings.selected_account
    policy_fallback = isinstance(fallback, AccountCodexQuotaFallback) and isinstance(fallback.hold, CreditsOffHold)
    if (isinstance(fallback, AccountCodexQuotaFallback) and isinstance(fallback.hold, CreditsOffHold)
            and now < fallback.hold.until
            and isinstance(agent_accounts.codex_credits_setting(fallback.owner.label), agent_accounts.CreditsOff)
            and (not isinstance(selected, agent_accounts.KnownCodexAccount)
                 or selected.home == fallback.owner.home and selected.login.casefold() == fallback.owner.login.casefold())):
        owner_report = selected_codex_report(readings, now)
        if (owner_report.problem or owner_report.quota_problem or owner_report.weekly_remaining is None
                or not math.isfinite(owner_report.weekly_remaining) or owner_report.weekly_remaining <= 0):
            return NoCodexFallbackRestoration()

    selected_can_work = isinstance(codex_can_work(selected_codex_report(readings, now), now, agent_accounts.codex_credits_setting(readings.workers.label)), CodexWorkAvailable)
    if isinstance(selected, agent_accounts.KnownCodexAccount):
        selected_can_work = selected_can_work and not _refusal_blocks_return(readings, selected, now)
    if not selected_can_work:
        if workers_entry_pending():
            return NoCodexFallbackRestoration()
        from scripts.accounts import accounts

        chosen = (accounts.pick("codex", now, keep=accounts.KeepAccount(readings.workers.label), prefer_weekly_room=True)
                  if policy_fallback else accounts.pick("codex", now, keep=accounts.KeepAccount(readings.workers.label)))
        if isinstance(chosen, accounts.NoAccountWithRoom):
            return NoCodexFallbackRestoration()
        if chosen.dir != readings.workers.home:
            move_id = uuid.uuid4().hex
            recovery_request: moves.RequestedMove = {"kind": "requested", "at": now.isoformat(),
                "tool": "codex", "subject": "workers", "move_id": move_id, "from": readings.workers.label,
                "to": chosen.label, "why": "Codex has room again", "window": "unknown"}
            if policy_fallback:
                recovery_request["cause"] = "credits off"
            moves.append_move(recovery_request)
            try:
                _ = accounts.swap("workers", chosen.label, why=recovery_request["why"],
                                  cause=recovery_request.get("cause", "refusal"), worker_moves="detach",
                                  worker_move_journal=(*entries, recovery_request))
            except (accounts.AccountsError, OSError) as error:
                if not isinstance(error, accounts.WorkerMovesStartFailed):
                    moves.append_move(moves.FailedMove(kind="failed", at=now.isoformat(), tool="codex",
                        subject="workers", move_id=move_id, reason=str(error)))
                return NoCodexFallbackRestoration()
            readings = CodexAlertPassReadings(accounts.CodexWorkersAccount(chosen.label, chosen.dir),
                agent_accounts.codex_account(chosen.dir), readings.checked_homes)
    selected = readings.selected_account
    if isinstance(selected, agent_accounts.KnownCodexAccount) and _refusal_blocks_return(readings, selected, now):
        return NoCodexFallbackRestoration()
    if isinstance(readings.workers, NoWorkersHome):
        return NoCodexFallbackRestoration()
    if not isinstance(
            codex_can_work(selected_codex_report(readings, now), now, agent_accounts.codex_credits_setting(readings.workers.label)), CodexWorkAvailable):
        return NoCodexFallbackRestoration()
    if isinstance(fallback, NoCodexQuotaFallback):
        _ = state.pop("no_worker_mode", None)
        return NoCodexFallbackRestoration()
    restored_account = selected if isinstance(selected, agent_accounts.KnownCodexAccount) else NO_RESTORATION_ACCOUNT
    role_map.write_role_map(role_map.WorkersClaudeRouteChange(
        role_map.DEFAULT_WORKERS_CLAUDE_ACCOUNT, "Codex recovered", now.isoformat()))
    failed_keys: list[str] = []
    try:
        current = registry_assignments()
    except OSError as error:
        failed_keys = list(fallback.moved)
        back = SwitchBack([], [], [f"{key} ({error})" for key in fallback.moved], restored_account)
    else:
        back = SwitchBack([], [], [], restored_account)
        for key in fallback.moved:
            if current.get(key) != "claude":
                back.kept.append(key)
            elif error := edit_registry("agents_set_assignment", key, "codex"):
                failed_keys.append(key)
                back.failed.append(f"{key} ({error})")
            else:
                back.moved.append(key)
    state["fallback"] = replace(fallback, moved=tuple(failed_keys)) if failed_keys else NoCodexQuotaFallback()
    if not failed_keys:
        _ = state.pop("no_worker_mode", None)
    if not failed_keys and isinstance(fallback, AccountCodexQuotaFallback):
        _ = state.get("refusal_notices", {}).pop(str(fallback.owner.home), None)
    stamp = now.isoformat(timespec="seconds")
    owner = (fallback.owner.label if isinstance(fallback, AccountCodexQuotaFallback) and fallback.owner.label != "default"
             else moves.UnknownFallbackOwner())
    if back.failed:
        routing = moves.RoutingNotRestored(kind="routing not restored", at=stamp, tool="codex",
            fallback_at=fallback.at, owner=owner, restored=back.moved, kept=back.kept,
            failed=back.failed, outcome="partial" if back.moved else "failed")
    else:
        routing = moves.RoutedBack(kind="routed back", at=stamp, tool="codex",
            fallback_at=fallback.at, owner=owner, restored=back.moved, kept=back.kept)
    state["restoration_owed"] = RestorationOutcomeOwed(routing,
        f"Codex recovered at {local_time(stamp)}: {back.describe()}.")
    _write_state(state)
    _ = _complete_restoration_outcome(state)
    return back


def _readings_after_restoration(readings: CodexAlertPassReadings, back: CodexFallbackRestoration) -> CodexAlertPassReadings:
    from scripts.accounts.accounts import CodexWorkersAccount

    if isinstance(back, SwitchBack) and isinstance(back.restored_account, agent_accounts.KnownCodexAccount):
        account = back.restored_account
        return CodexAlertPassReadings(CodexWorkersAccount(agent_accounts.codex_home_role_label(account.home), account.home),
            account, readings.checked_homes)
    return readings


def claude_roles(note: AgentNote, selection: ClaudeAccountsInUse) -> tuple[str, ...]:
    """Roles come from the account configuration written on the note."""
    del selection
    return note.get_list("role") if note.tool == "claude" else ()


def credit_line(note: AgentNote) -> str:
    """The credits Codex reports for the account, which carry work once its weekly allowance is used up."""
    name = note.label
    balance = note.get("credit_balance")
    if balance == "unlimited":
        return f"Credits on {name}: unlimited."
    try:
        return f"Credits left on {name}: {float(balance or ''):,.0f}."
    except ValueError:
        return f"No credit balance is reported for {name}."


def _claude_move_groups(roles: tuple[str, ...]) -> set[str]:
    aliases = {"unit": "units", "unit directors": "units", "units": "units",
               "showrunner": "showrunners", "showrunners": "showrunners"}
    return {aliases[role] for role in roles if role in aliases}


def _claude_moves_cover(account: str, groups: set[str]) -> bool:
    if not groups:
        return False
    entries = next_moves.read_next_moves()
    history = role_map.recorded_auto_moves(role_map.groups_file().with_name("roles.jsonl"))
    if not isinstance(history, role_map.RecordedAutoMoves):
        return False
    heads: dict[str, next_moves.ClaudeGroupMove] = {}
    for entry in entries:
        if isinstance(entry, next_moves.ClaudeGroupMove) and entry.id not in history.entry_ids:
            _ = heads.setdefault(entry.group, entry)
    return groups <= {group for group, head in heads.items() if head.to != account}


def _claude_quota_sender() -> bool:
    return socket.gethostname().split(".", 1)[0] == role_map.ROLE_MAP_HOST


def claude_next_move_push(note: AgentNote, selection: ClaudeAccountsInUse) -> QuotaNotice:
    """A phone reminder only when an in-use account has an uncovered group."""
    from scripts.notify.push_texts import render

    if not _claude_quota_sender():
        return QuotaStatusNote(note.label, "only natedev sends Claude quota pushes")
    groups = _claude_move_groups(claude_roles(note, selection))
    if not groups:
        return QuotaStatusNote(note.label, "no Claude group uses this account")
    try:
        if _claude_moves_cover(note.label, groups):
            return QuotaStatusNote(note.label, "what happens next is set up")
    except next_moves.NextMoveError as error:
        return QuotaStatusNote(note.label, str(error))
    return UserPush(note.label, render("quota.claude_next_move", account=note.label, remaining=percent(note)))


def codex_next_move_push(note: AgentNote, fallback: CodexQuotaFallback = NO_CODEX_QUOTA_FALLBACK,
                         now: datetime | None = None, *,
                         history: moves.RecordedAutoEntries | moves.UnreadableMoveJournal | moves.ReadCurrentMoveJournal
                         = moves.READ_CURRENT_MOVE_JOURNAL) -> QuotaNotice:
    from scripts.notify.push_texts import render
    from scripts.accounts import accounts

    left = note_file.remaining(note)
    if left is None or not 0 < left <= 2:
        return QuotaStatusNote(note.label, "no Codex account-move reminder outside 0–2% weekly usage left")
    at = now or datetime.now(timezone.utc)
    if (isinstance(fallback, AccountCodexQuotaFallback) and isinstance(fallback.hold, ScheduledMoveHold)
            and at < fallback.hold.until):
        return QuotaStatusNote(note.label, "scheduled move to Claude is held until the weekly reset")
    if isinstance(history, moves.ReadCurrentMoveJournal):
        history = moves.recorded_auto_entries()
    try:
        queued = [entry for entry in next_moves.read_next_moves() if isinstance(entry, next_moves.WorkersMove)
                  and (not isinstance(history, moves.RecordedAutoEntries) or entry.id not in history.entry_ids)]
    except next_moves.NextMoveError as error:
        return QuotaStatusNote(note.label, str(error))
    if queued:
        if next_moves.target_label(queued[0]) == note.label:
            return QuotaStatusNote(note.label, "the queued next entry targets the current account")
        return QuotaStatusNote(note.label, "what happens next is set up")
    credits_allowed = isinstance(agent_accounts.codex_credits_setting(note.label), agent_accounts.CreditsOn)
    chosen = _zero_destination(note.label, at, prefer_weekly_room=True, included_only=credits_allowed)
    destination = chosen.label if isinstance(chosen, accounts.PickedAccount) else "Claude"
    outcome_title = "use paid credits" if credits_allowed and isinstance(chosen, accounts.NoAccountWithRoom) else f"move to {destination}"
    if credits_allowed and isinstance(chosen, accounts.NoAccountWithRoom):
        outcome_body = (", and no move is set up for its workers. When it runs out, "
                        + f"they spend its paid credits, then move to {destination}.")
    elif isinstance(chosen, accounts.NoAccountWithRoom):
        outcome_body = (". When it runs out, its workers move on their own to Claude, "
                        + "since no other Codex account has room.")
    else:
        outcome_body = (f". When it runs out, its workers move on their own to {destination}, "
                        + "the Codex account with room whose weekly reset comes first.")
    return UserPush(note.label, render("quota.codex_next_move", account=note.label,
                                      remaining=percent(note), fallback=destination,
                                      outcome_title=outcome_title, outcome_body=outcome_body))


@dataclass(frozen=True)
class WorkersNextMovePass:
    readings: CodexAlertPassReadings
    history: moves.RecordedAutoEntries | moves.UnreadableMoveJournal
    journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL


def run_workers_next_moves(readings: CodexAlertPassReadings, now: datetime) -> CodexAlertPassReadings:
    return _workers_next_move_pass(readings, now).readings


def _retry_pending_move_journal(state: State) -> bool:
    """Keep durable retries until the journal can prove the routing or refusal."""
    try:
        if "pending_routing" in state:
            moves.append_routing(state["pending_routing"])
            del state["pending_routing"]
        failures = state.get("pending_move_failures", [])
        while failures:
            moves.append_move(failures[0])
            del failures[0]
    except OSError as error:
        print(f"account moves: cannot record pending workers move: {error}")
        return False
    return True


@dataclass(frozen=True)
class NoTargetReading:
    """The target has no stored weekly reading."""


@dataclass(frozen=True)
class TargetReadingUnavailable:
    last_read: datetime | NoTargetReading


@dataclass(frozen=True)
class TargetHeldByShutdown:
    reason: str


@dataclass(frozen=True)
class TargetLoginRenewalFailed:
    detail: str


@dataclass(frozen=True)
class TargetAccountUnavailable:
    reason: str


@dataclass(frozen=True)
class TargetAtOrBelowThreshold:
    left: float
    threshold: float


@dataclass(frozen=True)
class TargetUnresolvedRefusal:
    """The destination still holds a provider refusal."""


@dataclass(frozen=True)
class TargetPreparationRefused:
    reason: str


@dataclass(frozen=True)
class TargetIsCurrentAccount:
    """The head targets the account already selected for this group."""


BlockedHeadReason = (TargetReadingUnavailable | TargetAccountUnavailable | TargetAtOrBelowThreshold
                     | TargetUnresolvedRefusal | TargetPreparationRefused | TargetIsCurrentAccount
                     | TargetLoginRenewalFailed | TargetHeldByShutdown)


def blocked_next_move_reason_key(reason: BlockedHeadReason) -> str:
    """Changing low percentages describe the same blocker until its threshold changes."""
    stable = ((type(reason).__name__, reason.threshold) if isinstance(reason, TargetAtOrBelowThreshold)
              else reason)
    return hashlib.sha256(repr(stable).encode()).hexdigest()


def blocked_next_move_line(group: str, target: str, _entry_id: str, reason: BlockedHeadReason) -> str:
    if isinstance(reason, TargetHeldByShutdown):
        return f"{group} 1 → {target} due, {reason.reason}"
    if isinstance(reason, TargetLoginRenewalFailed):
        return (f"{group} 1 → {target} due, {target}'s login expired, and a probe session on it failed "
                f"({reason.detail}). Log in on {target} by hand.")
    if isinstance(reason, TargetIsCurrentAccount):
        return f"{group} 1 → {target} due, {target} is already the current account"
    if isinstance(reason, TargetReadingUnavailable):
        detail = ("no reading" if isinstance(reason.last_read, NoTargetReading)
                  else f"no fresh reading since {reason.last_read.astimezone(user_zone()):%H:%M}")
    elif isinstance(reason, TargetAccountUnavailable):
        detail = reason.reason
    elif isinstance(reason, TargetAtOrBelowThreshold):
        detail = f"{reason.left:g}% left, at or below {reason.threshold:g}%"
    elif isinstance(reason, TargetUnresolvedRefusal):
        detail = "an unresolved refusal"
    else:
        detail = f"preparation refused: {reason.reason}"
    return f"{group} 1 → {target} due, {target} has {detail}"


def notify_blocked_next_move(group: str, target: str, entry_id: str, reason: BlockedHeadReason,
                             now: datetime) -> bool:
    config = load_config()
    text = blocked_next_move_line(group, target, entry_id, reason)
    return deliver_notice([Job("", recipient, text, "account move blocked")
        for recipient in recipients(config)], now.isoformat())


def workers_entry_pending() -> bool:
    """Read the queued workers entries for this pass."""
    try:
        return any(isinstance(entry, next_moves.WorkersMove) for entry in next_moves.read_next_moves())
    except (OSError, next_moves.NextMoveError) as error:
        print(f"account moves: anomaly: cannot read workers entries: {error}")
        return False


def _workers_head_blocked(entry: next_moves.WorkersMove, reason: BlockedHeadReason,
                          now: datetime) -> None:
    fingerprint = blocked_next_move_reason_key(reason)
    with state_file() as state:
        notices = state.setdefault("workers_move_notices", {})
        if notices.get(entry.id) == fingerprint:
            return
        _ = notices.pop(entry.id, None)
    target = next_moves.target_label(entry)
    print(blocked_next_move_line("workers", target, entry.id, reason))
    if notify_blocked_next_move("workers", target, entry.id, reason, now):
        with state_file() as state:
            state.setdefault("workers_move_notices", {})[entry.id] = fingerprint


def _workers_request_replay(history: moves.RecordedAutoEntries, rows: Sequence[moves.MoveEntry]
                            ) -> tuple[moves.RecordedAutoEntries, dict[str, moves.RequestedMove]]:
    """A request without a result stays due, also when its role write is done: the move was cut."""
    pending: dict[str, moves.RequestedMove] = {}
    completed: set[str] = set()
    for index, row in enumerate(rows):
        if (row["kind"] != "requested" or row["tool"] != "codex" or row["subject"] != "workers"
                or row.get("cause") != "auto" or not row["why"].startswith("auto: ")):
            continue
        entry_id = row["why"].removeprefix("auto: ")
        later_rows = [later for later in rows[index + 1:] if later["move_id"] == row["move_id"]]
        if later_rows:
            if any(later["kind"] == "confirmed" or later["kind"] == "failed"
                   and later["reason"].startswith("superseded by ") for later in later_rows):
                completed.add(entry_id)
        else:
            pending[entry_id] = row
    return (moves.RecordedAutoEntries((history.entry_ids | completed) - (pending.keys() - completed)),
            {key: request for key, request in pending.items() if key not in completed})


def _start_workers_request(entry: next_moves.WorkersMove, request: moves.RequestedMove,
                           label: str, now: datetime, journal: Sequence[moves.MoveEntry]) -> bool | None:
    """Write the role and start detached moves; keep the entry until the moves end.

    False: the swap was refused and the entry stays. None: the refusal is not in the journal yet.
    """
    from scripts.accounts import accounts

    try:
        _ = accounts.swap("workers", label, why=request["why"], cause="auto", worker_moves="detach",
                          worker_move_journal=journal)
    except accounts.WorkerMovesStartFailed as error:
        _workers_head_blocked(entry, TargetPreparationRefused(str(error)), now)
        return False
    except (accounts.AccountsError, OSError) as error:
        failure = moves.FailedMove(kind="failed", at=now.isoformat(timespec="seconds"), tool="codex",
            subject="workers", move_id=request["move_id"], reason=str(error))
        # Save the refusal before attempting the journal append. A retry
        # must record it before requested rows can consume log entries.
        with state_file() as state:
            state.setdefault("pending_move_failures", []).append(failure)
        with state_file() as state:
            journal_ready = _retry_pending_move_journal(state)
        _workers_head_blocked(entry, TargetPreparationRefused(str(error)), now)
        return False if journal_ready else None
    return True


def _workers_next_move_pass(readings: CodexAlertPassReadings, now: datetime) -> WorkersNextMovePass:
    """Run at most one workers entry, locking the log before fallback state."""
    from scripts.accounts import accounts

    history: moves.RecordedAutoEntries | moves.UnreadableMoveJournal = moves.RecordedAutoEntries(frozenset())
    journal: list[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL
    if not role_map.is_role_map_host():
        evidence = moves.read_replay_evidence()
        if isinstance(evidence, moves.UnreadableMoveJournal):
            return WorkersNextMovePass(readings, evidence)
        return WorkersNextMovePass(readings, evidence.history, evidence.entries)
    try:
        with next_moves.next_moves_lock():
            with state_file() as state:
                if "pending_switch" in state:
                    _ = _complete_pending_claude_switch(state, state["pending_switch"])
                    if "pending_switch" in state:
                        return WorkersNextMovePass(readings, moves.UnreadableMoveJournal())
                journal_ready = _retry_pending_move_journal(state)
            if not journal_ready:
                return WorkersNextMovePass(readings, moves.UnreadableMoveJournal())
            evidence = moves.read_replay_evidence()
            if isinstance(evidence, moves.UnreadableMoveJournal):
                return WorkersNextMovePass(readings, evidence)
            history = evidence.history
            journal = list(evidence.entries)
            history, pending_requests = _workers_request_replay(history, journal)
            entries = next_moves.read_next_moves()
            workers = [entry for entry in entries if isinstance(entry, next_moves.WorkersMove)]
            try:
                accounts.resume_worker_moves(journal)
            except accounts.WorkerMovesStartFailed as error:
                if workers:
                    _workers_head_blocked(workers[0], TargetPreparationRefused(str(error)), now)
                else:
                    print(f"account moves: workers pass failed: {error}")
                return WorkersNextMovePass(readings, history, journal)
            with state_file() as state:
                notices = state.get("workers_move_notices", {})
                for entry_id in list(notices):
                    if not workers or entry_id != workers[0].id:
                        del notices[entry_id]
            if not workers:
                return WorkersNextMovePass(readings, history, journal)
            head = workers[0]
            kept = tuple(entry for entry in entries if entry.id not in history.entry_ids)
            if kept != entries:
                next_moves.write_next_moves(kept)
            if head.id in history.entry_ids:
                with state_file() as state:
                    _ = state.get("workers_move_notices", {}).pop(head.id, None)
                return WorkersNextMovePass(readings, history, journal)
            cut = pending_requests.get(head.id)
            if cut is not None and role_map.read_role_map().workers == cut["to"]:
                return WorkersNextMovePass(readings, history, journal)
            with state_file() as state:
                fallback = state["fallback"]
                if (isinstance(fallback, AccountCodexQuotaFallback)
                        and isinstance(fallback.hold, ScheduledMoveHold) and now < fallback.hold.until):
                    return WorkersNextMovePass(readings, history, journal)
            owner = readings.selected_account
            report = selected_codex_report(readings, now)
            if (not isinstance(owner, agent_accounts.KnownCodexAccount)
                    or isinstance(readings.workers, NoWorkersHome)
                    or report.problem or report.quota_problem or report.weekly_remaining is None
                    or not math.isfinite(report.weekly_remaining)):
                return WorkersNextMovePass(readings, history, journal)
            threshold = head.at_percent
            held_refusals = refusals.load()
            for entry in (head,):
                if report.weekly_remaining > entry.at_percent:
                    continue
                if next_moves.target_label(entry) == owner.label:
                    _workers_head_blocked(entry, TargetIsCurrentAccount(), now)
                    continue
                stamp = now.isoformat(timespec="seconds")
                why = f"auto: {entry.id}"
                if isinstance(entry.to, next_moves.WorkersToClaude):
                    if isinstance(entry.to.account, next_moves.ClaudeWorkersTarget):
                        try:
                            _ = accounts.claude_workers_account(entry.to.account.label)
                        except (accounts.AccountsError, OSError) as error:
                            _workers_head_blocked(entry, TargetAccountUnavailable(str(error)), now)
                            continue
                    reset = report.weekly_reset
                    if reset is None or reset.tzinfo is None or reset <= now:
                        continue
                    note = CodexQuotaAccountNote(note_file.note_path(owner.label),
                        {"weekly_remaining_usage": str(report.weekly_remaining), "resets": reset.isoformat()}, owner.label)
                    with state_file() as state:
                        fallback = state["fallback"]
                        if (isinstance(fallback, AccountCodexQuotaFallback)
                                and isinstance(fallback.hold, ScheduledMoveHold) and now < fallback.hold.until):
                            continue
                        result = switch_to_claude(state, note, stamp, owner, hold=ScheduledMoveHold(reset), why=why,
                                                  claude_account=entry.to.account)
                        journal_ready = "pending_routing" not in state
                    if not journal_ready:
                        return WorkersNextMovePass(readings, moves.UnreadableMoveJournal(), journal)
                    if isinstance(result, CodexWorkSwitchFailed):
                        _workers_head_blocked(entry, TargetPreparationRefused(result.cause), now)
                    if not isinstance(result, CodexWorkMovedToClaude):
                        continue
                else:
                    try:
                        target = accounts.check_codex_target(entry.to.label, readings.workers.home, repair=False)
                    except (accounts.AccountsError, OSError) as error:
                        _workers_head_blocked(entry, TargetAccountUnavailable(str(error)), now)
                        continue
                    identity = agent_accounts.KnownCodexAccount(target.home, target.login, target.label)
                    target_readings = CodexAlertPassReadings(accounts.CodexWorkersAccount(target.label, target.home),
                                                           identity, readings.checked_homes)
                    target_report = selected_codex_report(target_readings, now)
                    left = target_report.weekly_remaining
                    if (target_report.problem or target_report.quota_problem or left is None
                            or not math.isfinite(left) or target_report.weekly_reset is None
                            or target_report.weekly_reset <= now):
                        stored = _reading_for_account(readings, identity, now)
                        last_read = (stored.weekly.at if isinstance(stored, (refusals.FreshReading, refusals.StaleReading))
                                     and isinstance(stored.weekly, refusals.ReadingWindow) else NoTargetReading())
                        _workers_head_blocked(entry, TargetReadingUnavailable(last_read), now)
                        continue
                    if left <= threshold:
                        _workers_head_blocked(entry, TargetAtOrBelowThreshold(left, threshold), now)
                        continue
                    refusal = held_refusals.get(refusals.key("codex", target.login))
                    if refusal is not None and not refusals.resolved(refusal, _reading_for_account(readings, identity, now), now):
                        _workers_head_blocked(entry, TargetUnresolvedRefusal(), now)
                        continue
                    if entry.id in pending_requests:
                        request = pending_requests[entry.id]
                    else:
                        move_id = uuid.uuid4().hex
                        request = moves.RequestedMove(kind="requested", at=stamp, tool="codex",
                            subject="workers", move_id=move_id, cause="auto", why=why,
                            **{"from": owner.label, "to": target.label}, window="weekly")
                        moves.append_move(request)
                        journal.append(request)
                    finished = _start_workers_request(entry, request, target.label, now, journal)
                    if finished is None:
                        return WorkersNextMovePass(readings, moves.UnreadableMoveJournal(), journal)
                    if not finished:
                        continue
                    return WorkersNextMovePass(target_readings, history, journal)
                next_moves.write_next_moves(tuple(saved for saved in kept if saved.id != entry.id))
                with state_file() as state:
                    _ = state.get("workers_move_notices", {}).pop(entry.id, None)
                return WorkersNextMovePass(readings, history, journal)
    except (OSError, next_moves.NextMoveError, accounts.AccountsError) as error:
        print(f"account moves: workers pass failed: {error}")
    return WorkersNextMovePass(readings, history, journal)


def refusal_push(note: AgentNote, switch: CodexQuotaFallback, failed: str | None, already: bool) -> QuotaNotice:
    """The one direct user message sent when Codex refuses this episode."""
    from scripts.notify.push_texts import render

    if not isinstance(switch, NoCodexQuotaFallback):
        verb = "had already moved" if already else "moved"
        outcome = f"New work {verb} to Claude, the other coding assistant"
    elif failed:
        outcome = "New work could not move to Claude, the other coding assistant"
    else:
        outcome = "No delegated work needed to move to Claude, the other coding assistant"
    name = note.label
    credits = credit_line(note).replace(f" on {name}", "")
    if not isinstance(switch, NoCodexQuotaFallback) or not failed:
        return QuotaStatusNote(name, f"Codex refused work on {name}. {credits} {outcome}.")
    return UserPush(name, render("quota.codex_refused", account=name))


def relay(recipient: str, text: str, key: str | None) -> str | None:
    """Deliver text to one session; None when it arrived, otherwise why not."""
    timeout = conf.read("accounts").whole("quota_relay_timeout_seconds") - KILL_GRACE
    command = session_send.send_argv("--to", recipient, "--from", "quota_alert",
                                     "--timeout", str(timeout), *(["--key", key] if key else []))
    try:
        done = subprocess.run(command, input=text, capture_output=True, text=True, timeout=send_budget.seconds(timeout),
                              check=False)
    except (subprocess.TimeoutExpired, OSError) as error:
        return str(error)
    outcome = send_result.send_outcome(done.returncode, done.stdout)
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        match outcome.header.outcome:
            case "sent" | "skipped" | "replaced" | "kept":
                return None
            case "queued":
                # refusal_watch accepts a queued copy through this header.
                return " ".join(f"QUEUED: {outcome.header.detail}".split())
            case "retry":
                return " ".join(f"RETRY: {outcome.header.detail}".split())
            case "failed" | "refused" | "held" | "pending" | "closed" | "absent":
                pass
    return " ".join(failure_detail(done).split())


@dataclass(frozen=True)
class PushSent:
    """The user delivery completed successfully."""


@dataclass(frozen=True)
class PushFailed:
    why: str


@dataclass(frozen=True)
class PushUnconfirmed:
    why: str


PushDelivery = PushSent | PushFailed | PushUnconfirmed


def _push_outcome(delivery: PushDelivery) -> str:
    if isinstance(delivery, PushSent):
        return "sent"
    if isinstance(delivery, PushUnconfirmed):
        return f"delivery unconfirmed: {delivery.why}"
    return f"not delivered: {delivery.why}"


def tell_user(push: RenderedPush) -> PushDelivery:
    """Convert the sender process result into the user delivery state."""
    from scripts.notify.push_texts import user_send_argv

    expiry = push.id == "quota.claude_expiry"
    needs_confirmation = expiry or push.id in token_expiry.ALERT_IDS
    user_send_timeout = review.user_send_seconds()
    command = user_send_argv(push, timeout=user_send_timeout)
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=send_budget.user_seconds(user_send_timeout),
                              check=False)
    except subprocess.TimeoutExpired as error:
        return PushUnconfirmed(str(error)) if needs_confirmation else PushFailed(str(error))
    except OSError as error:
        return PushUnconfirmed(str(error)) if expiry else PushFailed(str(error))
    outcome = send_result.send_outcome(done.returncode, done.stdout)
    why = " ".join(failure_detail(done).split())
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        match outcome.header.outcome:
            case "sent":
                return PushSent()
            case "refused" | "held":
                return PushFailed(why)
            case "skipped" | "replaced":
                return PushFailed(why) if needs_confirmation else PushSent()
            case "kept":
                if not needs_confirmation:
                    return PushSent()
            case "retry":
                if not expiry:
                    return PushFailed(" ".join(f"RETRY: {outcome.header.detail}".split()))
            case "queued" | "failed" | "pending" | "closed" | "absent":
                pass
    return PushUnconfirmed(why) if needs_confirmation else PushFailed(why)


def deliver(jobs: list[Job]) -> list[str | None]:
    """Relay every job in parallel; one error or None per job, in order.

    A job's key keeps one queued copy per alert for a recipient that is not running.
    """
    if not jobs:
        return []
    keys = [f"quota {job.kind} {job.name}" if job.name else None for job in jobs]
    with ThreadPoolExecutor() as pool:
        return list(pool.map(relay, [job.recipient for job in jobs], [job.text for job in jobs], keys))


def deliver_and_record(jobs: list[Job], stamp: str) -> list[str]:
    """Stamp each alert's attempt on its episode, then deliver every job; one log line per job.

    The stamp comes first: the snapshot timer kills a run that outlasts its budget, and a kill
    after the sends but before a later stamp sent every alert again on the next run. A kill now
    costs at most one repeat.
    """
    with state_file() as state:
        for job in jobs:
            if job.kind == "alert" and job.name in state["episodes"]:
                state["episodes"][job.name]["last"][job.recipient] = stamp
    errors = deliver(jobs)
    return [f"quota {job.kind} {job.name} -> {job.recipient}: "
            + ("sent" if error is None else f"not delivered: {error}") for job, error in zip(jobs, errors)]


def deliver_notice(jobs: list[Job], stamp: str) -> bool:
    """Confirm every recipient's delivery before a caller suppresses future attempts."""
    outcomes = deliver_and_record(jobs, stamp)
    return bool(jobs) and outcomes == [f"quota {job.kind} {job.name} -> {job.recipient}: sent" for job in jobs]


def alert(notes: list[AgentNote], now: datetime | None = None, *,
          selection: ClaudeAccountsInUse | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
          codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """Send what is due, record it, and return one log line per attempt."""
    selection = _selection(selection)
    now = now or datetime.now(timezone.utc)
    codex_readings = resolve_codex_readings(codex_readings, now=now)
    _save_codex_readings(codex_readings, now)
    return _alert_for_saved_readings(notes, now, selection, codex_readings)


class QuotaNote(NamedTuple):
    name: str
    rendered: RenderedPush


def _zero_destination(label: str, now: datetime, *, prefer_weekly_room: bool = False,
                      included_only: bool = False) -> accounts.PickedAccount | accounts.NoAccountWithRoom:
    from scripts.accounts import accounts
    if prefer_weekly_room:
        chosen = accounts.pick("codex", now, exclude=frozenset({label}), keep=accounts.NoKeep(), prefer_weekly_room=True)
    else:
        chosen = accounts.pick("codex", now, exclude=frozenset({label}), keep=accounts.NoKeep())
    if included_only and isinstance(chosen, accounts.PickedAccount) and accounts.codex_destination_uses_credits(chosen, now):
        resets = [window.reset.at for reason in chosen.reasons for window in (reason.weekly, reason.five_hour)
                  if isinstance(window, accounts.ReadingWindow) and isinstance(window.reset, accounts.KnownReset)
                  and window.reset.at > now]
        return accounts.NoAccountWithRoom(accounts.EarliestReset(min(resets)) if resets else accounts.NoResetKnown(), chosen.reasons)
    return chosen


def _credits_destination(chosen: accounts.PickedAccount | accounts.NoAccountWithRoom, now: datetime) -> str:
    from scripts.accounts import accounts
    if isinstance(chosen, accounts.PickedAccount):
        return (f"When credits run out, workers move to {chosen.label} ({chosen.weekly_left:g}% weekly usage left)."
                if not accounts.codex_destination_uses_credits(chosen, now) else
                f"When credits run out, workers move to {chosen.label} and spend its own paid credits.")
    return f"When they run out, Codex work moves to Claude, the other coding assistant, until {reset_display(chosen.earliest_reset)}."


def _credits_run_out(balance: float, usage: run_out.CreditsUsed, now: datetime) -> datetime:
    seconds = (usage.observed_to - usage.observed_from).total_seconds()
    duration = balance / usage.used * seconds
    latest = datetime.max.replace(tzinfo=timezone.utc)
    until = now + timedelta(seconds=min(duration, (latest - now).total_seconds() - 86400))
    return until.astimezone(user_zone())


def _worker_move_failure_cause(cause: str) -> str:
    """Describe a swap failure without exposing the accounts command's diagnostics."""
    text = cause.lower()
    if "reading" in text or "cannot read" in text:
        return "its usage reading is unavailable"
    if "sign-in" in text or "sign in" in text:
        return "its sign-in is unavailable"
    if "used up" in text:
        return "its usage is exhausted"
    if "server" in text and "stop" in text:
        return "its server could not stop"
    if "permission" in text:
        return "access to its worker settings was denied"
    return "its worker account could not be prepared"


def _work_routing_failure_cause(cause: str) -> str:
    if "permission" in cause.lower():
        return "permission to update the work-routing settings was denied"
    if "longer than" in cause.lower() or "timeout" in cause.lower():
        return "the work-routing update timed out"
    return "the work-routing settings could not be updated"


@dataclass(frozen=True)
class WorkersMovedWithoutNotice:
    """The workers moved to room before they used paid credits."""


def _zero_open_notice(label: str, report: Report, credits: agent_accounts.CodexCreditsSetting,
                      now: datetime, *, state: State, owner: agent_accounts.KnownCodexAccount,
                      workers_queued: bool) -> ZeroEpisodeNotice | WorkersMovedWithoutNotice:
    from scripts.accounts import accounts
    reset = report.weekly_reset
    assert reset is not None
    values = {"account": label, "reset": reset_display(refusals.KnownReset(reset))}
    if isinstance(credits, agent_accounts.CreditsOff) and workers_queued:
        return ZeroEpisodeNotice(id="quota.codex_workers_stay", values={**values,
            "outcome": "workers wait for their next entry", "reason": "A workers next entry is queued.",
            "fallback": "The queued head controls the next move; fallback moves wait."})
    chosen = _zero_destination(label, now, prefer_weekly_room=True,
                               included_only=isinstance(credits, agent_accounts.CreditsOn))
    if isinstance(credits, agent_accounts.CreditsOn) and isinstance(chosen, accounts.PickedAccount):
        chosen = _zero_destination(label, now, prefer_weekly_room=True, included_only=True)
    stamp = now.isoformat(timespec="seconds")
    if (isinstance(chosen, accounts.PickedAccount) and not workers_queued
            and isinstance(state["fallback"], NoCodexQuotaFallback)):
        move_id = uuid.uuid4().hex
        request: moves.RequestedMove = {"kind": "requested", "at": stamp, "tool": "codex",
            "subject": "workers", "move_id": move_id, "from": label, "to": chosen.label,
            "why": f"{label} weekly usage used; " + ("credits off" if isinstance(credits, agent_accounts.CreditsOff) else "move to room"), "window": "weekly", "reset": reset.isoformat()}
        request["cause"] = "credits off" if isinstance(credits, agent_accounts.CreditsOff) else "auto"
        moves.append_move(request)
        try:
            if isinstance(credits, agent_accounts.CreditsOn):
                _ = accounts.swap("workers", chosen.label, why=request["why"], cause=request.get("cause", "refusal"),
                                  included_only=True, worker_moves="detach")
            else:
                _ = accounts.swap("workers", chosen.label, why=request["why"], cause=request.get("cause", "refusal"),
                                  worker_moves="detach")
        except (accounts.AccountsError, OSError) as error:
            if not isinstance(error, accounts.WorkerMovesStartFailed):
                moves.append_move(moves.FailedMove(kind="failed", at=stamp, tool="codex", subject="workers",
                    move_id=move_id, reason=str(error)))
            values["reason"] = f"Workers could not move to {chosen.label} because {_worker_move_failure_cause(str(error))}."
            if isinstance(credits, agent_accounts.CreditsOn):
                chosen = accounts.NoAccountWithRoom(accounts.NoResetKnown(), chosen.reasons)
        else:
            if isinstance(credits, agent_accounts.CreditsOn):
                return WorkersMovedWithoutNotice()
            values["target"] = chosen.label
            values["transition"] = (f"Codex work now uses {chosen.label}" if not accounts.codex_destination_uses_credits(chosen, now) else "Codex work now spends paid credits")
            values["funding"] = (f"{chosen.weekly_left:g}% weekly usage left"
                                 if not accounts.codex_destination_uses_credits(chosen, now) else "its own paid credits")
            return ZeroEpisodeNotice(id="quota.codex_workers_moved", values=values)
    if isinstance(credits, agent_accounts.CreditsOn):
        return _paid_credits_notice(label, report, chosen, reset)
    if isinstance(chosen, accounts.NoAccountWithRoom):
        entries = moves.read_moves()
        closed = {entry["move_id"] for entry in entries if entry["kind"] == "room again"}
        if not any(entry["tool"] == "codex" and entry["subject"] == "workers" and entry["kind"] == "no room"
                   and entry["move_id"] not in closed for entry in entries):
            moves.append_move(moves.NoRoomMove(kind="no room", at=stamp, tool="codex", subject="workers",
                move_id=uuid.uuid4().hex, earliest_reset=chosen.earliest_reset.at.isoformat()
                if isinstance(chosen.earliest_reset, accounts.EarliestReset) else ""))
        values["reason"] = "No other Codex account has room."
    candidates = [reset]
    if isinstance(chosen, accounts.NoAccountWithRoom) and isinstance(chosen.earliest_reset, accounts.EarliestReset):
        candidates.append(chosen.earliest_reset.at)
    for reason in chosen.reasons:
        for window in (reason.weekly, reason.five_hour):
            if isinstance(window, accounts.ReadingWindow) and isinstance(window.reset, accounts.KnownReset) and window.reset.at > now:
                candidates.append(window.reset.at)
    until = min(candidates)
    values["until"] = reset_display(refusals.KnownReset(until))
    note = CodexQuotaAccountNote(note_file.note_path(label), {"weekly_remaining_usage": "0", "resets": reset.isoformat()}, label)
    switched = switch_to_claude(state, note, stamp, owner, hold=CreditsOffHold(until))
    _ = _start_no_worker_mode(state, codex_can_work(report, now, credits), chosen)
    if isinstance(switched, CodexWorkSwitchFailed):
        values["outcome"] = "work could not move"
        values["fallback"] = (f"The switch to Claude failed because {_work_routing_failure_cause(switched.cause)}. "
                              + f"Codex work stays on {label}.")
    else:
        values["outcome"] = "work moves to Claude"
        values["fallback"] = ("New coding work uses Claude, the other coding assistant, until the earliest Codex refill "
                              + f"at {values['until']}. Current turns finish first.")
    del values["until"]
    return ZeroEpisodeNotice(id="quota.codex_workers_stay", values=values)


def _paid_credits_notice(label: str, report: Report,
                         chosen: accounts.PickedAccount | accounts.NoAccountWithRoom,
                         reset: datetime) -> ZeroEpisodeNotice:
    from scripts.accounts import accounts

    next_reset = (min(reset, chosen.earliest_reset.at)
                  if isinstance(chosen, accounts.NoAccountWithRoom)
                  and isinstance(chosen.earliest_reset, accounts.EarliestReset) else reset)
    try:
        balance = float(report.credit_balance or "")
        credits_left = f"{balance:,.0f}" if math.isfinite(balance) and balance >= 0 else "unknown"
    except ValueError:
        credits_left = "unlimited" if report.credit_balance == "unlimited" else "unknown"
    return ZeroEpisodeNotice(id="quota.paid_credits", values={
        "sender": "quota_alert", "account": label, "machine": remote.local_machine(), "credits": credits_left,
        "refill": reset_display(refusals.KnownReset(reset)),
        "next_reset": reset_display(refusals.KnownReset(next_reset)),
    })


def _credits_forecast_notice(label: str, report: Report, now: datetime) -> ZeroEpisodeNotice:
    from scripts.whoami import run_out

    balance_text = report.credit_balance
    try:
        balance = float(balance_text or "")
    except ValueError:
        balance = math.nan
    destination = "" if balance_text == "unlimited" else _credits_destination(_zero_destination(label, now), now)
    horizon = "run-out unknown"
    if balance_text == "unlimited":
        horizon = "have no limit"
        forecast = f"Codex account {label} has unlimited paid credits; its coding workers remain on paid usage with no credit limit."
    elif not math.isfinite(balance) or balance < 0:
        forecast = f"Codex account {label} reports no credit balance; its coding workers remain on paid usage, but the run-out time is unknown."
    else:
        usage = run_out.credits_used(run_out.read_credit_readings(run_out.READINGS_LOG), label,
                                     since=now - timedelta(hours=1), until=now)
        if isinstance(usage, (run_out.NoCreditReading, run_out.UsageUnknown)):
            reason = usage.reason if isinstance(usage, run_out.UsageUnknown) else "no numeric credit observations were recorded"
            forecast = f"Coding workers on Codex account {label} remain on paid usage. Their hourly use is unknown because {reason}; {balance:,.0f} credits left."
        elif usage.used == 0:
            horizon = "use is zero"
            forecast = f"Workers on Codex account {label} used no credits in the last hour; {balance:,.0f} paid credits left."
        else:
            until = _credits_run_out(balance, usage, now)
            time = until.strftime("%a %Y-%m-%d %H:%M %Z")
            horizon = until.strftime("until %a %H:%M %Z")
            forecast = (f"Workers on Codex account {label} used {usage.used:,.0f} paid credits in the last hour; "
                        + f"its {balance:,.0f} credits last until about {time}.")
    return ZeroEpisodeNotice(id="quota.codex_credits_forecast", values={"account": label, "horizon": horizon, "forecast": forecast, "destination": destination})


def _current_notice(label: str, notice: ZeroEpisodeNotice, *, workers_queued: bool = False) -> ZeroEpisodeNotice:
    """Copy pending facts for rendering; never persist the queued advice overlay."""
    values = dict(notice["values"])
    if "file" in values:
        values["file"] = note_file.note_name(label)
    if workers_queued:
        if notice["id"] == "quota.paid_credits":
            return ZeroEpisodeNotice(id="quota.paid_credits_queued", values={key: values[key] for key in
                ("sender", "account", "machine", "credits", "refill")})
        if notice["id"] == "quota.codex_credits_forecast":
            values["destination"] = "When credits run out, workers wait for the account move you queued."
    return ZeroEpisodeNotice(id=notice["id"], values=values)


def _codex_zero_alert(config: Config, readings: CodexAlertPassReadings, now: datetime) -> list[str]:
    from scripts.notify.push_texts import render

    del config

    identity = readings.selected_account
    report = selected_codex_report(readings, now)
    stamp = now.isoformat(timespec="seconds")
    pushes: list[tuple[QuotaNote, Literal["phone", "forecast_phone"], str]] = []
    log: list[str] = []
    workers_queued = workers_entry_pending()
    with state_file() as state:
        fallback = state["fallback"]
        if (isinstance(fallback, AccountCodexQuotaFallback) and isinstance(fallback.hold, ScheduledMoveHold)
                and now < fallback.hold.until):
            return log
        episodes = state.setdefault("codex_zero", {})
        for label, episode in list(episodes.items()):
            if (datetime.fromisoformat(episode["weekly_reset"]) <= now
                    or isinstance(identity, agent_accounts.KnownCodexAccount) and (identity.label != label
                        or not report.problem and not report.quota_problem and report.weekly_remaining is not None
                        and report.weekly_remaining > 0)):
                del episodes[label]
        if not isinstance(identity, agent_accounts.KnownCodexAccount):
            return log
        label = identity.label
        at_zero = (not report.problem and not report.quota_problem and report.weekly_remaining == 0
                   and report.weekly_reset is not None and report.weekly_reset.tzinfo is not None and report.weekly_reset > now)
        if at_zero and label in state["episodes"]:
            state["episodes"][label]["reached_zero"] = True
        existing_episode = label in episodes
        if not existing_episode:
            if not at_zero:
                return log
            reset = report.weekly_reset
            assert reset is not None
            credits = agent_accounts.codex_credits_setting(label)
            notice = _zero_open_notice(label, report, credits, now, state=state, owner=identity,
                                      workers_queued=workers_queued)
            if isinstance(notice, WorkersMovedWithoutNotice):
                return log
            episodes[label] = CodexZeroEpisode(since=stamp, weekly_reset=reset.isoformat(),
                credits="off" if isinstance(credits, agent_accounts.CreditsOff) else "on",
                phone="due", forecast_at=stamp, notice=notice)
            if isinstance(credits, agent_accounts.CreditsOff) and workers_queued:
                episodes[label]["credits_off_fallback"] = "waiting_for_next_entry"
            current = _current_notice(label, notice, workers_queued=workers_queued)
            rendered = render(current["id"], **current["values"])
            log.append(f"quota zero {label}: {rendered.title}\n{rendered.body}")
        episode = episodes[label]
        setting = agent_accounts.codex_credits_setting(label)
        if (at_zero and existing_episode
                and isinstance(setting, agent_accounts.CreditsOn) and not workers_queued
                and isinstance(state["fallback"], NoCodexQuotaFallback)):
            reconsidered = _zero_open_notice(label, report, setting, now, state=state, owner=identity,
                                            workers_queued=False)
            if isinstance(reconsidered, WorkersMovedWithoutNotice):
                del episodes[label]
                return log
        if isinstance(setting, agent_accounts.CreditsOn) and episode["credits"] == "off":
            episode["credits"] = "on"
            _ = episode.pop("credits_off_fallback", None)
        if (at_zero and isinstance(setting, agent_accounts.CreditsOff)
                and (episode["credits"] == "on"
                     or episode.get("credits_off_fallback") == "waiting_for_next_entry" and not workers_queued)):
            changed = _zero_open_notice(label, report, setting, now, state=state, owner=identity,
                                       workers_queued=workers_queued)
            assert not isinstance(changed, WorkersMovedWithoutNotice)
            episode["credits"] = "off"
            if workers_queued:
                episode["credits_off_fallback"] = "waiting_for_next_entry"
            else:
                _ = episode.pop("credits_off_fallback", None)
            episode["forecast_at"] = stamp
            _ = episode.pop("forecast_phone", None)
            _ = episode.pop("forecast_notice", None)
            result = render(changed["id"], **changed["values"])
            log.append(f"quota credits off {label}: {result.body}")

        # Old opening notices used values that the current templates do not accept.
        if episode["notice"]["id"] not in ("quota.paid_credits", "quota.codex_workers_moved", "quota.codex_workers_stay"):
            episode["notice"] = _paid_credits_notice(label, report,
                _zero_destination(label, now, prefer_weekly_room=True, included_only=True),
                report.weekly_reset or datetime.fromisoformat(episode["weekly_reset"]))
        if episode["phone"] == "due":
            episode["notice"] = _current_notice(label, episode["notice"])
            notice = _current_notice(label, episode["notice"], workers_queued=workers_queued)
            pushes.append((QuotaNote(label, render(notice["id"], **notice["values"])), "phone", episode["since"]))
        if at_zero and episode["credits"] == "on" and now - datetime.fromisoformat(episode["forecast_at"]) >= timedelta(hours=1):
            notice = _credits_forecast_notice(label, report, now)
            current = _current_notice(label, notice, workers_queued=workers_queued)
            rendered = render(current["id"], **current["values"])
            log.append(f"quota credits forecast {label}: {rendered.title}\n{rendered.body}")
            episode["forecast_at"] = stamp
            if "forecast_phone" not in episode:
                episode["forecast_phone"] = "due"
                episode["forecast_notice"] = notice
        if episode.get("forecast_phone") == "due" and "forecast_notice" in episode:
            episode["forecast_notice"] = _current_notice(label, episode["forecast_notice"])
            notice = _current_notice(label, episode["forecast_notice"], workers_queued=workers_queued)
            pushes.append((QuotaNote(label, render(notice["id"], **notice["values"])), "forecast_phone", episode["since"]))
    for push, field, since in pushes:
        delivery = tell_user(push.rendered)
        with state_file() as state:
            episode = state.get("codex_zero", {}).get(push.name)
            if (isinstance(delivery, PushSent) and episode is not None
                    and episode["since"] == since and episode.get(field) == "due"):
                episode[field] = stamp
        log.append(f"quota {field} {push.name} -> user: {_push_outcome(delivery)}")
    return log


@dataclass(frozen=True)
class ClaudeExpirySchedule:
    lead: timedelta
    repeat: timedelta


class NoClaudeExpirySchedule(Enum):
    CONFIGURED = "no usable expiry settings"


def _claude_expiry_schedule() -> ClaudeExpirySchedule | NoClaudeExpirySchedule:
    settings = conf.read(CONFIG)
    keys = ("claude_expiry.alert_hours", "claude_expiry.repeat_hours")
    if isinstance(settings.settings, dict) and not any(settings.effective_key(key) in settings.settings for key in keys):
        return NoClaudeExpirySchedule.CONFIGURED
    try:
        lead, repeat = (settings.whole(key) for key in keys)
        if not (1 <= lead <= 168 and 1 <= repeat <= 168):
            raise ValueError("expiry hours must be between 1 and 168")
    except ValueError as error:
        print(f"quota-alert: Claude expiry settings unavailable: {error}", file=sys.stderr)
        return NoClaudeExpirySchedule.CONFIGURED
    return ClaudeExpirySchedule(timedelta(hours=lead), timedelta(hours=repeat))


WEEKLY_EMPTY_FORMAT = "%a %m-%d %H:%M %Z"


@dataclass(frozen=True)
class WeeklyForecast:
    used_per_hour: float
    empty_at: datetime | None
    lasts_to_deadline: bool


def weekly_forecast(readings: list[run_out.Reading], now: datetime,
                    reset: datetime | None, *, current: run_out.Reading) -> WeeklyForecast | None:
    """Estimate weekly use from readings in the current window."""
    if not now - run_out.MAX_GAP <= current.at <= now or not 0 <= current.used_percent < 100:
        return None
    latest = next((reading for reading in reversed(readings) if reading.at <= current.at), None)
    if latest is None or now - latest.at > run_out.MAX_GAP:
        return None
    start = reset - timedelta(weeks=1) if reset is not None else None
    if (reset is not None and reset <= now) or (start is not None and current.at < start):
        return None
    readings = [reading for reading in readings
                if (start is None or start <= reading.at) and reading.at < current.at] + [current]
    # A refill can start a new window before the scheduled reset.
    for end in range(len(readings) - 1, 0, -1):
        if readings[end].used_percent < readings[end - 1].used_percent:
            readings = readings[end:]
            break
    rate = run_out.weighted_rate(readings, current.at.timestamp())
    if rate is None or not math.isfinite(rate) or rate <= 0:
        return None
    seconds = (100 - current.used_percent) / rate
    empty_at = None
    lasts_to_deadline = reset is not None and seconds >= (reset - current.at).total_seconds()
    if reset is not None and not lasts_to_deadline and (now - current.at).total_seconds() < seconds:
        empty_at = current.at + timedelta(seconds=seconds)
    return WeeklyForecast(rate * 3600, empty_at, lasts_to_deadline)


def _expiry_current_reading(note: AgentNote, readings: list[run_out.Reading],
                            now: datetime, left: float) -> run_out.Reading | None:
    observed = reset_time(note.get("weekly_usage_checked_at"))
    if isinstance(observed, note_file.InvalidReset):
        return None
    cutoff = observed.value if isinstance(observed, note_file.KnownReset) else now
    latest = next((reading for reading in reversed(readings) if reading.at <= cutoff), None)
    # The log retains more precision than the note for the same observation.
    if (latest is not None and (isinstance(observed, note_file.NoResetKnown) or latest.at == cutoff)
            and math.isclose(left, 100 - latest.used_percent, rel_tol=5e-6)):
        return latest
    if isinstance(observed, note_file.KnownReset):
        return run_out.Reading(observed.value, 100 - left)
    return None


def _expiry_attempt_time() -> datetime:
    return datetime.now(timezone.utc)


def _send_claude_expiry(note: AgentNote, readings: list[run_out.Reading],
                        now: datetime, schedule: ClaudeExpirySchedule) -> list[str]:
    from scripts.notify.push_texts import render

    left, reset = note_file.remaining(note), reset_time(note.get("resets"))
    if (not note_file.in_use(note) or not isinstance(reset, note_file.KnownReset) or reset.value <= now
            or left is None or not 0 < left <= 100):
        return []
    current = _expiry_current_reading(note, readings, now, left)
    if current is None:
        return []
    forecast = weekly_forecast(readings, now, reset.value, current=current)
    if forecast is None or forecast.empty_at is None or forecast.empty_at >= now + schedule.lead:
        return []
    label = note.label
    try:
        if _claude_moves_cover(label, _claude_move_groups(note.get_list("role"))):
            return []
    except next_moves.NextMoveError:
        return []
    if silence.active():
        return []
    display = forecast.empty_at.astimezone(user_zone()).strftime(WEEKLY_EMPTY_FORMAT)
    push = render("quota.claude_expiry", account=label, empty_at=display)
    with state_file() as state:
        at = _expiry_attempt_time()
        attempts = state.setdefault("claude_expiry", {})
        previous = attempts.get(label)
        if previous is not None and reset_time(previous["weekly_reset"]) == reset:
            last = reset_time(previous["attempted_at"])
            if isinstance(last, note_file.KnownReset) and at < last.value + schedule.repeat:
                return []
        attempt = ClaudeExpiryAttempt(weekly_reset=reset.value.isoformat(), attempted_at=at.isoformat())
        attempts[label] = attempt
    delivery = tell_user(push)
    # For expiry pushes, PushFailed means the sender confirmed that it did not post.
    if isinstance(delivery, PushFailed):
        with state_file() as state:
            attempts = state.get("claude_expiry", {})
            if attempts.get(label) == attempt:
                del attempts[label]
    return [f"quota Claude expiry {label} -> user: {_push_outcome(delivery)}"]


def _token_retry_due(attempt: TokenDeliveryAttempt, now: datetime) -> bool:
    last = reset_time(attempt["attempted_at"])
    return not attempt["delivered"] and (not isinstance(last, note_file.KnownReset) or now >= last.value + timedelta(days=1))


def _deliver_token_attempt(push: RenderedPush, attempt: TokenDeliveryAttempt, *,
                           expiry_slot: str | None = None) -> list[str]:
    delivery = tell_user(push)
    if not isinstance(delivery, PushUnconfirmed):
        with state_file() as state:
            alerts = state.get("token_expiry_alerts", {})
            current = state.get("mac_token_login_claim") if expiry_slot is None else alerts.get(expiry_slot)
            if current is not None and current == attempt:
                if isinstance(delivery, PushSent):
                    current["delivered"] = True
                elif expiry_slot is None:
                    _ = state.pop("mac_token_login_claim", None)
                else:
                    del alerts[expiry_slot]
    return [f"{push.id} -> user: {_push_outcome(delivery)}"]


def _send_token_expiry(push: RenderedPush, slot: str, event: str, now: datetime) -> list[str]:
    with state_file() as state:
        alerts = state.setdefault("token_expiry_alerts", {})
        previous = alerts.get(slot)
        if previous is not None and previous["event"] == event and not _token_retry_due(previous, now):
            return []
        attempt = TokenExpiryAttempt(event=event, attempted_at=now.isoformat(), delivered=False)
        alerts[slot] = attempt
    return _deliver_token_attempt(push, attempt, expiry_slot=slot)


def _send_mac_token_login(now: datetime, zone: tzinfo) -> list[str]:
    from scripts.notify.push_texts import render

    with state_file() as state:
        token = mac_fallback_token.read()
        if not isinstance(token, mac_fallback_token.MacFallbackToken) or not token.recently_checked(now):
            return []
        failure = token.active_login_failure(now)
        if not isinstance(failure, datetime):
            return []
        previous = state.get("mac_token_login_claim")
        if previous is not None:
            claimed_made = reset_time(previous["made"])
            if isinstance(claimed_made, note_file.KnownReset) and claimed_made.value > token.made:
                return []
            if isinstance(claimed_made, note_file.KnownReset) and claimed_made.value == token.made:
                claimed_failure = reset_time(previous["failure"])
                if isinstance(claimed_failure, note_file.KnownReset):
                    if failure < claimed_failure.value:
                        return []
                    good = token.last_good_use
                    if not isinstance(good, datetime) or good <= claimed_failure.value:
                        if not _token_retry_due(previous, now):
                            return []
                        failure = claimed_failure.value
        claim = MacTokenLoginClaim(made=token.made.isoformat(), failure=failure.isoformat(),
                                  attempted_at=now.isoformat(), delivered=False)
        state["mac_token_login_claim"] = claim
    time = failure.astimezone(zone).strftime(WEEKLY_EMPTY_FORMAT)
    return _deliver_token_attempt(render("quota.mac_token_login", time=time), claim)


def token_alerts(now: datetime) -> list[str]:
    from scripts.notify.push_texts import render

    if not _claude_quota_sender() or silence.active():
        return []
    settings = conf.read(CONFIG)
    configured = token_expiry.configured_tokens(settings)
    tokens = [(token, token.ends.day.isoformat()) for token in configured
              if isinstance(token.ends, token_expiry.CalendarEnd)]
    missing = ", ".join(token.source for token in configured if isinstance(token.ends, token_expiry.EndUnavailable))
    log: list[str] = []
    zone = user_zone()
    mac = mac_fallback_token.read()
    if isinstance(mac, mac_fallback_token.MacFallbackToken) and mac.recently_checked(now):
        if isinstance(mac.active_login_failure(now), datetime):
            log += _send_mac_token_login(now, zone)
        tokens.insert(0, (token_expiry.mac_token(mac, mac_fallback_token.Unknown.VALUE), mac.made.isoformat()))
    if missing:
        log.append(f"quota-alert: anomaly: token end dates unavailable: {missing}")
    if not tokens:
        return log
    try:
        leads = token_expiry.expiry_days(settings)
    except (OSError, ValueError):
        return [*log, "quota-alert: token expiry days unavailable"]
    for token, event in tokens:
        lead = token.expiry_lead(now, zone, leads)
        end_date = token.end_date(zone)
        if lead is not None and end_date is not None:
            push = render(token_expiry.EXPIRY_ALERT_ID, token=token.name, date=end_date.isoformat())
            log += _send_token_expiry(push, f"{token.key}/{lead}", event, now)
    return log


def _alert_for_saved_readings(notes: list[AgentNote], now: datetime, selection: ClaudeAccountsInUse,
                              codex_readings: CodexAlertPassReadings) -> list[str]:
    stamp = now.isoformat(timespec="seconds")
    config = load_config()
    threshold = config["threshold_percent"]
    pushes: list[UserPush] = []
    workers_pass = _workers_next_move_pass(codex_readings, now)
    workers_history = workers_pass.history
    codex_readings = workers_pass.readings
    log = _codex_zero_alert(config, codex_readings, now)
    with state_file() as state:
        episodes = state["episodes"]
        _ = close_recovered(state, notes, threshold, selection, now, codex_readings)
        back = switch_back(state, now, codex_readings, journal=workers_pass.journal)
        codex_readings = _readings_after_restoration(codex_readings, back)
        for note in [*low([note for note in notes if note.tool != "codex"], threshold, now=now),
                     *_low_codex_notes(notes, threshold, codex_readings, now)]:
            name = note.label
            if name not in episodes:
                episodes[name] = {"since": stamp, "push": "due", "last": {}}
                if note.tool == "codex":
                    reset = selected_codex_report(codex_readings, now).weekly_reset
                    if reset is not None:
                        episodes[name]["weekly_reset"] = reset.isoformat()
                elif note.tool == "claude":
                    reset = reset_time(note.get("resets"))
                    if isinstance(reset, note_file.KnownReset):
                        episodes[name]["weekly_reset"] = reset.value.isoformat()
            episode = episodes[name]
            if note.tool == "codex" and note_file.remaining(note) == 0:
                episode["reached_zero"] = True
            if episode.get("push") == "due":
                push = (claude_next_move_push(note, selection) if note.tool == "claude"
                        else codex_next_move_push(note, state["fallback"], now, history=workers_history))
                if isinstance(push, UserPush):
                    pushes.append(push)
                else:
                    log.append(f"quota status {name}: {push.text}")
    if isinstance(back, SwitchBack):
        log.append(f"switch to claude undone: {back.describe()}")
    schedule = _claude_expiry_schedule() if _claude_quota_sender() else NoClaudeExpirySchedule.CONFIGURED
    with ThreadPoolExecutor() as pool:
        push_futures = [pool.submit(tell_user, push.rendered) for push in pushes]
        expiry_futures = []
        if isinstance(schedule, ClaudeExpirySchedule) and not silence.active():
            readings = run_out.read_readings(run_out.READINGS_LOG)
            expiry_futures = [pool.submit(_send_claude_expiry, note, readings.get(note.label, []), now, schedule)
                              for note in notes if note.tool == "claude"]
        push_deliveries = [future.result() for future in push_futures]
    if pushes:
        with state_file() as state:
            for push, delivery in zip(pushes, push_deliveries):
                episode = state["episodes"].get(push.name)
                if isinstance(delivery, PushSent) and episode is not None and episode.get("push") == "due":
                    episode["push"] = stamp
                outcome = "sent" if isinstance(delivery, PushSent) else f"not delivered: {delivery.why}"
                log.append(f"quota push {push.name} -> user: {outcome}")
    for future in expiry_futures:
        log.extend(future.result())
    log.extend(token_alerts(now))
    return log


@dataclass
class CodexQuotaAccountNote:
    path: Path
    fields: dict[str, str]
    label: str
    tool: str = "codex"

    def get(self, key: str) -> str | None:
        return self.fields.get(key)

    def get_list(self, key: str) -> tuple[str, ...]:
        del key
        return ()


def _low_codex_notes(notes: list[AgentNote], threshold: float,
                     readings: CodexAlertPassReadings, now: datetime) -> list[AgentNote]:
    identity = readings.selected_account
    report = selected_codex_report(readings, now)
    if not isinstance(identity, agent_accounts.KnownCodexAccount) or report.problem:
        return []
    left, reset = report.weekly_remaining, report.weekly_reset
    if left is None or not math.isfinite(left) or not 0 <= left <= threshold or reset is None or reset.tzinfo is None or reset <= now:
        return []
    return [note for note in notes if note.tool == "codex"
            and (note.get("login") or "").casefold() == identity.login.casefold()]


@dataclass(frozen=True)
class WorkersTransitionDelivery:
    text: str


@dataclass(frozen=True)
class RefusedAt:
    at: datetime


@dataclass(frozen=True)
class RefusalTimeUnknown:
    """Use the alert pass's observation time when no refusal time was handed in."""


REFUSAL_TIME_UNKNOWN = RefusalTimeUnknown()


@dataclass(frozen=True)
class FallbackRefusalDelivery:
    lines: list[str]
    quota_notice: QuotaNotice
    notice: KnownRefusalNotice
    failures_to_stamp: tuple[str, ...]


@dataclass(frozen=True)
class StoppedWorker:
    """A worker whose open pass the refusal stopped, and the director session to tell."""

    session_id: str
    worker: str


@dataclass(frozen=True)
class StoppedWorkerUnknown:
    """The refusal did not stop a worker with a known director."""


STOPPED_WORKER_UNKNOWN = StoppedWorkerUnknown()


@dataclass(frozen=True)
class WorkersMoved:
    account: str


@dataclass(frozen=True)
class WorkersWaitForReset:
    reset: str


@dataclass(frozen=True)
class NoWorkerModeStarted:
    """No account had room, so this host turned on no-worker mode."""


@dataclass(frozen=True)
class RefusalOutcomeUnknown:
    """The route did not decide where the workers go."""


RefusalOutcome = WorkersMoved | WorkersWaitForReset | NoWorkerModeStarted | RefusalOutcomeUnknown
REFUSAL_OUTCOME_UNKNOWN = RefusalOutcomeUnknown()
NO_WORKER_MODE_STARTED = NoWorkerModeStarted()


@dataclass(frozen=True)
class RoutedRefusal:
    lines: list[str]
    outcome: RefusalOutcome


def stopped_worker_relay(worker: str, outcome: RefusalOutcome) -> str:
    """The director's notice for a worker whose pass a quota refusal stopped."""
    if isinstance(outcome, NoWorkerModeStarted):
        return (f"Codex refused worker {worker} for quota. No-worker mode started. "
                + "The stop of the Codex workers ends its pass as canceled. Do not rerun its assign command.")
    happened = (f"Workers moved to {outcome.account}, and the move resumes the pass. "
                if isinstance(outcome, WorkersMoved) else
                f"Workers wait for the reset at {outcome.reset}. " if isinstance(outcome, WorkersWaitForReset) else "")
    return f"Codex refused worker {worker} for quota. Its pass stays open. {happened}Do not rerun its assign command."


def blocked(notes: list[AgentNote], refusing_account: agent_accounts.RefusingCodexAccount,
            now: datetime | None = None, *,
            selection: ClaudeAccountsInUse | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
            codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS,
            window: refusals.KnownWindow | refusals.UnknownWindow = UNKNOWN_REFUSAL_WINDOW,
            refused_at: RefusedAt | RefusalTimeUnknown = REFUSAL_TIME_UNKNOWN,
            stopped: StoppedWorker | StoppedWorkerUnknown = STOPPED_WORKER_UNKNOWN) -> list[str]:
    routed = _route_refusal(notes, refusing_account, now, selection=selection,
                            codex_readings=codex_readings, window=window, refused_at=refused_at)
    if not isinstance(stopped, StoppedWorker):
        return routed.lines
    error = relay(f"session:{stopped.session_id}", stopped_worker_relay(stopped.worker, routed.outcome), None)
    delivery = f"not delivered: {error}" if error else "sent"
    return [f"quota relay {stopped.worker} -> {stopped.session_id}: {delivery}", *routed.lines]


def _route_refusal(notes: list[AgentNote], refusing_account: agent_accounts.RefusingCodexAccount,
            now: datetime | None = None, *,
            selection: ClaudeAccountsInUse | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
            codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS,
            window: refusals.KnownWindow | refusals.UnknownWindow = UNKNOWN_REFUSAL_WINDOW,
            refused_at: RefusedAt | RefusalTimeUnknown = REFUSAL_TIME_UNKNOWN) -> RoutedRefusal:
    """A provider refusal waits briefly, moves workers, or opens the Claude fallback."""
    from scripts.accounts import accounts
    del selection
    now = now or datetime.now(timezone.utc)
    readings = resolve_codex_readings(codex_readings, now=now)
    refusal_time = refused_at.at if isinstance(refused_at, RefusedAt) else now
    _save_codex_readings(readings, now)
    _finish_workers_transitions(now)
    stamp = now.isoformat(timespec="seconds")
    outcome: RefusalOutcome = REFUSAL_OUTCOME_UNKNOWN
    home = str(refusing_account.home)
    if isinstance(refusing_account, agent_accounts.UnknownCodexAccount):
        with state_file() as state:
            notices = state.setdefault("refusal_notices", {})
            previous = notices.get(home)
            if previous is not None and previous["kind"] == "unknown":
                return RoutedRefusal([f"Codex refusal at {home}: unknown account already reported"], outcome)
            notices[home] = UnknownRefusalNotice(kind="unknown")
        text = (f"Codex refused work for quota at {home}; refusing account unknown: "
                + f"{refusing_account.reason}. Nothing switched to Claude.")
        return RoutedRefusal([text], outcome)
    name = refusing_account.label
    reading = refusals.stored_reading("codex", refusing_account.login, now)
    window = _refusal_window(window, _reading_for_account(readings, refusing_account, now), refusal_time)
    reset: refusals.KnownReset | refusals.NoResetKnown = refusals.NoResetKnown()
    if isinstance(window, refusals.KnownWindow) and isinstance(reading, (refusals.FreshReading, refusals.StaleReading)):
        held_window = reading.weekly if window.name == "weekly" else reading.five_hour
        if isinstance(held_window, refusals.ReadingWindow):
            reset = held_window.reset
    refusals.write_refusal(refusals.AccountRefusal("codex", refusing_account.login, window, reset, refusal_time))
    note = next((note for note in notes if note.tool == "codex"
                 and (note.get("login") or "").casefold() == refusing_account.login.casefold()),
                CodexQuotaAccountNote(note_file.note_path(name), {}, name))
    pending_head = workers_entry_pending()
    selected = readings.selected_account
    account_status = ("the outgoing account" if isinstance(selected, agent_accounts.KnownCodexAccount)
                      else "an account whose worker selection is unknown")
    transition_line = ""
    no_room = ""
    with state_file() as state:
        if "pending_switch" in state:
            _ = _complete_pending_claude_switch(state, state["pending_switch"])
        current_workers = accounts.role_account("workers")
        owns_role = (current_workers.home == refusing_account.home
                     and isinstance(selected, agent_accounts.KnownCodexAccount) and selected.home == refusing_account.home
                     and selected.login.casefold() == refusing_account.login.casefold()
                     and not isinstance(readings.workers, NoWorkersHome)
                     and selected.home == readings.workers.home)
        if owns_role and not isinstance(readings.workers, NoWorkersHome):
            readiness = codex_can_work(selected_codex_report(readings, now), now,
                                       agent_accounts.codex_credits_setting(readings.workers.label))
            entries = moves.read_moves()
            if (isinstance(reset, refusals.KnownReset) and (pending_head
                    or isinstance(window, refusals.KnownWindow) and window.name == "five_hour"
                    and timedelta(0) < reset.at - now < timedelta(minutes=20))):
                window_name = window.name if isinstance(window, refusals.KnownWindow) else "unknown"
                window_text = "5-hour" if window_name == "five_hour" else window_name
                if any(entry["tool"] == "codex" and entry["subject"] == "workers" and entry["kind"] == "waited"
                       and entry["reset"] == reset.at.isoformat() for entry in entries):
                    return RoutedRefusal([f"workers already wait for {name}'s {window_text} reset"],
                                         WorkersWaitForReset(reset_display(reset)))
                move_id = uuid.uuid4().hex
                moves.append_move(moves.WaitedMove(kind="waited", at=stamp, tool="codex", subject="workers",
                    move_id=move_id, window=window_name, reset=reset.at.isoformat()))
                transition_line = f"workers wait for {name}'s {window_text} reset at {reset_display(reset)}"
                outcome = WorkersWaitForReset(reset_display(reset))
            elif pending_head:
                return RoutedRefusal([f"workers next entry is pending; {name} refusal recorded without a fallback move"], outcome)
            else:
                if isinstance(readiness, CodexWorkAvailable):
                    return RoutedRefusal([f"{name} refused work; its allowance or paid credits can still serve workers. Nothing switched to Claude."], outcome)
                chosen = accounts.pick("codex", now, exclude=frozenset({readings.workers.label}), keep=accounts.NoKeep())
                if isinstance(chosen, accounts.PickedAccount):
                    move_id = uuid.uuid4().hex
                    window_name: Literal["weekly", "five_hour", "unknown"] = window.name if isinstance(window, refusals.KnownWindow) else "unknown"
                    reason = f"{name} refused ({window_name} limit until {reset_display(reset)})"
                    request: moves.RequestedMove = {"kind": "requested", "at": stamp, "tool": "codex",
                        "subject": "workers", "move_id": move_id, "from": readings.workers.label, "to": chosen.label,
                        "why": reason, "window": window_name}
                    if isinstance(reset, refusals.KnownReset):
                        request["reset"] = reset.at.isoformat()
                    moves.append_move(request)
                    try:
                        _ = accounts.swap("workers", chosen.label, why=request["why"], cause=request.get("cause", "refusal"),
                                          worker_moves="detach")
                    except (accounts.AccountsError, OSError) as error:
                        if not isinstance(error, accounts.WorkerMovesStartFailed):
                            moves.append_move(moves.FailedMove(kind="failed", at=stamp, tool="codex", subject="workers",
                                move_id=move_id, reason=str(error)))
                        no_room = f"workers could not move from {name} to {chosen.label}: {error}; using Claude fallback"
                    else:
                        transition_line = f"workers moved from {name} to {chosen.label}: {reason}"
                        outcome = WorkersMoved(chosen.label)
                else:
                    closed = {entry["move_id"] for entry in entries if entry["kind"] == "room again"}
                    pending = any(entry["tool"] == "codex" and entry["subject"] == "workers" and entry["kind"] == "no room"
                                  and entry["move_id"] not in closed for entry in entries)
                    if not pending:
                        moves.append_move(moves.NoRoomMove(kind="no room", at=stamp, tool="codex", subject="workers",
                            move_id=uuid.uuid4().hex, earliest_reset=chosen.earliest_reset.at.isoformat()
                            if isinstance(chosen.earliest_reset, accounts.EarliestReset) else ""))
                    no_room = accounts.no_room_line("codex", chosen)
                    if _start_no_worker_mode(state, readiness, chosen):
                        outcome = NO_WORKER_MODE_STARTED
        if pending_head and not transition_line:
            return RoutedRefusal([f"workers next entry is pending; {name} refusal recorded without a fallback move"], outcome)
        if transition_line:
            delivery: WorkersTransitionDelivery | FallbackRefusalDelivery = WorkersTransitionDelivery(transition_line)
        else:
            notices = state.setdefault("refusal_notices", {})
            previous = notices.get(home)
            if previous is not None and previous["kind"] == "unknown":
                del notices[home]
                previous = None
            episode = state["episodes"].get(name)
            notice = (previous if previous is not None and previous["kind"] == "known"
                      and previous["login"].casefold() == refusing_account.login.casefold()
                      else KnownRefusalNotice(kind="known", login=refusing_account.login, label=name))
            notices[home] = notice
            fallback = state["fallback"]
            try:
                already = (not isinstance(fallback, NoCodexQuotaFallback)
                           and not any(family == "codex" for family in registry_assignments().values()))
            except OSError:
                already = False
            log: list[str] = [no_room] if owns_role else []
            if owns_role and not already:
                if episode is not None:
                    _ = episode.pop("switch_failed", None)
                _ = notice.pop("switch_failed", None)
                log.append(switch_to_claude(state, note, stamp, refusing_account).log_line())
                fallback = state["fallback"]
            elif owns_role:
                log.append(f"{name} refused work for quota; assignments already use Claude")
            else:
                log.append(f"{name} refused work for quota on {account_status}; nothing switched")
            moved = owns_role and not already and isinstance(fallback, AccountCodexQuotaFallback)
            failed = None
            if owns_role:
                failed = notice.get("switch_failed") or (episode.get("switch_failed") if episode is not None else None)
                push = refusal_push(note, fallback, failed, already)
            else:
                text = f"Codex refused work on {name}, {account_status}. Nothing switched to Claude."
                status = QuotaStatusNote(name, text)
                if "push" not in notice:
                    log.append(f"quota status {status.name}: {status.text}")
                    notice["push"] = stamp
                return RoutedRefusal(log, outcome)
            new_failure = owns_role and not already and failed is not None and failed not in notice.get("failure_pushes", [])
            if not moved and not new_failure and ("push" in notice or episode is not None and "refusal_push" in episode):
                return RoutedRefusal(log, outcome)
            delivery = FallbackRefusalDelivery(log, push, notice, (failed,) if failed is not None else ())
    if isinstance(delivery, WorkersTransitionDelivery):
        return RoutedRefusal([delivery.text], outcome)
    log = delivery.lines
    quota_notice = delivery.quota_notice
    if isinstance(quota_notice, QuotaStatusNote):
        log.append(f"quota status {quota_notice.name}: {quota_notice.text}")
    else:
        push_delivery = tell_user(quota_notice.rendered)
        log.append(f"quota refusal {name} -> user: {_push_outcome(push_delivery)}")
        if not isinstance(push_delivery, PushSent):
            return RoutedRefusal(log, outcome)
    with state_file() as state:
        notices = state.setdefault("refusal_notices", {})
        delivery.notice["push"] = stamp
        if delivery.failures_to_stamp:
            delivery.notice.setdefault("failure_pushes", []).extend(delivery.failures_to_stamp)
        notices[home] = delivery.notice
        if name in state["episodes"]:
            state["episodes"][name]["refusal_push"] = stamp
    return RoutedRefusal(log, outcome)


def refresh(notes: list[AgentNote], here: str | None, now: datetime | None = None, *,
            selection: ClaudeAccountsInUse | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
            codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """Refresh readings and restore routing, including the workers-back notice."""
    del here
    selection = _selection(selection)
    now = now or datetime.now(timezone.utc)
    codex_readings = resolve_codex_readings(codex_readings, now=now)
    _save_codex_readings(codex_readings, now)
    threshold = load_config()["threshold_percent"]
    with state_file() as state:
        mode_before = state.get("no_worker_mode", False)
        closed = close_recovered(state, notes, threshold, selection, now, codex_readings)
        back = switch_back(state, now, codex_readings)
        codex_readings = _readings_after_restoration(codex_readings, back)
        workers_back = mode_before and not state.get("no_worker_mode", False)
    lines = [f"{name}: alert closed" for name in closed]
    lines += [f"switch to claude undone: {back.describe()}"] if isinstance(back, SwitchBack) else []
    lines += _alert_for_saved_readings(notes, now, selection, codex_readings)
    lines += [f"{note.label}: {percent(note)} left, resets {note.get('resets')}"
              for note in notes if note_file.in_use(note)]
    return [*lines, "notes refreshed; workers-back notice sent" if workers_back else "notes refreshed; nobody was told"]


def current_session() -> str | None:
    """Return the current staff name of this process's Claude owner."""
    from scripts.production import staff

    pid = os.getppid()
    while pid > 1:
        try:
            identity = cast(dict[str, str], json.loads((SESSIONS / f"{pid}.json").read_text(encoding="utf-8"))).get("sessionId", "")
            owner = staff.snapshot().by_current_session(identity)
            return owner.name if owner is not None else None
        except (OSError, ValueError):
            pass
        found = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, check=False)
        try:
            pid = int(found.stdout.strip())
        except ValueError:
            return None
    return None


def notice(here: Callable[[], str | None] = current_session) -> str | None:
    """The switches OWNER's user has not been shown, now marked seen; None when none are due here.

    It runs on every prompt of every session in /etc/nixos, so the session lookup,
    which walks the process tree, comes after a read of the state without its lock.
    """
    try:
        if not cast(State, json.loads(STATE.read_text(encoding="utf-8"))).get("unseen"):
            return None
    except (OSError, ValueError):
        return None
    if here() != OWNER:
        return None
    with state_file() as state:
        return " ".join(state.pop("unseen", [])) or None


def main() -> None:
    match sys.argv[1:2]:
        case ["notice"] if len(sys.argv) == 2:
            if text := notice():
                print(json.dumps({"systemMessage": text, "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": f"The user was just shown this quota notice: {text} Mention it to them in "
                                         + "one line before anything else this turn; the gap it leaves is theirs "
                                         + "to address.",
                }}))
        case _:
            sys.exit(f"usage: {Path(__file__).name} notice")


if __name__ == "__main__":
    main()
