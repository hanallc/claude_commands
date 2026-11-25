"""Send scheduled Claude login reminders from stored dates on both machines."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal, TextIO, cast

from scripts.accounts import accounts
from scripts.agents import remote
from scripts.lib import conf
from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.maintenance.schedule import ScheduleBusy, locked_history
from scripts.message import send_budget, send_result
from scripts.notify import push_texts, review
from scripts.whoami import agent_accounts, note_file
from scripts.whoami.quota_alert import PushFailed, PushSent


TEXT_ID = "accounts.login_sitting"
SENDER = "accounts"
REMINDER_STATE_FILE = ".local/state/accounts/login_sittings.json"
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
class ScheduledLoginRequest:
    at: datetime
    batch: ReminderBatch


@dataclass(frozen=True)
class NoLoginRequest:
    """All known login ends have been reported."""


@dataclass(frozen=True)
class LoginRequests:
    current: tuple[LoginEnd, ...]
    upcoming: ScheduledLoginRequest | NoLoginRequest


@dataclass(frozen=True)
class LoginCardThresholds:
    error: timedelta
    warning: timedelta

    @classmethod
    def read(cls, settings: conf.ConfigRead) -> LoginCardThresholds:
        return cls(timedelta(days=settings.whole("login_card.error_days")),
                   timedelta(days=settings.whole("login_card.warning_days")))


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


class ReminderCopyStatus(StrEnum):
    SAVED = "saved"
    REJECTED = "rejected"
    UNSAVED = "unsaved"
    DURABILITY_UNCONFIRMED = "durability unconfirmed"
    UNREPORTED = "status unavailable"


@dataclass(frozen=True)
class PeerLoginDates:
    ends: tuple[LoginEnd, ...]
    copy_status: ReminderCopyStatus


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
    local: tuple[LoginEnd, ...] = ()


@dataclass(frozen=True)
class ReminderStateCopy:
    source: str
    copied_at: datetime
    state: ReminderState

    def encode(self) -> str:
        return json.dumps({"source": self.source, "copied_at": self.copied_at.isoformat(),
                           "state": _state_fields(self.state)}) + "\n"


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
    return _status_dates(reading, machine)


def _status_dates(reading: accounts.StatusReading, machine: str) -> tuple[LoginEnd, ...]:
    rows = [{"label": row.label, "login_ends_at": agent_accounts.login_end_iso(row.login_ends_at)}
            for row in reading.rows if row.tool == "claude"]
    return login_dates(rows, machine)


def _peer_date_records(stdout: str, machine: str) -> PeerLoginDates:
    date_line, newline, status_line = stdout.partition("\n")
    if not newline:
        raise ValueError("peer date record is incomplete")
    document = cast(object, json.loads(date_line))
    if not isinstance(document, dict):
        raise ValueError("account status must be an object")
    ends = login_dates(cast(dict[str, object], document).get("accounts"), machine)
    status = ReminderCopyStatus.UNREPORTED
    if status_line.endswith("\n"):
        try:
            report = cast(object, json.loads(status_line))
            if isinstance(report, dict):
                status = ReminderCopyStatus(cast(dict[str, object], report).get("copy_status"))
        except (ValueError, RecursionError):
            pass
    return PeerLoginDates(ends, status)


def peer_dates(machine: str, timeout: int, reminder_copy: ReminderStateCopy) -> PeerLoginDates | PeerReadFailed:
    command = '"$HOME/.claude/scripts/lib/py" -m scripts.accounts.login_sittings exchange'
    try:
        done = subprocess.run(["ssh", "-o", "BatchMode=yes", machine, command], input=reminder_copy.encode(),
                              capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        output = error.stdout
        stdout = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output or ""
        failure = f"{machine} account read timed out after {timeout} s"
    except OSError as error:
        return PeerReadFailed(str(error).splitlines()[0])
    else:
        stdout = done.stdout
        failure = f"{machine} account read exited {done.returncode}" if done.returncode else ""
    try:
        return _peer_date_records(stdout, machine)
    except (ValueError, OverflowError, RecursionError) as error:
        return PeerReadFailed(failure or str(error).splitlines()[0])


def load_state(path: Path) -> ReminderState:
    try:
        return _decode_state(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    except FileNotFoundError:
        return ReminderState()


def _decode_state(document: object) -> ReminderState:
    if not isinstance(document, dict):
        raise ValueError("expected reminder state")
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
                         accepted_batches, ends(fields.get("local")))


def _observe_dates(state: ReminderState, dates: tuple[LoginEnd, ...], now: datetime) -> None:
    current_ends = {(end.machine, end.account): end.at for end in dates}
    state.named = {end for end in state.named
                   if current_ends.get((end.machine, end.account), end.at) == end.at}
    for end in dates:
        key = end.machine, end.account
        previous = state.observed.get(key, ObservedLogin(end))
        renewal = LoginRenewal(now, end.at) if end.at > previous.end.at else previous.renewal
        state.observed[key] = ObservedLogin(end, renewal)


def _state_fields(state: ReminderState) -> dict[str, object]:
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

    return {"sitting": stamp(state.sitting), "named": rows(state.named), "local": rows(state.local),
            "peer": rows(state.peer), "peer_read": stamp(state.peer_read), "peer_attempt": stamp(state.peer_attempt),
            "observed": observed, "accepted_batches": {key: rows(batch) for key, batch in state.accepted_batches.items()}}


def save_state(path: Path, state: ReminderState) -> None:
    write_text_atomic(path, json.dumps(_state_fields(state)) + "\n", new_file_mode=0o600)


def _decode_copy(document: object) -> ReminderStateCopy:
    if not isinstance(document, dict):
        raise ValueError("expected a copy of the reminder state")
    copy = cast(dict[str, object], document)
    source = copy.get("source")
    copied_at = recorded_time(copy.get("copied_at"))
    state = copy.get("state")
    if not isinstance(source, str) or not source or not isinstance(copied_at, datetime) or not isinstance(state, dict):
        raise ValueError("invalid reminder state copy")
    fields = cast(dict[str, object], state)
    for key in ("sitting", "peer_read", "peer_attempt"):
        stamp = fields.get(key)
        if stamp != "" and not isinstance(recorded_time(stamp), datetime):
            raise ValueError("invalid reminder timestamp")
    batches = fields.get("accepted_batches")
    if not isinstance(batches, dict):
        raise ValueError("expected accepted reminder batches")
    row_groups = [fields.get(key) for key in ("named", "local", "peer", "observed")]
    row_groups.extend(cast(dict[str, object], batches).values())
    for rows in row_groups:
        if not isinstance(rows, list):
            raise ValueError("expected login dates in reminder state")
        for row in cast(list[object], rows):
            if not isinstance(row, dict):
                raise ValueError("expected a login date")
            entry = cast(dict[str, object], row)
            machine = entry.get("machine")
            if not isinstance(machine, str) or not login_dates([entry], machine):
                raise ValueError("invalid login date")
            if ("renewed_at" in entry or "renewed_login_end" in entry) and not all(
                    isinstance(recorded_time(entry.get(key)), datetime) for key in ("renewed_at", "renewed_login_end")):
                raise ValueError("invalid renewal timestamp")
    return ReminderStateCopy(source, copied_at, _decode_state(fields))


def _copy_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}_copy.json")


def _read_copy(path: Path, policy: SittingPolicy, now: datetime) -> ReminderState:
    try:
        copy = _decode_copy(cast(object, json.loads(_copy_path(path).read_text(encoding="utf-8"))))
    except (OverflowError, RecursionError) as error:
        raise ValueError("invalid reminder state copy") from error
    max_age = 2 * policy.peer_interval + timedelta(seconds=policy.peer_timeout)
    if copy.source != policy.machine or now - copy.copied_at > max_age:
        raise ValueError("the reminder state copy is unavailable")
    return copy.state


def _save_incoming_copy(stdin: TextIO, local_host: str) -> ReminderCopyStatus:
    try:
        source = conf.read("accounts").text("login_sitting_machine")
        copy = _decode_copy(cast(object, json.load(stdin)))
        if copy.source != source or local_host == source:
            return ReminderCopyStatus.REJECTED
    except (ValueError, OSError, OverflowError, RecursionError):
        return ReminderCopyStatus.REJECTED
    try:
        write_text_atomic(_copy_path(reminder_state_path()), copy.encode(), new_file_mode=0o600)
    except AtomicDurabilityUnconfirmed:
        return ReminderCopyStatus.DURABILITY_UNCONFIRMED
    except OSError:
        return ReminderCopyStatus.UNSAVED
    return ReminderCopyStatus.SAVED


def main(argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    """Read the local login dates, then try to save the sending machine's state."""
    if argv != ["exchange"]:
        print("Usage: login_sittings exchange", file=stderr)
        return 2
    local_host = remote.local_machine()
    dates = local_dates(local_host)
    print(json.dumps({"accounts": [{"label": end.account, "login_ends_at": end.at.isoformat()} for end in dates]}),
          file=stdout, flush=True)
    copy_status = _save_incoming_copy(stdin, local_host)
    print(json.dumps({"copy_status": copy_status}), file=stdout)
    return 0


