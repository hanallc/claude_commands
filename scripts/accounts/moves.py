"""Account transitions: a request is distinct from a confirmed session move."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from hashlib import sha256
from importlib import import_module
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, ReadOnly, TypedDict, Protocol, cast, override, assert_never

from scripts.accounts import role_map, worker_names
from scripts.agents.stop_results import MoveHandedOff, StopFailed
from scripts.lib import process_start
from scripts.lib.atomic_file import write_text_atomic

if TYPE_CHECKING:
    from scripts.whoami.account import ResolvedHistoryLabel, HistoricalAccountUnresolved


class HistoryResolver(Protocol):
    def resolve_history_label(self, label: str, at: str, *, login: str = "") -> ResolvedHistoryLabel | HistoricalAccountUnresolved: ...


@dataclass(frozen=True)
class HomeAccountUnresolved:
    """The journal lacks evidence of the numbered login this home held then."""

    home: Path

    @override
    def __str__(self) -> str:
        return "home account unresolved"


HistoricalAccountLabel = str | HomeAccountUnresolved


class MoveCommon(TypedDict):
    request_id: NotRequired[str]
    at: str
    tool: Literal["claude", "codex"]
    subject: str
    move_id: str


RequestedFields = TypedDict("RequestedFields", {"from": HistoricalAccountLabel, "to": HistoricalAccountLabel, "why": str,
                                              "window": Literal["weekly", "five_hour", "unknown"], "reset": NotRequired[str]})


MoveCause = Literal["refusal", "credits off", "by hand", "auto"]


class RecordedMoveRequest(MoveCommon, RequestedFields):
    terminal: NotRequired[str]
    kind: Literal["requested"]
    cause: NotRequired[MoveCause]
    scope: NotRequired[ReadOnly[Literal["group", "session"]]]


class RequestedMove(RecordedMoveRequest):
    scope: Literal["group", "session"]


class ConfirmedMove(MoveCommon):
    kind: Literal["confirmed"]
    home: NotRequired[str]
    config_dir: NotRequired[str]
    worker: NotRequired[str]
    run: NotRequired[str]


class FailedMove(MoveCommon):
    kind: Literal["failed"]
    reason: str


class WaitedMove(MoveCommon):
    kind: Literal["waited"]
    window: Literal["weekly", "five_hour", "unknown"]
    reset: str


class WaitEndedMove(MoveCommon):
    kind: Literal["wait ended"]


class NoRoomMove(MoveCommon):
    kind: Literal["no room"]
    earliest_reset: str


class RoomAgainMove(MoveCommon):
    kind: Literal["room again"]
    label: HistoricalAccountLabel


MoveOutcomeEntry = ConfirmedMove | FailedMove | WaitedMove | WaitEndedMove | NoRoomMove | RoomAgainMove
MoveEntry = RecordedMoveRequest | MoveOutcomeEntry
MoveWriteEntry = RequestedMove | MoveOutcomeEntry


@dataclass(frozen=True)
class UnknownFallbackOwner:
    """The routing has no recorded numbered owner."""

    @override
    def __str__(self) -> str:
        return "owner unknown"


class RoutingCommon(TypedDict):
    at: str
    tool: Literal["codex"]
    fallback_at: str
    owner: str | UnknownFallbackOwner


class RoutedToClaude(RoutingCommon):
    kind: Literal["routed to claude"]
    cause: Literal["refusal", "credits off", "auto"]
    why: str
    moved: list[str]
    claude_account: NotRequired[str]


class RoutedBack(RoutingCommon):
    kind: Literal["routed back"]
    restored: list[str]
    kept: list[str]


class RoutingNotRestored(RoutingCommon):
    kind: Literal["routing not restored"]
    outcome: Literal["partial", "failed"]
    restored: list[str]
    kept: list[str]
    failed: list[str]


RoutingEntry = RoutedToClaude | RoutedBack | RoutingNotRestored
JournalEntry = MoveEntry | RoutingEntry


def store_file() -> Path:
    return Path.home() / ".local" / "state" / "accounts" / "moves.jsonl"


@dataclass(frozen=True)
class MoveInProgress(MoveHandedOff):
    account: str
    command: str = field(default="", compare=False)


@dataclass(frozen=True)
class MoveWaitingForRow(StopFailed):
    """The batch could not take the session or staff row lock."""

    command: str = field(default="", compare=False)


@dataclass(frozen=True)
class MoveSuperseded(StopFailed):
    """A later request replaced this detached move."""

    reason: str = "a newer move ran"


class ExplicitMoveTarget(Enum):
    REQUESTED_ACCOUNT = "use the requested account"
    GROUP_ACCOUNT = "use the account requested by a group operation"


@dataclass(frozen=True)
class FollowGroupAccount:
    group: role_map.ClaudeGroup


@dataclass(frozen=True)
class MoveMember:
    reference: str
    name: str


@dataclass(frozen=True)
class MoveBatchResult:
    target: str | FollowGroupAccount
    pending: tuple[MoveMember, ...]


def native_row_key(reference: str) -> str:
    return "native-" + sha256(reference.encode()).hexdigest()


@dataclass(frozen=True)
class ClaimOwner:
    """The process of a user command; its claim is live while this process lives."""

    pid: int
    start: str


@dataclass(frozen=True)
class LatestMoveRequest:
    request_id: str
    target: str
    explicit_group_scope: bool = False
    # The user command that wrote the claim; None for an automatic move or a plain request.
    owner: ClaimOwner | None = field(default=None, compare=False)


class NoLatestMoveRequest:
    """The session has no saved move request."""


@dataclass(frozen=True)
class PendingMoveRequest:
    member: MoveMember
    request: LatestMoveRequest


def latest_request_path(native_session: str) -> Path:
    return store_file().parent / "latest-requests" / native_row_key(native_session)


@contextmanager
def claim_lock() -> Generator[None]:
    """Make each read, check and write of the latest-request files one atomic step."""
    path = latest_request_path("").parent.with_name("latest-requests.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def own_claim_owner() -> ClaimOwner:
    """Read the own process start, with one more read after a failure; no start is recorded after two failures."""
    for _ in range(2):
        started = process_start.process_start(os.getpid())
        if isinstance(started, process_start.Started):
            return ClaimOwner(os.getpid(), started.start)
    return ClaimOwner(os.getpid(), "")


def claim_is_live(request: LatestMoveRequest) -> bool:
    """A user claim is live while its process lives or cannot be read; a claim with no start, while its pid exists."""
    if request.owner is None:
        return False
    return _claim_owner_is_live(request.owner)


def _claim_owner_is_live(owner: ClaimOwner) -> bool:
    state = process_start.process_start(owner.pid)
    return (isinstance(state, process_start.Unreadable)
            or isinstance(state, process_start.Started) and owner.start in ("", state.start))


def _hand_queue_path(identity: str) -> Path:
    path = latest_request_path(identity)
    return path.parent.with_name("hand-move-queues") / path.name


def _hand_queue(identity: str) -> dict[str, ClaimOwner]:
    try:
        raw = cast(object, json.loads(_hand_queue_path(identity).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("invalid hand move queue")
    queued: dict[str, ClaimOwner] = {}
    for request, owner in cast(dict[str, object], raw).items():
        match owner:
            case [int(pid), str(start)] if type(pid) is int and pid > 1:
                queued[request] = ClaimOwner(pid, start)
            case _:
                raise ValueError("invalid hand move queue owner")
    return queued


def _save_hand_queue(identity: str, queued: dict[str, ClaimOwner]) -> None:
    path = _hand_queue_path(identity)
    if queued:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps({request: (owner.pid, owner.start) for request, owner in queued.items()}),
                          new_file_mode=0o600)
    else:
        path.unlink(missing_ok=True)


def queue_hand_move(identity: str, request_id: str, owner: ClaimOwner) -> None:
    """Register a hand move in arrival order under the claim lock."""
    with claim_lock():
        queued = _hand_queue(identity)
        if request_id not in queued:
            queued[request_id] = owner
            _save_hand_queue(identity, queued)


def hand_move_ahead(identity: str, request_id: str) -> ClaimOwner | None:
    """Read the oldest live hand move before this request. The caller holds the claim lock."""
    for queued_id, owner in _hand_queue(identity).items():
        if queued_id == request_id:
            break
        if _claim_owner_is_live(owner):
            return owner
    return None


def hand_move_is_queued(identity: str, request_id: str) -> bool:
    return request_id in _hand_queue(identity)


def end_hand_move(identity: str, request_id: str) -> None:
    with claim_lock():
        queued = _hand_queue(identity)
        if queued.pop(request_id, None) is not None:
            _save_hand_queue(identity, {key: owner for key, owner in queued.items() if _claim_owner_is_live(owner)})


def write_latest_request(native_session: str, request: LatestMoveRequest, *, name: str = "") -> None:
    """Replace the request of a native session; a caller that reads and checks first holds `claim_lock`.

    A move writes its request while it holds the session locks.
    One exception: a user command writes its claim without the row lock while another operation holds the row.
    An automatic holder reads the request again before its stop and drops the row if a hand move took its claim.
    An automatic move never takes a row while a live user claim is current.
    Concurrent hand moves use the queue to preserve the order of their claims.
    """
    path = latest_request_path(native_session)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: dict[str, str | bool | int] = {"request_id": request.request_id, "target": request.target,
        "name": name or "unnamed session", "native_session": native_session}
    if request.explicit_group_scope:
        fields["explicit_group_scope"] = True
    if request.owner is not None:
        fields["pid"], fields["start"] = request.owner.pid, request.owner.start
    write_text_atomic(path, json.dumps(fields), new_file_mode=0o600)


def _read_move_request(path: Path) -> PendingMoveRequest | NoLatestMoveRequest:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoLatestMoveRequest()
    except RecursionError as error:
        raise ValueError("move request JSON exceeds the nesting limit") from error
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        request_id, target = fields.get("request_id"), fields.get("target")
        explicit_group_scope = fields.get("explicit_group_scope", False)
        pid, start = fields.get("pid"), fields.get("start")
        owner = ClaimOwner(pid, start) if isinstance(pid, int) and isinstance(start, str) else None
        native_session, name = fields.get("native_session"), fields.get("name")
        if (isinstance(request_id, str) and request_id and isinstance(target, str) and target
                and (not target.startswith("group:") or target[6:] in role_map.CLAUDE_GROUPS)
                and isinstance(native_session, str) and native_session and latest_request_path(native_session) == path
                and isinstance(name, str) and name and isinstance(explicit_group_scope, bool)
                and (owner is not None or pid is None and start is None)):
            return PendingMoveRequest(MoveMember(native_session, name),
                                      LatestMoveRequest(request_id, target, explicit_group_scope, owner))
    raise ValueError("invalid latest move request")


def read_latest_request(native_session: str) -> LatestMoveRequest | NoLatestMoveRequest:
    saved = _read_move_request(latest_request_path(native_session))
    return saved if isinstance(saved, NoLatestMoveRequest) else saved.request


def pending_move_requests() -> tuple[PendingMoveRequest, ...]:
    pending: list[PendingMoveRequest] = []
    for path in sorted(latest_request_path("").parent.glob("native-*")):
        try:
            saved = _read_move_request(path)
        except (OSError, ValueError):
            print(f"Could not read move request {path.name}.", file=sys.stderr)
            continue
        if isinstance(saved, PendingMoveRequest):
            pending.append(saved)
    return tuple(pending)


MOVE_COMMAND = "~/.claude/scripts/lib/py ~/.claude/scripts/agents/stop_resume.py"
RETRY_COMMAND = f"{MOVE_COMMAND} retry"


def batch_move_command(reference: str, destination: str | FollowGroupAccount, *, user_command: bool = False) -> str:
    options = (["--group", destination.group] if isinstance(destination, FollowGroupAccount)
               else ["--target", destination])
    if user_command:
        options.append("--user-command")
    return f"{MOVE_COMMAND} batch {shlex.join([*options, '--', reference])}"


def remove_latest_request(native_session: str, request_id: str) -> None:
    """Remove only this request.

    A move removes its request while it holds the session locks.
    A user command that stops its wait for a held row removes its claim without the row lock.
    The request id check under `claim_lock` keeps a newer request in place.
    """
    with claim_lock():
        current = read_latest_request(native_session)
        if isinstance(current, LatestMoveRequest) and current.request_id == request_id:
            latest_request_path(native_session).unlink(missing_ok=True)


def set_claim_owner(native_session: str, request_id: str, owner: ClaimOwner | None) -> bool:
    """Give this request a new owner and keep its other fields; False when another request replaced it.

    The detached child takes the claim of its parent. A user command at its end keeps a plain request for a retry.
    """
    with claim_lock():
        saved = _read_move_request(latest_request_path(native_session))
        queued = _hand_queue(native_session)
        if owner is not None and request_id in queued and queued[request_id] != owner:
            queued[request_id] = owner
            _save_hand_queue(native_session, queued)
        if not isinstance(saved, PendingMoveRequest) or saved.request.request_id != request_id:
            return owner is not None and request_id in queued
        if saved.request.owner != owner:
            write_latest_request(native_session, replace(saved.request, owner=owner), name=saved.member.name)
        return True


def _batch_path(batch_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        raise ValueError("invalid move batch id")
    return store_file().parent / "move-results" / f"{batch_id}.json"


def read_batch_result(batch_id: str) -> MoveBatchResult:
    value = cast(object, json.loads(_batch_path(batch_id).read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError("invalid move batch result")
    fields = cast(dict[str, object], value)
    group, target, pending = fields.get("group"), fields.get("target"), fields.get("pending")
    if group is not None:
        if group not in role_map.CLAUDE_GROUPS:
            raise ValueError("invalid move batch group")
        target = FollowGroupAccount(group)
    if not isinstance(target, (str, FollowGroupAccount)) or not isinstance(pending, list):
        raise ValueError("invalid move batch result")
    members: list[MoveMember] = []
    for member in cast(list[object], pending):
        if not isinstance(member, dict):
            raise ValueError("invalid move batch member")
        saved = cast(dict[str, object], member)
        reference, name = saved.get("reference"), saved.get("name")
        if not isinstance(reference, str) or not reference or not isinstance(name, str) or not name:
            raise ValueError("invalid move batch member")
        members.append(MoveMember(reference, name))
    return MoveBatchResult(target, tuple(members))


def legacy_batch_ids() -> tuple[str, ...]:
    """Read newer reports first when more than one report names a session."""
    directory = store_file().parent / "move-results"
    reports: list[tuple[int, str]] = []
    for path in directory.glob("*.json"):
        if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
            continue
        try:
            reports.append((path.stat().st_mtime_ns, path.stem))
        except FileNotFoundError:
            continue
    return tuple(batch_id for _, batch_id in sorted(reports, reverse=True))


def _valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    fields = cast(dict[str, object], value)
    kind = fields.get("kind")
    if kind in ("routed to claude", "routed back", "routing not restored"):
        if "move_id" in fields or "subject" in fields or fields.get("tool") != "codex":
            return False
        if not isinstance(fields.get("owner"), (str, UnknownFallbackOwner)):
            return False
        try:
            for key in ("at", "fallback_at"):
                stamp = fields.get(key)
                if not isinstance(stamp, str) or datetime.fromisoformat(stamp).tzinfo is None:
                    return False
        except ValueError:
            return False
        keys = ("moved",) if kind == "routed to claude" else ("restored", "kept")
        if kind == "routed to claude" and (fields.get("cause") not in ("refusal", "credits off", "auto")
                                               or not isinstance(fields.get("why"), str)):
            return False
        if "claude_account" in fields:
            target = fields["claude_account"]
            if not isinstance(target, str) or not re.fullmatch(r"claude[1-9][0-9]*", target):
                return False
        if kind == "routing not restored":
            if fields.get("outcome") not in ("partial", "failed"):
                return False
            keys += ("failed",)
        return all(isinstance(fields.get(key), list) and all(isinstance(item, str) for item in cast(list[object], fields[key])) for key in keys)
    if not all(isinstance(fields.get(key), str) and fields[key] for key in ("at", "subject", "move_id")):
        return False
    if "request_id" in fields and (not isinstance(fields["request_id"], str) or not fields["request_id"]):
        return False
    if fields.get("tool") not in ("claude", "codex"):
        return False
    try:
        if datetime.fromisoformat(cast(str, fields["at"])).tzinfo is None:
            return False
        for field in ("reset", "earliest_reset"):
            if field in fields and isinstance(fields[field], str) and fields[field]:
                if datetime.fromisoformat(cast(str, fields[field])).tzinfo is None:
                    return False
    except ValueError:
        return False
    if "window" in fields and fields["window"] not in ("weekly", "five_hour", "unknown"):
        return False
    kind = fields.get("kind")
    if "cause" in fields and (kind != "requested" or fields["cause"] not in ("refusal", "credits off", "by hand", "auto")):
        return False
    if "scope" in fields and (kind != "requested" or fields["scope"] not in ("group", "session")):
        return False
    if kind in ("requested", "waited") and "reset" in fields:
        if not isinstance(fields["reset"], str) or not fields["reset"]:
            return False
    required = {"requested": ("from", "to", "why", "window"),
                "failed": ("reason",), "waited": ("window", "reset"),
                "wait ended": (), "no room": ("earliest_reset",), "room again": ("label",)}
    if kind == "confirmed":
        return "worker" not in fields or isinstance(fields["worker"], str)
    return isinstance(kind, str) and kind in required and all(isinstance(fields.get(key), str) for key in required[kind])


def _canonical_entry[Entry: MoveEntry](entry: Entry) -> Entry:
    from scripts.whoami.note_file import canonical_label

    fields: dict[str, object] = dict(entry)
    for field in ("from", "to", "label"):
        value = fields.get(field)
        if isinstance(value, str):
            fields[field] = canonical_label(value)
    return cast(Entry, cast(object, fields))



def append_journal_line(descriptor: int, document: str) -> None:
    role_map.append_journal_line(descriptor, document, valid_row=_valid_legacy_journal_row)


def _valid_legacy_journal_row(value: object) -> bool:
    if isinstance(value, dict):
        fields = dict(cast(dict[str, object], value))
        if fields.get("kind") == "routed to claude":
            _ = fields.setdefault("why", fields.get("cause"))
        return _valid(fields)
    return False


@dataclass(frozen=True)
class RecordedAutoEntries:
    entry_ids: frozenset[str]


@dataclass(frozen=True)
class UnreadableMoveJournal:
    """Workers entries cannot safely run without their replay evidence."""



@dataclass(frozen=True)
class ReadCurrentMoveJournal:
    """A standalone caller requests current replay evidence."""


READ_CURRENT_MOVE_JOURNAL = ReadCurrentMoveJournal()


@dataclass(frozen=True)
class MoveReplayEvidence:
    """Validated move rows and recorded queue entries from one journal read."""

    entries: tuple[MoveEntry, ...]
    history: RecordedAutoEntries


def read_replay_evidence() -> MoveReplayEvidence | UnreadableMoveJournal:
    """Read replay evidence under the same flock as journal appenders."""
    path = store_file()
    try:
        worker_names.recover_pending_journal(path)
        with path.open("rb") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_SH)
            lines = source.read().splitlines(keepends=True)
        rows: list[dict[str, object]] = []
        for index, line in enumerate(lines):
            try:
                raw = cast(object, json.loads(line))
                if not isinstance(raw, dict):
                    raise ValueError("expected a move journal object")
                row = cast(dict[str, object], worker_names.journal_entry(cast(dict[str, object], raw)))
                if row.get("kind") == "routed to claude":
                    _ = row.setdefault("why", row.get("cause"))
                if not _valid(row):
                    raise ValueError("invalid move journal row")
                rows.append(row)
            except (ValueError, UnicodeError):
                if index == len(lines) - 1 and not line.endswith(b"\n"):
                    print(f"account moves: skipping torn final line in {path}")
                    continue
                raise
        failed = {row.get("move_id") for row in rows if row.get("kind") == "failed"}
        ids: set[str] = set()
        for row in rows:
            why = row.get("why")
            if not isinstance(why, str) or not why.startswith("auto: "):
                continue
            if (row.get("kind") == "routed to claude"
                    or row.get("kind") == "requested" and row.get("tool") == "codex" and row.get("subject") == "workers"
                    and row.get("cause") == "auto" and row.get("move_id") not in failed):
                ids.add(why.removeprefix("auto: "))
        journal = _resolve_journal_entries(cast(list[JournalEntry], cast(object, rows)))
        return MoveReplayEvidence(tuple(account_moves(journal)), RecordedAutoEntries(frozenset(ids)))
    except FileNotFoundError:
        return MoveReplayEvidence((), RecordedAutoEntries(frozenset()))
    except (OSError, ValueError, UnicodeError) as error:
        print(f"account moves: anomaly: cannot read recorded workers moves: {error}")
        return UnreadableMoveJournal()


def recorded_auto_entries() -> RecordedAutoEntries | UnreadableMoveJournal:
    """Return recorded queue entries from the shared replay reader."""
    evidence = read_replay_evidence()
    return evidence.history if isinstance(evidence, MoveReplayEvidence) else evidence


def append_move(entry: MoveWriteEntry) -> None:
    """Append a complete transition while holding the shared writer lock."""
    entry = _canonical_entry(entry)
    if entry["kind"] == "requested":
        source = entry["from"]
        if isinstance(source, HomeAccountUnresolved):
            entry = entry.copy()
            entry["from"] = str(source.home)
    if any(entry.get(field) == "default" for field in ("from", "to", "label")):
        raise ValueError("name the account by number")
    path = store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+", encoding="utf-8", errors="replace") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        lines = worker_names.migrate_locked_journal(output, path)
        if entry["kind"] in ("requested", "confirmed", "wait ended", "room again", "failed"):
            for line in lines:
                try:
                    previous = cast(object, json.loads(line))
                except ValueError:
                    continue
                if not _valid(previous):
                    continue
                held = cast(dict[str, object], previous)
                if held.get("kind") != entry["kind"] or held.get("move_id") != entry["move_id"]:
                    continue
                if entry["kind"] == "requested":
                    return
                if (held.get("tool") == entry["tool"] and held.get("subject") == entry["subject"]
                        and (entry["kind"] != "confirmed" or
                             (held.get("worker") == entry.get("worker") and held.get("run") == entry.get("run")))):
                    return
        _ = output.seek(0, os.SEEK_END)
        append_journal_line(output.fileno(), json.dumps(entry, sort_keys=True))


def _resolve_journal_entries(journal: list[JournalEntry]) -> list[JournalEntry]:
    """Resolve legacy homes through historical evidence for both journal readers."""
    from scripts.whoami.note_file import NumberedAccountLabel, parse_account_label

    def numbered(value: object, tool: str) -> str | HomeAccountUnresolved:
        parsed = parse_account_label(value) if isinstance(value, str) else None
        if isinstance(parsed, NumberedAccountLabel) and parsed.tool == tool:
            return parsed.label
        return HomeAccountUnresolved(Path.home() / f".{tool}")

    evidence = _role_evidence(store_file().with_name("roles.jsonl"))
    # Never read a present-day login to interpret a past home.
    homes = {"claude": Path.home() / ".claude",
             "codex": Path.home() / ".codex"}

    def historical(entry: MoveEntry, field: str) -> HistoricalAccountLabel:
        tool = entry["tool"]
        home = homes[tool]
        recorded_login = dict(entry).get("login", "") if field == "from" else ""
        from scripts.whoami.account import ResolvedHistoryLabel
        resolver = cast(HistoryResolver, cast(object, import_module("scripts.accounts.accounts")))
        resolved = resolver.resolve_history_label(f"~/.{tool}", entry["at"], login=recorded_login if isinstance(recorded_login, str) else "")
        if isinstance(resolved, ResolvedHistoryLabel):
            return resolved.label
        at = datetime.fromisoformat(entry["at"])
        matching = sorted((item for item in evidence if item.tool == tool and item.at <= at), key=lambda item: item.at)
        return numbered(matching[-1].label, tool) if matching else HomeAccountUnresolved(home)

    entries: list[JournalEntry] = []
    for value in journal:
        raw: dict[str, object] = dict(value)
        if raw["kind"] in ("routed to claude", "routed back", "routing not restored"):
            raw["owner"] = _routing_owner(raw["owner"])
            if raw["kind"] == "routed to claude":
                _ = raw.setdefault("why", raw["cause"])
            entries.append(cast(RoutingEntry, cast(object, raw)))
            continue
        entry = _canonical_entry(cast(MoveEntry, value))
        fields: dict[str, object] = dict(entry)
        for field in ("from", "to", "label"):
            if fields.get(field) in ("default", f"~/.{entry['tool']}", str(homes[entry["tool"]])):
                fields[field] = historical(entry, field)
        if fields["kind"] == "requested":
            _ = fields.setdefault("cause", "refusal")
        entries.append(cast(MoveEntry, cast(object, fields)))
    return entries


def read_journal() -> list[JournalEntry]:
    """Read valid transitions in order, skipping malformed or incomplete lines."""
    try:
        with store_file().open("r+", encoding="utf-8", errors="replace") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_EX)
            lines = worker_names.migrate_locked_journal(source, store_file())
    except FileNotFoundError:
        return []
    entries: list[JournalEntry] = []
    for index, line in enumerate(lines):
        try:
            value = cast(object, json.loads(line))
        except ValueError:
            if index == len(lines) - 1 and not line.endswith("\n"):
                print(f"account moves: skipping torn final line in {store_file()}")
            continue
        if isinstance(value, dict):
            legacy = cast(dict[str, object], value)
            if legacy.get("kind") == "routed to claude":
                _ = legacy.setdefault("why", legacy.get("cause"))
        if _valid(cast(object, value)):
            entries.append(cast(JournalEntry, value))
    return _resolve_journal_entries(entries)


def account_moves(journal: list[JournalEntry]) -> list[MoveEntry]:
    """Narrow journal entries exhaustively to account transitions."""
    entries: list[MoveEntry] = []
    for entry in journal:
        match entry["kind"]:
            case "requested" | "confirmed" | "failed" | "waited" | "wait ended" | "no room" | "room again":
                entries.append(entry)
                continue
            case "routed to claude" | "routed back" | "routing not restored":
                continue
        assert_never(entry)
    return entries


def read_moves() -> list[MoveEntry]:
    """Account transitions only; work routing never participates in a move."""
    return account_moves(read_journal())


def _routing_owner(value: object) -> str | UnknownFallbackOwner:
    from scripts.whoami.note_file import NumberedAccountLabel

    class RoutingOwnerLabels(Protocol):
        AccountsError: type[Exception]

        def numbered_account_label(self, label: str, tool: Literal["claude", "codex"]) -> NumberedAccountLabel: ...

    if isinstance(value, str) and value not in ("unknown", "default"):
        # Resolve the shared input parser lazily: accounts also consumes this journal.
        accounts = cast(RoutingOwnerLabels, cast(object, import_module("scripts.accounts.accounts")))
        try:
            return accounts.numbered_account_label(value, "codex").label
        except accounts.AccountsError:
            pass
    return UnknownFallbackOwner()


def append_routing(entry: RoutingEntry) -> None:
    """Append a routing outcome once, under the account journal's writer lock."""
    if entry["owner"] == "default":
        raise ValueError("name the account by number")
    owner = _routing_owner(entry["owner"])
    saved: dict[str, object] = dict(entry)
    saved["owner"] = owner if isinstance(owner, str) else "unknown"
    path = store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+", encoding="utf-8", errors="replace") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        _ = worker_names.migrate_locked_journal(output, path)
        _ = output.seek(0)
        for line in output:
            try:
                previous = cast(object, json.loads(line))
            except ValueError:
                continue
            if _valid(previous):
                held = cast(dict[str, object], previous)
                if held.get("kind") == entry["kind"] and held.get("fallback_at") == entry["fallback_at"]:
                    return
        _ = output.seek(0, os.SEEK_END)
        append_journal_line(output.fileno(), json.dumps(saved, sort_keys=True))


@dataclass(frozen=True)
class HomeAccountEvidence:
    at: datetime
    tool: Literal["claude", "codex"]
    label: HistoricalAccountLabel


def _role_evidence(path: Path) -> list[HomeAccountEvidence]:
    try:
        lines = worker_names.role_history_lines(path)
    except (OSError, UnicodeError):
        return []
    found: list[HomeAccountEvidence] = []
    for line in lines:
        try:
            raw = cast(object, json.loads(line))
            if not isinstance(raw, dict):
                continue
            row = cast(dict[str, object], raw)
            role, at = row.get("role"), row.get("at")
            if role not in ("unit", "workers") or not isinstance(at, str):
                continue
            tool = "claude" if role == "unit" else "codex"
            account_label = row.get("account_label")
            label = (account_label if row.get("label") == "default" and isinstance(account_label, str)
                     else HomeAccountUnresolved(Path.home() / f".{tool}"))
            stamp = datetime.fromisoformat(at)
            if stamp.tzinfo is not None:
                found.append(HomeAccountEvidence(stamp, tool, label))
        except ValueError:
            continue
    return found
