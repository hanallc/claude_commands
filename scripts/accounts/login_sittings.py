"""Send scheduled Claude login reminders from stored dates on both machines."""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

from scripts.accounts import accounts
from scripts.agents import remote
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.maintenance.schedule import ScheduleBusy, locked_history
from scripts.message import send_budget, send_result
from scripts.notify import push_texts, review
from scripts.whoami import agent_accounts, note_file
from scripts.whoami.quota_alert import PushFailed, PushSent


TEXT_ID = "accounts.login_sitting"
SENDER = "accounts"
PushKind = Literal["sitting", "safety"]


@dataclass(frozen=True, order=True)
class LoginEnd:
    machine: str
    account: str
    at: datetime


@dataclass(frozen=True)
class ReminderBatch:
    kind: PushKind
    ends: tuple[LoginEnd, ...]

    @property
    def key(self) -> str:
        dates = [(end.machine, end.account, end.at.astimezone(timezone.utc).isoformat())
                 for end in sorted(self.ends)]
        digest = hashlib.sha256(json.dumps(dates).encode()).hexdigest()
        return f"login-{self.kind}-{digest}"


@dataclass(frozen=True)
class ReminderRetired:
    """The sender accepted the retirement."""


@dataclass(frozen=True)
class ReminderRetirementFailed:
    why: str


@dataclass(frozen=True)
class _LoginRequestRow:
    account: str
    machine: str
    login_end: datetime
    sitting: datetime


@dataclass(frozen=True)
class NoRecordedTime:
    """This event has not occurred."""


@dataclass(frozen=True)
class LoginRenewal:
    observed_at: datetime
    login_end: datetime


@dataclass(frozen=True)
class ObservedLogin:
    end: LoginEnd
    renewal: LoginRenewal | NoRecordedTime = field(default_factory=NoRecordedTime)


@dataclass(frozen=True)
class PeerReadFailed:
    reason: str


@dataclass(frozen=True)
class SittingPolicy:
    machine: str
    first: datetime
    interval: timedelta
    margin: timedelta
    peer_interval: timedelta
    peer_timeout: int

    @classmethod
    def read(cls, settings: conf.ConfigRead) -> SittingPolicy:
        return cls(settings.text("login_sitting_machine"),
                   datetime.fromisoformat(settings.text("login_sitting_first")),
                   timedelta(days=settings.whole("login_sitting_interval_days")),
                   timedelta(days=settings.whole("login_safety_margin_days")),
                   timedelta(minutes=settings.whole("login_peer_read_minutes")),
                   settings.whole("login_peer_read_timeout_seconds"))


@dataclass
class ReminderState:
    sitting: datetime | NoRecordedTime = field(default_factory=NoRecordedTime)
    named: set[LoginEnd] = field(default_factory=set)
    peer: tuple[LoginEnd, ...] = ()
    peer_read: datetime | NoRecordedTime = field(default_factory=NoRecordedTime)
    peer_attempt: datetime | NoRecordedTime = field(default_factory=NoRecordedTime)
    observed: dict[tuple[str, str], ObservedLogin] = field(default_factory=dict)
    accepted_batches: dict[str, tuple[LoginEnd, ...]] = field(default_factory=dict)


def recorded_time(value: object) -> datetime | NoRecordedTime:
    parsed = agent_accounts.login_end_from_iso(value)
    return parsed.at if isinstance(parsed, agent_accounts.LoginEndKnown) else NoRecordedTime()


def login_dates(rows: object, machine: str) -> tuple[LoginEnd, ...]:
    """Read only numbered Claude labels and dates from the public account rows."""
    if not isinstance(rows, list):
        raise ValueError("account rows must be a list")
    result: list[LoginEnd] = []
    for row in cast(list[object], rows):
        if not isinstance(row, dict):
            continue
        fields = cast(dict[str, object], row)
        label = fields.get("label")
        if not isinstance(label, str):
            continue
        parsed = note_file.parse_account_label(label)
        at = recorded_time(fields.get("login_ends_at"))
        if isinstance(parsed, note_file.NumberedAccountLabel) and parsed.tool == "claude" and isinstance(at, datetime):
            result.append(LoginEnd(machine, parsed.label, at))
    return tuple(result)