def _next_sitting(now: datetime, policy: SittingPolicy, state: ReminderState) -> datetime:
    reference = max(now, state.sitting) if isinstance(state.sitting, datetime) else now
    periods = max(0, (reference - policy.first) // policy.interval)
    sitting = policy.first + periods * policy.interval
    if isinstance(state.sitting, datetime) and state.sitting >= sitting:
        return sitting + policy.interval
    return sitting


def next_login_request(now: datetime, policy: SittingPolicy, state: ReminderState,
                       dates: tuple[LoginEnd, ...]) -> ScheduledLoginRequest | NoLoginRequest:
    """Select the next sitting or safety request from the unreported dates."""
    candidates = tuple(end for end in dates if end not in state.named)
    if not candidates:
        return NoLoginRequest()
    first = min(candidates, key=lambda end: (end.at, end.account))
    sitting = _next_sitting(now, policy, state)
    safety = max(now, first.at - policy.margin)
    if sitting <= safety:
        ends = tuple(end for end in sorted(candidates) if end.account == first.account)
        return ScheduledLoginRequest(sitting, ReminderBatch("sitting", ends))
    ends = tuple(end for end in candidates if end.at <= safety + policy.margin)
    return ScheduledLoginRequest(safety, ReminderBatch("safety", ends))


def reminder_state_path() -> Path:
    return accounts.accounts_home() / REMINDER_STATE_FILE


def login_requests(reading: accounts.StatusReading, settings: conf.ConfigRead) -> LoginRequests:
    """Use reported requests and the latest dates available on this machine."""
    policy = SittingPolicy.read(settings)
    local_host = remote.local_machine()
    path = reminder_state_path()
    now = reading.checked_at
    local_ends = _status_dates(reading, remote.this_machine())
    if local_host == policy.machine:
        state = load_state(path)
        dates = (*local_ends, *state.peer)
    else:
        state = _read_copy(path, policy, now)
        dates = (*state.local, *state.peer)
    latest = {(end.machine, end.account): end for end in (*dates, *local_ends)}
    current = tuple(sorted(
        (end for end in state.named if latest.get((end.machine, end.account), end) == end),
        key=lambda end: (end.at, end.account, end.machine)))
    upcoming = next_login_request(now, policy, state, dates) if current else NoLoginRequest()
    return LoginRequests(current, upcoming)


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
        read_peer: Callable[[str, int, ReminderStateCopy], PeerLoginDates | PeerReadFailed] = peer_dates,
        send: Callable[[ReminderBatch], PushSent | PushFailed] = send_push,
        retire: Callable[[str], ReminderRetired | ReminderRetirementFailed] = retire_push,
        state_path: Path | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> list[str]:
    settings = conf.read("accounts") if isinstance(configuration, conf.UseConfiguredValue) else configuration
    local_host = machine()
    policy = SittingPolicy.read(settings)
    if local_host != policy.machine:
        return []
    path = reminder_state_path() if isinstance(state_path, conf.UseConfiguredValue) else state_path
    with locked_history(path.with_suffix("")) as history:
        if isinstance(history, ScheduleBusy):
            return []
        return _run_claimed(now, policy, path, remote.this_machine(), peer_machine, read_local, read_peer, send, retire)


def _run_claimed(now: datetime, policy: SittingPolicy, path: Path, local_alias: str,
                peer_machine: Callable[[], str], read_local: Callable[[str], tuple[LoginEnd, ...]],
                read_peer: Callable[[str, int, ReminderStateCopy], PeerLoginDates | PeerReadFailed],
                send: Callable[[ReminderBatch], PushSent | PushFailed],
                retire: Callable[[str], ReminderRetired | ReminderRetirementFailed]) -> list[str]:
    """Read, send, and save while the caller holds the reminder lock."""
    state = load_state(path)
    lines: list[str] = []
    try:
        if isinstance(state.peer_attempt, NoRecordedTime) or now - state.peer_attempt >= policy.peer_interval:
            peer_alias = peer_machine()
            peer = read_peer(peer_alias, policy.peer_timeout, ReminderStateCopy(policy.machine, now, state))
            state.peer_attempt = now
            if isinstance(peer, PeerReadFailed):
                lines.append(f"login sittings: peer dates unavailable: {peer.reason}")
            else:
                state.peer = peer.ends
                state.peer_read = now
                _observe_dates(state, peer.ends, now)
                if peer.copy_status is not ReminderCopyStatus.SAVED:
                    lines.append(f"login sittings: {peer_alias} reminder copy {peer.copy_status.value}")
            save_state(path, state)
        local_ends = read_local(local_alias)
        state.local = local_ends
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

    def deliver(request: ScheduledLoginRequest) -> bool:
        batch = request.batch
        delivery = send(batch)
        if isinstance(delivery, PushFailed):
            lines.append(f"login sittings: {batch.kind} push failed: {delivery.why}")
            return False
        state.named.update(batch.ends)
        state.accepted_batches[batch.key] = batch.ends
        lines.append(f"login sittings: {batch.kind} reminder accepted")
        return True

    dates = (*local_ends, *state.peer)
    request = next_login_request(now, policy, state, dates)
    sitting = _next_sitting(now, policy, state)
    if sitting <= now:
        if isinstance(request, ScheduledLoginRequest) and not deliver(request):
            return lines
        state.sitting = sitting
        save_state(path, state)
        request = next_login_request(now, policy, state, dates)
    if isinstance(request, ScheduledLoginRequest) and request.at <= now and deliver(request):
        save_state(path, state)
    return lines


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], sys.stdin, sys.stdout, sys.stderr))