def local_dates(machine: str) -> tuple[LoginEnd, ...]:
    """Use the same stored rows as the accounts status command."""
    reading = accounts.read_status(source=accounts.StoredUsageOnly())
    rows = [{"label": row.label, "login_ends_at": agent_accounts.login_end_iso(row.login_ends_at)}
            for row in reading.rows if row.tool == "claude"]
    return login_dates(rows, machine)


def peer_dates(machine: str, timeout: int) -> tuple[LoginEnd, ...] | PeerReadFailed:
    command = ['"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/accounts.py" status --stored --json']
    try:
        done = subprocess.run(["ssh", "-o", "BatchMode=yes", machine, *command],
                              capture_output=True, text=True, timeout=timeout, check=False)
        if done.returncode:
            return PeerReadFailed(f"{machine} account read exited {done.returncode}")
        document = cast(object, json.loads(done.stdout))
        if not isinstance(document, dict):
            raise ValueError("account status must be an object")
        return login_dates(cast(dict[str, object], document).get("accounts"), machine)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return PeerReadFailed(str(error).splitlines()[0])


def load_state(path: Path) -> ReminderState:
    try:
        document = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return ReminderState()
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected reminder state")
    fields = cast(dict[str, object], document)

    def ends(rows: object) -> tuple[LoginEnd, ...]:
        result: list[LoginEnd] = []
        if isinstance(rows, list):
            for row in cast(list[object], rows):
                if isinstance(row, dict):
                    item = cast(dict[str, object], row)
                    machine = item.get("machine")
                    if isinstance(machine, str):
                        result.extend(login_dates([item], machine))
        return tuple(result)

    named, peer = set(ends(fields.get("named"))), ends(fields.get("peer"))
    # Use saved dates as the baseline when an older state has no observations.
    observed = {(end.machine, end.account): ObservedLogin(end) for end in (*sorted(named), *peer)}
    rows = fields.get("observed")
    if isinstance(rows, list):
        for row in cast(list[object], rows):
            if not isinstance(row, dict):
                continue
            item = cast(dict[str, object], row)
            machine = item.get("machine")
            if not isinstance(machine, str):
                continue
            for end in login_dates([item], machine):
                renewed_at = recorded_time(item.get("renewed_at"))
                renewed_end = recorded_time(item.get("renewed_login_end"))
                renewal = (LoginRenewal(renewed_at, renewed_end)
                           if isinstance(renewed_at, datetime) and isinstance(renewed_end, datetime)
                           else NoRecordedTime())
                observed[(end.machine, end.account)] = ObservedLogin(end, renewal)
    accepted_batches: dict[str, tuple[LoginEnd, ...]] = {}
    saved_batches = fields.get("accepted_batches")
    if isinstance(saved_batches, dict):
        for key, entries in cast(dict[object, object], saved_batches).items():
            if not isinstance(key, str) or not key or not isinstance(entries, list):
                continue
            batch_rows = cast(list[object], entries)
            batch_ends = ends(batch_rows)
            if len(batch_ends) == len(batch_rows) > 0:
                accepted_batches[key] = batch_ends
    return ReminderState(recorded_time(fields.get("sitting")), named, peer,
                         recorded_time(fields.get("peer_read")), recorded_time(fields.get("peer_attempt")), observed,
                         accepted_batches)


def _observe_dates(state: ReminderState, dates: tuple[LoginEnd, ...], now: datetime) -> None:
    current_ends = {(end.machine, end.account): end.at for end in dates}
    state.named = {end for end in state.named
                   if current_ends.get((end.machine, end.account), end.at) == end.at}
    for end in dates:
        key = end.machine, end.account
        previous = state.observed.get(key, ObservedLogin(end))
        renewal = LoginRenewal(now, end.at) if end.at > previous.end.at else previous.renewal
        state.observed[key] = ObservedLogin(end, renewal)


def save_state(path: Path, state: ReminderState) -> None:
    def stamp(at: datetime | NoRecordedTime) -> str:
        return at.isoformat() if isinstance(at, datetime) else ""

    def row(end: LoginEnd) -> dict[str, str]:
        return {"machine": end.machine, "label": end.account, "login_ends_at": end.at.isoformat()}

    def rows(ends: tuple[LoginEnd, ...] | set[LoginEnd]) -> list[dict[str, str]]:
        return [row(end) for end in sorted(ends)]

    observed: list[dict[str, str]] = []
    for key in sorted(state.observed):
        login = state.observed[key]
        item = row(login.end)
        if isinstance(login.renewal, LoginRenewal):
            item["renewed_at"] = login.renewal.observed_at.isoformat()
            item["renewed_login_end"] = login.renewal.login_end.isoformat()
        observed.append(item)

    write_text_atomic(path, json.dumps({"sitting": stamp(state.sitting), "named": rows(state.named),
        "peer": rows(state.peer), "peer_read": stamp(state.peer_read), "peer_attempt": stamp(state.peer_attempt),
        "observed": observed, "accepted_batches": {key: rows(batch) for key, batch in state.accepted_batches.items()}}) + "\n",
        new_file_mode=0o600)


def _next_sitting(now: datetime, policy: SittingPolicy, state: ReminderState) -> datetime:
    reference = max(now, state.sitting) if isinstance(state.sitting, datetime) else now
    periods = max(0, (reference - policy.first) // policy.interval)
    sitting = policy.first + periods * policy.interval
    if isinstance(state.sitting, datetime) and state.sitting >= sitting:
        return sitting + policy.interval
    return sitting


def _next_login_request(now: datetime, policy: SittingPolicy, state: ReminderState,
                        local_ends: tuple[LoginEnd, ...]) -> tuple[_LoginRequestRow, ...]:
    """Return the unreported machine dates for the next account."""
    dates = (*local_ends, *state.peer)
    candidates = [end for end in dates if end not in state.named]
    if not candidates:
        return ()
    selected = min(candidates, key=lambda end: (end.at, end.account)).account
    sitting = _next_sitting(now, policy, state)
    return tuple(_LoginRequestRow(end.account, end.machine, end.at, sitting)
                 for end in sorted(candidates) if end.account == selected)


def reminder_push(ends: tuple[LoginEnd, ...]) -> push_texts.RenderedPush:
    zone = user_zone()
    labels = sorted({end.account for end in ends})
    lines = [label + ": " + ", ".join(
        f"{'Mac' if end.machine == 'mac' else end.machine} {end.at.astimezone(zone):%a %m-%d}"
        for end in sorted(ends) if end.account == label) for label in labels]
    return push_texts.render(TEXT_ID, accounts="\n".join(lines))


def send_push(batch: ReminderBatch) -> PushSent | PushFailed:
    push = reminder_push(batch.ends)
    limit = review.user_send_seconds()
    try:
        done = subprocess.run(push_texts.user_send_argv(push, sender=SENDER, key=batch.key, timeout=limit),
                              capture_output=True, text=True, timeout=send_budget.user_seconds(limit), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return PushFailed(str(error).splitlines()[0])
    result = send_result.send_outcome(done.returncode, done.stdout)
    if isinstance(result, send_result.ConfirmedSendOutcome) and result.header.outcome in ("sent", "skipped", "kept", "replaced"):
        return PushSent()
    return PushFailed(f"send.py did not accept the login reminder (exit {done.returncode})")


def retire_push(key: str) -> ReminderRetired | ReminderRetirementFailed:
    try:
        done = subprocess.run(push_texts.user_retire_argv(SENDER, key), capture_output=True,
                              timeout=send_budget.user_seconds(review.user_send_seconds()), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return ReminderRetirementFailed(str(error).splitlines()[0])
    if done.returncode == 0:
        return ReminderRetired()
    return ReminderRetirementFailed(f"send.py did not retire the login reminder (exit {done.returncode})")


def run(now: datetime, *, configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
        machine: Callable[[], str] = remote.local_machine,
        peer_machine: Callable[[], str] = remote.other_machine,
        read_local: Callable[[str], tuple[LoginEnd, ...]] = local_dates,
        read_peer: Callable[[str, int], tuple[LoginEnd, ...] | PeerReadFailed] = peer_dates,
        send: Callable[[ReminderBatch], PushSent | PushFailed] = send_push,
        retire: Callable[[str], ReminderRetired | ReminderRetirementFailed] = retire_push,
        state_path: Path | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> list[str]:
    settings = conf.read("accounts") if isinstance(configuration, conf.UseConfiguredValue) else configuration
    local = machine()
    if local != settings.text("login_sitting_machine"):
        return []
    policy = SittingPolicy.read(settings)
    path = (accounts.accounts_home() / ".local/state/accounts/login_sittings.json"
            if isinstance(state_path, conf.UseConfiguredValue) else state_path)
    with locked_history(path.with_suffix("")) as history:
        if isinstance(history, ScheduleBusy):
            return []
        return _run_claimed(now, policy, path, local, peer_machine, read_local, read_peer, send, retire)


def _run_claimed(now: datetime, policy: SittingPolicy, path: Path, local: str,
                peer_machine: Callable[[], str], read_local: Callable[[str], tuple[LoginEnd, ...]],
                read_peer: Callable[[str, int], tuple[LoginEnd, ...] | PeerReadFailed],
                send: Callable[[ReminderBatch], PushSent | PushFailed],
                retire: Callable[[str], ReminderRetired | ReminderRetirementFailed]) -> list[str]:
    """Read, send, and save while the caller holds the reminder lock."""
    state = load_state(path)
    lines: list[str] = []
    try:
        if isinstance(state.peer_attempt, NoRecordedTime) or now - state.peer_attempt >= policy.peer_interval:
            peer = read_peer(peer_machine(), policy.peer_timeout)
            state.peer_attempt = now
            if isinstance(peer, PeerReadFailed):
                lines.append(f"login sittings: peer dates unavailable: {peer.reason}")
            else:
                state.peer = peer
                state.peer_read = now
                _observe_dates(state, peer, now)
            save_state(path, state)
        local_ends = read_local(local)
        _observe_dates(state, local_ends, now)
        save_state(path, state)
    finally:
        # Use only saved observations, even if a read or save fails.
        state = load_state(path)
        saved_ends = {identity: login.end for identity, login in state.observed.items()}
        batches = tuple(state.accepted_batches.items())
        for key, batch in batches:
            if not all(saved_ends.get((end.machine, end.account), end) != end for end in batch):
                continue
            outcome = retire(key)
            if isinstance(outcome, ReminderRetirementFailed):
                lines.append(f"login sittings: retirement failed: {outcome.why}")
            else:
                del state.accepted_batches[key]
                lines.append("login sittings: reminder retired")
        if len(state.accepted_batches) < len(batches):
            save_state(path, state)

    def deliver(ends: tuple[LoginEnd, ...], kind: PushKind) -> bool:
        batch = ReminderBatch(kind, ends)
        delivery = send(batch)
        if isinstance(delivery, PushFailed):
            lines.append(f"login sittings: {kind} push failed: {delivery.why}")
            return False
        state.named.update(ends)
        state.accepted_batches[batch.key] = batch.ends
        lines.append(f"login sittings: {kind} reminder accepted")
        return True

    sitting = _next_sitting(now, policy, state)
    if sitting <= now:
        request = _next_login_request(now, policy, state, local_ends)
        selected = tuple(LoginEnd(row.machine, row.account, row.login_end) for row in request)
        if selected and not deliver(selected, "sitting"):
            return lines
        state.sitting = sitting
        save_state(path, state)
    safety = tuple(end for end in (*local_ends, *state.peer) if end.at <= now + policy.margin and end not in state.named)
    if safety and deliver(safety, "safety"):
        save_state(path, state)
    return lines
