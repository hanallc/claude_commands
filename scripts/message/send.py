#!/usr/bin/env python3
"""Send one message to a Claude session, a Codex worker, or the user: the one way scripts do it.

Agents holding the SendMessage tool call it directly; scripts call this. The rules
every message follows are /message (~/.claude/commands/message.md).

  send.py --to NAME|uds:<path>|session:<id>|worker:<unit>/<worker>|codex:<thread>
          [--from NAME] [--summary TEXT] [--key KEY [--repeat-minutes N]]
          [--here | --machine HOST] [--timeout SECONDS]
          (--text TEXT | --file PATH | stdin)
  send.py --to user --text-id ID --value KEY=VALUE ... [--through-silence]
                          a registered push; --help lists the templates
  send.py ack KEY       later sends with KEY are skipped
  send.py reopen KEY    forget KEY: acknowledgement and repeat window
  send.py pending [NAME]  print and clear what is kept for this session, or for NAME's
  send.py drain-sockets   deliver recent stuck socket replies; archive files at least 24 h old
  send.py retire --to ADDRESS --from SENDER --key KEY
                          remove obsolete queued messages for ADDRESS; for --to user,
                          also stop the emergency push sent with KEY (see below)
  send.py clear-check     retire each live emergency push whose --clears-when check passes

Pane workers use worker:<unit>/<worker>.

Delivery. A Claude recipient is tried through the postmaster outbox first when
its heartbeat is fresh. A proved miss permits a headless `claude -p` relay.
Unconfirmed postmaster delivery starts no relay and creates no queued copy.
The relay makes one SendMessage call. It runs sonnet in auto mode (haiku cannot run in auto mode),
with hooks, MCP and slash commands off and the caller's CLAUDE_* variables
removed, so it never passes for the session that ran this. Its relay
name is separate from the sender, who is named in the message. Delivery is read from the
SendMessage tool result in the relay's stream, never from what the model says;
the last stream per recipient stays in STATE/relay/<to>.jsonl.
Staff `worker:` paths deliver through observed panes or their holders.
Other `codex:` addresses use a reported herdr pane or the shared Codex daemon.
An absent staff recipient keeps its queue without selecting a session family.
One staff snapshot selects each staff recipient.
Unit names also accept the form from copies.unit_owner. Ambiguous staff names stop delivery.
Use a staff name or staff:<id> to reach the current staff conversation.
An explicit session or registered socket address keeps its native conversation.
A name without a staff row matches a live freestanding Claude session's registry name.
An unmatched name is refused without a relay or queue.
Explicit non-staff addresses keep their transport and queue behavior.
Worker addresses use native IDs from the local or peer roster for staff lookup.
Each message records its runtime sender ID separately from `--from`.
Queues and SSH forwarding retain the original sender ID.
`--here` checks only this machine. `--machine` accepts a configured alias or host name and uses its SSH alias.

`--to user` requires a registered `--text-id`. The template supplies the title and action. `--need`
says what they must do: `note` nothing, `decision` decide while work goes on,
`blocked` act before work can go on, repeated until they acknowledge it. How
it reaches them is `user()`'s business: no caller names the channel.

`--key` names a message. With `--repeat-minutes`, a send with the same key to the
same recipient inside the window is skipped; `ack` skips every later send with
the key until `reopen`. A failed send does not count. A delivered send or a
message kept for later delivery holds the window, so a retry cannot deliver a
queued copy twice. While the user's silence is on, a
kept push with the same key from the same sender replaces the older kept one.
`--through-silence` sends a user push during silence. Review and priority stay the same.
A normal push does not repeat or need acknowledgement.

One event sends one emergency push. An emergency push (`--to user --need blocked`)
with no `--key` gets the key `user-emergency:<sender>:<first 12 hex of the title's
sha256>`. With no `--repeat-minutes`, its window is the push's lifetime on the
phone, pushover.EXPIRE_S, counted from the last push that went out: a repeat is
skipped and its reason names the `send.py retire` that ends the first push.
`retire --to user` forgets the key's last send to the user, so the next send with
it goes out at once. Lower needs (note, decision) have a window only with
`--repeat-minutes`.

An emergency push stops by itself when its cause is fixed. `--clears-when CMD` gives a shell
command, run on this machine, that exits 0 when the cause is gone (`--clears-when github-warm-status`).
A sent push with it is recorded in STATE/emergencies.json: key, sender, check, machine (the host
that posted the push: this one, or --machine HOST), sent and expires (sent + pushover.EXPIRE_S).
`clear-check` runs each record's check with no stdin and a check_timeout() limit. On exit 0 it
retires the push as `retire --to user` does (on the posting host over ssh when that is another
host) and removes the record; it drops an expired record; it never pushes. It prints one line per
record: `retired: KEY from SENDER: ...`, `kept: KEY from SENDER: ...`, `dropped: KEY from SENDER: ...`,
or `FAILED: KEY from SENDER: ...` (the record stays; exit 3). With no records it prints nothing.
The notifier tick (notifier.sh) runs it on both machines when the file exists, and github-warmup
runs it after the keys are warm. An emergency push with neither `--clears-when` nor
`--self-clearing` is refused: REFUSED, exit 2. `--self-clearing` is for a script that retires its
own push (stall_watch.py, escalate.py); sessions that send by hand give `--clears-when`.

(user, 2026-10-05) Outcomes have a header followed by any payload lines and are logged with the full
message text as one JSON line in STATE/log.jsonl:
  SENT: how                                                       exit 0
  RETRY: why     keyed delivery in progress; retry later        exit 6
  SKIPPED: why   acknowledged, inside the repeat window, or       exit 0
                 an emergency push with the key is still live
  PENDING: why   an earlier send has not confirmed acceptance     exit 5
  QUEUED: why    a Claude recipient not reached; kept in          exit 1
                 STATE/queue/, the latest per key, under the
                 recipient's Claude session id, so a rename loses
                 nothing. A known staff member without live transport
                 keeps messages under its current session id.
  FAILED: why    not delivered and not kept: a Codex recipient,   exit 3
                 the user, or ssh to HOST
  REPLACED: why  a newer version replaced this push; not sent     exit 0
  KEPT: why      the user's silence is on; posted when it ends    exit 0
  REFUSED: why   rejected before delivery; not kept              exit 2
  HELD: not sent, failed review; revise and resend, no fallback     exit 4
        also a free-text push that reports an auto-mode refusal or paid credit use, or asks
        the user to type a go: the detail gives the auto_mode.refused, quota.paid_credits
        (quota.paid_credits_queued when a workers move is queued) or session.needs_go call
        to send instead
`retire --to user` forgets the key's last send to the user, prints "retired N queued messages", then "dropped N kept pushes": the
pushes kept during silence with KEY from SENDER (silence.drop), so an obsolete alert never goes out
when silence ends. Then one line from pushover.py cancel, which stops the emergency (--need blocked)
push sent with KEY:
  cancelled: the emergency push for KEY stopped                 exit 0
  nothing to cancel: no emergency push for KEY is repeating      exit 0
  FAILED: the emergency push for KEY may still repeat: why       exit 3
Usage errors, an unreadable --file and an empty message exit 2. STATE is $XDG_STATE_HOME/message, or ~/.local/state/message.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import partial
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, NoReturn, NotRequired, TextIO, TypedDict, cast, get_args

from scripts.herdr.place import HerdrPlace
from scripts.agents import remote as machine_routes
from scripts.herdr import herdr_types
from scripts.lib import process_group
from scripts.production import session_staff, staff, unit_lookup, unit_names
from scripts.lib import conf, models_registry, process_start
from scripts.lib.atomic_file import write_bytes_atomic
from scripts.lib.atomic_file import write_text_atomic as write_atomic
from scripts.notify import decision_priority
from scripts.message import send_budget
from scripts.message.send_result import (
    Outcome as Outcome, EXIT as EXIT, OUTCOME_HEADERS as OUTCOME_HEADERS,
    OutcomeHeader as OutcomeHeader, NoOutcomeHeader as NoOutcomeHeader, parse_outcome as parse_outcome,
    ConfirmedSendOutcome as ConfirmedSendOutcome, UnconfirmedSendOutcome as UnconfirmedSendOutcome,
    send_outcome as send_outcome,
)
from scripts.message import sessions  # noqa: E402
from scripts.message.relay_identity import (
    RelayIdentity, SavedRelaySender, live_relay_identity, read_relay_sender, relay_session_name,
)
from scripts.message import postmaster  # noqa: E402
from scripts.message import codex_daemon  # noqa: E402
if TYPE_CHECKING:
    from scripts.message import roster
    from scripts.accounts import accounts
    from scripts.whoami.account import named_account_environment
from scripts.notify import push_texts  # noqa: E402
from scripts.notify import push_reference
from scripts.message import rca  # noqa: E402
from scripts.message import repeat_guard  # noqa: E402
from scripts.notify import review  # noqa: E402
from scripts.notify import silence  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402
from scripts.lib.probe_session import PROBE_ENV  # noqa: E402
from scripts.notify.user_action import (FIX, REFUSED_BEFORE_SENDING, ActionRefused, ActionRequired, ActionUnstated,
                         UserAction, NoActionRequired, action_conflicts, first_line, named_places, parse_user_action,
                         SessionLine, split_session, session_prefix, typed_texts)  # noqa: E402

STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "message"
CLAUDE = Path.home() / ".local" / "bin" / "claude"
SESSIONS = Path.home() / ".claude" / "sessions"
USER = "user"
USER_CHANNEL = Path(__file__).resolve().parent.parent / "notify" / "pushover.py"
# On the remote host: its own interpreter shim and copy of this script.
REMOTE = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send.py"'
MODEL = "sonnet"
AUTO_MODE_TEXT = "auto_mode.refused"
AUTO_MODE_CALL = ('~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to user --text-id auto_mode.refused'
                  + ' --value session=<session> --value machine=<machine>'
                  + ' --value "command=<a refused command, short: the program and its key arguments>"'
                  + ' [one --value command=... for each] --value "waits=<a sentence: what waits; no instruction>"'
                  + ' [--value "rule=<the allow rule>"]')
# The push shows a longer refused command cut to its first 89 characters and "…".
REFUSED_COMMAND_LENGTH = 90
PAID_CREDITS_TEXT = "quota.paid_credits"
PAID_CREDITS_CALL = ('~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to user --text-id quota.paid_credits'
                     + ' --value account=<account> --value machine=<machine> --value credits=<paid credits left>'
                     + ' --value "refill=<when its plan refills>" --value "next_reset=<the next Codex reset>"')
PAID_CREDITS_QUEUED_TEXT = "quota.paid_credits_queued"
PAID_CREDITS_QUEUED_CALL = ('~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to user'
                            + ' --text-id quota.paid_credits_queued --value account=<account> --value machine=<machine>'
                            + ' --value credits=<paid credits left> --value "refill=<when its plan refills>"')
PAID_CREDITS_REPORT = re.compile(r"\bpaid[\s-]+credits?\b|\bcodex_credit_accounts\b", re.IGNORECASE)
PAID_CREDITS_HELD = ("not sent: this push reports paid credit use. Send it with the quota.paid_credits template: "
                     + PAID_CREDITS_CALL + ". When a workers move is queued, send quota.paid_credits_queued: "
                     + PAID_CREDITS_QUEUED_CALL)
NEEDS_GO_TEXT = "session.needs_go"
NEEDS_GO_CALL = ('~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to user --text-id session.needs_go'
                 + ' --value session=<session> --value machine=<machine> --value "go=<the Go: line to type>"'
                 + ' --value "why=<why the session waits>" --value "does=<what the go does>"')
# "type: Go: ...", "until you type", "waits for your go": a free-text push that asks for a go.
NEEDS_GO_REPORT = re.compile(r"\btype:?\s+[\"'`\u201c]?Go:|\buntil you (allow|type)\b|\bwaits? for your go\b", re.IGNORECASE)
NEEDS_GO_HELD = ("not sent: this push asks the user to type a go. Send it with the session.needs_go template: "
                 + NEEDS_GO_CALL)
# Registered pushes a session or script sends with --text-id; `send.py --help` lists them.
SESSION_TEMPLATES = {
    "decision.choices": "a decision with structured choices and a recommendation; see review.py show decision.choices.",
    "review_watch.acknowledge": "a reminder to acknowledge a review report; see review.py show review_watch.acknowledge.",
    AUTO_MODE_TEXT: ("auto mode refused a command; the session continues its other work. "
                     + "Send auto_mode.refused only when the refused step holds work and the user has no step for it yet. "
                     + "If the user already has one, send nothing. "
                     + "With other work to run, go on with it and leave out --need blocked. " + AUTO_MODE_CALL),
    PAID_CREDITS_TEXT: ("an account now spends paid credits; a script with no Claude session names itself with --from. "
                        + PAID_CREDITS_CALL),
    PAID_CREDITS_QUEUED_TEXT: ("an account now spends paid credits and the user queued a workers move; workers wait for"
                               + " the move, never go to Claude. " + PAID_CREDITS_QUEUED_CALL),
    NEEDS_GO_TEXT: "a session is stopped until the user types a go in its pane. " + NEEDS_GO_CALL,
    rca.STARTED_TEXT: ("a unit starts to fix the cause of a permission stop or refusal. Sent by rca.py start;"
                       + " sessions run rca.py, not this: " + rca.CALL + " start <id> --owner <unit>"),
    rca.FIXED_TEXT: ("the fix merged; that permission does not stop or refuse the session again. Sent by rca.py fixed;"
                     + " sessions run rca.py, not this: " + rca.CALL
                     + ' fixed <id> --cause "<cause>" --fix "<fix>" --commit <sha>'),
    rca.REWRITTEN_TEXT: ("the push review's rewrite of a fixed text it refused twice. Sent by rca.py fixed;"
                         + " sessions run rca.py, not this."),
    "nate.adhoc": "an update the user asked for with /nate adhoc; see /nate.",
}
AUTO_MODE_REPORT = re.compile(r"\bauto[\s-]mode\b", re.IGNORECASE)
AUTO_MODE_REFUSAL = re.compile(r"\b(refused|denied|stopped)\b", re.IGNORECASE)
# The rca.py pushes describe permission stops or refusals. A cause can name an auto-mode refusal.
RCA_TEXTS = frozenset({rca.STARTED_TEXT, rca.FIXED_TEXT, rca.REWRITTEN_TEXT})
AUTO_MODE_HELD = ("not sent: this push reports an auto-mode refusal. Send it with the auto_mode.refused template: "
                  + AUTO_MODE_CALL)
PERMISSION_MODE = "auto"
# Exec preserves this PID: Claude's cc-socks/<pid>.sock belongs to the recorded process.
# Publish before Claude starts, so even an immediate reply can resolve its sender.
RELAY_EXEC = """import os, sys
from pathlib import Path
from scripts.message.relay_identity import publish_relay_identity
publish_relay_identity(os.getpid(), sys.argv[2], Path(sys.argv[1]))
os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
"""
EMERGENCIES = "emergencies.json"
NO_CLEAR_STEP = ("an emergency push (--need blocked) repeats on the phone until it is cancelled, and this one has no"
                 + ' way to clear. Add --clears-when "<command that exits 0 when fixed>", such as'
                 + " --clears-when github-warm-status: send.py then cancels the push when that command exits 0")


def waits_review_seconds() -> float:
    """Push review budget in seconds."""
    return conf.read("send").number("waits_review_seconds")


def delivery_timeout() -> float:
    """Default delivery budget in seconds."""
    return conf.read("send").number("delivery_timeout")


def kill_grace() -> float:
    """Relay shutdown grace in seconds."""
    return conf.read("send").number("kill_grace")


def user_cancel_timeout() -> float:
    """Emergency cancellation budget in seconds."""
    return conf.read("send").number("user_cancel_timeout")


def check_timeout() -> float:
    """Emergency check budget in seconds."""
    return conf.read("send").number("check_timeout")


Need = Literal["note", "decision", "blocked"]
CHANNEL_PRIORITY: dict[Need, str] = {"note": "0", "decision": "0", "blocked": "2"}
_ACKNOWLEDGED_AT = " acknowledged at "


def acknowledged_skip_reason(key: str, at: str) -> str:
    """Name the acknowledgement that prevents a keyed send."""
    return f"key {key!r}{_ACKNOWLEDGED_AT}{at}; `send.py reopen` to resume"


def is_acknowledged_skip(reason: str) -> bool:
    """Distinguish an acknowledged skip from a repeat-window skip."""
    return _ACKNOWLEDGED_AT in reason

LogEntry = TypedDict("LogEntry", {"time": str, "machine": str, "from": str, "to": str, "key": str | None,
                                  "summary": str, "text": str, "outcome": Outcome, "detail": str,
                                  "via": NotRequired[Literal["postmaster", "relay", "queue"]],
                                  "postmaster": NotRequired[postmaster.PostmasterStatus],
                                  "seconds": NotRequired[float], "human": NotRequired[bool], "receiver": NotRequired[str],
                                  "sender_native_id": NotRequired[str]})


class SocketRecoveryAttempt(TypedDict):
    key: str
    time: str


class QueuedProcessGeneration(TypedDict):
    pid: int
    started: str
    socket: str


class QueuedClaudeMailbox(TypedDict):
    kind: Literal["conversation", "staff"]
    machine: str
    conversation: str
    staff_id: NotRequired[str]
    generation: NotRequired[QueuedProcessGeneration]


class SavedRelayProcess(TypedDict):
    kind: Literal["relay-process"]
    machine: str
    generation: QueuedProcessGeneration
    reply_to: NotRequired[QueuedClaudeMailbox]


KeyedMessage = TypedDict(
    "KeyedMessage",
    {
        "time": str,
        "from": str,
        "to": str,
        "key": str,
        "summary": str,
        "text": str,
        "reason": str,
        "shown_sender": NotRequired[str],
        "sender_native_id": NotRequired[str],
        "socket_recovery": NotRequired[str],
        "stable_recipient": NotRequired[str],
        "claude_recipient": NotRequired[QueuedClaudeMailbox],
        "send_order": NotRequired[int],
    },
)
UnkeyedMessage = TypedDict(
    "UnkeyedMessage",
    {
        "time": str,
        "from": str,
        "to": str,
        "key": None,
        "summary": str,
        "text": str,
        "reason": str,
        "shown_sender": NotRequired[str],
        "sender_native_id": NotRequired[str],
        "socket_recovery": NotRequired[str],
        "stable_recipient": NotRequired[str],
        "claude_recipient": NotRequired[QueuedClaudeMailbox],
        "send_order": NotRequired[int],
    },
)


QueuedMessage = KeyedMessage | UnkeyedMessage


class PostmasterOwner(TypedDict):
    entry_id: str
    owner_pid: int
    owner_start: str


class ReservedPostmaster(PostmasterOwner):
    stage: Literal["reserved"]


class PublishingPostmaster(PostmasterOwner):
    stage: Literal["publishing"]


class DirectRelayInProgress(PostmasterOwner):
    stage: Literal["direct-relay"]


class RelayRetryBlocked(PostmasterOwner):
    """The postmaster allowed fallback. Older direct relays also used this stage."""

    stage: Literal["relaying"]


class RelayAcceptanceUnconfirmed(PostmasterOwner):
    """The relay returned without proof of acceptance or non-delivery."""

    stage: Literal["relay-unconfirmed"]


class ResolvedPostmaster(PostmasterOwner):
    stage: Literal["resolved"]
    outcome: Literal["delivered", "queued", "failed", "expired", "withdrawn"]


PostmasterReservation = (
    ReservedPostmaster | PublishingPostmaster | DirectRelayInProgress | RelayRetryBlocked
    | RelayAcceptanceUnconfirmed | ResolvedPostmaster
)


class PendingDelivery(TypedDict):
    started: str
    limit_seconds: float
    accepted_before: NotRequired[str]
    owner_pid: NotRequired[int]
    owner_start: NotRequired[str]
    clearing: NotRequired[Emergency]
    recipient: NotRequired[QueuedClaudeMailbox | SavedRelayProcess]
    postmaster: NotRequired[PostmasterReservation]


class KeyState(TypedDict):
    # Map each recipient to its claim time. A failed send releases its claim.
    last: dict[str, str]
    pending: NotRequired[dict[str, str | PendingDelivery]]
    acknowledged: NotRequired[str]
    clearing: NotRequired[Emergency]


KeyStates = dict[str, KeyState]


@dataclass(frozen=True)
class ClaudeSender:
    session_id: str


@dataclass(frozen=True)
class CodexSender:
    thread_id: str


@dataclass(frozen=True)
class UnavailableSender:
    """The message has no recorded runtime identity."""


SenderStamp = ClaudeSender | CodexSender | UnavailableSender


@dataclass(frozen=True)
class RuntimeSender:
    """Read the sender identity from this process."""


@dataclass(frozen=True)
class ForwardedSender:
    stamp: SenderStamp


@dataclass(frozen=True)
class QueueOrder:
    """Order one send before its delivery can finish."""
    value: int


def new_queue_order() -> QueueOrder:
    """Issue a shared sequence number under the queue lock."""
    with locked():
        path = STATE / "queue-order"
        try:
            previous = int(path.read_text(encoding="utf-8"))
            if previous < 0:
                raise ValueError("negative queue order")
        except (OSError, ValueError):
            previous = 0
            for queue in (STATE / "queue").glob("*.jsonl"):
                try:
                    entries = read_queue(queue)
                except (OSError, UnicodeError):
                    continue
                previous = max(previous, max((entry.get("send_order", 0) for entry in entries), default=0))
        order = QueueOrder(previous + 1)
        write_atomic(path, str(order.value) + "\n")
        return order


@dataclass(frozen=True)
class LegacyQueueOrder:
    """Keep the queue rules for a caller without an attempt order."""


LEGACY_QUEUE_ORDER = LegacyQueueOrder()


@dataclass(frozen=True)
class ConversationRecipient:
    machine: str
    conversation: str

    @property
    def address(self) -> str:
        return f"session:{self.conversation}"


@dataclass(frozen=True)
class StaffClaudeRecipient:
    staff_id: staff.StaffId
    current: ConversationRecipient


ClaudeMailbox = ConversationRecipient | StaffClaudeRecipient


@dataclass(frozen=True)
class NoRelayReplyMailbox:
    """The relay has no known conversation for recovery."""


NO_RELAY_REPLY_MAILBOX = NoRelayReplyMailbox()


@dataclass(frozen=True)
class RelayProcessRecipient:
    machine: str
    pid: int
    started: str
    reply_to: ConversationRecipient | NoRelayReplyMailbox = NO_RELAY_REPLY_MAILBOX


ClaudeRecipientIdentity = ConversationRecipient | RelayProcessRecipient | StaffClaudeRecipient


@dataclass(frozen=True)
class FixedStaffLookup:
    to: staff.StaffId
    kind: staff.Kind

    def member(self, membership: staff.StaffSnapshot) -> staff.StaffRow | RecipientLookupFailed:
        for row in staff.active(membership, self.kind):
            if row.id == self.to and not isinstance(row.retirement, staff.RetiredProducer):
                return row
        return RecipientLookupFailed(f"staff ID {self.to} has no current {self.kind} row")


@dataclass(frozen=True)
class NoFixedStaffLookup:
    """Allow delivery without a fixed staff requirement."""


NO_FIXED_STAFF_LOOKUP = NoFixedStaffLookup()


@dataclass(frozen=True)
class NoPaneReportCheck:
    """The relay does not use the missing argv fallback."""


@dataclass(frozen=True)
class ClaudeDeliveryAttempt:
    recipient: ClaudeRecipientIdentity
    pid: int
    started: str
    socket: str
    staff_requirement: FixedStaffLookup | NoFixedStaffLookup = NO_FIXED_STAFF_LOOKUP
    fallback_pane: HerdrPlace | NoPaneReportCheck = NoPaneReportCheck()


@dataclass(frozen=True)
class NoClaudeDeliveryAttempt:
    """This message has no bound Unix socket delivery."""


def conversation_recipient(mailbox: ClaudeMailbox) -> ConversationRecipient:
    return mailbox.current if isinstance(mailbox, StaffClaudeRecipient) else mailbox


def _claude_recipient_key(recipient: ClaudeRecipientIdentity) -> str:
    if isinstance(recipient, StaffClaudeRecipient):
        return staff_queue_key(recipient.staff_id)
    if isinstance(recipient, RelayProcessRecipient):
        return f"relay-process:{recipient.machine}:{recipient.pid}:{recipient.started}"
    return recipient.address if recipient.machine == machine() else f"{recipient.machine}/{recipient.address}"


def stable_mailbox_key(mailbox: ClaudeMailbox) -> str:
    return staff_queue_key(mailbox.staff_id) if isinstance(mailbox, StaffClaudeRecipient) else mailbox.address


def _mailbox_record(mailbox: ClaudeMailbox) -> QueuedClaudeMailbox:
    target = conversation_recipient(mailbox)
    if isinstance(mailbox, StaffClaudeRecipient):
        return QueuedClaudeMailbox(kind="staff", machine=target.machine, conversation=target.conversation,
                                   staff_id=str(mailbox.staff_id))
    return QueuedClaudeMailbox(kind="conversation", machine=target.machine, conversation=target.conversation)


def _generation_record(attempt: ClaudeDeliveryAttempt) -> QueuedProcessGeneration:
    return QueuedProcessGeneration(pid=attempt.pid, started=attempt.started, socket=attempt.socket)


def _attempt_record(attempt: ClaudeDeliveryAttempt) -> QueuedClaudeMailbox | SavedRelayProcess:
    generation = _generation_record(attempt)
    recipient = attempt.recipient
    if isinstance(recipient, RelayProcessRecipient):
        saved = SavedRelayProcess(kind="relay-process", machine=recipient.machine, generation=generation)
        if not isinstance(recipient.reply_to, NoRelayReplyMailbox):
            saved["reply_to"] = _mailbox_record(recipient.reply_to)
        return saved
    mailbox = _mailbox_record(recipient)
    mailbox["generation"] = generation
    return mailbox


@dataclass(frozen=True)
class Message:
    """Routing identity and body with sending-machine display text, or the raw sender for legacy callers."""
    to: str
    sender: str
    summary: str
    text: str
    key: str | None
    shown_sender: str = ""
    human: bool = False
    stamp: SenderStamp = UnavailableSender()
    claude_attempt: ClaudeDeliveryAttempt | NoClaudeDeliveryAttempt = field(
        default=NoClaudeDeliveryAttempt(), compare=False, repr=False)
    sent_header: str = field(default_factory=lambda: sent_line(now()), compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.shown_sender:
            object.__setattr__(self, "shown_sender", self.sender)


@dataclass(frozen=True)
class SenderNotResolved:
    pass


@dataclass(frozen=True)
class ShownSender:
    text: str


@dataclass(frozen=True)
class NoHeldRevision:
    pass


@dataclass(frozen=True)
class HeldRevision:
    source: str
    identity: str
    machine: str = ""


RevisionHold = NoHeldRevision | HeldRevision
NO_HELD_REVISION = NoHeldRevision()


@dataclass(frozen=True)
class FailedPush:
    title: str
    action: UserAction
    body: str
    hold: RevisionHold = NO_HELD_REVISION
    reviewed: bool = False
    need: Need = "note"
    held: bool = False
    text_id: str = ""
    sender: str = ""

    @property
    def fallback_allowed(self) -> bool:
        return (self.reviewed and not self.held and self.need == "blocked"
                and any(entry.id == self.text_id for entry in push_texts.catalog()))

    @property
    def message(self) -> str:
        session, body = split_session(self.body)
        return first_line(self.action, session) + "\n" + body

    @property
    def text(self) -> str:
        return (f"Title: {self.title}\n{self.message}\n\n"
                + f"Sent by: {self.sender} · Template: {self.text_id}")


@dataclass(frozen=True)
class NoFailedPush:
    pass


FailureText = FailedPush | NoFailedPush
NO_FAILED_PUSH = NoFailedPush()


class ReviewedPushRecord(TypedDict):
    title: str
    action: str | None
    body: str
    held: NotRequired[bool]
    held_source: NotRequired[str]
    held_identity: NotRequired[str]
    held_machine: NotRequired[str]
    text_id: NotRequired[str]
    sender: NotRequired[str]


class FailedPushRecord(ReviewedPushRecord):
    time: float
    message: str
    fallback: str
    reviewed: bool
    need: Need


class FailedPushTransportRecord(ReviewedPushRecord):
    reviewed: bool
    need: Need


UNREVIEWED_FALLBACK = ("Only a reviewed blocked push with a registered template can use a fallback. "
    "Use send.py with a registered template after you repair the delivery failure.")


HELD_FALLBACK = "This push was not sent because it failed review. Revise it and resend it with send.py; no fallback is allowed."


def failure_detail(reason: str, failed: FailedPush) -> str:
    if failed.held:
        return reason.partition(" fallback text:")[0] + " " + HELD_FALLBACK
    reason = reason.partition(" fallback text:")[0].partition(" " + UNREVIEWED_FALLBACK)[0]
    return reason + (" fallback text:\n" + failed.text if failed.fallback_allowed else " " + UNREVIEWED_FALLBACK)


class Result(NamedTuple):
    outcome: Outcome
    detail: str
    failure: FailureText = NO_FAILED_PUSH
    receiver: str = ""


class PaneTextDelivered(Result):
    """The observed worker pane accepted the text input."""


class WorkerMessageUnconfirmed(Result):
    """The ordinary message may have reached the worker pane."""


class WorkerResultNotSubmitted(Result):
    """The prompt failed before it could send input."""


class WorkerResultUnconfirmed(Result):
    """The prompt may have sent input, and the transcript receipt is missing."""


class RecordedDeliveryAcceptance(Result):
    """This keyed send has durable acceptance; later bookkeeping must not repeat it."""


class ConfirmedSocketMiss(Result):
    """No message bytes reached the selected socket."""


class UncertainRelayDelivery(Result):
    """A repeat could send another copy of an unconfirmed delivery."""


class UnconfirmedPostmasterDelivery(UncertainRelayDelivery):
    """The postmaster owns delivery; retain the key and do not send again."""


POSTMASTER_UNCONFIRMED = "postmaster delivery is unconfirmed; the message may already have arrived; do not resend it by hand"


@dataclass(frozen=True)
class NoClaudeRoute:
    pass


@dataclass(frozen=True)
class ClaudeRoute:
    via: Literal["postmaster", "relay", "queue"]
    postmaster: postmaster.PostmasterStatus
    seconds: float


Route = NoClaudeRoute | ClaudeRoute
NO_CLAUDE_ROUTE = NoClaudeRoute()


class RelayAttempt(NamedTuple):
    message: Message


@dataclass(frozen=True)
class NoQueuedProcessGeneration:
    """No Claude process generation was proved for this mailbox."""


NO_QUEUED_PROCESS_GENERATION = NoQueuedProcessGeneration()


class SessionIdWithoutLiveRecipient(NamedTuple):
    recipient: ClaudeMailbox
    generation: session_staff.ProvedProcessGeneration | NoQueuedProcessGeneration = NO_QUEUED_PROCESS_GENERATION

    @property
    def address(self) -> str:
        return conversation_recipient(self.recipient).address


class ClaudeRoutingFailed(NamedTuple):
    failure: unit_lookup.SocketUnavailable


RelayPlan = RelayAttempt | SessionIdWithoutLiveRecipient | ClaudeRoutingFailed


@dataclass(frozen=True)
class NoUserRecipient:
    pass


MessageUserAction = UserAction | NoUserRecipient


@dataclass(frozen=True)
class ClaudeRecipient:
    address: str


@dataclass(frozen=True)
class CodexSessionRecipient:
    address: str
    thread_id: str
    name: str
    daemon: codex_daemon.CodexDaemon


@dataclass(frozen=True)
class CodexPaneRecipient:
    address: str
    place: HerdrPlace
    thread_id: str


@dataclass(frozen=True)
class QueuedStaffRecipient:
    address: str
    name: str
    session_id: str


@dataclass(frozen=True)
class PaneWorkerRecipient:
    row: staff.StaffRow


SessionRecipient = ClaudeRecipient | CodexSessionRecipient | CodexPaneRecipient


@dataclass(frozen=True)
class UserDelivery:
    """Deliver through the user-notification gate."""


@dataclass(frozen=True)
class RemoteDelivery:
    host: str
    address: str
    selected_conversation: bool = False


@dataclass(frozen=True)
class RecipientLookupFailed:
    reason: str


class RecipientNameMissing(SystemExit):
    """The recipient name has no staff row or live session."""

    def __init__(self, reason: str) -> None:
        super().__init__(2)
        self.reason: str = reason


@dataclass(frozen=True)
class NoStaffRecipient:
    """The target keeps its transport resolution because no staff row matches."""


@dataclass(frozen=True)
class NoStaffSender:
    """The environment does not identify a current staff sender."""


@dataclass(frozen=True)
class StableRecipient:
    """Identify the recipient across changes to its transport address."""

    value: str


@dataclass(frozen=True)
class TransportQueueOwner:
    """Only the transport address identifies the queue owner."""


TRANSPORT_QUEUE_OWNER = TransportQueueOwner()


DeliveryRoute = SessionRecipient | QueuedStaffRecipient | PaneWorkerRecipient | UserDelivery | RemoteDelivery | RecipientLookupFailed


@dataclass(frozen=True)
class LocalOnly:
    """Resolve and deliver only here."""


@dataclass(frozen=True)
class LocalThenPeer:
    """Ask the peer only after proving this machine has no match."""


@dataclass(frozen=True)
class NamedMachine:
    host: str


RecipientResolutionScope = LocalOnly | LocalThenPeer | NamedMachine


@dataclass(frozen=True)
class RecipientMatches:
    entries: list[roster.RosterEntry]
    destination: LocalOnly | NamedMachine
    problems: list[str]


@dataclass(frozen=True)
class LocalRosterIncomplete:
    problems: list[str]


@dataclass(frozen=True)
class RecipientSearchFailed:
    reason: str
    problems: list[str]


@dataclass(frozen=True)
class RosterLookup:
    to: str


@dataclass(frozen=True)
class ForwardedConversation:
    to: str


@dataclass(frozen=True)
class WorkerHolderRecipient:
    """Keep the holder from the worker's staff snapshot."""

    to: str
    staff_id: staff.StaffId


RecipientSelection = RosterLookup | ForwardedConversation | WorkerHolderRecipient | FixedStaffLookup


@dataclass(frozen=True)
class ExpectedStaffSession:
    session_id: str


@dataclass(frozen=True)
class VerifiedStaffDestination:
    address: str


@dataclass(frozen=True)
class TransportRecipientIdentity:
    """Keep the established per-address key identity."""


@dataclass(frozen=True)
class LogicalRecipientIdentity:
    """A stable recipient whose transport address may change."""
    name: str


class Options(NamedTuple):
    selection: RecipientSelection
    sender: str | None
    summary: str | None
    key: str | None
    repeat_minutes: float | None
    scope: RecipientResolutionScope
    timeout: float
    need: Need
    action: MessageUserAction
    text: str | None
    file: str | None
    answerable: bool = False
    text_id: str = ""
    values: tuple[str, ...] = ()
    # The auto_mode.refused commands as given: rca.py opens its case with them.
    commands: tuple[str, ...] = ()
    # An emergency push's shell check that exits 0 when its cause is gone; "" for none.
    clears_when: str = ""
    # The sending script retires its own emergency push.
    self_clearing: bool = False
    shown_sender: ShownSender | SenderNotResolved = SenderNotResolved()
    human: bool = False
    no_accounting: bool = False
    report_recipient: bool = False
    sender_origin: RuntimeSender | ForwardedSender = RuntimeSender()
    due: str = ""
    reference: push_reference.PushReference = push_reference.NO_REFERENCE

    recipient_identity: TransportRecipientIdentity | LogicalRecipientIdentity = TransportRecipientIdentity()
    silence_policy: silence.SilencePolicy = silence.SilencePolicy.KEEP_DURING_SILENCE

    @property
    def to(self) -> str:
        return self.selection.to


# ---------------------------------------------------------------------------
# State: one lock guards the key states and the queues. It is never held across
# a delivery, so a slow relay blocks nobody.
# ---------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def machine() -> str:
    return socket.gethostname().split(".")[0]


def file_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "._- " else "_" for char in name) or "_"


def loads(text: str) -> object:
    """json.loads as an `object`, so callers narrow before use."""
    try:
        return json.loads(text)  # pyright: ignore[reportAny]
    except ValueError:
        return None


def as_dict(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


@contextmanager
def locked() -> Generator[None]:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / ".lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_key_states(states: KeyStates) -> None:
    """Replace and sync the key records before their next external step."""
    write_atomic(STATE / "keys.json", json.dumps(states, indent=2, sort_keys=True) + "\n")


@contextmanager
def key_states() -> Generator[KeyStates]:
    """The key states, under the lock, written back on exit when changed."""
    path = STATE / "keys.json"
    with locked():
        try:
            states = cast(KeyStates, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            states = {}
        before = json.dumps(states, sort_keys=True)
        yield states
        if json.dumps(states, sort_keys=True) != before:
            write_key_states(states)


def queue_path(key: str) -> Path:
    return STATE / "queue" / f"{file_name(key)}.jsonl"


def session_key(session: sessions.SessionRecord) -> str:
    return f"session-{session['sessionId']}"


def staff_queue_key(identity: staff.StaffId) -> str:
    """Keep a member's queue key fixed across names and current sessions."""
    return f"staff:{identity}"


def queue_for(to: str, *, membership: staff.StaffSnapshot | None = None) -> Path:
    """Use a staff ID for names and a native key for explicit addresses."""
    if to.startswith("session:") and len(to) > len("session:"):
        return queue_path(f"session-{to.removeprefix('session:')}")
    if to.startswith("uds:"):
        session = sessions.addressed(to, sessions.live_sessions())
        if session is None:
            raise ValueError(dead_socket_reason(to))
        return queue_path(session_key(session))
    member = sessions.staff_recipient(to, staff.snapshot() if membership is None else membership)
    if isinstance(member, sessions.StaffNameMissing):
        raise ValueError(f"no staff row for {to!r}")
    return queue_path(staff_queue_key(member.id))


def _valid_queued_mailbox(value: object) -> bool:
    mailbox = as_dict(value)
    kind = mailbox.get("kind")
    if (kind not in ("conversation", "staff")
            or not all(as_str(mailbox.get(key)) for key in ("machine", "conversation"))
            or kind == "staff" and not as_str(mailbox.get("staff_id"))):
        return False
    if "generation" not in mailbox:
        return True
    generation = as_dict(mailbox["generation"])
    pid = generation.get("pid")
    return (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and bool(as_str(generation.get("started"))) and isinstance(generation.get("socket"), str))


def _queued_message(value: object) -> QueuedMessage | None:
    values = as_dict(value)
    string_fields = ("time", "from", "to", "summary", "text", "reason")
    if not all(isinstance(values.get(field), str) for field in string_fields):
        return None
    key = values.get("key")
    if "shown_sender" in values and not isinstance(values["shown_sender"], str):
        return None
    if "stable_recipient" in values and not isinstance(values["stable_recipient"], str):
        return None
    if "claude_recipient" in values and not _valid_queued_mailbox(values["claude_recipient"]):
        return None
    order = values.get("send_order")
    if "send_order" in values and (not isinstance(order, int) or isinstance(order, bool) or order < 0):
        return None
    if "sender_native_id" in values:
        try:
            _ = read_sender_stamp(values["sender_native_id"])
        except ValueError:
            return None
    if key is None:
        return cast(UnkeyedMessage, cast(object, values))
    if isinstance(key, str):
        return cast(KeyedMessage, cast(object, values))
    return None


@dataclass(frozen=True)
class ReadableQueueRow:
    message: QueuedMessage
    raw: bytes


@dataclass(frozen=True)
class QueueRowNeedsInspection:
    raw: bytes


QueueRow = ReadableQueueRow | QueueRowNeedsInspection


def _read_queue_rows(path: Path) -> list[QueueRow]:
    try:
        text = path.read_text(encoding="utf-8", newline="")
    except FileNotFoundError:
        return []
    rows: list[QueueRow] = []
    with StringIO(text, newline="\n") as queue:
        for line in queue:
            raw = line.encode("utf-8")
            entry = _queued_message(loads(line))
            rows.append(QueueRowNeedsInspection(raw) if entry is None else ReadableQueueRow(entry, raw))
    return rows


def read_queue(path: Path) -> list[QueuedMessage]:
    return [row.message for row in _read_queue_rows(path) if isinstance(row, ReadableQueueRow)]


def _new_queue_row(entry: QueuedMessage) -> ReadableQueueRow:
    return ReadableQueueRow(entry, (json.dumps(entry) + "\n").encode("utf-8"))


def _matches_queued_key(entry: QueuedMessage, key: str,
                        recipient: StableRecipient | TransportQueueOwner, *, own_path: bool) -> bool:
    if entry["key"] != key:
        return False
    if "claude_recipient" in entry or "stable_recipient" in entry:
        owner = _saved_mailbox_key(entry)
        return isinstance(recipient, StableRecipient) and owner == recipient.value
    if isinstance(recipient, StableRecipient) and entry["to"].startswith(("session:", "staff:")):
        return own_path and entry["to"] == recipient.value
    return own_path


def _queued_not_newer(entry: QueuedMessage, order: QueueOrder | LegacyQueueOrder) -> bool:
    """Keep legacy handling when either side has no attempt order."""
    return isinstance(order, LegacyQueueOrder) or "send_order" not in entry or entry["send_order"] <= order.value


def enqueue(message: Message, reason: str, at: datetime, *,
            recipient: StableRecipient | TransportQueueOwner = TRANSPORT_QUEUE_OWNER,
            order: QueueOrder | LegacyQueueOrder = LEGACY_QUEUE_ORDER,
            membership: staff.StaffSnapshot | None = None,
            mailbox: ClaudeMailbox | NoRelayReplyMailbox = NO_RELAY_REPLY_MAILBOX,
            generation: session_staff.ProvedProcessGeneration | NoQueuedProcessGeneration = NO_QUEUED_PROCESS_GENERATION
            ) -> None:
    """Keep an undelivered message under its selected mailbox."""
    if isinstance(mailbox, NoRelayReplyMailbox):
        path = queue_for(message.to, membership=membership)
        if isinstance(recipient, StableRecipient) and recipient.value.startswith("staff:"):
            message = replace(message, to=recipient.value)
    else:
        owner = stable_mailbox_key(mailbox)
        path = queue_path(owner if isinstance(mailbox, StaffClaudeRecipient) else f"session-{mailbox.conversation}")
        message = replace(message, to=owner)
        recipient = StableRecipient(owner)
    if message.key is None:
        entry: QueuedMessage = {
            "time": at.isoformat(),
            "to": message.to,
            "key": None,
            "summary": message.summary,
            "text": message.text,
            "reason": reason,
            "shown_sender": message.shown_sender,
            "sender_native_id": sender_native_id(message.stamp),
            "from": message.sender,
        }
    else:
        entry = {
            "time": at.isoformat(),
            "to": message.to,
            "key": message.key,
            "summary": message.summary,
            "text": message.text,
            "reason": reason,
            "shown_sender": message.shown_sender,
            "sender_native_id": sender_native_id(message.stamp),
            "from": message.sender,
        }
    if isinstance(recipient, StableRecipient):
        entry["stable_recipient"] = recipient.value
    if not isinstance(mailbox, NoRelayReplyMailbox):
        saved = _mailbox_record(mailbox)
        attempt = message.claude_attempt
        if isinstance(attempt, ClaudeDeliveryAttempt) and not isinstance(attempt.recipient, RelayProcessRecipient):
            saved["generation"] = _generation_record(attempt)
        elif isinstance(generation, session_staff.ProvedProcessGeneration):
            saved["generation"] = QueuedProcessGeneration(pid=generation.pid, started=generation.started, socket="")
        entry["claude_recipient"] = saved
    if isinstance(order, QueueOrder):
        entry["send_order"] = order.value
    with locked():
        path.parent.mkdir(parents=True, exist_ok=True)
        queues = [(path, _read_queue_rows(path))]
        if message.key is not None and isinstance(recipient, StableRecipient):
            for previous in path.parent.glob("*.jsonl"):
                if previous == path:
                    continue
                try:
                    queues.append((previous, _read_queue_rows(previous)))
                except (OSError, UnicodeError):
                    continue
        if message.key is not None and any(
            isinstance(old, ReadableQueueRow)
            and _matches_queued_key(old.message, message.key, recipient, own_path=previous == path)
            and not _queued_not_newer(old.message, order)
            for previous, entries in queues for old in entries
        ):
            return
        kept = [old for old in queues[0][1] if message.key is None
                or isinstance(old, QueueRowNeedsInspection)
                or not _matches_queued_key(old.message, message.key, recipient, own_path=True)]
        insertion = len(kept)
        while insertion and isinstance(kept[insertion - 1], QueueRowNeedsInspection):
            insertion -= 1
        if insertion == len(kept) and kept and not kept[-1].raw.endswith(b"\n"):
            insertion -= 1
        kept.insert(insertion, _new_queue_row(entry))
        _write_queue(path, kept)
        if message.key is not None and isinstance(recipient, StableRecipient):
            for previous, entries in queues[1:]:
                remaining = [old for old in entries
                             if isinstance(old, QueueRowNeedsInspection)
                             or not _matches_queued_key(old.message, message.key, recipient, own_path=False)]
                if len(remaining) != len(entries):
                    _rewrite_queue(previous, remaining)


def _write_queue(path: Path, kept: list[QueueRow]) -> None:
    """Keep the original bytes of every retained row."""
    if kept:
        write_bytes_atomic(path, b"".join(row.raw for row in kept))
    else:
        path.unlink(missing_ok=True)


def _rewrite_queue(path: Path, kept: list[QueueRow]) -> None:
    _write_queue(path, kept)


def _queue_paths_for_address(to: str, membership: staff.StaffSnapshot | None = None) -> list[Path]:
    if to == USER:
        return [queue_path(to)]
    if to.startswith("uds:"):
        session = sessions.addressed(to, sessions.live_sessions())
        if session is None:
            return [queue_path(to)]
        current_session = session["sessionId"]
        destination = queue_path(session_key(session))
    elif to.startswith("session:"):
        current_session = to.removeprefix("session:")
        destination = queue_path(f"session-{current_session}")
    else:
        try:
            return [queue_for(to, membership=membership)]
        except ValueError:
            return []
    paths = [destination, queue_path(to)]
    try:
        membership = staff.snapshot() if membership is None else membership
    except staff.StaffUnreadable:
        return list(dict.fromkeys(paths))
    member = membership.by_current_session(current_session)
    if member is not None:
        paths.append(queue_path(staff_queue_key(member.id)))
    return list(dict.fromkeys(paths))


def remove_queued_key(to: str, key: str, *,
                      recipient: StableRecipient | TransportQueueOwner = TRANSPORT_QUEUE_OWNER,
                      order: QueueOrder | LegacyQueueOrder = LEGACY_QUEUE_ORDER,
                      membership: staff.StaffSnapshot | None = None) -> int:
    """Remove matching copies no newer than the delivered send."""
    paths = _queue_paths_for_address(to, membership)
    removed = 0
    with locked():
        candidates = (list(dict.fromkeys([*paths, *(STATE / "queue").glob("*.jsonl")]))
                      if isinstance(recipient, StableRecipient) else paths)
        for path in candidates:
            try:
                entries = _read_queue_rows(path)
            except (OSError, UnicodeError):
                if path in paths:
                    raise
                continue
            kept = [entry for entry in entries
                    if isinstance(entry, QueueRowNeedsInspection)
                    or not (_matches_queued_key(entry.message, key, recipient, own_path=path in paths)
                            and _queued_not_newer(entry.message, order))]
            if len(kept) != len(entries):
                _rewrite_queue(path, kept)
            removed += len(entries) - len(kept)
    return removed


def retire(to: str, sender: str, key: str) -> int:
    """Remove obsolete messages for the selected queue owner."""
    if to == USER:
        owner, direct = USER, queue_path(USER)
    elif to.startswith("session:"):
        owner, direct = to, queue_path(f"session-{to[8:]}")
    elif to.startswith("uds:"):
        owners = {row["sessionId"] for row in sessions.live_sessions() if row["messagingSocketPath"] == to[4:]}
        if len(owners) != 1:
            return 0
        identity = owners.pop()
        owner, direct = f"session:{identity}", queue_path(f"session-{identity}")
    else:
        member = sessions.staff_recipient(to, staff.snapshot())
        if not isinstance(member, staff.StaffRow):
            return 0
        owner = staff_queue_key(member.id)
        direct = queue_path(owner)

    removed = 0
    with locked():
        paths = dict.fromkeys((direct, queue_path(to), *(STATE / "queue").glob("*.jsonl")))
        for path in paths:
            try:
                entries = _read_queue_rows(path)
            except (OSError, UnicodeError):
                if path in (direct, queue_path(to)):
                    raise
                continue
            kept: list[QueueRow] = []
            for row in entries:
                if isinstance(row, QueueRowNeedsInspection):
                    kept.append(row)
                    continue
                entry = row.message
                saved = _saved_mailbox_key(entry)
                legacy_owner = (path == direct and "stable_recipient" not in entry and "claude_recipient" not in entry
                                and not entry["to"].startswith(("uds:", "session:", "staff:")))
                if (saved == owner or legacy_owner) and (entry["key"] == key
                        or entry["key"] is None and entry["from"] == sender):
                    removed += 1
                else:
                    kept.append(row)
            if len(kept) != len(entries):
                _rewrite_queue(path, kept)
    return removed


def cancel_user_push(sender: str, key: str) -> tuple[int, str]:
    """Stop the user's emergency push sent with key, through the same channel user() sends on."""
    code, out, err = run([sys.executable, str(USER_CHANNEL), "cancel", "--source", sender, key], "",
                         user_cancel_timeout())
    if code == 0 and "pushover: nothing to cancel" in out:
        return 0, f"nothing to cancel: no emergency push for {key} is repeating"
    if code == 0:
        return 0, f"cancelled: the emergency push for {key} stopped"
    why = one_line(err or out) or ("timed out" if code is None else f"exit {code}")
    return EXIT["failed"], f"FAILED: the emergency push for {key} may still repeat: {why}"


def retire_user(sender: str, key: str) -> tuple[int, list[str]]:
    """`retire --to user`: drop the queued and kept copies, forget the key's last send, cancel the push."""
    removed = retire(USER, sender, key)
    # The event is over: a new send with the key pushes at once.
    forget_sent(key, USER)
    dropped = silence.drop(sender, key)
    code, line = cancel_user_push(sender, key)
    return code, [f"retired {removed} queued message{'s' if removed != 1 else ''}",
                  f"dropped {dropped} kept push{'es' if dropped != 1 else ''}", line]


# ---------------------------------------------------------------------------
# Emergency pushes that clear by themselves (--clears-when, clear-check)
# ---------------------------------------------------------------------------


class Emergency(TypedDict):
    key: str
    sender: str
    # The --clears-when shell command; it runs on this machine.
    check: str
    # The host that posted the push, where its retire runs.
    machine: str
    # UTC ISO times.
    sent: str
    expires: str


EMERGENCY_FIELDS = ("key", "sender", "check", "machine", "sent", "expires")


def read_emergencies() -> list[Emergency]:
    """The live emergency records; the caller holds the lock."""
    try:
        text = (STATE / EMERGENCIES).read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[Emergency] = []
    for item in as_list(loads(text)):
        record = as_dict(item)
        if all(isinstance(record.get(field), str) for field in EMERGENCY_FIELDS):
            records.append(cast(Emergency, cast(object, record)))
    return records


def write_emergencies(records: list[Emergency]) -> None:
    """Write the records under the lock; no records removes the file, so the notifier tick runs nothing."""
    path = STATE / EMERGENCIES
    if records:
        write_atomic(path, json.dumps(records, indent=2) + "\n")
    else:
        path.unlink(missing_ok=True)


def record_emergency(record: Emergency) -> None:
    """Keep a sent emergency push for clear-check; it replaces an older record with its sender and key."""
    with locked():
        _record_emergency_locked(record)


def _record_emergency_locked(record: Emergency) -> None:
    """Replace one clearing record while the caller holds the state lock."""
    kept = [old for old in read_emergencies() if (old["sender"], old["key"]) != (record["sender"], record["key"])]
    write_emergencies([*kept, record])


def check_exit(command: str, timeout: float) -> int | None:
    """Run a --clears-when check with no stdin; its exit code, or None when it ran past the timeout."""
    with subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, cwd=Path.home(), start_new_session=True) as process:
        try:
            return process.wait(timeout)
        except subprocess.TimeoutExpired:
            try:
                process_group.send_process_group(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _ = process.wait()
            return None


def retire_record(record: Emergency) -> tuple[int, str]:
    """Retire the push as `retire --to user` does, on the host that posted it; the exit code and the cancel line."""
    if record["machine"] == machine():
        code, lines = retire_user(record["sender"], record["key"])
        return code, lines[-1]
    # This host holds the key's window (remote() sends --repeat-minutes 0); the posting host holds the keys.
    forget_sent(record["key"], USER)
    forwarded = shlex.join(["retire", "--to", USER, "--from", record["sender"], "--key", record["key"]])
    command = ["ssh", "-o", "BatchMode=yes", record["machine"], f"{REMOTE} {forwarded}"]
    code, out, err = run(command, "", user_cancel_timeout() + conf.read("send").number("remote_transport_seconds"))
    last = (out.strip().splitlines() or [""])[-1]
    if code == 0:
        return 0, f"on {record['machine']}: {last}"
    why = one_line(err or last) or ("timed out" if code is None else f"exit {code}")
    return EXIT["failed"], f"on {record['machine']}: the emergency push for {record['key']} may still repeat: {why}"


@contextmanager
def clear_check_turn() -> Generator[bool]:
    """True when this run holds the clear-check turn; a run that finds another in progress skips."""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / ".clear-check.lock", "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


class ConfiguredCheckTimeout(Enum):
    CURRENT = "current"


def clear_check(timeout: float | ConfiguredCheckTimeout = ConfiguredCheckTimeout.CURRENT) -> tuple[int, list[str]]:
    """Retire each live emergency push whose check passes and drop the expired ones; never pushes.

    The lock is not held while checks and retires run: a retire takes it, and a send must not wait on a check.
    """
    timeout = check_timeout() if isinstance(timeout, ConfiguredCheckTimeout) else timeout
    if not (STATE / EMERGENCIES).exists():
        return 0, []
    with clear_check_turn() as turn:
        if not turn:
            return 0, ["clear-check: another run is in progress"]
        with locked():
            records = read_emergencies()
        code = 0
        lines: list[str] = []
        done: list[Emergency] = []
        zone = user_zone()
        for record in records:
            name = f"{record['key']} from {record['sender']}"
            expires = datetime.fromisoformat(record["expires"])
            if now() >= expires and not record["key"].startswith("github.keys_cold:"):
                done.append(record)
                lines.append(f"dropped: {name}: its push expired at {expires.astimezone(zone):%H:%M:%S %Z}")
                continue
            exit_code = check_exit(record["check"], timeout)
            if exit_code is None:
                lines.append(f"kept: {name}: its check ran past {timeout:g} s and was stopped")
            elif exit_code != 0:
                lines.append(f"kept: {name}: its check exits {exit_code}")
            else:
                retired, line = retire_record(record)
                if retired == 0:
                    done.append(record)
                    lines.append(f"retired: {name}: its check passed; {line}")
                else:
                    code = retired
                    lines.append(f"FAILED: {name}: its check passed, but {line.removeprefix('FAILED: ')}")
        # A file with no records goes too, so the notifier tick stops running this.
        if done or not records:
            with locked():
                write_emergencies([record for record in read_emergencies() if record not in done])
        return code, lines


def append_record(path: Path, entry: LogEntry | SocketRecoveryAttempt, *, durable: bool = False) -> None:
    """Append under the state lock; recovery records must survive a stopped drain."""
    with locked():
        repeat_guard.append_jsonl(path, entry, durable=durable)


def log(message: Message, result: Result, at: datetime, route: Route = NO_CLAUDE_ROUTE) -> None:
    entry: LogEntry = {"time": at.isoformat(), "machine": machine(), "from": message.sender, "to": result.receiver or message.to,
                       "key": message.key, "summary": message.text.strip().splitlines()[0] if message.text.strip()
                       else "", "text": message.text, "outcome": result.outcome, "detail": result.detail}
    entry["sender_native_id"] = sender_native_id(message.stamp)
    entry["human"] = message.human
    if isinstance(route, ClaudeRoute):
        entry["via"] = route.via
        entry["postmaster"] = route.postmaster
        entry["seconds"] = round(route.seconds, 3)
    try:
        append_record(STATE / "log.jsonl", entry,
                      durable=message.key is not None and message.key.startswith("socket-recovery:"))
    except Exception as error:
        repeat_guard.warn(f"message accounting: {error}")


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


@contextmanager
def delivery_turn(message: Message) -> Generator[bool]:
    """Exclude only this key and recipient; never hold the global state lock during transport."""
    if message.key is None:
        yield True
        return
    directory = STATE / "delivery-locks"
    directory.mkdir(parents=True, exist_ok=True)
    identity = json.dumps([message.key, message.to])
    path = directory / (hashlib.sha256(identity.encode()).hexdigest() + ".lock")
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def confirmed_skip(message: Message, repeat: timedelta | None, at: datetime, *,
                   live: bool = False, aliases: tuple[str, ...] = ()) -> str | None:
    """Read prior acceptance under a delivery turn; a lock alone never means sent."""
    if message.key is None:
        return None
    with key_states() as states:
        state = states.get(message.key, {"last": {}})
        if "acknowledged" in state:
            return acknowledged_skip_reason(message.key, state["acknowledged"])
        if repeat is not None:
            pending = pending_deliveries(state)
            for address in dict.fromkeys((message.to, *aliases)):
                last = state["last"].get(address)
                attempt = pending.get(address)
                if last is None or attempt is not None and attempt["started"] == last:
                    continue
                if at - datetime.fromisoformat(last) < repeat:
                    if live:
                        return still_live(message.key, message.sender, datetime.fromisoformat(last), repeat)
                    minutes = repeat.total_seconds() / 60
                    return f"key {message.key!r} last sent to {address} at {last}, inside its {minutes:g}-minute window"
    return None


def record_acceptance(message: Message, at: datetime) -> None:
    """Only a confirmed transport or recoverable queue/kept handoff enters the repeat window."""
    if message.key is not None:
        with key_states() as states:
            state = states.setdefault(message.key, {"last": {}})
            state["last"][message.to] = at.isoformat()


@dataclass(frozen=True)
class ClaimGranted:
    """This attempt can deliver the message."""


@dataclass(frozen=True)
class RepeatAccepted:
    """An earlier attempt already accepted the message."""
    reason: str


@dataclass(frozen=True)
class RepeatPending:
    """An earlier attempt has not confirmed acceptance."""
    reason: str


def pending_deliveries(state: KeyState) -> dict[str, PendingDelivery]:
    """Read legacy pending timestamps with the default delivery limit."""
    return {recipient: PendingDelivery(started=mark, limit_seconds=delivery_timeout()) if isinstance(mark, str) else mark
            for recipient, mark in state.get("pending", {}).items()}


def release_postmaster_claim(state: KeyState, recipient: str, pending: PendingDelivery) -> None:
    """Remove the reservation without removing an older accepted timestamp."""
    _ = state.get("pending", {}).pop(recipient, None)
    if state["last"].get(recipient) == pending["started"]:
        if "accepted_before" in pending:
            state["last"][recipient] = pending["accepted_before"]
        else:
            _ = state["last"].pop(recipient, None)


class RepeatUnconfirmed(RepeatPending):
    """Publication can have happened even when its evidence no longer exists."""


def resolve_postmaster_claim(states: KeyStates, key: str, recipient: str,
                             reservation: PostmasterReservation,
                             outcome: Literal["delivered", "queued", "failed", "expired", "withdrawn"]) -> ResolvedPostmaster:
    """Persist the definite result before completing or releasing the reservation."""
    resolved = ResolvedPostmaster(stage="resolved", entry_id=reservation["entry_id"],
        owner_pid=reservation["owner_pid"], owner_start=reservation["owner_start"], outcome=outcome)
    marker = pending_deliveries(states[key])[recipient]
    marker["postmaster"] = resolved
    write_key_states(states)
    return resolved


def postmaster_claim(states: KeyStates, key: str, recipient: str) -> ClaimGranted | RepeatAccepted | RepeatPending:
    """Advance only from the saved stage or a definite receipt."""
    state = states.get(key, {"last": {}})
    pending = pending_deliveries(state).get(recipient)
    if pending is None:
        return ClaimGranted()
    if "postmaster" not in pending:
        return ClaimGranted()
    reservation = pending["postmaster"]
    if reservation["stage"] == "reserved" or reservation["stage"] == "direct-relay":
        if _owner_released(reservation):
            release_postmaster_claim(state, recipient, pending)
            return ClaimGranted()
        return RepeatPending(POSTMASTER_UNCONFIRMED)
    if reservation["stage"] == "relaying" or reservation["stage"] == "relay-unconfirmed":
        if not _owner_released(reservation):
            return RepeatPending(POSTMASTER_UNCONFIRMED)
        target = pending.get("recipient")
        if reservation["stage"] == "relaying" and target is not None and target["kind"] == "relay-process":
            release_postmaster_claim(state, recipient, pending)
            return ClaimGranted()
        return RepeatUnconfirmed(POSTMASTER_UNCONFIRMED)
    if reservation["stage"] == "publishing":
        receipt = postmaster.receipt(STATE, reservation["entry_id"])
        if isinstance(receipt, postmaster.ResultPending):
            if postmaster.unpublished(STATE, reservation["entry_id"]):
                return RepeatUnconfirmed(POSTMASTER_UNCONFIRMED)
            return RepeatPending(POSTMASTER_UNCONFIRMED)
        target = pending.get("recipient")
        if target is not None and postmaster.receipt_has_recipient_fields(receipt) and (
                target["kind"] == "relay-process" or "generation" not in target
                or not postmaster.receipt_matches_recipient(receipt, target["conversation"],
                    target["generation"]["pid"], target["generation"]["started"])):
            return RepeatUnconfirmed(POSTMASTER_UNCONFIRMED)
        if (target is None and recipient.startswith("session:") and "session_id" in receipt
                and receipt["session_id"] != recipient[8:]):
            return RepeatUnconfirmed(POSTMASTER_UNCONFIRMED)
        if receipt["outcome"] == "abandoned":
            return RepeatPending(POSTMASTER_UNCONFIRMED)
        reservation = resolve_postmaster_claim(states, key, recipient, reservation, receipt["outcome"])
    if reservation["outcome"] in ("delivered", "queued"):
        state["last"][recipient] = pending["started"]
        detail = "delivery" if reservation["outcome"] == "delivered" else "queue acceptance"
        return RepeatAccepted(f"postmaster confirmed {detail} of entry {reservation['entry_id']}")
    release_postmaster_claim(state, recipient, pending)
    return ClaimGranted()


def reserve_postmaster_claim(message: Message, at: datetime, limit: float,
                             entry_id: str = "") -> ClaimGranted | RepeatAccepted | RepeatPending:
    return _reserve_claude_delivery(message, at, limit, entry_id, direct_relay=False)


def _legacy_staff_reservation(key: str, recipient: StaffClaudeRecipient) -> ClaimGranted | RepeatAccepted | RepeatPending:
    """Resolve old claims under the current or original conversation of the staff member."""
    with key_states() as states:
        pending = pending_deliveries(states.get(key, {"last": {}}))
        for address in dict.fromkeys((recipient.current.address, f"session:{recipient.staff_id}")):
            marker = pending.get(address)
            if marker is None or "recipient" in marker or "postmaster" not in marker:
                continue
            result = postmaster_claim(states, key, address)
            if isinstance(result, RepeatUnconfirmed):
                return RepeatPending(result.reason)
            if not isinstance(result, ClaimGranted):
                return result
    return ClaimGranted()


def _reserve_claude_delivery(message: Message, at: datetime, limit: float, entry_id: str, *,
                             direct_relay: bool) -> ClaimGranted | RepeatAccepted | RepeatPending:
    """Persist the claim before postmaster publication or direct relay."""
    if message.key is None:
        return ClaimGranted()
    with key_states() as states:
        state = states.get(message.key, {"last": {}})
        result = postmaster_claim(states, message.key, message.to)
        if isinstance(result, ClaimGranted) and entry_id:
            owner = process_start.process_start(os.getpid())
            if not isinstance(owner, process_start.Started):
                return RepeatPending("postmaster owner lifetime is unknown; not yet accepted")
            states[message.key] = state
            previous = pending_deliveries(state).get(message.to)
            if previous is not None and state["last"].get(message.to) == previous["started"]:
                state["last"][message.to] = at.isoformat()
            else:
                _ = state["last"].setdefault(message.to, at.isoformat())
            reservation = (DirectRelayInProgress(stage="direct-relay", entry_id=entry_id,
                                                 owner_pid=os.getpid(), owner_start=owner.start)
                           if direct_relay else
                           ReservedPostmaster(stage="reserved", entry_id=entry_id,
                                              owner_pid=os.getpid(), owner_start=owner.start))
            attempt = PendingDelivery(started=at.isoformat(), limit_seconds=limit, postmaster=reservation)
            if isinstance(message.claude_attempt, ClaudeDeliveryAttempt):
                attempt["recipient"] = _attempt_record(message.claude_attempt)
            if previous is not None and "accepted_before" in previous:
                attempt["accepted_before"] = previous["accepted_before"]
            state.setdefault("pending", {})[message.to] = attempt
        return result


def publish_postmaster_entry(message: Message, entry_id: str, source: Path, target: Path, *,
                             selection: RecipientSelection, session: sessions.SessionRecord) -> bool:
    """Persist publishing before exposing the entry through replacement."""
    def recipient_allows_publication() -> bool:
        if not isinstance(selection, FixedStaffLookup):
            return True
        try:
            current = _current_staff_session(selection, postmaster.RegisteredClaudeRecipient(session))
        except BaseException:
            with suppress(OSError):
                source.unlink(missing_ok=True)
            raise
        return not isinstance(current, RecipientLookupFailed)

    if message.key is None:
        return recipient_allows_publication() and postmaster.publish_entry(source, target)
    with key_states() as states:
        marker = pending_deliveries(states.get(message.key, {"last": {}})).get(message.to)
        if marker is None or "postmaster" not in marker:
            raise ValueError("a later send took the key before publication")
        reservation = marker["postmaster"]
        if (reservation["stage"] != "reserved" or reservation["entry_id"] != entry_id
                or not _owns_attempt(reservation)):
            raise ValueError("a later send took the key before publication")
        if not recipient_allows_publication():
            return False
        marker["postmaster"] = PublishingPostmaster(stage="publishing", entry_id=entry_id,
            owner_pid=reservation["owner_pid"], owner_start=reservation["owner_start"])
    os.replace(source, target)
    return True


def cancel_unpublished_postmaster_claim(message: Message, entry_id: str) -> None:
    """Cancel a reservation when its setup fails before transport starts."""
    if message.key is None:
        return
    with key_states() as states:
        state = states.get(message.key, {"last": {}})
        marker = pending_deliveries(state).get(message.to)
        if marker is not None and "postmaster" in marker:
            reservation = marker["postmaster"]
            if (reservation["entry_id"] == entry_id and reservation["stage"] in ("reserved", "direct-relay")
                    and _owns_attempt(reservation)):
                release_postmaster_claim(state, message.to, marker)


def record_postmaster_result(message: Message, entry_id: str,
                             delivery: postmaster.Delivered | postmaster.RelayRequired) -> None:
    """Persist resolution before the postmaster client removes its receipt."""
    if message.key is None:
        return
    with key_states() as states:
        marker = pending_deliveries(states.get(message.key, {"last": {}})).get(message.to)
        if marker is None or "postmaster" not in marker:
            return
        reservation = marker["postmaster"]
        if (reservation["entry_id"] != entry_id or not _owns_attempt(reservation)
                or reservation["stage"] not in ("reserved", "publishing")):
            return
        if isinstance(delivery, postmaster.Delivered):
            _ = resolve_postmaster_claim(states, message.key, message.to, reservation, "delivered")
        elif reservation["stage"] == "publishing" and delivery.reason in ("failed", "expired", "unclaimed"):
            _ = resolve_postmaster_claim(states, message.key, message.to, reservation,
                "withdrawn" if delivery.reason == "unclaimed" else delivery.reason)


def finish_postmaster_claim(message: Message, entry_id: str,
                            delivery: postmaster.Delivered | postmaster.RelayRequired, *, recipient: StableRecipient) -> bool:
    """Keep fallback ownership under the registered session until delivery ends."""
    if message.key is None:
        return True
    record_postmaster_result(message, entry_id, delivery)
    with key_states() as states:
        state = states.get(message.key, {"last": {}})
        marker = pending_deliveries(state).get(message.to)
        if marker is None or "postmaster" not in marker:
            return False
        reservation = marker["postmaster"]
        if reservation["entry_id"] != entry_id or not _owns_attempt(reservation):
            return False
        if isinstance(delivery, postmaster.Delivered):
            state["last"][message.to] = marker["started"]
            previous = state["last"].get(recipient.value)
            if previous is None or datetime.fromisoformat(previous) <= datetime.fromisoformat(marker["started"]):
                state["last"][recipient.value] = marker["started"]
            return True
        if reservation["stage"] == "publishing":
            return False
        marker["postmaster"] = RelayRetryBlocked(stage="relaying", entry_id=entry_id,
            owner_pid=reservation["owner_pid"], owner_start=reservation["owner_start"])
        return True


def complete_postmaster_fallback(message: Message, recipient: StableRecipient, entry_id: str,
                                 at: datetime, result: Result) -> bool:
    """Record the relay result before releasing ownership."""
    if message.key is None:
        return True
    with key_states() as states:
        state = states.get(message.key, {"last": {}})
        marker = pending_deliveries(state).get(message.to)
        if marker is None or "postmaster" not in marker:
            return False
        reservation = marker["postmaster"]
        if (reservation["stage"] not in ("direct-relay", "relaying") or reservation["entry_id"] != entry_id
                or not _owns_attempt(reservation)):
            return False
        if isinstance(result, UncertainRelayDelivery):
            marker["postmaster"] = RelayAcceptanceUnconfirmed(stage="relay-unconfirmed", entry_id=entry_id,
                owner_pid=reservation["owner_pid"], owner_start=reservation["owner_start"])
            return True
        if result.outcome == "failed":
            release_postmaster_claim(state, message.to, marker)
            return True
        if result.outcome not in ("sent", "queued"):
            return False
        later = state["last"].get(recipient.value)
        newer = later is not None and datetime.fromisoformat(later) > at
        state["last"][message.to] = marker["started"]
        if not newer:
            state["last"][recipient.value] = marker["started"]
        _ = state.get("pending", {}).pop(message.to, None)
        return not newer


def _owner_released(record: Mapping[str, object]) -> bool:
    """Reclaim only a gone process or a confirmed replacement."""
    pid = record.get("owner_pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    observed = process_start.process_start(pid)
    if isinstance(observed, (process_start.Gone, process_start.Zombie)):
        return True
    saved = record.get("owner_start")
    return isinstance(observed, process_start.Started) and isinstance(saved, str) and bool(saved) and observed.start != saved



def _owns_attempt(record: Mapping[str, object]) -> bool:
    """Only the original live process can complete its owned attempt."""
    if "owner_pid" not in record:
        return True
    if record["owner_pid"] != os.getpid():
        return False
    observed = process_start.process_start(os.getpid())
    return isinstance(observed, process_start.Started) and observed.start == record.get("owner_start")


def _finish_clearing(state: KeyState) -> None:
    """Project a clearing record while the caller holds the key lock."""
    if "clearing" not in state:
        return
    _record_emergency_locked(state["clearing"])
    del state["clearing"]


def claim(message: Message, recipient: StableRecipient, repeat: timedelta | None, at: datetime, *,
          delivery_limit: float,
          legacy_aliases: tuple[str, ...] = (), live: bool = False, postmaster_recipient: str = "",
          clears_when: str = "", posted_on: str = "", emergency: bool = False) -> ClaimGranted | RepeatAccepted | RepeatPending:
    """Check acceptance before recording an attempt under the stable recipient.

    The lock prevents overlapping sends with one key from both sending.
    Known transport aliases contribute timestamps only to the window check.
    With `live`, the window is the lifetime of an emergency push.
    """
    if message.key is None:
        return ClaimGranted()
    with key_states() as states:
        state: KeyState = states.get(message.key, {"last": {}})
        _finish_clearing(state)
        for alias in (postmaster_recipient, recipient.value, *legacy_aliases):
            recorded = postmaster_claim(states, message.key, alias)
            if not isinstance(recorded, ClaimGranted):
                return recorded
        if "acknowledged" in state:
            return RepeatAccepted(acknowledged_skip_reason(message.key, state["acknowledged"]))
        pending = pending_deliveries(state)
        for alias in (recipient.value, *legacy_aliases):
            attempt = pending.get(alias)
            if attempt is None or "owner_pid" not in attempt or attempt["started"] != state["last"].get(alias):
                continue
            if not _owner_released(attempt):
                return RepeatPending(f"key {message.key!r}: an earlier send to {recipient.value} is pending; not yet accepted")
            del state["last"][alias]
            if "pending" in state:
                _ = state["pending"].pop(alias, None)
        # An abandoned delivery must not block a window with no end.
        timestamps = [state["last"][alias] for alias in (recipient.value, *legacy_aliases)
                      if alias in state["last"]
                      and not (alias in pending and pending[alias]["started"] == state["last"][alias]
                               and at - datetime.fromisoformat(state["last"][alias])
                               >= timedelta(seconds=pending[alias]["limit_seconds"]))]
        last = max(timestamps, key=datetime.fromisoformat) if timestamps else None
        unaccepted = any(alias in pending and pending[alias]["started"] == last
                         for alias in (recipient.value, *legacy_aliases)) if last is not None else False
        pending_reason = f"key {message.key!r}: an earlier send to {recipient.value} is pending; not yet accepted"
        if last is not None and message.key.startswith("github.keys_cold:"):
            if unaccepted:
                return RepeatPending(pending_reason)
            return RepeatAccepted(f"key {message.key!r}: GitHub keys still cold; this event already sent its push")
        if repeat is not None and last is not None and at - datetime.fromisoformat(last) < repeat:
            if unaccepted:
                return RepeatPending(pending_reason)
            if live:
                return RepeatAccepted(still_live(message.key, message.sender, datetime.fromisoformat(last), repeat))
            minutes = repeat.total_seconds() / 60
            return RepeatAccepted(f"key {message.key!r} last sent to {recipient.value} at {last}, inside its {minutes:g}-minute window")
        attempt = PendingDelivery(started=at.isoformat(), limit_seconds=delivery_limit)
        if isinstance(message.claude_attempt, ClaudeDeliveryAttempt):
            attempt["recipient"] = _attempt_record(message.claude_attempt)
        accepted_before = state["last"].get(recipient.value)
        previous = pending.get(recipient.value)
        if (recipient.value == postmaster_recipient and accepted_before is not None
                and (previous is None or previous["started"] != accepted_before)):
            attempt["accepted_before"] = accepted_before
        if emergency:
            owner = process_start.process_start(os.getpid())
            if not isinstance(owner, process_start.Started):
                return RepeatPending("emergency owner lifetime is unknown; not yet accepted")
            attempt.update(owner_pid=os.getpid(), owner_start=owner.start)
        if clears_when:
            clearing = Emergency(key=message.key, sender=message.sender, check=clears_when, machine=posted_on,
                                 sent=at.isoformat(), expires=(at + emergency_lifetime()).isoformat())
            if message.key.startswith("github.keys_cold:"):
                # A warm-check command also clears an interrupted cold-key claim.
                _record_emergency_locked(clearing)
            elif emergency:
                attempt["clearing"] = clearing
            else:
                _record_emergency_locked(clearing)
        state["last"][recipient.value] = at.isoformat()
        state.setdefault("pending", {})[recipient.value] = attempt
        states[message.key] = state
    return ClaimGranted()


def accept_sent(key: str, to: str, at: datetime, *, delivered: bool = True) -> None:
    """Confirm only the claim owned by this accepted attempt."""
    with key_states() as states:
        state = states.get(key)
        if state is None:
            return
        pending = state.get("pending", {})
        owned = pending_deliveries(state).get(to)
        if owned is None or owned["started"] != at.isoformat():
            return
        if not _owns_attempt(owned):
            return
        if delivered and "clearing" in owned:
            state["clearing"] = owned["clearing"]
        del pending[to]
        if not pending:
            del state["pending"]
    with key_states() as states:
        state = states.get(key)
        if state is not None:
            _finish_clearing(state)


def user_emergency_key(sender: str, title: str) -> str:
    """The key an emergency push to the user goes with when the sender gives none: one per sender and
    title, so a repeat of one event is skipped and `send.py retire` can stop the push."""
    return f"user-emergency:{sender}:{hashlib.sha256(title.encode()).hexdigest()[:12]}"


def emergency_lifetime() -> timedelta:
    """How long Pushover repeats an emergency push that the user does not acknowledge."""
    # Imported here: only an emergency send reads it, and pushover's imports cost about 20 ms.
    from scripts.notify.pushover import EXPIRE_S
    return timedelta(seconds=EXPIRE_S)


def still_live(key: str, sender: str, sent: datetime, lifetime: timedelta) -> str:
    """Why an emergency push to the user with this key is skipped: the first one still repeats."""
    zone = user_zone()
    retire_command = shlex.join(["send.py", "retire", "--to", USER, "--from", sender, "--key", key])
    return (f"an emergency push with key {key} went to the user at {sent.astimezone(zone):%H:%M:%S %Z} and is still"
            + f" live until {(sent + lifetime).astimezone(zone):%H:%M:%S %Z}; to send a new one, retire it first:"
            + f" {retire_command}")


def forget_sent(key: str, to: str, at: datetime | None = None) -> None:
    """Remove this recipient's claim after a failed send.

    With `at`, keep the claim when a later attempt owns it.
    """
    with key_states() as states:
        state = states.get(key)
        if state is None or to not in state["last"] or (at is not None and state["last"][to] != at.isoformat()):
            return
        owned = pending_deliveries(state).get(to)
        if at is not None and owned is not None and not _owns_attempt(owned):
            return
        if at is not None and owned is not None and "accepted_before" in owned:
            state["last"][to] = owned["accepted_before"]
        else:
            del state["last"][to]
        if "pending" in state and (at is None or (owned is not None and owned["started"] == at.isoformat())):
            _ = state["pending"].pop(to, None)
            if not state["pending"]:
                del state["pending"]
        if not state["last"] and "acknowledged" not in state:
            del states[key]


def acknowledge(key: str) -> str:
    with key_states() as states:
        state = states.setdefault(key, {"last": {}})
        if "acknowledged" in state:
            return f"{key}: already acknowledged at {state['acknowledged']}"
        state["acknowledged"] = now().isoformat()
    return f"{key}: acknowledged; sends with it are skipped until `send.py reopen {shlex.quote(key)}`"


def reopen(key: str) -> str:
    with key_states() as states:
        if states.pop(key, None) is None:
            return f"{key}: no record; nothing to reopen"
    return f"{key}: reopened; its next send goes out"


@dataclass(frozen=True)
class NoRelaySocketPid:
    """The address does not name a relay socket."""


def _relay_socket_pid(address: str) -> int | NoRelaySocketPid:
    path = Path(address.removeprefix("uds:"))
    if (path.is_absolute() and path.parent.name == "cc-socks" and path.suffix == ".sock"
            and len(path.stem) <= 10 and path.stem.isdecimal()):
        pid = int(path.stem)
        if 0 < pid <= 2_147_483_647:
            return pid
    return NoRelaySocketPid()


def relay_sender(address: str) -> str:
    """The name published by the process that owned this relay socket, if known."""
    pid = _relay_socket_pid(address)
    if isinstance(pid, NoRelaySocketPid):
        return ""
    identity = read_relay_sender(pid, STATE / "relay_senders")
    return identity.sender if isinstance(identity, SavedRelaySender) else ""


def socket_sender(address: str, metadata: dict[str, object]) -> str:
    """Prefer the relay's saved identity, then the legacy queue's recipient metadata."""
    return (relay_sender(address)
            or next((as_str(metadata.get(field)) for field in ("to_name", "to-name", "recipient_name")
                     if as_str(metadata.get(field))), "")
            or (as_str(metadata.get("to")) if not as_str(metadata.get("to")).startswith("uds:") else ""))


def dead_socket_reason(address: str, name: str = "") -> str:
    name = name or socket_sender(address, {})
    return (f"dead socket {address}; relay sender {name or '(unknown)'} has no live session; message not queued")


def socket_recipient(address: str, records: list[sessions.SessionRecord],
                     membership: staff.StaffSnapshot) -> ClaudeRecipient | RecipientLookupFailed:
    """Keep the requested socket when its registry owner is unique."""
    owners = {record["sessionId"] for record in records if record["messagingSocketPath"] == address[4:]}
    if len(owners) > 1:
        return RecipientLookupFailed("the messaging socket has conflicting Claude registry owners")
    if owners:
        owner = _conversation_staff_owner(owners.pop(), membership)
        checked = _checked_staff_recipient(address, owner)
        if isinstance(checked, RecipientLookupFailed):
            usage_error(checked.reason)
    elif isinstance(_relay_socket_pid(address), NoRelaySocketPid):
        return RecipientLookupFailed(dead_socket_reason(address))
    return ClaudeRecipient(address)


def _relay_reply_conversation(address: str, sender: str, membership: staff.StaffSnapshot
                              ) -> ConversationRecipient | NoRelayReplyMailbox | RecipientLookupFailed:
    if not sender:
        return NO_RELAY_REPLY_MAILBOX
    owner = membership.by_id(staff.StaffId(sender)) or _conversation_staff_owner(sender, membership)
    member = _checked_staff_recipient(address, owner)
    if isinstance(member, RecipientLookupFailed):
        return member
    if isinstance(member, NoStaffRecipient):
        member = resolve_staff_recipient(sender, membership)
    if isinstance(member, staff.StaffRow):
        return ConversationRecipient(machine(), str(member.current_session))
    return NO_RELAY_REPLY_MAILBOX


def _socket_delivery_plan(message: Message, registry: sessions.RegistryRead,
                          records: list[sessions.SessionRecord], membership: staff.StaffSnapshot,
                          mailbox: ClaudeMailbox | NoRelayReplyMailbox) -> RelayPlan:
    path = message.to.removeprefix("uds:")
    owners = {record["sessionId"] for record in records if record["messagingSocketPath"] == path}
    if len(owners) > 1:
        return ClaudeRoutingFailed(unit_lookup.ClaudeOwnerMismatch("the messaging socket has conflicting Claude registry owners"))
    if owners:
        target = ConversationRecipient(machine(), owners.pop())
        if isinstance(mailbox, NoRelayReplyMailbox):
            mailbox = target
        elif conversation_recipient(mailbox) != target:
            return ClaudeRoutingFailed(unit_lookup.ClaudeOwnerMismatch("the requested socket names a different Claude conversation"))
        plan = _claude_delivery_plan(message, mailbox, registry, records)
        if isinstance(plan, RelayAttempt) and plan.message.to != message.to:
            return ClaudeRoutingFailed(unit_lookup.ClaudeOwnerMismatch("the requested socket differs from the proved Claude owner's socket"))
        return plan
    if registry.unreadable:
        return ClaudeRoutingFailed(unit_lookup.SocketUnavailable("the Claude registry is unreadable"))
    pid = _relay_socket_pid(message.to)
    if isinstance(pid, NoRelaySocketPid):
        return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(dead_socket_reason(message.to)))
    try:
        _ = Path(path).lstat()
        os.kill(pid, 0)
    except (FileNotFoundError, ProcessLookupError) as error:
        fallback = _relay_reply_conversation(message.to, relay_sender(message.to), membership)
        if isinstance(fallback, RecipientLookupFailed):
            return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(fallback.reason))
        if isinstance(fallback, ConversationRecipient):
            plan = _claude_delivery_plan(message, fallback, registry, records)
            if not isinstance(plan, SessionIdWithoutLiveRecipient):
                return plan
        return ClaudeRoutingFailed(unit_lookup.ClaudeSocketAbsent(f"{error}; {dead_socket_reason(message.to)}"))
    except (OSError, ValueError) as error:
        return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(f"cannot verify the relay socket: {error}"))
    started = sessions.process_start(pid)
    identity = live_relay_identity(pid, STATE / "relay_senders")
    if started and isinstance(identity, RelayIdentity):
        fallback = _relay_reply_conversation(message.to, identity.sender, membership)
        if isinstance(fallback, RecipientLookupFailed):
            return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(fallback.reason))
        attempt = ClaudeDeliveryAttempt(RelayProcessRecipient(machine(), pid, started, fallback), pid, started, path)
        return RelayAttempt(replace(message, claude_attempt=attempt))
    return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(
        f"the messaging socket has no proved Claude owner: {message.to}; {dead_socket_reason(message.to)}"))


def _current_staff_session(
    selection: FixedStaffLookup,
    target: ExpectedStaffSession | postmaster.RegisteredClaudeRecipient,
) -> VerifiedStaffDestination | RecipientLookupFailed:
    """Check the current staff row after any Claude registry read."""
    try:
        records = [] if isinstance(target, ExpectedStaffSession) else sessions.live_sessions()
        member = selection.member(staff.snapshot())
    except (OSError, ValueError, staff.StaffRefused) as error:
        return RecipientLookupFailed(f"could not check the current session for staff ID {selection.to}: {error}")
    if isinstance(member, RecipientLookupFailed):
        return member
    if isinstance(target, ExpectedStaffSession):
        if member.current_session != target.session_id:
            return RecipientLookupFailed(f"the session changed for staff ID {selection.to}")
        return VerifiedStaffDestination(target.session_id)
    session = sessions.addressed(f"session:{member.current_session}", records)
    if session is None:
        return RecipientLookupFailed(f"no live Claude session answers to staff ID {selection.to}")
    address = f"uds:{session['messagingSocketPath']}"
    if sessions.addressed(address, records) != session:
        return RecipientLookupFailed(f"the socket no longer belongs to staff ID {selection.to}")
    if (
        session["sessionId"] != target.session["sessionId"]
        or session["messagingSocketPath"] != target.session["messagingSocketPath"]
        or session["pid"] != target.session["pid"]
        or session.get("procStart") != target.session.get("procStart")
    ):
        return RecipientLookupFailed(f"the session or socket changed for staff ID {selection.to}")
    return VerifiedStaffDestination(address)


def _fixed_staff_relay_plan(message: Message, selection: FixedStaffLookup) -> RelayAttempt | ClaudeRoutingFailed:
    """Bind the current staff session to its checked Claude socket."""
    try:
        registry = sessions.read_registry()
        records = sessions.running_records(registry)
        membership = staff.snapshot()
        member = selection.member(membership)
    except (OSError, ValueError, staff.StaffRefused) as error:
        return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(
            f"could not check the current session for staff ID {selection.to}: {error}"))
    if isinstance(member, RecipientLookupFailed):
        return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(member.reason))
    mailbox = StaffClaudeRecipient(member.id, ConversationRecipient(machine(), str(member.current_session)))
    plan = _claude_delivery_plan(message, mailbox, registry, records)
    if isinstance(plan, SessionIdWithoutLiveRecipient):
        return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(
            f"no live Claude session answers to staff ID {selection.to}"))
    if isinstance(plan, ClaudeRoutingFailed):
        return plan
    attempt = plan.message.claude_attempt
    assert isinstance(attempt, ClaudeDeliveryAttempt)
    return RelayAttempt(replace(plan.message, claude_attempt=replace(attempt, staff_requirement=selection)))


def relay_socket(message: Message, timeout: float, *,
                 selection: RecipientSelection | NoFixedStaffLookup = NO_FIXED_STAFF_LOOKUP) -> tuple[Message, Result]:
    """Retry only a confirmed socket miss and preserve the selected mailbox."""
    if isinstance(selection, FixedStaffLookup):
        plan = _fixed_staff_relay_plan(message, selection)
        if isinstance(plan, ClaudeRoutingFailed):
            return message, Result("failed", plan.failure.reason)
        message = plan.message
    attempt = message.claude_attempt
    if not isinstance(attempt, ClaudeDeliveryAttempt):
        return message, Result("failed", "no Claude delivery attempt was selected")
    started = time.monotonic()
    result = relay(message, timeout)
    if not isinstance(result, ConfirmedSocketMiss):
        return message, result
    if isinstance(selection, FixedStaffLookup):
        replacement = _fixed_staff_relay_plan(message, selection)
        if isinstance(replacement, ClaudeRoutingFailed):
            return message, Result("failed", f"{result.detail}; {replacement.failure.reason}")
        if replacement.message.to == message.to:
            return message, Result("failed", f"{result.detail}; no replacement socket for staff ID {selection.to}")
        retried = relay(replacement.message, max(1.0, timeout - (time.monotonic() - started)))
        return replacement.message, retried
    mailbox = attempt.recipient
    if isinstance(mailbox, RelayProcessRecipient):
        mailbox = mailbox.reply_to
    if isinstance(mailbox, NoRelayReplyMailbox):
        return message, Result("failed", f"{result.detail}; {dead_socket_reason(message.to)}")
    registry = sessions.read_registry()
    if isinstance(mailbox, StaffClaudeRecipient):
        member = staff.snapshot().by_id(mailbox.staff_id)
        if member is None or isinstance(member.retirement, staff.RetiredProducer):
            return message, Result("failed", "the staff recipient is unavailable")
        mailbox = StaffClaudeRecipient(member.id, ConversationRecipient(machine(), str(member.current_session)))
    plan = _claude_delivery_plan(message, mailbox, registry, sessions.running_records(registry))
    if isinstance(plan, ClaudeRoutingFailed):
        return message, Result("failed", plan.failure.reason)
    address = conversation_recipient(mailbox).address
    if isinstance(attempt.recipient, RelayProcessRecipient) and isinstance(plan, SessionIdWithoutLiveRecipient):
        return message, Result("failed", f"{result.detail}; {dead_socket_reason(message.to)}")
    if isinstance(plan, SessionIdWithoutLiveRecipient) or plan.message.claude_attempt == attempt:
        return message, Result("queued", result.detail, receiver=address)
    retried = relay(plan.message, max(1.0, timeout - (time.monotonic() - started)))
    if retried.outcome in ("sent", "queued"):
        retried = retried._replace(receiver=address)
    return plan.message, retried


def socket_reply_outcomes() -> dict[str, Literal["attempt", "sent", "failed"]]:
    """Each attempt needs a result; counts also distinguish retries with equal timestamps."""
    outcomes: dict[str, Literal["attempt", "sent", "failed"]] = {}
    attempts: dict[str, int] = {}
    results: dict[str, int] = {}
    for filename, counts in (("socket-recovery-attempts.jsonl", attempts), ("log.jsonl", results)):
        try:
            with (STATE / filename).open(encoding="utf-8") as handle:
                for line in handle:
                    entry = as_dict(loads(line))
                    key = as_str(entry.get("key"))
                    if key.startswith("socket-recovery:"):
                        counts[key] = counts.get(key, 0) + 1
                        if filename == "log.jsonl":
                            outcomes[key] = "sent" if entry.get("outcome") == "sent" else "failed"
        except FileNotFoundError:
            pass
    for key, count in attempts.items():
        if count > results.get(key, 0):
            outcomes[key] = "attempt"
    return outcomes


def socket_archive_path(name: str, content: bytes = b"") -> Path:
    """Never replace an archive; an identical entry is a completed earlier checkpoint."""
    archive = STATE / "socket-queue-archive"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / name
    while target.exists():
        if content and target.read_bytes() == content:
            return target
        target = archive / ("_" + target.name)
    return target


def drain_socket_queues() -> tuple[int, list[str]]:
    """Only the explicit drain holds this lock; normal messages do not wait for its relays."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / ".drain-sockets.lock").open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return recover_socket_queues(sessions.read_registry(), now())


@dataclass
class SocketQueueCheckpoint:
    path: Path
    content: bytes

    def check_current(self) -> None:
        if self.path.read_bytes() != self.content:
            raise OSError(f"socket queue changed during the drain: {self.path}")

    def rewrite(self, remaining: list[QueueRow]) -> None:
        with locked():
            self.check_current()
            _rewrite_queue(self.path, remaining)
            self.content = b"".join(row.raw for row in remaining)


def finish_socket_reply(checkpoint: SocketQueueCheckpoint, remaining: list[QueueRow],
                        row: ReadableQueueRow, line: str) -> None:
    """Report and flush before the checkpoint removes the evidence a rerun needs."""
    print(line, flush=True)
    remaining.remove(row)
    checkpoint.rewrite(remaining)


def _saved_mailbox_key(entry: QueuedMessage) -> str:
    """Return the queue key only when the persisted recipient fields agree."""
    address = entry["to"]
    stable = entry.get("stable_recipient", "")
    if "claude_recipient" in entry:
        saved = entry["claude_recipient"]
        if saved["machine"] != machine():
            return ""
        if saved["kind"] == "conversation":
            owner = f"session:{saved['conversation']}"
            if "staff_id" in saved or address != owner and not address.startswith("uds:"):
                return ""
        else:
            staff_id = saved.get("staff_id", "")
            owner = f"staff:{staff_id}"
            if address not in (owner, staff_id):
                return ""
        if stable and stable != owner:
            return ""
        if address.startswith("uds:") and "generation" in saved and address[4:] != saved["generation"]["socket"]:
            return ""
        return owner
    if address.startswith("session:"):
        return address if not stable or stable == address else ""
    if address.startswith("uds:"):
        return stable if stable.startswith("session:") and stable[8:] else ""
    if address.startswith("staff:"):
        return address if not stable or stable == address else ""
    if stable.startswith("staff:"):
        return stable if address == stable[6:] else ""
    return stable


def _saved_claude_mailbox(entry: QueuedMessage, membership: staff.StaffSnapshot) -> ClaudeMailbox | NoRelayReplyMailbox:
    key = _saved_mailbox_key(entry)
    if key.startswith("session:") and key[8:]:
        return ConversationRecipient(machine(), key[8:])
    if key.startswith("staff:"):
        member = membership.by_id(staff.StaffId(key[6:]))
        if member is not None:
            return StaffClaudeRecipient(member.id, ConversationRecipient(machine(), str(member.current_session)))
    return NO_RELAY_REPLY_MAILBOX


def _socket_recovery_id(path: Path, index: int, row: ReadableQueueRow) -> str:
    saved = row.message.get("socket_recovery", "")
    if saved:
        return saved
    raw = row.raw.removesuffix(b"\n").removesuffix(b"\r")
    return "socket-recovery:" + hashlib.sha256(f"{path.name}:{index}:".encode() + raw).hexdigest()


def recover_socket_queues(registry: sessions.RegistryRead, at: datetime) -> tuple[int, list[str]]:
    """With the drain lock held, retry bound replies and archive uncertain deliveries."""
    records = sessions.running_records(registry)
    membership = staff.snapshot()
    outcomes = socket_reply_outcomes()
    old_files: list[tuple[SocketQueueCheckpoint, list[datetime]]] = []
    lines: list[str] = []
    code = 0
    for path in sorted((STATE / "queue").glob("uds__*.sock.jsonl")):
        try:
            remaining = _read_queue_rows(path)
        except (OSError, UnicodeError) as error:
            lines.append(f"unresolved socket queue {path}: unreadable ({error}); left in place")
            code = EXIT["failed"]
            continue
        checkpoint = SocketQueueCheckpoint(path, b"".join(row.raw for row in remaining))
        replies: list[tuple[ReadableQueueRow, datetime, str]] = []
        for index, row in enumerate(remaining):
            if isinstance(row, QueueRowNeedsInspection):
                lines.append(f"unresolved socket queue {path}: rejected message row needs inspection; left in place")
                code = EXIT["failed"]
                continue
            entry = row.message
            if not _saved_mailbox_key(entry):
                lines.append(f"unresolved socket queue {path}: recipient identity is missing; left in place")
                code = EXIT["failed"]
                continue
            try:
                stamp = datetime.fromisoformat(entry["time"]).astimezone(timezone.utc)
            except ValueError:
                lines.append(f"unresolved socket queue {path}: unreadable time; left in place")
                code = EXIT["failed"]
                continue
            replies.append((row, stamp, _socket_recovery_id(path, index, row)))
        retry: list[tuple[ReadableQueueRow, datetime, str]] = []
        for row, stamp, identity in replies:
            entry = row.message
            outcome = outcomes.get(identity)
            metadata = as_dict(entry)
            name = socket_sender(entry["to"], metadata) or entry["to"]
            if outcome == "attempt":
                target = socket_archive_path(f"{file_name(identity)}.jsonl", row.raw)
                if not target.exists():
                    write_bytes_atomic(target, row.raw)
                finish_socket_reply(checkpoint, remaining, row,
                    f"UNCONFIRMED: stuck reply from {entry['time']} to {name} may have arrived; archived, not resent")
                code = EXIT["failed"]
            elif outcome == "sent":
                finish_socket_reply(checkpoint, remaining, row, f"SENT: stuck reply from {entry['time']} to {name}")
            else:
                retry.append((row, stamp, identity))
        cutoff = at - timedelta(hours=24)
        if retry and len(retry) == len(remaining) and all(stamp <= cutoff for _, stamp, _ in retry):
            old_files.append((checkpoint, [stamp for _, stamp, _ in retry]))
            continue
        young: list[ReadableQueueRow] = []
        archived = False
        for row, stamp, identity in retry:
            if stamp <= cutoff:
                target = socket_archive_path(f"{file_name(identity)}.jsonl", row.raw)
                if not target.exists():
                    write_bytes_atomic(target, row.raw)
                print(f"archived expired reply from {row.message['time']} in {path}", flush=True)
                remaining.remove(row)
                archived = True
            else:
                young.append(row)
        if archived:
            checkpoint.rewrite(remaining)
        positions = {id(row): index for index, row in enumerate(remaining)}
        attempts = [(row, _socket_recovery_id(path, positions[id(row)], row)) for row in young]
        saved = False
        for position, (row, identity) in enumerate(attempts):
            entry = row.message
            mailbox = _saved_claude_mailbox(entry, membership)
            if isinstance(mailbox, NoRelayReplyMailbox):
                lines.append(f"unresolved socket queue {path}: recipient identity is missing; left in place")
                code = EXIT["failed"]
                continue
            target = conversation_recipient(mailbox)
            message = Message(target.address, entry["from"], entry["summary"],
                              f"Stuck message from {entry['time']}:\n" + entry["text"], identity,
                              entry.get("shown_sender", entry["from"]),
                              stamp=read_sender_stamp(entry.get("sender_native_id", "unavailable")),
                              sent_header=queued_sent_line(entry["time"]))
            plan = _claude_delivery_plan(message, mailbox, registry, records)
            if not isinstance(plan, RelayAttempt):
                reason = plan.failure.reason if isinstance(plan, ClaudeRoutingFailed) else "no live receiver"
                lines.append(f"unresolved socket queue {path}: {reason}; left in place")
                code = EXIT["failed"]
                continue
            started = now()
            append_record(STATE / "socket-recovery-attempts.jsonl",
                          SocketRecoveryAttempt(key=identity, time=started.isoformat()), durable=True)
            if not saved:
                # Save all IDs before delivery changes the row positions.
                for index, (candidate, candidate_id) in enumerate(attempts):
                    if candidate.message.get("socket_recovery") != candidate_id:
                        entry_with_id = candidate.message.copy()
                        entry_with_id["socket_recovery"] = candidate_id
                        updated = _new_queue_row(entry_with_id)
                        remaining[positions[id(candidate)]] = updated
                        attempts[index] = (updated, candidate_id)
                row, _ = attempts[position]
                checkpoint.rewrite(remaining)
                saved = True
            _, result = relay_socket(plan.message, delivery_timeout())
            if isinstance(result, UncertainRelayDelivery):
                lines.append(f"UNCONFIRMED: socket queue {path}: {result.detail}; the next drain archives it")
                code = EXIT["failed"]
                continue
            log(message, result, started)
            if result.outcome != "sent":
                lines.append(f"FAILED: socket queue {path}: {result.detail}; kept for retry")
                code = EXIT["failed"]
                continue
            finish_socket_reply(checkpoint, remaining, row,
                f"SENT: stuck reply from {entry['time']} to {socket_sender(entry['to'], as_dict(entry)) or target.address}")
    if old_files:
        stamps = [stamp for _, file_stamps in old_files for stamp in file_stamps]
        with locked():
            for checkpoint, _ in old_files:
                checkpoint.check_current()
            print(f"archiving {len(old_files)} socket queue files: oldest {min(stamps).isoformat()},"
                  + f" newest {max(stamps).isoformat()}", flush=True)
            for checkpoint, _ in old_files:
                os.replace(checkpoint.path, socket_archive_path(checkpoint.path.name))
    return code, lines


@dataclass(frozen=True)
class CodexQueueOwner:
    thread_id: str


@dataclass(frozen=True)
class CodexHookContext:
    event: Literal["SessionStart", "PostToolUse"]


class PendingTextFormat(Enum):
    TEXT = "text"


def _pending_mailbox_matches(mailbox: ClaudeMailbox, own: str, membership: staff.StaffSnapshot) -> bool:
    if isinstance(mailbox, StaffClaudeRecipient):
        member = membership.by_current_session(own)
        return member is not None and member.id == mailbox.staff_id
    return mailbox.conversation == own


def _pending_owner_is_current(session: sessions.SessionRecord) -> bool:
    pid = session["pid"]
    started = session.get("procStart", "")
    current = unit_lookup.current_claude_record(pid, started, session["sessionId"])
    return (not isinstance(current, unit_lookup.SocketUnavailable)
            and sessions.process_start(pid) == started
            and current["messagingSocketPath"] == session["messagingSocketPath"])


def pending(to: str | CodexQueueOwner | None, *, emit: bool = False,
            output: PendingTextFormat | CodexHookContext = PendingTextFormat.TEXT) -> str:
    """Drain native session keys and the current member's fixed staff key."""
    if isinstance(to, str) and to.startswith("codex:"):
        to = CodexQueueOwner(to.removeprefix("codex:"))
    try:
        membership = staff.local_snapshot() if isinstance(to, CodexQueueOwner) else staff.snapshot()
    except staff.StaffUnreadable:
        if isinstance(to, str) and not to.startswith(("session:", "uds:", "staff:")):
            raise
        membership = staff.StaffSnapshot(())
    explicit_staff = isinstance(to, str) and to.startswith("staff:")
    session: sessions.SessionRecord | None = None
    if isinstance(to, CodexQueueOwner):
        own = to.thread_id
        keys = [f"session-{own}", f"session:{own}", f"codex:{own}", own]
    else:
        records = sessions.live_sessions()
        if to is None:
            own = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
            session = next((record for record in records if own and record["sessionId"] == own), None)
            if session is None:
                raise ValueError("pending without a name runs inside a live Claude session")
        else:
            session = sessions.addressed(to, records, membership=membership)
        keys = [to] if to is not None and to.startswith(("session:", "uds:", "staff:")) else []
        own = session["sessionId"] if session is not None else ""
        if not own and to is not None and not to.startswith(("session:", "uds:")):
            member = (membership.by_id(staff.StaffId(to[6:])) if explicit_staff
                      else sessions.staff_recipient(to, membership))
            if isinstance(member, staff.StaffRow):
                own = str(member.current_session)
        if own:
            keys.append(f"session-{own}")
        if session is not None:
            keys.append(f"uds:{session['messagingSocketPath']}")
    if own:
        owner = membership.by_current_session(own)
        if owner is not None:
            keys.append(staff_queue_key(owner.id))
    paths = list(dict.fromkeys(queue_path(key) for key in keys if key))
    stable_values = set(keys)
    native = to if isinstance(to, str) and to.startswith("session:") else f"session:{own}" if own else ""
    with locked():
        entries: list[QueuedMessage] = []
        rewrites: list[tuple[Path, list[QueueRow]]] = []
        candidates = list(dict.fromkeys([*paths, *(STATE / "queue").glob("*.jsonl")]))
        for path in candidates:
            selected: list[QueuedMessage] = []
            kept: list[QueueRow] = []
            try:
                queued = _read_queue_rows(path)
            except (OSError, UnicodeError):
                if path in paths:
                    raise
                continue
            for row in queued:
                if isinstance(row, QueueRowNeedsInspection):
                    kept.append(row)
                    continue
                entry = row.message
                owner = _saved_mailbox_key(entry)
                if explicit_staff:
                    matches = bool(own) and owner == to
                elif ("claude_recipient" in entry or entry["to"].startswith(("uds:", "staff:"))
                        or owner.startswith("staff:")):
                    target = _saved_claude_mailbox(entry, membership)
                    matches = (not isinstance(target, NoRelayReplyMailbox)
                               and _pending_mailbox_matches(target, own, membership))
                    if isinstance(to, CodexQueueOwner) and isinstance(target, ConversationRecipient):
                        matches = matches and "claude_recipient" not in entry and not entry["to"].startswith("uds:")
                elif entry["to"].startswith("session:"):
                    matches = bool(native) and owner == native
                elif "stable_recipient" in entry:
                    matches = bool(owner) and owner in stable_values
                else:
                    matches = path in paths
                if matches:
                    selected.append(entry)
                else:
                    kept.append(row)
            if selected:
                entries.extend(selected)
                rewrites.append((path, kept))
        if (not isinstance(to, CodexQueueOwner) and not explicit_staff
                and any("claude_recipient" in entry or entry["to"].startswith("uds:") for entry in entries)
                and (session is None or not _pending_owner_is_current(session))):
            return ""
        entries.sort(key=lambda entry: entry["time"])
        newest: dict[tuple[str, str], QueuedMessage] = {}
        unkeyed_or_legacy: list[QueuedMessage] = []
        for entry in entries:
            owner = _saved_mailbox_key(entry) if "stable_recipient" in entry or "claude_recipient" in entry else ""
            key = entry["key"]
            if owner and key is not None:
                previous = newest.get((owner, key))
                if (previous is None
                        or "send_order" not in previous and "send_order" not in entry
                        or "send_order" in entry and _queued_not_newer(previous, QueueOrder(entry["send_order"]))):
                    newest[owner, key] = entry
            else:
                unkeyed_or_legacy.append(entry)
        entries = sorted([*unkeyed_or_legacy, *newest.values()], key=lambda entry: entry["time"])
        text = "\n\n".join(stamped_text(
            f"Queued message {index} of {len(entries)}, from {entry['from']} at {entry['time']}:"
            + shown_sender_line(entry["from"], entry.get("shown_sender", entry["from"]))
            + queued_sent_line(entry["time"]),
            "\n" + entry["text"].rstrip(), read_sender_stamp(entry.get("sender_native_id", "unavailable")))
            for index, entry in enumerate(entries, 1))
        if emit and text:
            rendered = (json.dumps({"hookSpecificOutput": {"hookEventName": output.event, "additionalContext": text}})
                        if isinstance(output, CodexHookContext) else text)
            print(rendered, flush=True)
        for path, kept in rewrites:
            _rewrite_queue(path, kept)
        return text


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _recipient_roster_problems(
    to: str, problems: list[str], observed: roster.Roster, membership: staff.StaffSnapshot,
) -> list[str]:
    """Use staff identities for anomalies and text for diagnostics without typed owners."""
    member = resolve_staff_recipient(to, membership, observed=observed.entries)
    references = {to, to.split(":", 1)[-1]}
    if isinstance(member, staff.StaffRow):
        references.update((member.name, str(member.id), str(member.current_session)))
    typed: dict[str, bool] = {}
    for anomaly in observed.row_anomalies:
        owner = anomaly.row
        matches = (owner.id == member.id if isinstance(member, staff.StaffRow)
                   else to.casefold() in (owner.name.casefold(), owner.id, owner.current_session,
                                          f"session:{owner.current_session}", f"codex:{owner.current_session}"))
        for text in (anomaly.diagnostic, f"warning: {anomaly.diagnostic}"):
            typed[text] = typed.get(text, False) or matches
    related: list[str] = []
    for problem in problems:
        if problem in typed:
            matches = typed[problem]
        else:
            matches = any(reference and re.search(
                rf"(?<![\w-]){re.escape(reference)}(?![\w-])", problem, re.IGNORECASE)
                for reference in references)
        if matches:
            related.append(problem)
    return related


def _print_roster_problems(
    to: str, problems: list[str], observed: roster.Roster, membership: staff.StaffSnapshot,
) -> None:
    related = _recipient_roster_problems(to, problems, observed, membership)
    for problem in related:
        print(f"send.py: roster: {problem}", file=sys.stderr)
    omitted = len(problems) - len(related)
    if omitted:
        print(f"send.py: roster problems omitted: {omitted}; "
              + "run ~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py", file=sys.stderr)


def _messageable_recipient(entry: roster.RosterEntry) -> SessionRecipient:
    from scripts.message import roster

    if not isinstance(entry.reach, roster.Messageable):
        raise AssertionError("a messageable recipient needs an address")
    address = entry.reach.address
    if address.startswith("codex:"):
        if isinstance(entry.pane, HerdrPlace):
            return CodexPaneRecipient(address, entry.pane, address.removeprefix("codex:"))
        if not isinstance(entry.daemon, codex_daemon.CodexDaemon):
            usage_error(f"{address} has no owning Codex daemon")
        return CodexSessionRecipient(
            address,
            address.removeprefix("codex:"),
            entry.name,
            entry.daemon,
        )
    return ClaudeRecipient(address)


def _cannot_receive_message(entry: roster.RosterEntry) -> NoReturn:
    from scripts.message import roster

    usage_error(roster.cannot_receive_message_text(entry))


def _ambiguous_name(name: str, entries: list[roster.RosterEntry]) -> NoReturn:
    from scripts.message import roster

    lines = [f"{name} matches {len(entries)} sessions:"]
    for entry in entries:
        recipient = (
            entry.reach.address
            if isinstance(entry.reach, roster.Messageable)
            else f"(cannot take messages: {entry.reach.reason})"
        )
        account = f", {entry.account.label}" if isinstance(entry.account, roster.AccountLabel) else ""
        owner = (f", {entry.daemon.home}" if isinstance(entry.daemon, codex_daemon.CodexDaemon) else "")
        lines.append(f"  {recipient} ({entry.role}{account}{owner})")
    usage_error("\n".join(lines))


def _sender_staff_row(membership: staff.StaffSnapshot) -> staff.StaffRow | NoStaffSender:
    from scripts.message import roster

    sender = roster.sender_identity(os.environ)
    identity = (sender.thread_id if isinstance(sender, roster.CodexThread)
                else sender.session_id if isinstance(sender, roster.ClaudeSession) else "")
    member = membership.by_current_session(identity)
    return member if member is not None else NoStaffSender()


def resolve_staff_recipient(
    target: str, membership: staff.StaffSnapshot, *, records: tuple[sessions.SessionRecord, ...] | list[sessions.SessionRecord] = (),
    observed: tuple[roster.RosterEntry, ...] | list[roster.RosterEntry] = (),
) -> staff.StaffRow | NoStaffRecipient | RecipientLookupFailed:
    """Select a staff recipient, including its unit form."""
    return _staff_recipient_match(target, membership, records=records, observed=observed, allow_unit_form=True)


def staff_named(name: str, membership: staff.StaffSnapshot) -> staff.StaffRow | sessions.StaffNameMissing:
    """Expose the shared staff lookup to native session lookup."""
    member = _staff_recipient_match(name, membership, allow_unit_form=True, include_retired_names=False)
    if isinstance(member, RecipientLookupFailed):
        raise ValueError(member.reason)
    return member if isinstance(member, staff.StaffRow) else sessions.StaffNameMissing(name)


def _conversation_staff_owner(conversation: str, membership: staff.StaffSnapshot) -> staff.StaffRow | None:
    return membership.by_current_session(conversation) or next(
        (row for row in membership.retired_producers if row.current_session == conversation), None)


def _checked_staff_recipient(target: str, member: staff.StaffRow | None
                             ) -> staff.StaffRow | NoStaffRecipient | RecipientLookupFailed:
    if member is not None and isinstance(member.retirement, staff.RetiredProducer):
        return RecipientLookupFailed(f"{target}: {member.name} is a retired producer")
    return member or NoStaffRecipient()


def _staff_recipient_match(
    target: str, membership: staff.StaffSnapshot, *, records: tuple[sessions.SessionRecord, ...] | list[sessions.SessionRecord] = (),
    observed: tuple[roster.RosterEntry, ...] | list[roster.RosterEntry] = (), allow_unit_form: bool = False,
    include_retired_names: bool = True,
) -> staff.StaffRow | NoStaffRecipient | RecipientLookupFailed:
    """Match staff references and resolve unit aliases when requested."""
    from scripts.message import roster

    if target.startswith("staff:"):
        return _checked_staff_recipient(target, membership.by_id(staff.StaffId(target[6:])))
    if target.startswith("session:"):
        return _checked_staff_recipient(target, _conversation_staff_owner(target[8:], membership))
    reference = target
    if target.startswith("worker:"):
        for entry in observed:
            if isinstance(entry.reach, roster.Messageable) and entry.reach.address == target:
                if isinstance(entry.thread, roster.CodexThread):
                    reference = entry.thread.thread_id
                elif isinstance(entry.session, roster.ClaudeSession):
                    reference = entry.session.session_id
        if reference == target:
            path = target.removeprefix("worker:")
            if len(path.split("/")) == 2:
                sender = _sender_staff_row(membership)
                ancestor = sender if isinstance(sender, staff.StaffRow) else None
                while ancestor is not None and ancestor.kind != "showrunner":
                    ancestor = membership.parent_of(ancestor)
                if ancestor is not None and path.split("/", 1)[0] != ancestor.name:
                    path = f"{ancestor.name}/{path}"
            member = membership.by_path(path)
            if member is not None and member.kind == "worker":
                return member
    if target.startswith("uds:"):
        record = next((record for record in records
                       if f"uds:{record['messagingSocketPath']}" == target), None)
        if record is not None:
            return _checked_staff_recipient(target, _conversation_staff_owner(record["sessionId"], membership))
        reference = socket_sender(target, {})
    elif target.startswith("codex:"):
        reference = target[6:]
    identity = staff.StaffId(reference)
    member = membership.rows_by_id.get(identity) or membership.by_current_session(reference)
    if member is not None:
        return member
    if target.startswith(("codex:", "worker:")) or not reference:
        return _checked_staff_recipient(target, membership.by_id(identity)) if reference else NoStaffRecipient()

    def normalized_name(value: str) -> str:
        try:
            return unit_names.name(value).casefold()
        except unit_names.BadName:
            return value.casefold()

    def name_chain(candidate: staff.StaffRow) -> list[str]:
        chain = [candidate.name]
        parent = candidate
        visited = {candidate.id}
        while parent.kind != "showrunner" and isinstance(parent.parent, staff.ReportsTo):
            ancestor = membership.by_id(parent.parent.id)
            if ancestor is None or ancestor.id in visited:
                break
            visited.add(ancestor.id)
            chain.insert(0, ancestor.name)
            parent = ancestor
        return chain

    normalized = "/".join(normalized_name(part) for part in reference.split("/"))
    matches: dict[staff.StaffId, staff.StaffRow] = {}
    paths: dict[staff.StaffId, str] = {}

    def match_names(candidates: tuple[staff.StaffRow, ...]) -> None:
        for candidate in candidates:
            chain = name_chain(candidate)
            path = "/".join(chain)
            canonical = "/".join(normalized_name(part) for part in chain)
            short = normalized_name(candidate.name)
            if normalized == canonical or ("/" not in normalized and normalized == short):
                matches[candidate.id] = candidate
                paths[candidate.id] = path

    match_names(membership.rows)
    if allow_unit_form and not matches:
        from scripts.copies import copies

        for candidate in membership.find(kind="unit"):
            short = normalized_name(copies.unit_owner(candidate.name))
            if short != normalized.rsplit("/", 1)[-1]:
                continue
            chain = name_chain(candidate)
            path = "/".join(chain)
            chain[-1] = short
            canonical = "/".join(normalized_name(part) for part in chain)
            if normalized == canonical or ("/" not in normalized and normalized == short):
                matches[candidate.id] = candidate
                paths[candidate.id] = path
    if not matches:
        retired_id = membership.by_id(identity)
        if retired_id is not None:
            return _checked_staff_recipient(target, retired_id)
        if include_retired_names:
            match_names(membership.retired_producers)
    foreign_workers_only = False
    if "/" not in normalized and matches and all(row.kind == "worker" for row in matches.values()):
        sender = _sender_staff_row(membership)
        unit = sender if isinstance(sender, staff.StaffRow) else None
        if unit is not None and unit.kind == "worker":
            unit = membership.parent_of(unit)
        if unit is not None and unit.kind == "unit":
            own_workers = {key: row for key, row in matches.items() if row.parent == staff.ReportsTo(unit.id)}
            if own_workers:
                matches = own_workers
            else:
                foreign_workers_only = True
    if len(matches) == 1 and not foreign_workers_only:
        return _checked_staff_recipient(target, next(iter(matches.values())))
    if matches:
        details: list[str] = []
        for row in matches.values():
            addresses = ", ".join(dict.fromkeys(
                entry.reach.address for entry in _staff_transport_matches(row, list(observed))
                if isinstance(entry.reach, roster.Messageable))) or f"session:{row.current_session}"
            details.append(f"{paths[row.id]} (staff:{row.id}, {addresses})")
        return RecipientLookupFailed(f"ambiguous staff row for {target!r}: {', '.join(details)}")
    return NoStaffRecipient()


def _staff_transport_matches(member: staff.StaffRow, entries: list[roster.RosterEntry]) -> list[roster.RosterEntry]:
    from scripts.message import roster

    return [entry for entry in entries if (
        isinstance(entry.session, roster.ClaudeSession) and entry.session.session_id == member.current_session
        or isinstance(entry.thread, roster.CodexThread) and entry.thread.thread_id == member.current_session
        or isinstance(entry.reach, roster.Messageable)
        and entry.reach.address in (f"session:{member.current_session}", f"codex:{member.current_session}", member.current_session)
    )]


def _native_address_matches(to: str, entries: list[roster.RosterEntry]) -> list[roster.RosterEntry]:
    from scripts.message import roster

    if to.startswith(("session:", "worker:", "codex:")):
        return [entry for entry in entries if (
            isinstance(entry.reach, roster.Messageable) and entry.reach.address == to
            or to.startswith("session:") and isinstance(entry.session, roster.ClaudeSession)
            and to == f"session:{entry.session.session_id}"
            or to.startswith("session:") and isinstance(entry.thread, roster.CodexThread)
            and to == f"session:{entry.thread.thread_id}"
        )]
    return []


def _registry_name_matches(to: str, entries: list[roster.RosterEntry]) -> list[roster.RosterEntry]:
    from scripts.message import roster

    if to.startswith(("session:", "worker:", "codex:", "uds:")):
        return []
    return [entry for entry in entries
            if entry.kind == "claude" and entry.role == "freestanding"
            and isinstance(entry.registry_name, roster.RegistryName)
            and entry.registry_name.value.casefold() == to.casefold()]


def _matched_recipient(
    to: str, matches: list[roster.RosterEntry], destination: LocalOnly | NamedMachine,
) -> SessionRecipient | RemoteDelivery:
    from scripts.message import roster

    if len(matches) > 1:
        _ambiguous_name(to, matches)
    entry = matches[0]
    if isinstance(entry.reach, roster.CannotReceiveMessages):
        _cannot_receive_message(entry)
    if isinstance(entry.thread, roster.CodexThread) and to == f"session:{entry.thread.thread_id}":
        if isinstance(destination, NamedMachine):
            return RemoteDelivery(destination.host, f"codex:{entry.thread.thread_id}")
        if isinstance(entry.pane, HerdrPlace):
            return CodexPaneRecipient(f"codex:{entry.thread.thread_id}", entry.pane, entry.thread.thread_id)
    if isinstance(destination, NamedMachine):
        if isinstance(entry.session, roster.ClaudeSession) and (to.startswith("session:")
                or entry.reach.address.startswith(("uds:", "session:"))):
            return RemoteDelivery(destination.host, f"session:{entry.session.session_id}", selected_conversation=True)
        return RemoteDelivery(destination.host, entry.reach.address)
    return _messageable_recipient(entry)


def matching_recipients(
    to: str, scope: LocalOnly | LocalThenPeer, observed: roster.Roster, *,
    membership: staff.StaffSnapshot | None = None,
) -> RecipientMatches | LocalRosterIncomplete | RecipientSearchFailed:
    """Find rows without delivering or exiting; ask the peer only after a local miss."""
    from scripts.message import roster

    membership = staff.snapshot() if membership is None else membership
    native = to.startswith("session:")
    member = NoStaffRecipient() if native else _staff_recipient_match(to, membership, observed=observed.entries)
    if isinstance(member, RecipientLookupFailed):
        return RecipientSearchFailed(member.reason, [member.reason])
    staff_alias = (resolve_staff_recipient(to, membership, observed=observed.entries)
                   if isinstance(member, NoStaffRecipient) and not native else member)
    if isinstance(staff_alias, RecipientLookupFailed):
        return RecipientSearchFailed(staff_alias.reason, [staff_alias.reason])
    if isinstance(member, staff.StaffRow):
        for anomaly in observed.row_anomalies:
            if anomaly.row.id == member.id:
                return RecipientSearchFailed(anomaly.diagnostic, observed.diagnostics)
    matches = (_staff_transport_matches(member, observed.entries) if isinstance(member, staff.StaffRow)
               else _native_address_matches(to, observed.entries)
               or (_registry_name_matches(to, observed.entries) if isinstance(staff_alias, NoStaffRecipient) else []))
    if matches or isinstance(scope, LocalOnly):
        return RecipientMatches(matches, LocalOnly(), observed.diagnostics)
    addressed = to.startswith(("session:", "worker:", "codex:"))
    if observed.problems and not addressed:
        return LocalRosterIncomplete(observed.diagnostics)
    peer = roster.peer_roster(os.environ)
    problems = [*observed.diagnostics, *peer.diagnostics]
    member = NoStaffRecipient() if native else _staff_recipient_match(to, membership, observed=[*observed.entries, *peer.entries])
    if isinstance(member, RecipientLookupFailed):
        return RecipientSearchFailed(member.reason, [*problems, member.reason])
    if isinstance(member, staff.StaffRow):
        matches = _staff_transport_matches(member, observed.entries)
        if matches:
            return RecipientMatches(matches, LocalOnly(), problems)
        matches = _staff_transport_matches(member, peer.entries)
    else:
        matches = (_native_address_matches(to, peer.entries)
                   or (_registry_name_matches(to, peer.entries) if isinstance(staff_alias, NoStaffRecipient) else []))
    if not matches and peer.problems:
        return RecipientSearchFailed("; ".join(peer.problems), problems)
    return RecipientMatches(matches, NamedMachine(roster.remote.other_machine()), problems)


def resolve_recipient(
    target: str | FixedStaffLookup, scope: RecipientResolutionScope, observed: roster.Roster, *,
    membership: staff.StaffSnapshot | None = None, records: list[sessions.SessionRecord] | None = None,
) -> DeliveryRoute:
    """Resolve locally first and require a current staff row for a fixed staff lookup."""
    from scripts.message import roster

    membership = staff.snapshot() if membership is None else membership
    to = target.to if isinstance(target, FixedStaffLookup) else target
    fixed_member = target.member(membership) if isinstance(target, FixedStaffLookup) else NoStaffRecipient()
    if isinstance(fixed_member, RecipientLookupFailed):
        usage_error(fixed_member.reason)
    problems = observed.diagnostics
    native = not isinstance(target, FixedStaffLookup) and to.startswith("session:")

    def lookup() -> DeliveryRoute:
        nonlocal problems
        if native and isinstance(scope, NamedMachine):
            return RemoteDelivery(scope.host, to, selected_conversation=True)
        if not isinstance(target, FixedStaffLookup) and to.startswith("uds:"):
            socket_records = sessions.live_sessions() if records is None else records
            return socket_recipient(to, socket_records, membership)
        member = (fixed_member if native or isinstance(fixed_member, staff.StaffRow)
                  else _staff_recipient_match(to, membership, observed=observed.entries))
        if isinstance(member, RecipientLookupFailed):
            usage_error(member.reason)
        if (not isinstance(scope, NamedMachine)
                and isinstance(member, staff.StaffRow) and member.kind == "worker"):
            holder = membership.parent_of(member)
            if holder is not None and (isinstance(holder.worktree, staff.WorktreeUnfilled)
                                       or holder.worktree.machine == roster.remote.local_machine()):
                return PaneWorkerRecipient(member)
        if isinstance(member, staff.StaffRow):
            for anomaly in observed.row_anomalies:
                if anomaly.row.id == member.id:
                    usage_error(anomaly.diagnostic)
        if isinstance(scope, NamedMachine):
            return RemoteDelivery(scope.host, (f"codex:{member.current_session}" if to.startswith("codex:")
                                              else to if to.startswith("worker:") else staff_queue_key(member.id))
                                  if isinstance(member, staff.StaffRow) else to)
        if to.startswith("codex:"):
            thread_id = to.removeprefix("codex:")
            if not thread_id:
                usage_error("codex: needs a thread id")
        resolution_scope = LocalOnly() if native and sessions.addressed(to, records or []) is not None else scope
        result = matching_recipients(to, resolution_scope, observed, membership=membership)
        problems = result.problems
        if isinstance(result, RecipientMatches) and result.entries:
            route = _matched_recipient(to, result.entries, result.destination)
            if (isinstance(route, RemoteDelivery) and route.selected_conversation
                    and isinstance(member, staff.StaffRow)):
                return RemoteDelivery(route.host, staff_queue_key(member.id))
            return route
        if isinstance(member, NoStaffRecipient) and not native:
            fallback = resolve_staff_recipient(to, membership, observed=observed.entries)
            if isinstance(fallback, RecipientLookupFailed):
                usage_error(fallback.reason)
            if isinstance(fallback, staff.StaffRow):
                return resolve_recipient(str(fallback.id), scope, observed, membership=membership, records=records)
        if isinstance(result, LocalRosterIncomplete):
            usage_error(f"absence of {to} here is not proven; use an address or --machine")
        if isinstance(result, RecipientSearchFailed):
            related = _recipient_roster_problems(to, problems, observed, membership)
            reason = (f"recipient lookup failed for {to!r}"
                      if any(problem in result.reason for problem in problems if problem not in related)
                      else result.reason)
            return RecipientLookupFailed(reason)
        if to.startswith("worker:"):
            usage_error(f"no roster session has address {to}")
        if to.startswith("codex:"):
            from scripts.whoami.agent_accounts import codex_home_display

            checked = ", ".join(codex_home_display(daemon.home) for daemon in codex_daemon.daemons(os.environ))
            return RecipientLookupFailed(
                f"no live Codex daemon has thread {to.removeprefix('codex:')} loaded; checked {checked}")
        if isinstance(member, staff.StaffRow):
            address = f"session:{member.current_session}"
            if sessions.addressed(address, records or [], membership=membership) is not None:
                return ClaudeRecipient(address)
            if isinstance(target, FixedStaffLookup):
                usage_error(f"no live session answers to staff ID {to}")
            return QueuedStaffRecipient(address, member.name, str(member.current_session))
        if to.startswith("session:"):
            return ClaudeRecipient(to)
        reason = f"no staff row or live session for {to!r}"
        print(f"send.py: {reason}", file=sys.stderr)
        raise RecipientNameMissing(reason)

    failed = True
    try:
        route = lookup()
        if native and not isinstance(route, RemoteDelivery):
            owner = _conversation_staff_owner(to[8:], membership)
            checked = _checked_staff_recipient(to, owner)
            if isinstance(checked, RecipientLookupFailed):
                usage_error(checked.reason)
        failed = isinstance(route, RecipientLookupFailed)
        return route
    finally:
        if failed:
            _print_roster_problems(to, problems, observed, membership)


def run(command: list[str], stdin: str, timeout: float, env: dict[str, str] | None = None,
        cwd: Path | None = None) -> tuple[int | None, str, str]:
    """Run command; (exit code or None on timeout, stdout, stderr).

    A timeout stops the process group. Notice deliveries instead inherit their
    supervisor's group, whose whole-job deadline also stops descendants.
    """
    grace = kill_grace()
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env, cwd=cwd, start_new_session=os.environ.get("SEND_REPEAT_NOTICE_JOB") != "1") as process:
        try:
            out, err = process.communicate(stdin, timeout=timeout)
            return process.returncode, out, err
        except subprocess.TimeoutExpired:
            for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, None)):
                try:
                    if os.environ.get("SEND_REPEAT_NOTICE_JOB") == "1":
                        process.send_signal(sig)
                    else:
                        process_group.send_process_group(process.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    out, err = process.communicate(timeout=grace if wait is None and os.environ.get("SEND_REPEAT_NOTICE_JOB") == "1" else wait)
                    return None, out, err
                except subprocess.TimeoutExpired:
                    continue
            return None, "", ""


def relay_prompt(message: Message) -> str:
    lines = [
        "You are a relay that delivers one message. Make exactly one SendMessage call:",
        f"- to: {json.dumps(message.to)}",
        f"- summary: {json.dumps(quote_sender_lines(message.summary))}",
        "- message: the text between the BEGIN and END lines below, copied exactly. Keep every character, space, "
        + "line break, backtick and placeholder as written. Add nothing, drop nothing, reword nothing. Leave out "
        + "the BEGIN and END lines.",
    ]
    if not message.to.startswith("uds:"):
        lines.append("If that call fails because the name matched no session or more than one, call ListAgents once, "
                     + f"then make the call once more with `to` set to the row whose name is exactly "
                     + f"{json.dumps(message.to)}, followed by its [ref]. If no row has that name, stop.")
    lines += ["Then stop: reply with one line, the SendMessage result. Call no other tool.",
              "----- BEGIN MESSAGE -----", agent_text(message, via_relay=True), "----- END MESSAGE -----"]
    return "\n".join(lines)


def one_line(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    return " ".join(as_str(as_dict(part).get("text")) for part in as_list(content))


def describe(answer: str) -> str:
    """A SendMessage result as one line: its message and msg_id when it is JSON."""
    fields = as_dict(loads(answer))
    said = as_str(fields.get("message")) or answer
    msg_id = as_str(fields.get("msg_id"))
    return one_line(f"{said} (msg_id {msg_id})" if msg_id else said)


@dataclass(frozen=True)
class CheckedRelayAcceptance:
    """The proxy completed the approved write to the checked recipient."""

    tool_use_id: str
    detail: str


@dataclass(frozen=True)
class RelayRequestNotForwarded:
    """The proxy did not start the recipient write."""


@dataclass(frozen=True)
class RelayRequestInterrupted:
    """The proxy may have sent part or all of the request."""

    reason: str


@dataclass(frozen=True)
class RelayRequestForwarded:
    """The proxy wrote the approved request on the checked recipient connection."""

    tool_use_id: str
    payload: str


class RefusedRelayRequest(Result):
    """The proxy refused the request before it wrote any bytes."""


RelayForward = RelayRequestNotForwarded | RelayRequestInterrupted | RelayRequestForwarded | RefusedRelayRequest


@dataclass
class RelayRecipientWrite:
    """Track the recipient write across the proxy thread and its caller."""

    state: RelayRequestNotForwarded | RelayRequestInterrupted | RelayRequestForwarded = field(
        default_factory=RelayRequestNotForwarded)


def read_relay(observed: RelayTransportResponse, text: str) -> CheckedRelayAcceptance | Result:
    """Use the proxy forward to decide delivery and the tool stream for details."""
    forward = observed.forward
    if isinstance(forward, RefusedRelayRequest):
        return forward
    if isinstance(forward, RelayRequestInterrupted):
        return _relay_problem(forward, forward.reason)
    if isinstance(forward, RelayRequestForwarded) and forward.payload != text:
        return Result("failed", "the proxy forwarded a different payload")
    events = [as_dict(loads(line)) for line in observed.stdout.splitlines()]
    mode = next((as_str(event.get("permissionMode")) for event in events
                 if event.get("type") == "system" and event.get("subtype") == "init"), "unknown")

    def report(detail: str, *, queue_if_unsent: bool = False) -> CheckedRelayAcceptance | Result:
        if isinstance(forward, RelayRequestForwarded):
            return CheckedRelayAcceptance(forward.tool_use_id, f"mode {mode}; verbatim; {detail}")
        return _relay_problem(forward, detail, queue_if_unsent=queue_if_unsent)

    sends = [part for event in events if event.get("type") == "assistant"
             for value in as_list(as_dict(event.get("message")).get("content"))
             if (part := as_dict(value)).get("type") == "tool_use" and part.get("name") == "SendMessage"]
    if len(sends) != 1:
        said = next((as_str(event.get("result")) for event in reversed(events) if event.get("type") == "result"), "")
        reason = f"{len(sends)} SendMessage calls" if sends else f"the relay made no SendMessage call: {one_line(said) or 'no output'}"
        return report(reason, queue_if_unsent=not sends)
    call = sends[0]
    inputs = as_dict(call.get("input"))
    if inputs.get("to") != observed.target or inputs.get("message") != text:
        return report("SendMessage used a different target or payload")
    tool_id = as_str(call.get("id"))
    results = [part for event in events if event.get("type") == "user"
               for value in as_list(as_dict(event.get("message")).get("content"))
               if (part := as_dict(value)).get("type") == "tool_result" and part.get("tool_use_id") == tool_id]
    if not tool_id or len(results) != 1:
        return report("SendMessage did not return one matching result")
    result = results[0]
    answer = result_text(result.get("content"))
    success = as_dict(loads(answer)).get("success")
    failed = result.get("is_error") is True
    detail = describe(answer)
    if failed or success is not True:
        if isinstance(forward, RelayRequestForwarded) and detail:
            detail = f"SendMessage did not confirm success: {detail}"
        return report(detail or "SendMessage did not confirm success", queue_if_unsent=failed or success is False)
    if isinstance(forward, RelayRequestNotForwarded):
        return Result("failed", "SendMessage has no matching completed proxy forward")
    if forward.tool_use_id != tool_id:
        return report("SendMessage used a different tool ID from the proxy forward")
    return report(detail)


def _relay_problem(forward: RelayForward, reason: str, *, queue_if_unsent: bool = False) -> Result:
    if isinstance(forward, RefusedRelayRequest):
        return forward
    if isinstance(forward, RelayRequestNotForwarded):
        return Result("queued" if queue_if_unsent else "failed", reason)
    if isinstance(forward, RelayRequestForwarded):
        return Result("sent", f"the proxy forwarded the approved payload; {reason}")
    return UncertainRelayDelivery("failed", f"{reason}; delivery unconfirmed")


def claude_binary() -> str | None:
    if os.access(CLAUDE, os.X_OK):
        return str(CLAUDE)
    return shutil.which("claude")


@dataclass(frozen=True)
class SocketAttemptValidated:
    """The fixed recipient still owns the socket."""


@dataclass(frozen=True)
class RelayTransportResponse:
    code: int | None
    stdout: str
    stderr: str
    target: str
    forward: RelayForward


RELAY_TOOL_GUARD = """
import json
from pathlib import Path
import sys

try:
    required = json.loads(Path(sys.argv[1]).read_text())
    event = json.load(sys.stdin)
    inputs = event["tool_input"]
    tool_id = event["tool_use_id"]
    if (event["tool_name"] != "SendMessage" or not isinstance(tool_id, str) or not tool_id
            or any(inputs.get(key) != value for key, value in required.items())):
        raise ValueError("unexpected SendMessage input")
    with Path(sys.argv[2]).open("x") as approval:
        approval.write(tool_id)
except Exception:
    sys.exit(2)
"""


def _relay_settings(directory: Path, message: Message, payload: str, approved: Path) -> str:
    expected = directory / "expected.json"
    write_atomic(expected, json.dumps({"to": message.to, "message": payload,
                                       "summary": quote_sender_lines(message.summary)}))
    guard = shlex.join([sys.executable, "-I", "-c", RELAY_TOOL_GUARD, str(expected), str(approved)])
    return json.dumps({
        "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
        "hooks": {"PreToolUse": [{"matcher": "SendMessage",
                                "hooks": [{"type": "command", "command": guard, "onFailure": "block"}]}]},
    })


CLAUDE_RELAY_ENVELOPE = re.compile(
    r'<cross-session-message'
    + "".join(rf'(?: {field}="[^"<>\r\n]+")?' for field in
              ("from", "from-session", "hop-chain", "from-name", "from-mode", "from-plugin"))
    + r'>\n(.*)\n</cross-session-message>', re.DOTALL)
# Claude caps the request at 1 Mi UTF-16 code units. Each unit uses at most three UTF-8 bytes.
CLAUDE_RELAY_MAX_BYTES = 3 * 1048576


# The two tag passes use the bracket sets from Claude Code 2.1.319.
_CLAUDE_RELAY_OPENERS = "<˂ᐸᚲ‹≮≺⊀⋖〈❬❮❰⟨⦑⦓⧼⩹⪦〈︿﹤＜𝈶≪⋘⪡⫷⟪︽"
_CLAUDE_RELAY_CLOSERS = ">˃ᐳ›≯≻⊁⋗〉❭❯❱⟩⧽〉﹀﹥＞𖼿𝈷"
_CLAUDE_RELAY_SLASHES = "/／∕⁄"
_CLAUDE_RELAY_NAME_CHARS = r"A-Za-z0-9_\-ſK"
_CLAUDE_RELAY_TAG_NAMES = ("cross-session-message", "teammate-message", "agent-message")
_CLAUDE_RELAY_TAG_GAP = (
      r"\u00ad\u034f\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u115f\u1160\u17b4\u17b5"
    + r"\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-"
    + r"\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    + r"\U00016fe4\U000e0000-\U000e0fff\u0300-\u0344\u0346-\u036f\u0483-\u0489\u0591-\u05bd\u05bf"
    + r"\u05c1\u05c2\u05c4\u05c5\u05c7\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e4"
    + r"\u06e7\u06e8\u06ea-\u06ed\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\u3099\u309a\ufe20-\ufe2f"
    + r"\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029"
)
_CLAUDE_RELAY_SEPARATORS = r"\-_ˍ˗֊־ـߺ᐀᠆‐‑‒–—―‗‿⁀⁃⁔⁻₋−➖⸗⸚⸺⸻⹀⹝〜〰゠ー︱︲︳︴﹍﹎﹏﹘﹣－＿ｰ𐵮𐺭"


# These Unicode folds only affect the text Claude reads to find a tag.
# The keys give the letters in each fold or a special character in that reading.
_CLAUDE_RELAY_FOLDS = {
    "-": "˗֊־᠆‐‑‒–—―⁃⁻₋−➖⸗⸚⸺⸻⹝〜〰ー︱︲﹘﹣－ｰ\U00010d6e𐺭",
    "0": "⁰₀⓪０\U0001ccf0𝟎𝟘𝟢𝟬𝟶🯰",
    "1": "¹₁①１\U0001ccf1𝟏𝟙𝟣𝟭𝟷🯱",
    "10": "⑩",
    "11": "⑪",
    "12": "⑫",
    "13": "⑬",
    "14": "⑭",
    "15": "⑮",
    "16": "⑯",
    "17": "⑰",
    "18": "⑱",
    "19": "⑲",
    "2": "²₂②２\U0001ccf2𝟐𝟚𝟤𝟮𝟸🯲",
    "20": "⑳",
    "21": "㉑",
    "22": "㉒",
    "23": "㉓",
    "24": "㉔",
    "25": "㉕",
    "26": "㉖",
    "27": "㉗",
    "28": "㉘",
    "29": "㉙",
    "3": "³₃③３\U0001ccf3𝟑𝟛𝟥𝟯𝟹🯳",
    "30": "㉚",
    "31": "㉛",
    "32": "㉜",
    "33": "㉝",
    "34": "㉞",
    "35": "㉟",
    "36": "㊱",
    "37": "㊲",
    "38": "㊳",
    "39": "㊴",
    "4": "⁴₄④４\U0001ccf4𝟒𝟜𝟦𝟰𝟺🯴",
    "40": "㊵",
    "41": "㊶",
    "42": "㊷",
    "43": "㊸",
    "44": "㊹",
    "45": "㊺",
    "46": "㊻",
    "47": "㊼",
    "48": "㊽",
    "49": "㊾",
    "5": "⁵₅⑤５\U0001ccf5𝟓𝟝𝟧𝟱𝟻🯵",
    "50": "㊿",
    "6": "⁶₆⑥６\U0001ccf6𝟔𝟞𝟨𝟲𝟼🯶",
    "7": "⁷₇⑦７\U0001ccf7𝟕𝟟𝟩𝟳𝟽🯷",
    "8": "⁸₈⑧８\U0001ccf8𝟖𝟠𝟪𝟴𝟾🯸",
    "9": "⁹₉⑨９\U0001ccf9𝟗𝟡𝟫𝟵𝟿🯹",
    "_": "\u02cdـߺ‗‿⁀⁔︳︴﹍﹎﹏＿",
    "a": (
          "ªÀÁÂÃÄÅàáâãäåĀāĂăĄąǍǎǞǟǠǡǺǻȀȁȂȃȦȧɑΆΑάαАаӐӑӒӓᎪᴀᴬᵃᵅḀḁẠạẢảẤấẦầẨẩẪẫẬậẮắẰằẲẳẴẵẶặἀἁἂἃἄἅἆἇἈἉἊἋἌἍἎ"
        + "ἏὰάᾀᾁᾂᾃᾄᾅᾆᾇᾈᾉᾊᾋᾌᾍᾎᾏᾰᾱᾲᾳᾴᾶᾷᾸᾹᾺΆᾼₐÅⒶⓐⱭⲀⲁꓮꭺＡａ\U0001ccd6𝐀𝐚𝐴𝑎𝑨𝒂𝒜𝒶𝓐𝓪𝔄𝔞𝔸𝕒𝕬𝖆𝖠𝖺𝗔𝗮𝘈𝘢𝘼𝙖𝙰𝚊𝚨𝛂𝛢𝛼𝜜𝜶𝝖𝝰𝞐𝞪𞀰𞁑"
        + "🄰"
    ),
    "au": "㍳",
    "b": "ʙΒβϐВвᏏᏴᏼᲀᴮᵇḂḃḄḅḆḇℬⒷⓑⲂⲃꓐꮟＢｂ𐞄\U0001ccd7𝐁𝐛𝐵𝑏𝑩𝒃\U0001d49d𝒷𝓑𝓫𝔅𝔟𝔹𝕓𝕭𝖇𝖡𝖻𝗕𝗯𝘉𝘣𝘽𝙗𝙱𝚋𝚩𝛣𝜝𝝗𝞑🄱",
    "bar": "㍴",
    "bq": "㏃",
    "c": (
          "ÇçĆćĈĉĊċČčϲϹСсᏟᲃᴄᶜḈḉℂℭⅭⅽⒸⓒⲤⲥꓚꟲꮯＣｃ\U0001ccd8𝐂𝐜𝐶𝑐𝑪𝒄𝒞𝒸𝓒𝓬\U0001d506𝔠\U0001d53a𝕔𝕮𝖈𝖢𝖼𝗖𝗰𝘊𝘤𝘾𝙘𝙲𝚌𞀿𞁞🄫"
        + "🄲"
    ),
    "cal": "㎈",
    "cc": "㏄",
    "cd": "㏅🄭",
    "cm": "㎝",
    "cm2": "㎠",
    "cm3": "㎤",
    "d": "ĎďԀԁᎠᏧᴅᴰᵈḊḋḌḍḎḏḐḑḒḓⅅⅆⅮⅾⒹⓓꓓꭰꮷＤｄ\U0001ccd9𝐃𝐝𝐷𝑑𝑫𝒅𝒟𝒹𝓓𝓭𝔇𝔡𝔻𝕕𝕯𝖉𝖣𝖽𝗗𝗱𝘋𝘥𝘿𝙙𝙳𝚍🄳",
    "da": "㍲",
    "db": "㏈",
    "dj": "🆐",
    "dl": "㎗",
    "dm": "㍷",
    "dm2": "㍸",
    "dm3": "㍹",
    "dz": "ǄǅǆǱǲǳ",
    "e": (
          "ÈÉÊËèéêëĒēĔĕĖėĘęĚěȄȅȆȇȨȩΈΕέεϵЀЁЕеѐёӖӗᎬᴇᴱᵉḔḕḖḗḘḙḚḛḜḝẸẹẺẻẼẽẾếỀềỂểỄễỆệἐἑἒἓἔἕἘἙἚἛἜἝὲέῈΈₑℯℰⅇⒺⓔⲈ"
        + "ⲉꓰꭼＥｅ\U0001ccda𝐄𝐞𝐸𝑒𝑬𝒆\U0001d4a0\U0001d4ba𝓔𝓮𝔈𝔢𝔼𝕖𝕰𝖊𝖤𝖾𝗘𝗲𝘌𝘦𝙀𝙚𝙴𝚎𝚬𝛆𝛜𝛦𝜀𝜖𝜠𝜺𝝐𝝚𝝴𝞊𝞔𝞮𝟄𞀵𞁖🄴"
    ),
    "erg": "㋍",
    "ev": "㋎",
    "f": "ᶠḞḟℱⒻⓕꓝꜰꟳＦｆ\U0001ccdb𝐅𝐟𝐹𝑓𝑭𝒇\U0001d4a1𝒻𝓕𝓯𝔉𝔣𝔽𝕗𝕱𝖋𝖥𝖿𝗙𝗳𝘍𝘧𝙁𝙛𝙵𝚏🄵",
    "fax": "℻",
    "ff": "ﬀ",
    "ffi": "ﬃ",
    "ffl": "ﬄ",
    "fi": "ﬁ",
    "fl": "ﬂ",
    "fm": "㎙",
    "g": "ĜĝĞğĠġĢģǦǧǴǵɡɢՑցᏀᴳᵍᶢḠḡℊⒼⓖꓖꞬꮐＧｇ𐞒\U0001ccdc𝐆𝐠𝐺𝑔𝑮𝒈𝒢\U0001d4bc𝓖𝓰𝔊𝔤𝔾𝕘𝕲𝖌𝖦𝗀𝗚𝗴𝘎𝘨𝙂𝙜𝙶𝚐🄶",
    "gal": "㏿",
    "gb": "㎇",
    "ghz": "㎓",
    "gpa": "㎬",
    "gy": "㏉",
    "h": (
          "ĤĥȞȟʜʰΉΗηНнҺһՀհᎻᏂᴴḢḣḤḥḦḧḨḩḪḫẖἨἩἪἫἬἭἮἯᾘᾙᾚᾛᾜᾝᾞᾟῊΉῌₕℋℌℍℎⒽⓗⲎⲏꓧꮋꮒＨｈ𐞖\U0001ccdd𝐇𝐡𝐻\U0001d455𝑯𝒉"
        + "\U0001d4a3𝒽𝓗𝓱\U0001d50b𝔥\U0001d53f𝕙𝕳𝖍𝖧𝗁𝗛𝗵𝘏𝘩𝙃𝙝𝙷𝚑𝚮𝛨𝜢𝝜𝞖🄷"
    ),
    "ha": "㏊",
    "hg": "㋌",
    "hp": "㏋",
    "hpa": "㍱",
    "hv": "🅊",
    "hz": "㎐",
    "i": (
          "ÌÍÎÏìíîïĨĩĪīĬĭĮįİıƖǏǐȈȉȊȋɩɪͅΊΐΙΪίιϊІЇіїᴵᵢᶥᶦḬḭḮḯỈỉỊịἰἱἲἳἴἵἶἷἸἹἺἻἼἽἾἿὶίιῐῑῒΐῖῗῘῙῚΊⁱℐℑℹⅈⅠⅰⒾⓘⲒ"
        + "ⲓꓲꞮＩｉ\U0001ccde𝐈𝐢𝐼𝑖𝑰𝒊\U0001d4a4𝒾𝓘𝓲\U0001d50c𝔦𝕀𝕚𝕴𝖎𝖨𝗂𝗜𝗶𝘐𝘪𝙄𝙞𝙸𝚒𝚤𝚰𝛊𝛪𝜄𝜤𝜾𝝞𝝸𝞘𝞲𞁌𞁨🄸"
    ),
    "ii": "Ⅱⅱ",
    "iii": "Ⅲⅲ",
    "ij": "Ĳĳ",
    "in": "㏌",
    "iu": "㍺",
    "iv": "Ⅳⅳ",
    "ix": "Ⅸⅸ",
    "j": "ĴĵǰʲͿϳЈјᎫᴊᴶⅉⒿⓙⱼꓙꭻＪｊ\U0001ccdf𝐉𝐣𝐽𝑗𝑱𝒋𝒥𝒿𝓙𝓳𝔍𝔧𝕁𝕛𝕵𝖏𝖩𝗃𝗝𝗷𝘑𝘫𝙅𝙟𝙹𝚓𞁍🄹",
    "k": "ĶķǨǩΚκϰЌКкќᏦᴋᴷᵏḰḱḲḳḴḵₖKⓀⓚⲔⲕꓗꮶＫｋ\U0001cce0𝐊𝐤𝐾𝑘𝑲𝒌𝒦𝓀𝓚𝓴𝔎𝔨𝕂𝕜𝕶𝖐𝖪𝗄𝗞𝗸𝘒𝘬𝙆𝙠𝙺𝚔𝚱𝛋𝛞𝛫𝜅𝜘𝜥𝜿𝝒𝝟𝝹𝞌𝞙𝞳𝟆𞀹𞁚🄺",
    "ka": "㎄",
    "kb": "㎅",
    "kcal": "㎉",
    "kg": "㎏",
    "khz": "㎑",
    "kk": "㏍",
    "kl": "㎘",
    "km": "㎞㏎",
    "km2": "㎢",
    "km3": "㎦",
    "kpa": "㎪",
    "kt": "㏏",
    "kv": "㎸",
    "kw": "㎾",
    "l": "ĹĺĻļĽľʟˡӀӏᏞᴸᶫḶḷḸḹḺḻḼḽₗℒℓⅬⅼⓁⓛꓡꮮＬｌ\U0001cce1𝐋𝐥𝐿𝑙𝑳𝒍\U0001d4a7𝓁𝓛𝓵𝔏𝔩𝕃𝕝𝕷𝖑𝖫𝗅𝗟𝗹𝘓𝘭𝙇𝙡𝙻𝚕𞁐🄻",
    "lj": "Ǉǈǉ",
    "lm": "㏐",
    "ln": "㏑",
    "log": "㏒",
    "ltd": "㋏",
    "lx": "㏓",
    "m": "µΜμМмᎷᴍᴹᵐḾḿṀṁṂṃₘℳⅯⅿⓂⓜⲘⲙꓟꮇＭｍ\U0001cce2𝐌𝐦𝑀𝑚𝑴𝒎\U0001d4a8𝓂𝓜𝓶𝔐𝔪𝕄𝕞𝕸𝖒𝖬𝗆𝗠𝗺𝘔𝘮𝙈𝙢𝙼𝚖𝚳𝛭𝜧𝝡𝞛𞀻🄼",
    "m2": "㎡",
    "m3": "㎥",
    "ma": "㎃",
    "mb": "㎆㏔",
    "mc": "🅪",
    "md": "🅫",
    "mg": "㎎",
    "mhz": "㎒",
    "mil": "㏕",
    "ml": "㎖",
    "mm": "㎜",
    "mm2": "㎟",
    "mm3": "㎣",
    "mol": "㏖",
    "mpa": "㎫",
    "mr": "🅬",
    "ms": "㎳",
    "mv": "㎷㎹🅋",
    "mw": "㎽㎿",
    "n": "ÑñŃńŅņŇňǸǹɴՈոᴺᶰṄṅṆṇṈṉṊṋⁿₙℕⓃⓝⲚⲛꓠＮｎ\U0001cce3𝐍𝐧𝑁𝑛𝑵𝒏𝒩𝓃𝓝𝓷𝔑𝔫\U0001d545𝕟𝕹𝖓𝖭𝗇𝗡𝗻𝘕𝘯𝙉𝙣𝙽𝚗𝚴𝛮𝜨𝝢𝞜🄽",
    "na": "㎁",
    "nf": "㎋",
    "nj": "Ǌǋǌ",
    "nm": "㎚",
    "no": "№",
    "ns": "㎱",
    "nv": "㎵",
    "nw": "㎻",
    "o": (
          "ºÒÓÔÕÖòóôõöŌōŎŏŐőƠơǑǒǪǫǬǭȌȍȎȏȪȫȬȭȮȯȰȱΌΟοόОоӦӧՕօᲂᴏᴼᵒṌṍṎṏṐṑṒṓỌọỎỏỐốỒồỔổỖỗỘộỚớỜờỞởỠỡỢợὀὁὂὃὄὅὈ"
        + "ὉὊὋὌὍὸόῸΌₒℴⓄⓞⲞⲟꓳＯｏ\U0001cce4𝐎𝐨𝑂𝑜𝑶𝒐𝒪\U0001d4c4𝓞𝓸𝔒𝔬𝕆𝕠𝕺𝖔𝖮𝗈𝗢𝗼𝘖𝘰𝙊𝙤𝙾𝚘𝚶𝛐𝛰𝜊𝜪𝝄𝝤𝝾𝞞𝞸𞀼𞁜🄾"
    ),
    "ov": "㍵",
    "p": "ΡρϱРрᏢᴘᴾᵖᵨṔṕṖṗῤῥῬₚℙⓅⓟⲢⲣꓑꮲＰｐ\U0001cce5𝐏𝐩𝑃𝑝𝑷𝒑𝒫𝓅𝓟𝓹𝔓𝔭\U0001d547𝕡𝕻𝖕𝖯𝗉𝗣𝗽𝘗𝘱𝙋𝙥𝙿𝚙𝚸𝛒𝛠𝛲𝜌𝜚𝜬𝝆𝝔𝝦𝞀𝞎𝞠𝞺𝟈𞀾🄿",
    "pa": "㎀㎩",
    "pc": "㍶",
    "pf": "㎊",
    "ph": "㏗",
    "ppm": "㏙",
    "ppv": "🅎",
    "pr": "㏚",
    "ps": "㎰",
    "pte": "㉐",
    "pv": "㎴",
    "pw": "㎺",
    "q": "ԚԛԶզℚⓆⓠꟴＱｑ𐞥\U0001cce6𝐐𝐪𝑄𝑞𝑸𝒒𝒬𝓆𝓠𝓺𝔔𝔮\U0001d548𝕢𝕼𝖖𝖰𝗊𝗤𝗾𝘘𝘲𝙌𝙦𝚀𝚚🅀",
    "r": (
          "ŔŕŖŗŘřƦȐȑȒȓʀʳᎡᏒᴿᵣṘṙṚṛṜṝṞṟℛℜℝⓇⓡꓣꭱꮢＲｒ𐞪\U0001cce7𝐑𝐫𝑅𝑟𝑹𝒓\U0001d4ad𝓇𝓡𝓻\U0001d515𝔯\U0001d549𝕣𝕽𝖗𝖱"
        + "𝗋𝗥𝗿𝘙𝘳𝙍𝙧𝚁𝚛🄬🅁"
    ),
    "rad": "㎭",
    "rs": "₨",
    "s": "ŚśŜŝŞşŠšſȘșˢЅѕᏚṠṡṢṣṤṥṦṧṨṩẛₛⓈⓢꓢꜱ\ua7f1ꮪＳｓ\U0001cce8𝐒𝐬𝑆𝑠𝑺𝒔𝒮𝓈𝓢𝓼𝔖𝔰𝕊𝕤𝕾𝖘𝖲𝗌𝗦𝘀𝘚𝘴𝙎𝙨𝚂𝚜𞁩🅂",
    "sd": "🅌",
    "sm": "℠",
    "sr": "㏛",
    "ss": "ßẞ🅍",
    "st": "ﬅﬆ",
    "sv": "㏜",
    "t": "ŢţŤťȚțΤτТтᎢᲄᲅᴛᵀᵗṪṫṬṭṮṯṰṱẗₜⓉⓣⲦⲧꓔꭲＴｔ\U0001cce9𝐓𝐭𝑇𝑡𝑻𝒕𝒯𝓉𝓣𝓽𝔗𝔱𝕋𝕥𝕿𝖙𝖳𝗍𝗧𝘁𝘛𝘵𝙏𝙩𝚃𝚝𝚻𝛕𝛵𝜏𝜯𝝉𝝩𝞃𝞣𝞽𞁀🅃",
    "tel": "℡",
    "thz": "㎔",
    "tm": "™",
    "u": (
          "ÙÚÛÜùúûüŨũŪūŬŭŮůŰűŲųƯưǓǔǕǖǗǘǙǚǛǜȔȕȖȗΰϋύՍսᏌᴜᵁᵘᵤᶸṲṳṴṵṶṷṸṹṺṻỤụỦủỨứỪừỬửỮữỰựὐὑὒὓὔὕὖὗὺύῠῡῢΰῦῧⓊⓤꓴ"
        + "ꮜＵｕ\U0001ccea𝐔𝐮𝑈𝑢𝑼𝒖𝒰𝓊𝓤𝓾𝔘𝔲𝕌𝕦𝖀𝖚𝖴𝗎𝗨𝘂𝘜𝘶𝙐𝙪𝚄𝚞𝛖𝜐𝝊𝞄𝞾🅄"
    ),
    "v": "ѴѵѶѷᏙᴠᵛᵥṼṽṾṿⅤⅴⓋⓥⱽꓦꮩＶｖ\U0001cceb𝐕𝐯𝑉𝑣𝑽𝒗𝒱𝓋𝓥𝓿𝔙𝔳𝕍𝕧𝖁𝖛𝖵𝗏𝗩𝘃𝘝𝘷𝙑𝙫𝚅𝚟𝛎𝜈𝝂𝝼𝞶🅅",
    "vi": "Ⅵⅵ",
    "vii": "Ⅶⅶ",
    "viii": "Ⅷⅷ",
    "w": "ŴŵʷԜԝᎳᏔᴡᵂẀẁẂẃẄẅẆẇẈẉẘ\u209dⓌⓦꓪꮃꮤＷｗ\U0001ccec𝐖𝐰𝑊𝑤𝑾𝒘𝒲𝓌𝓦𝔀𝔚𝔴𝕎𝕨𝖂𝖜𝖶𝗐𝗪𝘄𝘞𝘸𝙒𝙬𝚆𝚠🅆",
    "wb": "㏝",
    "wc": "🅏",
    "wz": "🄮",
    "x": "ˣΧχХхᵡᵪẊẋẌẍₓⅩⅹⓍⓧⲬⲭꓫＸｘ\U0001cced𝐗𝐱𝑋𝑥𝑿𝒙𝒳𝓍𝓧𝔁𝔛𝔵𝕏𝕩𝖃𝖝𝖷𝗑𝗫𝘅𝘟𝘹𝙓𝙭𝚇𝚡𝚾𝛘𝛸𝜒𝜲𝝌𝝬𝞆𝞦𝟀𞁃𞁡🅇",
    "xi": "Ⅺⅺ",
    "xii": "Ⅻⅻ",
    "y": (
          "ÝýÿŶŷŸȲȳʏʸΎΫϒϓϔЎУуўӮӯӰӱӲӳᎩẎẏẙỲỳỴỵỶỷỸỹὙὛὝὟῨῩῪΎ\u209eⓎⓨⲨⲩꓬꭹＹｙ𐞲\U0001ccee𝐘𝐲𝑌𝑦𝒀𝒚𝒴𝓎𝓨𝔂𝔜𝔶𝕐𝕪𝖄𝖞𝖸𝗒𝗬𝘆"
        + "𝘠𝘺𝙔𝙮𝚈𝚢𝚼𝛶𝜰𝝪𝞤𞁁𞁟🅈"
    ),
    "z": (
          "ŹźŻżŽžΖζᏃᴢᶻẐẑẒẓẔẕ\u209fℤℨⓏⓩⲌⲍꓜꮓＺｚ\U0001ccef𝐙𝐳𝑍𝑧𝒁𝒛𝒵𝓏𝓩𝔃\U0001d51d𝔷\U0001d551𝕫𝖅𝖟𝖹𝗓𝗭𝘇𝘡𝘻𝙕𝙯𝚉𝚣𝚭𝛧𝜡"
        + "𝝛𝞕🅉"
    ),
    "\ue100": "Νν",
    "\ue101": "Υυ",
    "\ue1f0": "᐀⹀゠",
}
_CLAUDE_RELAY_READING = {
    ord(ch): chr(0xe000 + ord(fold[0])) + "".join(chr(0xe200 + ord(tail)) for tail in fold[1:])
    if fold.isascii() else fold
    for fold, glyphs in _CLAUDE_RELAY_FOLDS.items()
    for ch in glyphs
}
# Original private-use characters cannot act as markers in the folded reading.
_CLAUDE_RELAY_READING.update(dict.fromkeys(range(0xe020, 0xe07f), "\ufffd"))
_CLAUDE_RELAY_READING.update(dict.fromkeys(range(0xe100, 0xe200), "\ufffd"))
_CLAUDE_RELAY_READING.update(dict.fromkeys(range(0xe220, 0xe27f), "\ufffd"))


def _claude_relay_tag_pattern(tag: str, *, folded: bool) -> str:
    """Build the tag spelling for one of the two escape passes."""
    letters: list[str] = []
    for char in tag:
        if char == "-":
            choices = r"_\-\ue05f\ue02d\ue25f\ue22d\ue1f0" if folded else _CLAUDE_RELAY_SEPARATORS
        else:
            choices = char + char.upper()
            if char == "s":
                choices += "ſ"
            if folded:
                choices += chr(0xe000 + ord(char)) + chr(0xe200 + ord(char))
                if char in "nv":
                    choices += "\ue100"
                if char in "uy":
                    choices += "\ue101"
        letters.append(f"[{choices}]")
    return f"[{_CLAUDE_RELAY_TAG_GAP}]*+".join(letters)


_CLAUDE_RELAY_OPENER = rf"[{_CLAUDE_RELAY_OPENERS}](?!\\)"
_CLAUDE_RELAY_TAG_BOUNDARY = rf"(?![{_CLAUDE_RELAY_NAME_CHARS}])"
_CLAUDE_RELAY_TAG_FILLER = _CLAUDE_RELAY_NAME_CHARS + _CLAUDE_RELAY_OPENERS + _CLAUDE_RELAY_CLOSERS
_CLAUDE_RELAY_FOLDED_TAGS = "|".join(_claude_relay_tag_pattern(tag, folded=True) for tag in _CLAUDE_RELAY_TAG_NAMES)
_CLAUDE_RELAY_FIRST_ESCAPE = re.compile(
    _CLAUDE_RELAY_OPENER
    + rf"(?=[^{_CLAUDE_RELAY_TAG_FILLER}]*?(?:{_CLAUDE_RELAY_FOLDED_TAGS}){_CLAUDE_RELAY_TAG_BOUNDARY})"
)
_CLAUDE_RELAY_CLOSE_NAME = _claude_relay_tag_pattern(_CLAUDE_RELAY_TAG_NAMES[0], folded=False)
_CLAUDE_RELAY_CLOSE_ESCAPE = re.compile(
    _CLAUDE_RELAY_OPENER
    + rf"(?=[^{_CLAUDE_RELAY_TAG_FILLER}{_CLAUDE_RELAY_SLASHES}]*+[{_CLAUDE_RELAY_SLASHES}]"
    + rf"[^{_CLAUDE_RELAY_TAG_FILLER}]*+{_CLAUDE_RELAY_CLOSE_NAME}{_CLAUDE_RELAY_TAG_BOUNDARY})"
)
_CLAUDE_RELAY_ANY_OPENER = re.compile(f"[{_CLAUDE_RELAY_OPENERS}]")


def _encode_claude_relay_body(text: str) -> str:
    """Match the message body escape in Claude Code 2.1.319."""
    if _CLAUDE_RELAY_ANY_OPENER.search(text) is None:
        return text

    # Find message tags in the folded reading. Replace only the opener in the original text.
    reading = text.translate(_CLAUDE_RELAY_READING)
    expanded = len(reading) != len(text)
    parts: list[str] = []
    start = position = offset = 0
    for match in _CLAUDE_RELAY_FIRST_ESCAPE.finditer(reading):
        if expanded:
            while offset < match.start():
                char = text[position]
                offset += len(_CLAUDE_RELAY_READING.get(ord(char), char))
                position += 1
        else:
            position = match.start()
        parts.extend((text[start:position], "<\\"))
        start = position + 1
    parts.append(text[start:])

    # The envelope also escapes closing cross-session tags in the original spelling.
    return _CLAUDE_RELAY_CLOSE_ESCAPE.sub(r"<\\", "".join(parts))


def _matched_relay_payload(wire: bytearray, expected: str) -> str | RefusedRelayRequest:
    """Match the Claude message body inside one request and its optional auth row."""
    try:
        rows = wire.decode("utf-8").removesuffix("\n").split("\n", 2)
    except UnicodeError:
        return RefusedRelayRequest("failed", "the relay request is not UTF-8")
    if len(rows) == 2:
        auth = as_dict(loads(rows[0]))
        if auth.get("type") != "auth" or not as_str(auth.get("token")):
            return RefusedRelayRequest("failed", "the relay request has an invalid auth row")
    elif len(rows) != 1:
        return RefusedRelayRequest("failed", "the relay sent more than one request")
    request = as_dict(loads(rows[-1]))
    message = as_dict(request.get("message"))
    if (request.get("type") != "user" or request.get("priority") != "next" or "file_attachments" in request
            or message.get("role") != "user"):
        return RefusedRelayRequest("failed", "the relay request has an invalid user frame")
    envelope = CLAUDE_RELAY_ENVELOPE.fullmatch(as_str(message.get("content")))
    if envelope is None or envelope[1] != _encode_claude_relay_body(expected):
        return RefusedRelayRequest("failed", "the relay request does not match the approved payload")
    return expected


def _forward_relay_socket(listener: socket.socket, peer: socket.socket, stop: socket.socket,
                          attempt: ClaudeDeliveryAttempt, approved: Path,
                          expected: str, write: RelayRecipientWrite) -> RelayForward | unit_lookup.SocketUnavailable:
    """Forward the approved request on the connection that passed the owner check."""
    request_bytes = bytearray()
    with selectors.DefaultSelector() as reader:
        _ = reader.register(listener, selectors.EVENT_READ)
        _ = reader.register(stop, selectors.EVENT_READ)
        if all(key.fileobj is stop for key, _ in reader.select()):
            return write.state
        with listener.accept()[0] as connection:
            _ = reader.unregister(listener)
            connection.settimeout(peer.gettimeout())
            readers = {connection: peer, peer: connection}
            for incoming in readers:
                _ = reader.register(incoming, selectors.EVENT_READ)
            stopping = False
            while readers:
                ready = reader.select(0 if stopping else None)
                if not ready and stopping:
                    break
                for key, _ in ready:
                    if key.fileobj is stop:
                        _ = reader.unregister(stop)
                        stopping = True
                        continue
                    incoming = cast(socket.socket, key.fileobj)
                    outgoing = readers[incoming]
                    data = incoming.recv(65536)
                    if data and incoming is connection:
                        request_bytes.extend(data)
                        if len(request_bytes) > CLAUDE_RELAY_MAX_BYTES:
                            return RefusedRelayRequest("failed", "the relay request exceeds the Claude socket limit")
                        continue
                    if data:
                        outgoing.sendall(data)
                        continue
                    if incoming is connection:
                        if not request_bytes.endswith(b"\n"):
                            return write.state
                        payload = _matched_relay_payload(request_bytes, expected)
                        if isinstance(payload, RefusedRelayRequest):
                            return payload
                        tool_id = approved.read_text(encoding="utf-8").strip()
                        if not tool_id:
                            return RefusedRelayRequest("failed", "the relay tool call has no approval")
                        if isinstance(attempt.fallback_pane, HerdrPlace):
                            pane = _validate_fallback_pane(attempt, attempt.fallback_pane, peer.gettimeout() or 1.0)
                            if isinstance(pane, unit_lookup.SocketUnavailable):
                                return pane
                        check = unit_lookup.validate_claude_connection(peer, attempt.pid, attempt.started)
                        if isinstance(check, unit_lookup.SocketUnavailable):
                            return check
                        owner = _validate_attempt_recipient(attempt)
                        if isinstance(owner, unit_lookup.SocketUnavailable):
                            return owner
                        write.state = RelayRequestInterrupted("the proxy did not complete the request")
                        peer.sendall(request_bytes)
                        write.state = RelayRequestForwarded(tool_id, payload)
                    outgoing.shutdown(socket.SHUT_WR)
                    _ = reader.unregister(incoming)
                    del readers[incoming]
    return write.state


@contextmanager
def _relay_proxy_directory() -> Generator[str]:
    """Remove the proxy directory when the sender closes its pipe or dies."""
    program = """
import sys
from tempfile import TemporaryDirectory

with TemporaryDirectory(prefix="claude-send-", dir="/tmp") as directory:
    print(directory, flush=True)
    sys.stdin.buffer.read()
"""
    with subprocess.Popen([sys.executable, "-I", "-c", program], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True, start_new_session=True) as cleanup:
        assert cleanup.stdout is not None
        directory = cast(TextIO, cleanup.stdout).readline().strip()
        if not directory:
            raise OSError("cannot create the private relay socket directory")
        yield directory


def _validate_attempt_recipient(attempt: ClaudeDeliveryAttempt) -> SocketAttemptValidated | unit_lookup.SocketUnavailable:
    recipient = attempt.recipient
    if isinstance(recipient, RelayProcessRecipient):
        if (sessions.process_start(attempt.pid) != attempt.started
                or not isinstance(live_relay_identity(attempt.pid, STATE / "relay_senders"), RelayIdentity)):
            return unit_lookup.SocketUnavailable("the relay process identity is unreadable")
    else:
        current = unit_lookup.current_claude_record(
            attempt.pid, attempt.started, conversation_recipient(recipient).conversation)
        if isinstance(current, unit_lookup.SocketUnavailable):
            return current
        if current["messagingSocketPath"] != attempt.socket:
            return unit_lookup.ClaudeOwnerMismatch("the Claude messaging socket changed before delivery")
    if isinstance(attempt.staff_requirement, FixedStaffLookup):
        assert isinstance(recipient, StaffClaudeRecipient)
        staff_session = _current_staff_session(attempt.staff_requirement, ExpectedStaffSession(recipient.current.conversation))
        if isinstance(staff_session, RecipientLookupFailed):
            return unit_lookup.SocketUnavailable(staff_session.reason)
    return SocketAttemptValidated()


def _validate_fallback_pane(attempt: ClaudeDeliveryAttempt, place: HerdrPlace,
                            timeout: float) -> SocketAttemptValidated | unit_lookup.SocketUnavailable:
    from scripts.herdr import herdr

    recipient = attempt.recipient
    assert not isinstance(recipient, RelayProcessRecipient)
    try:
        report = herdr.Herdr(place.session).pane_session_report(place.pane, deadline=time.monotonic() + timeout)
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return unit_lookup.SocketUnavailable(f"cannot read the Claude pane report before delivery: {error}")
    if isinstance(report, herdr.ReportedAgentSession) and report.session_id != conversation_recipient(recipient).conversation:
        return unit_lookup.ClaudeOwnerMismatch("the Claude pane reports a different conversation")
    return SocketAttemptValidated()


def _bound_relay(message: Message, timeout: float, command: list[str], env: dict[str, str],
                 attempt: ClaudeDeliveryAttempt
                 ) -> RelayTransportResponse | unit_lookup.SocketUnavailable | Result:
    write = RelayRecipientWrite()
    try:
        check = _validate_attempt_recipient(attempt)
        if isinstance(check, unit_lookup.SocketUnavailable):
            return check
        peer = unit_lookup.connect_claude_socket(attempt.socket, attempt.pid, attempt.started, timeout=min(timeout, 1.0))
        if isinstance(peer, unit_lookup.SocketUnavailable):
            return peer
        with peer:
            check = _validate_attempt_recipient(attempt)
            if isinstance(check, unit_lookup.SocketUnavailable):
                return check
            with (_relay_proxy_directory() as directory,
                  socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener):
                private = Path(directory)
                proxy = str(private / "delivery.sock")
                target = replace(message, to=f"uds:{proxy}")
                approved = private / "approved"
                payload = agent_text(target, via_relay=True)
                settings = _relay_settings(private, target, payload, approved)
                listener.bind(proxy)
                listener.listen(1)
                cancel, stop = socket.socketpair()
                with cancel, stop, ThreadPoolExecutor(max_workers=1) as executor:
                    try:
                        forwarding = executor.submit(_forward_relay_socket, listener, peer, stop, attempt,
                                                     approved, payload, write)
                    except RuntimeError as error:
                        return _relay_problem(write.state, f"cannot start the relay proxy thread: {error}")
                    try:
                        result = run([*command, "--settings", settings], relay_prompt(target), timeout, env, Path.home())
                    finally:
                        cancel.close()
                    try:
                        forwarded = forwarding.result()
                    except (OSError, ValueError) as error:
                        return _relay_problem(write.state, f"cannot forward through the checked socket: {error}")
        if isinstance(forwarded, unit_lookup.SocketUnavailable):
            return forwarded
        return RelayTransportResponse(*result, target.to, forwarded)
    except (OSError, ValueError) as error:
        return _relay_problem(write.state, f"cannot relay through the checked socket: {error}")


def relay(message: Message, timeout: float) -> Result:
    attempt = message.claude_attempt
    if not isinstance(attempt, ClaudeDeliveryAttempt):
        return Result("failed", "no Claude delivery attempt was selected")
    claude = claude_binary()
    if claude is None:
        return Result("queued", "no claude binary found")
    tools = "SendMessage"
    # An inherited CLAUDE_* variable makes the relay pass for the caller's session:
    # CLAUDE_CODE_MESSAGING_SOCKET, for one, is that session's inbox.
    env = {name: value for name, value in os.environ.items() if not name.startswith("CLAUDE")}
    if not TYPE_CHECKING:
        from scripts.accounts import accounts
        from scripts.whoami.account import named_account_environment

    try:
        selected = accounts.account_for_unscoped_launch(os.environ)
    except accounts.AccountsError as error:
        return Result("queued", str(error))
    if not isinstance(selected, accounts.LegacyHomeLaunch):
        env.update(named_account_environment(selected.dir))
    model = models_registry.claude_model(MODEL, env)
    command = [claude, "-p", "--model", model, "--permission-mode", PERMISSION_MODE, "--permission-prompts", "none",
               "--tools", tools, "--allowedTools", tools,
               "--setting-sources", "",
               "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
               "-n", relay_session_name(message.sender), "--output-format", "stream-json", "--verbose"]
    # A Mac keeps Claude's login in its keychain, which ssh cannot open; the user's
    # `claude setup-token` token, encrypted behind gpg-agent, stands in for it.
    token_file = Path.home() / ".config" / "claude" / "oauth-token.gpg"
    try:
        if token_file.exists():
            token = subprocess.run(["gpg", "--pinentry-mode", "error", "--quiet", "--decrypt", str(token_file)],
                                   capture_output=True, text=True, timeout=10, check=False)
            if token.returncode != 0:
                return Result("queued", f"gpg-agent is cold on {machine()}: run github-warmup there")
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token.stdout.strip()
        relay_env = models_registry.agent_environment(env)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return Result("failed", f"cannot prepare the relay: {error}")
    command = [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", RELAY_EXEC, str(STATE / "relay_senders"), message.sender, *command]
    observed = _bound_relay(message, timeout, command, relay_env, attempt)
    if isinstance(observed, unit_lookup.ClaudeSocketAbsent):
        return ConfirmedSocketMiss("queued", observed.reason)
    if isinstance(observed, unit_lookup.SocketUnavailable):
        return Result("failed", observed.reason)
    if isinstance(observed, Result):
        return observed
    code, out, err = observed.code, observed.stdout, observed.stderr
    try:
        raw = STATE / "relay" / f"{file_name(message.to)}.jsonl"
        raw.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(raw, out + err)
    except OSError as error:
        print(f"message relay log: {error}", file=sys.stderr)
    if code is None and not isinstance(observed.forward, RelayRequestForwarded):
        return _relay_problem(observed.forward, f"relay timed out after {timeout:g} s")
    result = read_relay(observed, agent_text(message, via_relay=True))
    if isinstance(result, CheckedRelayAcceptance):
        return Result("sent", result.detail)
    if result.outcome == "queued" and code != 0:
        return Result("queued", f"relay exit {code}: {result.detail}; {one_line(err, 200)}")
    return result


def _claude_delivery_plan(message: Message, mailbox: ClaudeMailbox, registry: sessions.RegistryRead,
                          records: list[sessions.SessionRecord]) -> RelayPlan:
    conversation = conversation_recipient(mailbox).conversation
    checked = unit_lookup.checked_conversation(conversation, registry=registry, probe_address=False)
    candidates = [record for record in records if record["sessionId"] == conversation]
    if isinstance(checked, unit_lookup.ClaudeArgvUnavailable):
        try:
            _ = staff.native_id(conversation)
        except staff.StaffRefused as error:
            return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(str(error)))
        registered = [item.record for item in registry.entries if item.record["sessionId"] == conversation]
        if registry.unreadable or len(candidates) != 1 or candidates != registered:
            return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(checked.reason))
        candidate = candidates[0]
        started = candidate.get("procStart", "")
        path = candidate["messagingSocketPath"]
        if candidate["pid"] != checked.pid or not started or not path:
            return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(checked.reason))
        attempt = ClaudeDeliveryAttempt(mailbox, checked.pid, started, path, fallback_pane=checked.place)
        return RelayAttempt(replace(message, to=f"uds:{path}", claude_attempt=attempt))
    if isinstance(checked, unit_lookup.UnknownUnit):
        return ClaudeRoutingFailed(unit_lookup.SocketUnavailable(checked.reason))
    if isinstance(checked, unit_lookup.ProvedAbsentUnit):
        if registry.unreadable:
            return ClaudeRoutingFailed(unit_lookup.SocketUnavailable("the Claude registry is unreadable"))
        if candidates:
            return ClaudeRoutingFailed(unit_lookup.SocketUnavailable("the routing snapshot names an owner that could not be proved"))
        return SessionIdWithoutLiveRecipient(mailbox)
    generation = checked.generation
    claude = checked.owner.claude if isinstance(checked.owner, unit_lookup.LabeledSession) else checked.owner
    if not isinstance(claude, unit_lookup.LiveClaude) or not isinstance(generation, session_staff.ProvedProcessGeneration):
        return ClaudeRoutingFailed(unit_lookup.SocketUnavailable("the conversation has no proved Claude process generation"))
    paths = {row["messagingSocketPath"] for row in candidates
             if row["pid"] == generation.pid and row.get("procStart", "") == generation.started}
    if len(paths) != 1:
        return ClaudeRoutingFailed(unit_lookup.ClaudeOwnerMismatch("the proved Claude owner differs from the routing snapshot"))
    path = paths.pop()
    if not path:
        if registry.unreadable:
            return ClaudeRoutingFailed(unit_lookup.SocketUnavailable("the Claude registry is unreadable"))
        return SessionIdWithoutLiveRecipient(mailbox, generation)
    attempt = ClaudeDeliveryAttempt(mailbox, generation.pid, generation.started, path)
    return RelayAttempt(replace(message, to=f"uds:{path}", claude_attempt=attempt))


def relay_plan(message: Message, registry: sessions.RegistryRead, records: list[sessions.SessionRecord],
               membership: staff.StaffSnapshot, mailbox: ClaudeMailbox | NoRelayReplyMailbox) -> RelayPlan:
    """Bind delivery to the conversation and process in the routing snapshot."""
    if message.to.startswith("uds:"):
        return _socket_delivery_plan(message, registry, records, membership, mailbox)
    if isinstance(mailbox, NoRelayReplyMailbox):
        return ClaudeRoutingFailed(unit_lookup.SocketUnavailable("no Claude conversation was selected at routing"))
    return _claude_delivery_plan(message, mailbox, registry, records)


def _postmaster_recipient(attempt: RelayAttempt, records: list[sessions.SessionRecord],
                          membership: staff.StaffSnapshot) -> postmaster.PostmasterRecipient:
    # The postmaster consumer does not check the recipient's process generation.
    del attempt, records, membership
    return postmaster.RecipientNotInRegistry()


def deliver_codex(
    message: Message,
    recipient: CodexSessionRecipient | CodexPaneRecipient,
    timeout: float,
    *,
    selection: RecipientSelection,
) -> Result:
    text = agent_text(message)
    if isinstance(selection, FixedStaffLookup):
        current = _current_staff_session(selection, ExpectedStaffSession(recipient.thread_id))
        if isinstance(current, RecipientLookupFailed):
            return Result("failed", current.reason)
    if isinstance(recipient, CodexPaneRecipient):
        try:
            _prompt_pane(recipient.place, text, target=recipient.thread_id, timeout=timeout)
            return Result("sent", f"{recipient.address} at {recipient.place.session}:{recipient.place.pane}")
        except (herdr_types.HerdrFailure, OSError) as error:
            return Result("failed", str(error))
    queued = codex_daemon.queue(recipient.daemon, recipient.thread_id, text, timeout=timeout)
    if isinstance(queued, codex_daemon.Queued):
        return Result("sent", f"queued on Codex session {recipient.name}")
    if isinstance(queued, codex_daemon.QueueTimedOut):
        return Result("failed", f"timed out after {queued.seconds:g} s")
    return Result("failed", queued.reason)


def action_arguments(action: UserAction) -> list[str]:
    if isinstance(action, ActionRequired):
        return ["--action", action.text]
    return ["--no-action"]


def reviewed_failure(stdout: str, original: FailedPush, *,
                     prefix: str = "pushover: reviewed JSON: ", machine: str = "",
                     remote_record: bool = False) -> FailedPush:
    for line in stdout.splitlines():
        if not line.startswith(prefix):
            continue
        record = as_dict(loads(line.removeprefix(prefix)))
        title, body = record.get("title"), record.get("body")
        action = parse_user_action(optional_str(record.get("action")), record.get("action") is None)
        if isinstance(title, str) and isinstance(body, str) and isinstance(action, (ActionRequired, NoActionRequired)):
            source = record.get("held_source")
            identity = record.get("held_identity")
            hold: RevisionHold = (HeldRevision(source, identity, machine)
                                  if isinstance(source, str) and source and isinstance(identity, str) and identity
                                  else NO_HELD_REVISION)
            reviewed = record.get("reviewed") is True if remote_record else record.get("reviewed") is not False
            need = record.get("need") if remote_record else original.need
            if need not in ("note", "decision", "blocked"):
                need = original.need
            session = record.get("session")
            if isinstance(session, str):
                body = session_prefix(SessionLine(session)) + body
            return FailedPush(title, action, body, hold, reviewed=reviewed, need=need, held=record.get("held") is True,
                              text_id=as_str(record.get("text_id")) or original.text_id,
                              sender=as_str(record.get("sender")) or original.sender)
        return original
    return original


def user(message: Message, need: Need, action: UserAction, timeout: float, *, answerable: bool = False, text_id: str = "", values: tuple[str, ...] = (), due: str = "", reference: push_reference.PushReference = push_reference.NO_REFERENCE, remote_forward: bool = False, silence_policy: silence.SilencePolicy = silence.SilencePolicy.KEEP_DURING_SILENCE) -> Result:
    """Reach the user. The channel is this function's business alone."""
    key = ["--key", message.key] if message.key else []
    selected_priority = decision_priority.priority(need, due)
    command = [sys.executable, str(USER_CHANNEL), "--priority", selected_priority, "--source", message.sender, *key,
               *(["--answerable"] if answerable else []), *action_arguments(action), message.summary, message.text]
    if text_id:
        command = [sys.executable, str(USER_CHANNEL), "--priority", selected_priority, "--source", message.sender, *key,
                   "--text-id", text_id, *(["--answerable"] if answerable else [])]
        for value in values:
            command.extend(["--value", value])
    if silence_policy == silence.SilencePolicy.SEND_THROUGH_SILENCE:
        command[2:2] = ["--through-silence"]
    if due:
        command[2:2] = ["--due", due]
    if reference["kind"] != "none":
        command[2:2] = ["--push-reference", json.dumps(reference)]
    if remote_forward:
        command[2:2] = ["--ssh-forwarded-reference"]
    code, out, err = run(command, "", timeout)
    if code == 6:
        failed = reviewed_failure(out, FailedPush(message.summary, action, message.text, need=need, held=True, text_id=text_id, sender=message.sender))
        return Result("held", "not sent, failed review: " + (err.removeprefix("pushover: not sent: ").strip() or "review failed"), failed)
    if code == 5:
        return Result("replaced", "a newer version replaced this push; nothing was sent")
    if code == 4:
        return Result("held", err.removesuffix("\n") or out.removesuffix("\n"))
    if code == 0:
        kept = next((line.removeprefix("pushover: kept: ") for line in out.splitlines()
                     if line.startswith("pushover: kept: ")), "")
        for line in out.splitlines():
            if line.startswith("pushover: reviewed JSON: "):
                record = as_dict(loads(line.removeprefix("pushover: reviewed JSON: ")))
                if record.get("sent_despite_failed_review") is True:
                    if text_id:
                        template_fix_notice(record)
                    rules = record.get("failed_rules", [])
                    if not kept:
                        return Result("sent", "to the user, sent despite failed review: " + str(rules))
        if kept:
            return Result("kept", f"for the user: {kept}; it goes out when silence ends")
        before, separator, sent = out.partition("pushover: sent as rewritten:\n")
        del before
        return Result("sent", "to the user, rewritten:\n" + sent.removesuffix("\n") if separator else "to the user")
    why = one_line(err or out) or ("timed out" if code is None else f"exit {code}")
    if code == 2 and f"{REFUSED_BEFORE_SENDING}:" in err:
        return Result("refused", why)
    failed = reviewed_failure(out, FailedPush(message.summary, action, message.text, need=need, text_id=text_id, sender=message.sender))
    return Result("failed", failure_detail(f"the user was not reached: {why}", failed), failed)


class TemplateFixErrorRecord(TypedDict):
    time: str
    text_id: str
    error: str


def template_fix_notice(record: dict[str, object]) -> None:
    """Launch one session notice per template per day without waiting for another claim."""
    text_id = as_str(record.get("text_id"))
    if not text_id or push_texts.user_specified(text_id):
        return
    directory = review.state_path() / "template-fixes"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        text_key = hashlib.sha256(text_id.encode()).hexdigest()
        key = now().astimezone(user_zone()).date().isoformat() + "-" + text_key
        stamp = directory / key
        with (directory / (text_key + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            if stamp.exists():
                return
            recipient = "natedev"
            try:
                for line in review.NOTIFY_CONFIG.read_text(encoding="utf-8").splitlines():
                    name, separator, value = line.partition("#")[0].partition("=")
                    if separator and name.strip() == "TEMPLATE_FIX_SESSION" and value.strip():
                        recipient = value.strip()
            except OSError:
                pass
            text = "Fix registry push " + text_id + " today: it was sent despite failed review.\n" + json.dumps(record, ensure_ascii=False)
            command = [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).resolve()), "template-fix-deliver", text_id, recipient, text]
            # Delivery and its error record run in the detached child.
            _ = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            write_atomic(stamp, text_id)
    except OSError as error:
        print(f"send.py: template fix notice failed for {text_id}: {error}", file=sys.stderr)
        log_template_fix_error(text_id, str(error))


def log_template_fix_error(text_id: str, error: str) -> None:
    try:
        review.state_path().mkdir(parents=True, exist_ok=True)
        with (review.state_path() / "template-fix-errors.jsonl").open("a", encoding="utf-8") as output:
            entry: TemplateFixErrorRecord = {"time": now().isoformat(), "text_id": text_id, "error": error}
            _ = output.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def deliver_template_fix(text_id: str, recipient: str, text: str) -> None:
    """Record recipient refusals as well as transport failures, with no retry."""
    command = [sys.executable, str(Path(__file__).resolve()), "--to", recipient, "--from", "push template review",
               "--summary", "Fix push template " + text_id, "--text", text]
    try:
        code, out, err = run(command, "", send_budget.seconds())
        if code != 0:
            log_template_fix_error(text_id, (err or out).strip() or ("timed out" if code is None else f"exit {code}"))
    except OSError as error:
        log_template_fix_error(text_id, str(error))


def recipient_report(errors: str) -> tuple[str, str]:
    """Internal forwards report their resolved address without owning another log row."""
    receiver = ""
    diagnostics: list[str] = []
    for line in errors.splitlines():
        if line.startswith("SEND_RECIPIENT: "):
            receiver = line.removeprefix("SEND_RECIPIENT: ")
        else:
            diagnostics.append(line)
    return receiver, "\n".join(diagnostics)


def remote(message: Message, host: str, options: Options) -> Result:
    """Forward delivery and queue ownership; this invocation owns its accounting."""
    selection = options.selection
    target = selection.to if isinstance(selection, FixedStaffLookup) else message.to
    forwarded = ["--no-accounting", "--report-recipient", "--here", "--to", target, "--from", message.sender, "--summary", message.summary,
                 "--timeout", f"{options.timeout:g}"]
    if isinstance(selection, FixedStaffLookup):
        forwarded += ["--recipient-kind", selection.kind]
    forwarded += ["--forwarded-sender-stamp", sender_native_id(message.stamp)]
    if isinstance(options.selection, ForwardedConversation):
        forwarded += ["--forwarded-conversation"]
    if message.to != USER:
        forwarded += ["--forwarded-shown-sender", message.shown_sender]
    if options.human:
        forwarded += ["--human"]
    if message.key is not None:
        forwarded += ["--key", message.key]
        if isinstance(options.recipient_identity, LogicalRecipientIdentity):
            forwarded += ["--recipient-key", options.recipient_identity.name]
    if message.to == USER:
        forwarded += ["--need", options.need]
        if options.silence_policy == silence.SilencePolicy.SEND_THROUGH_SILENCE:
            forwarded += ["--through-silence"]
        if options.reference["kind"] != "none":
            forwarded += ["--push-reference", json.dumps(options.reference)]
        if options.due:
            forwarded += ["--due", options.due]
        if options.need == "blocked":
            # This host refused a push with no clear step and keeps its --clears-when record.
            forwarded += ["--self-clearing"]
        if options.text_id:
            forwarded += ["--text-id", options.text_id]
            for value in options.values:
                forwarded += ["--value", value]
        if options.answerable:
            forwarded += ["--answerable"]
        if isinstance(options.action, NoUserRecipient):
            raise AssertionError("a user message must state its action")
        forwarded += action_arguments(options.action)
    if options.repeat_minutes is not None:
        forwarded += ["--repeat-minutes", f"{options.repeat_minutes:g}"]
    elif message.to == USER and options.need == "blocked" and message.key is not None:
        # This host's claim holds the emergency's lifetime window; the run on host must not apply it again.
        forwarded += ["--repeat-minutes", "0"]
    command = ["ssh", "-o", "BatchMode=yes", host, f"{'CLAUDE_PID= ' if message.to == USER else ''}{REMOTE} {shlex.join(forwarded)}"]
    code, out, err = run(command, message.text, send_budget.remote_seconds(options.timeout))
    receiver, err = recipient_report(err)
    failure: FailureText = NO_FAILED_PUSH
    if message.to == USER and "send.py: failed push JSON: " in err:
        if isinstance(options.action, NoUserRecipient):
            raise AssertionError("a user message must state its action")
        failure = reviewed_failure(err, FailedPush(message.summary, options.action, message.text, need=options.need, text_id=options.text_id, sender=message.sender),
                                   prefix="send.py: failed push JSON: ", machine=host, remote_record=True)
    outcome = send_outcome(code, out)
    if isinstance(outcome, UnconfirmedSendOutcome):
        why = one_line(err or out) or ("timed out" if code is None else f"exit {code}")
        return Result("held" if isinstance(failure, FailedPush) and failure.held else "failed", f"ssh {host}: {why}", failure)
    parsed = outcome.header
    detail = parsed.detail
    if parsed.payload:
        detail += "\n" + parsed.payload
    return Result(parsed.outcome, f"on {host}: {detail}", failure, receiver)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def user_failure(message: Message, action: MessageUserAction, result: Result, need: Need, text_id: str = "") -> Result:
    if isinstance(action, NoUserRecipient):
        raise AssertionError("a user message must state its action")
    failed = result.failure
    if isinstance(failed, NoFailedPush):
        failed = FailedPush(message.summary, action, message.text, need=need, text_id=text_id, sender=message.sender)
    result = Result("held" if failed.held else "failed", failure_detail(result.detail, failed), failed)
    try:
        pid = os.environ.get("CLAUDE_PID", "")
        if not pid:
            return Result(result.outcome, result.detail, failed)
        record = as_dict(loads((SESSIONS / f"{pid}.json").read_text(encoding="utf-8")))
        session_id = as_str(record.get("sessionId"))
        if session_id and Path(session_id).name == session_id and session_id not in (".", ".."):
            entry: FailedPushRecord = {"time": time.time(), "title": failed.title,
                "action": failed.action.text if isinstance(failed.action, ActionRequired) else None,
                "body": failed.body, "message": failed.message, "fallback": failed.text,
                "reviewed": failed.reviewed, "need": failed.need, "held": failed.held,
                "text_id": failed.text_id, "sender": failed.sender}
            if isinstance(failed.hold, HeldRevision):
                entry["held_source"] = failed.hold.source
                entry["held_identity"] = failed.hold.identity
                entry["held_machine"] = failed.hold.machine
            directory = Path(os.environ.get("NOTIFY_STATE_DIR", str(Path.home() / ".local/state/notify"))) / "failed"
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                write_atomic(directory / f"{session_id}.json", json.dumps(entry))
    except OSError as error:
        print(f"send.py: could not write fallback marker: {error}", file=sys.stderr)
    return Result(result.outcome, result.detail, failed)


def own_session_name() -> str:
    """The name of the Claude session this runs under, or "" when there is none."""
    try:
        record = as_dict(loads((SESSIONS / f"{os.environ['CLAUDE_PID']}.json").read_text(encoding="utf-8")))
    except (KeyError, OSError):
        return ""
    return as_str(record.get("name"))


def default_sender() -> str:
    """send.py, and the Claude session it runs under when there is one."""
    name = own_session_name()
    return f"send.py in {name}" if name else "send.py"


def auto_mode_values(values: dict[str, str], commands: list[str], sender: str) -> dict[str, str]:
    """The auto_mode.refused values: the session gives the facts and each refused command, in order.
    This fills the sender, the kind of action (rca.action_of), the waits sentence and the grammar,
    and shows each command on its own line."""
    if not sender:
        raise ValueError(AUTO_MODE_TEXT + " is sent by a Claude session: its sender is that session's name,"
                         + " and this run has no session record")
    given = set(values)
    required = {"session", "machine", "waits"}
    if not required <= given or not given <= required | {"rule"}:
        raise ValueError(AUTO_MODE_TEXT + " takes --value session, machine, command, waits and an optional rule;"
                         + f" missing {sorted(required - given)}; extra {sorted(given - required - {'rule'})}")
    shown = [command.strip() for command in commands]
    if not shown or not all(shown):
        raise ValueError(AUTO_MODE_TEXT + ": give each refused command as its own --value command=..., none empty")
    shown = [command if len(command) <= REFUSED_COMMAND_LENGTH else command[:REFUSED_COMMAND_LENGTH - 1] + "…"
             for command in shown]
    waits = values["waits"].strip().rstrip(".")
    if typed_texts(waits) or named_places(waits):
        raise ValueError(AUTO_MODE_TEXT + ": waits says only what waits; it tells the user to type nothing"
                         + " and names no pane or session. The Action line says what the user does")
    one = len(shown) == 1
    rule = values.get("rule", "").strip()
    return {"sender": sender, "session": values["session"], "machine": values["machine"],
            "action": rca.action_of(commands), "refused": "\n".join(shown),
            "noun": "command" if one else "commands", "this": "this" if one else "these",
            "waits_line": rca.waits_line(waits), "rule_line": " The rule: " + rule if rule else ""}


def auto_mode_key(session: str, commands: tuple[str, ...]) -> str:
    """The key an auto_mode.refused alert goes with when the session gives none: one per session and set of
    commands, as rca.py joins its cases, so `send.py retire` finds the alert of a fixed case."""
    joined = "\n".join(sorted({rca.one_line(command) for command in commands}))
    return f"auto-mode:{session}:{hashlib.sha256(joined.encode()).hexdigest()[:12]}"


def reviewed_waits(options: Options) -> Options:
    """Review the waits sentence within the configured budget.

    Keep the sentence only when the reviewer approves it. Send the alert without the sentence if the review fails.
    """
    values = {key: item for key, _, item in (value.partition("=") for value in options.values)}
    if not values.get("waits_line") or review.switched_off():
        return options
    rendered = push_texts.render(AUTO_MODE_TEXT, **values)
    draft = review.Draft(rendered.sender, decision_priority.priority(options.need, options.due), rendered.title, rendered.action,
                         rendered.body, False, rendered.session)
    outcome = review.review(draft, False, limit=waits_review_seconds())
    if isinstance(outcome, review.Approved):
        return options
    why = outcome.why if isinstance(outcome, review.Unreviewed | review.Withheld) else ", ".join(outcome.failed_rules)
    print(f"send.py: {AUTO_MODE_TEXT} goes without its waits sentence: the review gave {why}", file=sys.stderr)
    values["waits_line"] = ""
    rendered = push_texts.render(AUTO_MODE_TEXT, **values)
    return options._replace(values=tuple(f"{key}={item}" for key, item in values.items()),
                            text=session_prefix(rendered.session) + rendered.body)


def paid_credits_values(text_id: str, values: dict[str, str], session: str, named: str | None) -> dict[str, str]:
    """The quota.paid_credits or quota.paid_credits_queued values with the sender filled: this session's own
    name. A script with no session record names itself with --value sender or --from (a remote send forwards both)."""
    sender = session or values.get("sender", "") or named or ""
    if not sender.strip():
        raise ValueError(text_id + " needs a sender: send it from a Claude session, or name the script with --from")
    return values | {"sender": sender}


def needs_go_values(values: dict[str, str], sender: str) -> dict[str, str]:
    """The session.needs_go values with the sender filled: this session's own name. The template ends
    why and does with its own period."""
    if not sender:
        raise ValueError(NEEDS_GO_TEXT + " is sent by a Claude session: its sender is that session's name,"
                         + " and this run has no session record")
    if "sender" in values:
        raise ValueError(NEEDS_GO_TEXT + " takes no --value sender: it is this session's own name")
    ends = {key: values[key].strip().rstrip(".") for key in ("why", "does") if key in values}
    return values | ends | {"sender": sender}


def reports_auto_mode_refusal(text: str) -> bool:
    return AUTO_MODE_REPORT.search(text) is not None and AUTO_MODE_REFUSAL.search(text) is not None


def refusal_report(options: Options) -> rca.Report:
    """Open a case for the session with refused commands, the waits sentence, and the alert's sender and key."""
    values = {key: item for key, _, item in (value.partition("=") for value in options.values)}
    return rca.Report(values["session"], values["machine"], "auto_mode", options.commands,
                      waits=values["waits_line"].strip().removesuffix("."), source=options.sender or "",
                      key=options.key or "")


def wrong_template(options: Options, shown: str) -> str | None:
    """The hold detail when a user push tells in free text what a registered template carries, else None."""
    if options.to != USER:
        return None
    registered_text = bool(options.text_id) and not push_texts.agent_written(options.text_id)
    if options.text_id != AUTO_MODE_TEXT and options.text_id not in RCA_TEXTS and reports_auto_mode_refusal(shown):
        return AUTO_MODE_HELD
    if not registered_text and PAID_CREDITS_REPORT.search(shown) is not None:
        return PAID_CREDITS_HELD
    if not registered_text and NEEDS_GO_REPORT.search(shown) is not None:
        return NEEDS_GO_HELD
    return None


def attr(args: argparse.Namespace, name: str) -> object:
    return getattr(args, name)  # pyright: ignore[reportAny]


def optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def parse(argv: list[str]) -> Options:
    parser = argparse.ArgumentParser(prog="send.py",
                                     description=(
                                         "Send one message to a roster name, uds:<path>, "
                                         "session:<id>, codex:<thread id>, or user. "
                                         "Use worker:<unit>/<worker> for a pane worker. "
                                     ),
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="Also: send.py ack KEY | reopen KEY | pending [NAME]\n\n"
                                     + "Registered pushes a session or script sends with --text-id:\n"
                                     + "\n".join(f"  {text_id}: {use}" for text_id, use in SESSION_TEMPLATES.items()))
    _ = parser.add_argument(
        "--to",
        required=True,
        help=(
            "a roster name, uds:<path>, session:<id>, "
            "codex:<thread id>, or user. "
            "Use worker:<unit>/<worker> for a pane worker. "
        ),
    )
    _ = parser.add_argument("--recipient-kind", choices=get_args(staff.Kind),
                            help="require a current staff row of this kind at the fixed staff ID in --to")
    _ = parser.add_argument("--text-id", default="", help="registered script push")
    _ = parser.add_argument("--value", action="append", default=[], help="registry template key=value")
    _ = parser.add_argument("--answerable", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--through-silence", action="store_true", help="to `user`: send during silence with the same review and priority")
    _ = parser.add_argument("--from", dest="sender", help="the sender name the recipient sees")
    _ = parser.add_argument("--human", action="store_true", help="the message was written by a person, including a verbatim human relay")
    _ = parser.add_argument("--no-accounting", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--report-recipient", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--forwarded-shown-sender", help=argparse.SUPPRESS)
    _ = parser.add_argument("--forwarded-sender-stamp", help=argparse.SUPPRESS)
    _ = parser.add_argument("--forwarded-conversation", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--summary", help="SendMessage's short label, default the first line; the title to `user`")
    _ = parser.add_argument("--priority", choices=("0", "1", "2"), help=argparse.SUPPRESS)
    _ = parser.add_argument("--need", choices=list(CHANNEL_PRIORITY), default="note",
                            help="to `user`: what they must do (default note: nothing)")
    _ = parser.add_argument("--question", default="", help="the open question record for decision.choices")
    _ = parser.add_argument("--push-reference", default="", help=argparse.SUPPRESS)
    _ = parser.add_argument("--due", default="", help="decision deadline in ISO 8601 with an explicit offset")
    _ = parser.add_argument("--action", help="to `user`: what the user does")
    _ = parser.add_argument("--no-action", action="store_true", help="to `user`: nothing is needed")
    clears = parser.add_mutually_exclusive_group()
    _ = clears.add_argument("--clears-when", metavar="CMD",
                            help="to `user` with --need blocked: a shell command that exits 0 when the cause is fixed;"
                            + " the push is then cancelled")
    _ = clears.add_argument("--self-clearing", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--key", help=("names the message for --repeat-minutes, ack and the queue; a kept push to"
                                           + " the user replaces an older kept one with this key from the same sender"))
    _ = parser.add_argument("--recipient-key", help="stable logical recipient for keyed deduplication across address changes")
    _ = parser.add_argument("--repeat-minutes", type=float,
                           help="skip a delivered or kept send with --key inside this window; failed sends do not count")
    location = parser.add_mutually_exclusive_group()
    _ = location.add_argument("--machine", help="deliver from a configured machine alias or host name")
    _ = location.add_argument("--here", action="store_true", help="resolve and deliver only here")
    _ = parser.add_argument("--timeout", type=float, default=delivery_timeout(), help=f"delivery seconds (default {delivery_timeout():g})")
    body = parser.add_mutually_exclusive_group()
    _ = body.add_argument("--text")
    _ = body.add_argument("--file")
    args = parser.parse_args(argv)
    forwarded_stamp = optional_str(attr(args, "forwarded_sender_stamp"))
    origin: RuntimeSender | ForwardedSender = RuntimeSender()
    if forwarded_stamp is not None:
        if attr(args, "here") is not True:
            parser.error("--forwarded-sender-stamp needs --here")
        if "SSH_CONNECTION" not in os.environ:
            parser.error("--forwarded-sender-stamp needs an SSH receive environment (SSH_CONNECTION)")
        try:
            origin = ForwardedSender(read_sender_stamp(forwarded_stamp))
        except ValueError as error:
            parser.error(str(error))
    to = as_str(attr(args, "to"))
    text_id = as_str(attr(args, "text_id"))
    try:
        reference = push_reference.admission_reference(text_id, as_str(attr(args, "question")), as_str(attr(args, "push_reference")),
            remote_forward=isinstance(origin, ForwardedSender))
    except (OSError, ValueError) as error:
        usage_error(str(error).replace("\n", " "))
    if reference["kind"] == "question" and attr(args, "key") is None:
        args.key = "question:" + reference["path"]
    raw_values = cast(list[str], attr(args, "value"))
    # Only auto_mode.refused repeats a key: one command value for each refused command.
    commands: list[str] = []
    if text_id:
        if to != USER:
            parser.error("--text-id goes with --to user")
        from scripts.notify import push_texts
        values: dict[str, str] = {}
        for value in raw_values:
            key, separator, item = value.partition("=")
            if separator and key == "command" and text_id == AUTO_MODE_TEXT:
                commands.append(item)
                continue
            if not separator or key in values:
                parser.error("--value needs distinct key=value pairs")
            values[key] = item
        try:
            if text_id in (AUTO_MODE_TEXT, NEEDS_GO_TEXT):
                if attr(args, "sender") is not None or attr(args, "machine") is not None:
                    parser.error(text_id + " takes no --from or --machine: it goes from this session's own name and machine")
                sender = own_session_name()
                values = (auto_mode_values(values, commands, sender) if text_id == AUTO_MODE_TEXT
                          else needs_go_values(values, sender))
                raw_values = [f"{key}={item}" for key, item in values.items()]
            elif text_id in (PAID_CREDITS_TEXT, PAID_CREDITS_QUEUED_TEXT):
                values = paid_credits_values(text_id, values, own_session_name(), optional_str(attr(args, "sender")))
                raw_values = [f"{key}={item}" for key, item in values.items()]
            rendered = push_texts.render(text_id, **values)
            # A value must not give a second action: the Action line is the one thing the user does.
            conflicts = action_conflicts(rendered.action, rendered.body)
            if conflicts:
                raise ValueError(text_id + " gives a second action: " + "; ".join(conflicts))
        except ValueError as error:
            parser.error(str(error))
        args.summary, args.text = rendered.title, session_prefix(rendered.session) + rendered.body
        args.need = "blocked" if as_str(attr(args, "need")) == "blocked" else rendered.need
        args.action = rendered.action.text if isinstance(rendered.action, ActionRequired) else None
        args.no_action = isinstance(rendered.action, NoActionRequired)
        args.sender = optional_str(attr(args, "sender")) or rendered.sender
        if text_id == "github.keys_cold":
            args.key = "github.keys_cold:" + values["machine"]
            if attr(args, "self_clearing") is not True:
                args.clears_when = "github-warm-status"
        if text_id == AUTO_MODE_TEXT and attr(args, "key") is None:
            args.key = auto_mode_key(values["session"], tuple(commands))
    elif raw_values:
        parser.error("--value needs --text-id")
    priority = optional_str(attr(args, "priority"))
    if priority is not None:
        args.need = {"0": "note", "1": "decision", "2": "blocked"}[priority]
    due = as_str(attr(args, "due"))
    try:
        _ = decision_priority.read_due(due)
    except ValueError as error:
        parser.error(str(error))
    if due and (to != USER or attr(args, "need") != "decision"):
        parser.error("--due needs --to user and --need decision")
    action_text = optional_str(attr(args, "action"))
    no_action = attr(args, "no_action") is True
    if to != USER:
        if attr(args, "through_silence") is True:
            parser.error("--through-silence must go with --to user")
        if attr(args, "answerable") is True:
            parser.error("--answerable goes with --to user")
        if action_text is not None or no_action:
            parser.error("--action and --no-action go with --to user")
        action: MessageUserAction = NoUserRecipient()
    else:
        parsed_action = parse_user_action(action_text, no_action)
        if isinstance(parsed_action, ActionRefused):
            parser.error(f"{parsed_action.reason}. {FIX}")
        if isinstance(parsed_action, ActionUnstated):
            parser.error(FIX)
        action = parsed_action
    kind = optional_str(attr(args, "recipient_kind"))
    selection: RecipientSelection = RosterLookup(to)
    if attr(args, "forwarded_conversation") is True:
        if not isinstance(origin, ForwardedSender) or not re.fullmatch(r"session:[\w-]+", to):
            parser.error("--forwarded-conversation needs a forwarded session address")
        selection = ForwardedConversation(to)
    if kind is not None:
        if to == USER or isinstance(selection, ForwardedConversation):
            parser.error("--recipient-kind requires a fixed staff ID in --to")
        selection = FixedStaffLookup(staff.StaffId(to), cast(staff.Kind, kind))
    host = optional_str(attr(args, "machine"))
    if host is not None:
        mapped = machine_routes.machine_alias(host)
        if isinstance(mapped, machine_routes.MachineNameRefused):
            parser.error(mapped.reason)
        host = mapped.alias
    forwarded_sender = optional_str(attr(args, "forwarded_shown_sender"))
    if forwarded_sender is not None and (attr(args, "here") is not True or not forwarded_sender):
        parser.error("--forwarded-shown-sender needs --here and nonempty sending-machine display text")
    scope: RecipientResolutionScope = (
        NamedMachine(host) if host is not None else
        LocalOnly() if attr(args, "here") is True else LocalThenPeer()
    )
    get = {name: attr(args, name) for name in Options._fields
           if name not in {"action", "selection", "scope", "values", "commands", "shown_sender", "sender_origin", "reference", "recipient_identity", "silence_policy"}}
    recipient_key = optional_str(attr(args, "recipient_key"))
    options = Options(selection=selection, sender=optional_str(get["sender"]), summary=optional_str(get["summary"]),
                      key=optional_str(get["key"]), repeat_minutes=optional_float(get["repeat_minutes"]),
                      scope=scope, timeout=optional_float(get["timeout"]) or delivery_timeout(),
                      need=cast(Need, get["need"]), action=action, text=optional_str(get["text"]),
                      file=optional_str(get["file"]), answerable=get["answerable"] is True, text_id=text_id, values=tuple(raw_values),
                      commands=tuple(commands), clears_when=optional_str(get["clears_when"]) or "",
                      self_clearing=get["self_clearing"] is True,
                      human=get["human"] is True, no_accounting=get["no_accounting"] is True,
                      report_recipient=get["report_recipient"] is True,
                      shown_sender=ShownSender(forwarded_sender) if forwarded_sender is not None else SenderNotResolved(),
                      sender_origin=origin, due=due, reference=reference,
                      silence_policy=(silence.SilencePolicy.SEND_THROUGH_SILENCE if attr(args, "through_silence") is True
                                      else silence.SilencePolicy.KEEP_DURING_SILENCE),
                      recipient_identity=LogicalRecipientIdentity(recipient_key) if recipient_key else TransportRecipientIdentity())
    if options.to == USER and options.summary is None:
        parser.error("--to user needs --summary, its title, and takes no --codex")
    if options.to != USER and options.need != "note":
        parser.error("--need goes with --to user")
    if options.to == USER and options.need != "note" and not isinstance(options.action, ActionRequired):
        parser.error(f"--need {options.need} requires --action. {FIX}")
    if isinstance(options.recipient_identity, LogicalRecipientIdentity) and options.key is None:
        parser.error("--recipient-key needs --key")
    if options.repeat_minutes is not None and options.key is None:
        parser.error("--repeat-minutes needs --key")
    if get["clears_when"] is not None and not options.clears_when.strip():
        parser.error("--clears-when needs a command")
    if (options.clears_when or options.self_clearing) and (options.to != USER or options.need != "blocked"):
        parser.error("--clears-when and --self-clearing go with --to user --need blocked")
    return options


class RetireRequest(NamedTuple):
    to: str
    sender: str
    key: str


def parse_retire(argv: list[str]) -> RetireRequest:
    parser = argparse.ArgumentParser(prog="send.py retire")
    _ = parser.add_argument("--to", required=True)
    _ = parser.add_argument("--from", dest="sender", required=True)
    _ = parser.add_argument("--key", required=True)
    args = parser.parse_args(argv)
    return RetireRequest(
        as_str(attr(args, "to")),
        as_str(attr(args, "sender")),
        as_str(attr(args, "key")),
    )


def usage_error(text: str) -> NoReturn:
    print(f"send.py: {text}", file=sys.stderr)
    sys.exit(2)


def body_text(options: Options) -> str:
    if options.text is not None:
        return options.text
    if options.file is not None:
        try:
            return Path(options.file).read_text(encoding="utf-8")
        except OSError as error:
            usage_error(f"cannot read --file: {error}")
    if sys.stdin.isatty():
        usage_error("give the message with --text, --file or stdin")
    return sys.stdin.read()


def sender_shown_name(sender: str, stamp: SenderStamp, observed: roster.Roster) -> str:
    """Resolve the display from the native id without changing reply addresses."""
    from scripts.message import roster

    if isinstance(stamp, UnavailableSender):
        return sender
    matches = _native_address_matches(sender_native_id(stamp), observed.entries)
    if len(matches) == 1:
        return roster.shown_name(matches[0]).render()
    return sender


def sender_stamp() -> SenderStamp:
    """Read the native sender identity independently of the display name."""
    from scripts.message import roster

    identity = roster.sender_identity(os.environ)
    if isinstance(identity, roster.ClaudeSession):
        return ClaudeSender(identity.session_id)
    if isinstance(identity, roster.CodexThread):
        return CodexSender(identity.thread_id)
    return UnavailableSender()


def sender_native_id(stamp: SenderStamp) -> str:
    if isinstance(stamp, ClaudeSender):
        return f"session:{stamp.session_id}"
    if isinstance(stamp, CodexSender):
        return f"codex:{stamp.thread_id}"
    return "unavailable"


def read_sender_stamp(value: object) -> SenderStamp:
    """Decode recorded provenance without reading the current runtime."""
    if value == "unavailable":
        return UnavailableSender()
    if isinstance(value, str):
        kind, separator, identity = value.partition(":")
        if separator and re.fullmatch(r"[\w-]+", identity):
            if kind == "session":
                return ClaudeSender(identity)
            if kind == "codex":
                return CodexSender(identity)
    raise ValueError("invalid sender native id")


def shown_sender_line(sender: str, display: str) -> str:
    return f"\nShown sender: {display}" if display != sender else ""


def sent_line(at: datetime) -> str:
    try:
        zone = user_zone()
    except (OSError, ValueError) as error:
        print(f"send.py: Sent line omitted: {one_line(str(error))}", file=sys.stderr)
        return ""
    local = at.astimezone(zone)
    return f"\nSent: {local:%H:%M} {local.tzname() or zone.key}"


def queued_sent_line(stored_time: str) -> str:
    try:
        at = datetime.fromisoformat(stored_time)
        return sent_line(at) if at.utcoffset() is not None else ""
    except (OverflowError, ValueError):
        return ""


MESSAGE_LINE_BREAK = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")


def quote_sender_lines(text: str) -> str:
    """Separate caller text from generated sender metadata."""
    normalized = MESSAGE_LINE_BREAK.sub("\n", text)
    return re.sub(r"(?m)^([^\S\n]*Sender native id:)", r"> \1", normalized)


def stamped_text(first: str, rest: str, stamp: SenderStamp) -> str:
    preview, separator, remainder = quote_sender_lines(first).partition("\n")
    return (preview + f"\nSender native id: {sender_native_id(stamp)}" + separator + remainder
            + quote_sender_lines(rest))


def agent_text(message: Message, *, via_relay: bool = False) -> str:
    """Format caller text and add the sending machine's display and native stamp."""
    first, separator, rest = quote_sender_lines(message.text).partition("\n")
    sender = relay_session_name(message.sender) if via_relay else message.sender
    return stamped_text(first + shown_sender_line(sender, message.shown_sender) + message.sent_header,
                        separator + rest, message.stamp)


def delivered_text(message: Message, entry_id: str) -> str:
    name = MESSAGE_LINE_BREAK.sub(" ", message.sender.removeprefix("send.py in "))
    summary = MESSAGE_LINE_BREAK.sub(" ", message.summary)
    shown = shown_sender_line(message.sender, message.shown_sender)
    if shown:
        shown = "\n" + MESSAGE_LINE_BREAK.sub(" ", shown.removeprefix("\n"))
    return stamped_text(f"{name}: {summary}{shown}{message.sent_header}\nTag: {entry_id[:8]}",
                        "\n\n" + message.text, message.stamp)


def send(options: Options) -> Result:
    try:
        membership = (staff.snapshot() if options.to != USER
                      else staff.StaffSnapshot(()))
    except staff.StaffUnreadable as error:
        usage_error(f"staff lookup for {options.to!r}: {error}")
    if not isinstance(options.recipient_identity, LogicalRecipientIdentity):
        return _send(options, membership=membership)
    identity = Message(options.recipient_identity.name, options.sender or default_sender(),
                       options.summary or "", "", options.key, sent_header="")
    with delivery_turn(identity) as acquired:
        if not acquired:
            return Result("retry", f"key {options.key!r} has a delivery in progress; retry")
        return _send(options, membership=membership)


def _prompt_pane(place: HerdrPlace, text: str, *, target: str, timeout: float) -> None:
    """Submit text and require the selected pane to start a turn."""
    from scripts.herdr import herdr

    herdr.Herdr(place.session).prompt(
        place.pane, text, target=target, proof=herdr.SubmitStarts(int(timeout * 1000)))


@dataclass(frozen=True)
class WorkerResultReceipt:
    marker: str
    wait_for_idle: bool = False
    before_input: Callable[[], object] = lambda: None

    def received(self, worker: staff.StaffRow, membership: staff.StaffSnapshot) -> bool:
        from scripts.production import worker_admission

        backend = membership.backends.get(worker.id, staff.BACKEND_UNAVAILABLE)
        if isinstance(backend, staff.BackendUnavailable):
            raise ValueError("worker transcript backend is unavailable")
        source = worker_admission.transcript_source(worker, backend)
        return any(isinstance(entry, str) and entry.startswith("user:\n") and self.marker in entry.splitlines()[1:]
                   for entry in worker_admission.transcript_entries(source, set()))


class OrdinaryWorkerMessage(Enum):
    SEND = "send a message without a detached run receipt"


def deliver_worker_text(worker: staff.StaffRow, membership: staff.StaffSnapshot, text: str, *,
                        sender: str, timeout: float, reason: str = "",
                        receipt: WorkerResultReceipt | OrdinaryWorkerMessage = OrdinaryWorkerMessage.SEND,
                        selection: RecipientSelection | NoFixedStaffLookup = NO_FIXED_STAFF_LOOKUP) -> Result:
    """Deliver to a proved pane or retain the message through its holder."""
    from scripts.herdr import worker_panes
    from scripts.herdr import herdr

    holder = membership.parent_of(worker)
    if holder is None:
        return Result("failed", f"{worker.name}: worker holder is unavailable")
    if not reason:
        state = next((item for item in worker_panes.observe(holder.id, membership) if item.row.id == worker.id), None)
        if isinstance(state, worker_panes.OpenWorker):
            attempting_input = False
            ordinary_states = (herdr.AgentStatus.WORKING, herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE)

            def require_idle() -> None:
                nonlocal attempting_input
                readiness = prompt_client.readiness(state.pane)
                if not (readiness["agent_status"].idle and readiness["interactive_ready"]):
                    raise herdr_types.HerdrFailure("worker is not ready for input")
                assert isinstance(receipt, WorkerResultReceipt)
                _ = receipt.before_input()
                attempting_input = True

            def check_message_status() -> None:
                status = prompt_client.readiness(state.pane)["agent_status"]
                if status not in ordinary_states:
                    raise herdr_types.HerdrFailure(f"worker is {status.value}")

            submission = herdr.TrackedPromptSubmission(check_message_status)

            if isinstance(receipt, WorkerResultReceipt):
                wait_for_idle = receipt.wait_for_idle and state.status == herdr.AgentStatus.WORKING
                can_prompt = state.status.idle or wait_for_idle
                on_submission = require_idle
            else:
                wait_for_idle = False
                can_prompt = state.status in ordinary_states
                on_submission = submission

            if can_prompt:
                ordinary_address = (isinstance(receipt, OrdinaryWorkerMessage)
                                    and not isinstance(selection, FixedStaffLookup))
                prepared_client = None if ordinary_address else herdr.Herdr(herdr.shared_session())
                try:
                    prompt_client = prepared_client if prepared_client is not None else herdr.Herdr(herdr.shared_session())
                    if wait_for_idle:
                        _ = prompt_client.wait_turn_end(state.pane, int(timeout * 1000))
                    if isinstance(selection, FixedStaffLookup):
                        current = _current_staff_session(selection, ExpectedStaffSession(worker.current_session))
                        if isinstance(current, RecipientLookupFailed):
                            return Result("failed", current.reason)
                    prompt_client.prompt(state.pane, text, target=str(worker.current_session),
                                         proof=herdr.SubmitStarts(int(timeout * 1000)), on_submission=on_submission)
                    if isinstance(receipt, OrdinaryWorkerMessage):
                        return PaneTextDelivered("sent", f"worker {worker.name} at {state.pane}")
                except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                    reason = str(error)
                    if isinstance(receipt, OrdinaryWorkerMessage):
                        if submission.may_have_submitted:
                            return WorkerMessageUnconfirmed("failed",
                                f"worker message delivery unconfirmed: {reason}. "
                                + "The message may already have arrived. Do not resend it.")
                    elif isinstance(error, (herdr_types.HerdrFailure, OSError)):
                        launch_error = error.__cause__ if isinstance(error, herdr_types.HerdrFailure) else error
                        if (not attempting_input or isinstance(error, herdr.HerdrLaunchRefused)
                                or isinstance(launch_error, OSError)
                                and prepared_client is not None
                                and cast(object, launch_error.filename) == prepared_client.binary):
                            return WorkerResultNotSubmitted("retry", reason)
                    else:
                        raise
                if isinstance(receipt, WorkerResultReceipt):
                    try:
                        if receipt.received(worker, membership):
                            return PaneTextDelivered("sent", f"worker {worker.name} at {state.pane}")
                    except (OSError, ValueError) as error:
                        reason = f"worker transcript is unavailable: {error}"
                    return WorkerResultUnconfirmed("retry", reason or "the worker transcript has no result receipt")
            else:
                reason = f"worker is {state.status.value}"
                if isinstance(receipt, WorkerResultReceipt) and state.status != herdr.AgentStatus.BLOCKED:
                    return Result("retry", reason)
        elif state is None:
            reason = "worker observation is unavailable"
        elif isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            reason = state.reason
        elif isinstance(state, worker_panes.StoppedWorker):
            reason = "worker is stopped"
        else:
            reason = "worker pane is closed"
    if isinstance(selection, FixedStaffLookup):
        return Result("failed", reason)
    address = f"session:{holder.current_session}"
    options = parse(["--here", "--to", address, "--from", sender,
                     "--timeout", str(timeout), "--text", f"Worker {worker.name}: {one_line(reason)}\n{text}"])
    return _send(options._replace(selection=WorkerHolderRecipient(address, holder.id)), membership=membership)


def _send(options: Options, *, membership: staff.StaffSnapshot) -> Result:
    from scripts.message import roster

    if options.to == USER and not options.text_id:
        return Result("refused", push_texts.TEMPLATE_REQUIRED)
    if options.to == USER:
        options = options._replace(timeout=max(options.timeout, review.user_send_seconds()))
    if options.text_id == AUTO_MODE_TEXT:
        options = reviewed_waits(options)
    text = body_text(options).rstrip("\n")
    if not text.strip() and not options.text_id:
        usage_error("the message is empty")
    first = next(iter(text.strip().splitlines()), options.summary or "")
    summary = options.summary or (first if len(first) <= 60 else first[:59] + "…")
    # A forwarded display is authoritative; only recipient lookup may read the receiver's roster.
    forwarded_conversation = isinstance(options.selection, ForwardedConversation)
    target = (staff_queue_key(options.selection.staff_id)
              if isinstance(options.selection, WorkerHolderRecipient) else options.to)
    needs_recipient_roster = (target != USER and not forwarded_conversation
                              and not target.startswith("uds:") and not isinstance(options.scope, NamedMachine))
    needs_sender_roster = options.to != USER and isinstance(options.shown_sender, SenderNotResolved)
    reads_registry = (needs_recipient_roster or needs_sender_roster
                      or (options.to != USER))
    registry = sessions.read_registry() if reads_registry else sessions.RegistryRead((), ())
    running = sessions.running_records(registry)
    if reads_registry:
        membership = replace(membership, running_sessions=frozenset(record["sessionId"] for record in running))
    records = [record for record in running if sessions.messaging_socket_exists(record)]
    stamp = (options.sender_origin.stamp if isinstance(options.sender_origin, ForwardedSender) else sender_stamp())
    participants: roster.AllRosterSessions | roster.MessageParticipants = roster.AllRosterSessions()
    if needs_recipient_roster and not target.startswith(("session:", "worker:", "codex:")):
        local_member = resolve_staff_recipient(target, membership, records=records)
        if (isinstance(local_member, staff.StaffRow) and local_member.kind != "worker"
                and sessions.addressed(f"session:{local_member.current_session}", records, membership=membership) is not None):
            identities = {str(local_member.current_session)}
            if isinstance(stamp, ClaudeSender):
                identities.add(stamp.session_id)
            elif isinstance(stamp, CodexSender):
                identities.add(stamp.thread_id)
            participants = roster.MessageParticipants(frozenset(identities))
    observed = (roster.roster(os.environ, roster.ThisMachine(), membership=membership, registry=registry,
                             participants=participants) if needs_recipient_roster or needs_sender_roster
                else roster.Roster([], []))
    fixed = isinstance(options.selection, FixedStaffLookup)
    native = not fixed and target.startswith("session:")
    raw_socket = not fixed and target.startswith("uds:")
    if isinstance(options.selection, FixedStaffLookup):
        member = options.selection.member(membership)
    elif raw_socket or native and not forwarded_conversation:
        member = NoStaffRecipient()
    else:
        member = resolve_staff_recipient(target, membership, observed=observed.entries)
    if isinstance(member, RecipientLookupFailed):
        usage_error(member.reason)
    mailbox: ClaudeMailbox | NoRelayReplyMailbox = NO_RELAY_REPLY_MAILBOX
    if native:
        host = options.scope.host if isinstance(options.scope, NamedMachine) else machine()
        mailbox = ConversationRecipient(host, options.to[8:])
    elif raw_socket:
        owners = {row["sessionId"] for row in running if row["messagingSocketPath"] == options.to[4:]}
        if len(owners) == 1:
            mailbox = ConversationRecipient(machine(), owners.pop())
    elif isinstance(member, staff.StaffRow):
        mailbox = StaffClaudeRecipient(member.id, ConversationRecipient(machine(), str(member.current_session)))

    route: DeliveryRoute
    if options.to == USER:
        route = (RemoteDelivery(options.scope.host, USER) if isinstance(options.scope, NamedMachine)
                 else UserDelivery())
    elif forwarded_conversation:
        route = ClaudeRecipient(options.to)
    else:
        try:
            selected = options.selection if isinstance(options.selection, FixedStaffLookup) else target
            route = resolve_recipient(selected, options.scope, observed, membership=membership, records=running)
        except RecipientNameMissing as error:
            from scripts.message import failed_lookup

            failed_lookup.report(options.sender or default_sender(), options.to, f"send.py: {error.reason}")
            raise
    destination = route.address if isinstance(route, SessionRecipient | QueuedStaffRecipient | RemoteDelivery) else options.to
    if isinstance(route, RemoteDelivery) and route.selected_conversation:
        mailbox = ConversationRecipient(route.host, route.address[8:])
    elif isinstance(route, ClaudeRecipient) and isinstance(mailbox, NoRelayReplyMailbox) and not raw_socket:
        selected = sessions.addressed(destination, running, membership=membership)
        if destination.startswith("session:"):
            mailbox = ConversationRecipient(machine(), destination[8:])
        elif selected is not None:
            mailbox = ConversationRecipient(machine(), selected["sessionId"])
    sender = options.sender or default_sender()
    if options.to == USER and options.sender is None and sender != "send.py":
        options = options._replace(answerable=True)
    emergency = options.to == USER and options.need == "blocked"
    key = user_emergency_key(sender, summary) if emergency and options.key is None else options.key
    display = (options.shown_sender.text if isinstance(options.shown_sender, ShownSender)
               else sender_shown_name(sender, stamp, observed) if options.to != USER else sender)
    started = now()
    message = Message(destination, sender, summary, text, key, display, options.human, stamp,
                      sent_header=sent_line(started))
    claude_route = isinstance(route, (ClaudeRecipient, QueuedStaffRecipient))
    if claude_route and fixed:
        assert isinstance(mailbox, StaffClaudeRecipient)
        plan = _claude_delivery_plan(message, mailbox, registry, running)
        if isinstance(plan, SessionIdWithoutLiveRecipient):
            plan = ClaudeRoutingFailed(unit_lookup.SocketUnavailable(
                f"no live Claude session answers to staff ID {mailbox.staff_id}"))
    elif claude_route:
        plan = relay_plan(message, registry, running, membership, mailbox)
    else:
        plan = ClaudeRoutingFailed(unit_lookup.SocketUnavailable("this route does not use a Claude socket"))
    if isinstance(route, QueuedStaffRecipient) and not isinstance(plan, SessionIdWithoutLiveRecipient):
        route = ClaudeRecipient(route.address)
    unproved_claude = (claude_route and isinstance(plan, ClaudeRoutingFailed)
                       or isinstance(route, RecipientLookupFailed) and raw_socket)
    if isinstance(plan, ClaudeRoutingFailed) and claude_route:
        route = RecipientLookupFailed(plan.failure.reason)
    recipient: ClaudeRecipientIdentity | NoRelayReplyMailbox = mailbox
    if isinstance(plan, RelayAttempt):
        attempt = plan.message.claude_attempt
        assert isinstance(attempt, ClaudeDeliveryAttempt)
        recipient = attempt.recipient
        message = replace(message, claude_attempt=attempt)
        if not isinstance(recipient, RelayProcessRecipient):
            mailbox = recipient
    native_address = conversation_recipient(mailbox).address if not isinstance(mailbox, NoRelayReplyMailbox) else ""
    typed_recipient = (claude_route or isinstance(route, RemoteDelivery)) and not isinstance(recipient, NoRelayReplyMailbox)
    attempt_recipient = StableRecipient(
        _claude_recipient_key(recipient) if typed_recipient and not isinstance(recipient, NoRelayReplyMailbox)
        else f"staff:{member.id}" if isinstance(member, staff.StaffRow) else options.to)
    stable = (StableRecipient(options.recipient_identity.name)
              if isinstance(options.recipient_identity, LogicalRecipientIdentity) else attempt_recipient)
    legacy_aliases = () if typed_recipient else (destination, stable.value)
    order = new_queue_order() if options.to != USER else LEGACY_QUEUE_ORDER
    # An emergency push repeats on the phone until the user acknowledges it, it is retired, or it
    # expires: with no --repeat-minutes, a send with its key inside that lifetime is skipped.
    live = emergency and options.repeat_minutes is None
    window = (timedelta(minutes=options.repeat_minutes) if options.repeat_minutes is not None
              else emergency_lifetime() if live else None)
    # Held before the claim, so the resend with the template is not skipped by --repeat-minutes.
    action_text = options.action.text if isinstance(options.action, ActionRequired) else ""
    # An emergency push with no clear step would repeat after its cause is fixed: refused, not claimed.
    refused = emergency and not options.clears_when and not options.self_clearing
    held = None if refused else wrong_template(options, "\n".join((summary, text, action_text)))
    cold_check = options.clears_when if options.text_id == "github.keys_cold" else ""
    identity = replace(message, to=stable.value)
    postmaster_identity = replace(message, to=attempt_recipient.value)
    recorded = reserve_postmaster_claim(postmaster_identity, started, options.timeout)
    if (isinstance(recorded, ClaimGranted) and key is not None and claude_route
            and isinstance(recipient, StaffClaudeRecipient)):
        recorded = _legacy_staff_reservation(key, recipient)
    if not isinstance(recorded, ClaimGranted):
        claim_result = recorded
    elif unproved_claude or (isinstance(options.recipient_identity, LogicalRecipientIdentity) and not emergency):
        aliases = legacy_aliases if unproved_claude else (postmaster_identity.to,)
        skip = None if refused or held is not None else confirmed_skip(identity, window, started, live=live, aliases=aliases)
        claim_result = RepeatAccepted(skip) if skip is not None else ClaimGranted()
    else:
        claim_result = ClaimGranted() if refused or held is not None else claim(
            message, attempt_recipient, window, started, delivery_limit=send_budget.seconds(options.timeout) if emergency else options.timeout,
            legacy_aliases=legacy_aliases, live=live, postmaster_recipient=postmaster_identity.to, clears_when=options.clears_when if emergency else cold_check,
            emergency=emergency,
            posted_on=options.scope.host if isinstance(options.scope, NamedMachine) else machine())
    log_route: Route = NO_CLAUDE_ROUTE
    guarded = repeat_guard.Decision()
    if not refused and held is None and isinstance(claim_result, ClaimGranted) and not isinstance(route, (RecipientLookupFailed, RemoteDelivery)):
        guarded = repeat_guard.check(STATE / "repeats", repeat_guard.Attempt(
            sender, attempt_recipient.value,
            text, machine(), started.timestamp(), options.human or sender.casefold() == USER or options.to == USER))
    if refused:
        result = Result("refused", NO_CLEAR_STEP)
    elif held is not None:
        result = Result("held", held)
    elif isinstance(claim_result, RepeatAccepted):
        result = Result("skipped", claim_result.reason)
    elif isinstance(claim_result, RepeatUnconfirmed):
        result = UnconfirmedPostmasterDelivery("failed", claim_result.reason)
    elif isinstance(claim_result, RepeatPending):
        result = Result("pending", claim_result.reason)
    elif guarded.held:
        result = Result("held", f"repeat guard: copy {guarded.copies} held; owner notice queued")
    elif isinstance(route, RecipientLookupFailed):
        from scripts.message import failed_lookup

        failed_lookup.report(sender, options.to, f"FAILED: {route.reason}")
        result = Result("failed", route.reason)
    elif isinstance(route, RemoteDelivery):
        forwarded = options._replace(selection=ForwardedConversation(route.address)) if route.selected_conversation else options
        result = remote(message, route.host, forwarded)
    elif isinstance(route, UserDelivery):
        if isinstance(options.action, NoUserRecipient):
            raise AssertionError("a user message must state its action")
        result = user(message, options.need, options.action, options.timeout, answerable=options.answerable, text_id=options.text_id, values=options.values, due=options.due, reference=options.reference, remote_forward=isinstance(options.sender_origin, ForwardedSender), silence_policy=options.silence_policy)
        if options.text_id == AUTO_MODE_TEXT and result.outcome in ("sent", "kept"):
            _ = rca.open_case(refusal_report(options), defer_owner=True)
    elif isinstance(route, (CodexSessionRecipient, CodexPaneRecipient)):
        result = deliver_codex(message, route, options.timeout, selection=options.selection)
    elif isinstance(route, QueuedStaffRecipient):
        result = Result("queued", f"no live Claude or Codex session answers to {route.name} ({route.session_id}),"
                        + " so it is kept for it")
        assert isinstance(plan, SessionIdWithoutLiveRecipient)
        enqueue(replace(message, to=stable_mailbox_key(plan.recipient)), result.detail, started,
                recipient=attempt_recipient, order=order, membership=membership, mailbox=plan.recipient)
    elif isinstance(route, PaneWorkerRecipient):
        result = deliver_worker_text(route.row, membership, agent_text(message),
                                     sender=message.sender, timeout=options.timeout, selection=options.selection)
    else:
        claude_started = time.monotonic()
        def relay_and_queue(relay_message: Message, timeout: float) -> Result:
            delivery_message, relayed_result = relay_socket(relay_message, timeout, selection=options.selection)
            if isinstance(options.selection, FixedStaffLookup):
                if relayed_result.outcome == "queued":
                    return Result("failed", relayed_result.detail)
                if relayed_result.outcome == "sent":
                    owner = delivery_message.claude_attempt
                    assert isinstance(owner, ClaudeDeliveryAttempt) and isinstance(owner.recipient, StaffClaudeRecipient)
                    return relayed_result._replace(receiver=owner.recipient.current.address)
            if relayed_result.outcome != "queued":
                return relayed_result
            attempt = delivery_message.claude_attempt
            assert isinstance(attempt, ClaudeDeliveryAttempt)
            queue_recipient = attempt.recipient
            if isinstance(queue_recipient, RelayProcessRecipient):
                return Result("failed", f"{relayed_result.detail}; {dead_socket_reason(message.to)}")
            enqueue(replace(delivery_message, to=stable_mailbox_key(queue_recipient)), relayed_result.detail, started,
                    recipient=StableRecipient(_claude_recipient_key(queue_recipient)), order=order,
                    membership=membership, mailbox=queue_recipient)
            return relayed_result

        def relay_with_claim(attempt: Message, timeout: float, delivery_identity: Message, entry_id: str) -> Result:
            nonlocal claim_result
            relayed = relay_and_queue(attempt, timeout)
            if not complete_postmaster_fallback(delivery_identity, stable, entry_id, started, relayed):
                claim_result = RepeatPending("a later send took the key")
            elif key is not None and relayed.outcome in ("sent", "queued"):
                return RecordedDeliveryAcceptance(*relayed)
            return relayed

        if isinstance(plan, SessionIdWithoutLiveRecipient):
            result = Result(
                "queued",
                f"no live session answers to {plan.address}, so it is kept under that name",
            )
            enqueue(replace(message, to=stable_mailbox_key(plan.recipient)), result.detail, started,
                    recipient=attempt_recipient, order=order,
                    membership=membership, mailbox=plan.recipient, generation=plan.generation)
            if isinstance(plan.generation, session_staff.ProvedProcessGeneration):
                log_route = ClaudeRoute("queue", "unresolved", time.monotonic() - claude_started)
        else:
            assert isinstance(plan, RelayAttempt)
            live = records
            postmaster_recipient = _postmaster_recipient(plan, live, membership)
            entry_id = postmaster.new_entry_id()
            try:
                if isinstance(postmaster_recipient, postmaster.RegisteredClaudeRecipient):
                    reservation = reserve_postmaster_claim(postmaster_identity, started, options.timeout, entry_id)
                else:
                    reservation = _reserve_claude_delivery(postmaster_identity, started, options.timeout, entry_id,
                                                        direct_relay=True)
            except BaseException:
                cancel_unpublished_postmaster_claim(postmaster_identity, entry_id)
                raise
            if not isinstance(reservation, ClaimGranted):
                claim_result = reservation
                result = (UnconfirmedPostmasterDelivery("failed", reservation.reason)
                          if isinstance(reservation, RepeatUnconfirmed)
                          else Result("skipped" if isinstance(reservation, RepeatAccepted) else "pending", reservation.reason))
            elif isinstance(postmaster_recipient, postmaster.RegisteredClaudeRecipient):
                try:
                    delivery = postmaster.attempt(STATE, postmaster_recipient.session, message.sender, entry_id,
                                                  delivered_text(message, entry_id), live,
                                                  reply_identity=sender_native_id(message.stamp), membership=membership,
                                                  deadline=claude_started + options.timeout,
                                                  publish=partial(publish_postmaster_entry, postmaster_identity, entry_id,
                                                                  selection=options.selection, session=postmaster_recipient.session),
                                                  resolve=partial(record_postmaster_result, postmaster_identity, entry_id))
                except BaseException:
                    cancel_unpublished_postmaster_claim(postmaster_identity, entry_id)
                    raise
                if isinstance(delivery, postmaster.Delivered):
                    _ = finish_postmaster_claim(postmaster_identity, entry_id, delivery, recipient=stable)
                    result = Result("sent", f"postmaster; {'verbatim' if delivery.verbatim else 'NOT VERBATIM'}; "
                                    + f"session:{postmaster_recipient.session['sessionId']}")
                    if key is not None:
                        result = RecordedDeliveryAcceptance(*result)
                    log_route = ClaudeRoute("postmaster", "delivered", time.monotonic() - claude_started)
                elif isinstance(delivery, postmaster.DeliveryUnconfirmed):
                    result = UnconfirmedPostmasterDelivery("failed", POSTMASTER_UNCONFIRMED)
                    log_route = ClaudeRoute("postmaster", "claimed-no-result", time.monotonic() - claude_started)
                else:
                    if finish_postmaster_claim(postmaster_identity, entry_id, delivery, recipient=stable):
                        result = relay_with_claim(plan.message, max(1.0, options.timeout - delivery.seconds),
                                                  postmaster_identity, entry_id)
                        log_route = ClaudeRoute("relay", delivery.reason, time.monotonic() - claude_started)
                    else:
                        reason = "a later send took the key; no relay or queued copy was started"
                        claim_result = RepeatPending(reason)
                        result = Result("failed", reason)
            else:
                result = relay_with_claim(plan.message, options.timeout, postmaster_identity, entry_id)
                log_route = ClaudeRoute("relay", "unresolved", time.monotonic() - claude_started)
    if result.outcome == "sent" and message.key is not None:
        addresses = ((native_address,) if isinstance(plan, RelayAttempt) and native_address
                     else () if isinstance(plan, RelayAttempt)
                     else tuple(dict.fromkeys((message.to, options.to))))
        for address in addresses:
            _ = remove_queued_key(address, message.key, recipient=attempt_recipient, order=order,
                                  membership=membership)
    if isinstance(options.recipient_identity, LogicalRecipientIdentity) and not emergency:
        if (not refused and held is None and isinstance(claim_result, ClaimGranted)
                and not isinstance(result, RecordedDeliveryAcceptance)
                and result.outcome in {"sent", "queued", "kept", "replaced", "skipped"}):
            record_acceptance(identity, started)
    else:
        # An undelivered emergency releases its claim for a later attempt.
        # Other queued or kept messages retain their claims until delivery.
        if (key is not None and not refused and not unproved_claude and held is None
                and isinstance(claim_result, ClaimGranted)
                and not isinstance(result, (UncertainRelayDelivery, WorkerMessageUnconfirmed))
                and ((emergency and result.outcome != "sent")
                     or result.outcome in {"failed", "refused", "held", "pending"})):
            forget_sent(key, attempt_recipient.value, started)
        elif (key is not None and not refused and held is None and isinstance(claim_result, ClaimGranted)
              and not isinstance(result, RecordedDeliveryAcceptance)
              and result.outcome in {"sent", "queued", "kept", "replaced", "skipped"}):
            accept_sent(key, attempt_recipient.value, started, delivered=result.outcome == "sent")
    if message.to == USER and (result.outcome == "failed" or (result.outcome == "held" and isinstance(result.failure, FailedPush))):
        result = user_failure(message, options.action, result, options.need, options.text_id)
    reported_to = native_address if isinstance(plan, SessionIdWithoutLiveRecipient) else message.to
    if isinstance(plan, RelayAttempt) and isinstance(plan.message.claude_attempt, ClaudeDeliveryAttempt):
        recipient = plan.message.claude_attempt.recipient
        if not isinstance(recipient, RelayProcessRecipient):
            reported_to = conversation_recipient(recipient).address
    if fixed and result.receiver:
        reported_to = result.receiver
    if not options.no_accounting:
        log(replace(message, to=reported_to), result, started, log_route)
    if options.report_recipient:
        print("SEND_RECIPIENT: " + (result.receiver or reported_to), file=sys.stderr)
    return result._replace(receiver="")


def repeat_notice_owner(sender: str) -> str:
    """Follow staff parents to the sender's showrunner."""
    membership = staff.snapshot()
    member = resolve_staff_recipient(sender, membership)
    if isinstance(member, NoStaffRecipient) or isinstance(member, staff.StaffRow) and member.kind == "producer":
        return rca.owner_address("")
    if isinstance(member, RecipientLookupFailed):
        usage_error(member.reason)
    visited: set[staff.StaffId] = set()
    while member.kind != "showrunner":
        if isinstance(member.parent, staff.StaffRoot) or member.id in visited:
            usage_error(f"missing showrunner staff row for {sender!r}")
        visited.add(member.id)
        parent = membership.by_id(member.parent.id)
        if parent is None:
            usage_error(f"missing showrunner staff row for {sender!r}")
        member = parent
    return f"session:{member.current_session}"


def deliver_repeat_notice(state: repeat_guard.SenderState, notice: repeat_guard.Notice,
                          limits: repeat_guard.Limits) -> repeat_guard.NoticeOutcome:
    """Only the notifier invokes this slow path, outside every guard lock."""
    first = datetime.fromtimestamp(notice.first, timezone.utc).isoformat()
    last = datetime.fromtimestamp(notice.last, timezone.utc).isoformat()
    text = (f"Message flood from {state.sender} on {state.host}: {notice.copies} copies to "
            + f"{notice.receivers} sessions, first {first}, last {last}.\n"
            + f"Text: {notice.text}\nRepeated copies are held; distinct texts still arrive. Find the sender's cause.")
    arguments = [sys.executable, str(Path(__file__).resolve()), "--to", repeat_notice_owner(state.sender), "--from", repeat_guard.NOTICE_SENDER,
                 "--text", text]
    arguments += ["--key", "repeat:" + notice.id + ":" + notice.kind, "--repeat-minutes", "0"]
    code, out, err = run(arguments, "", limits.notice_timeout_seconds)
    result = send_outcome(code, out)
    if isinstance(result, ConfirmedSendOutcome):
        if result.header.outcome == "sent":
            return "delivered"
        if result.header.outcome in {"queued", "kept"}:
            return "queued"
    print(f"repeat notice {notice.kind}: {one_line(err or out)}", file=sys.stderr)
    # A skipped or replaced notice does not prove delivery of this notice.
    if isinstance(result, ConfirmedSendOutcome):
        return "unknown" if result.header.outcome in {"skipped", "replaced"} else "failed"
    return "unknown" if code is None or code <= 0 else "failed"


def attempt_repeat_notice(state: repeat_guard.SenderState, notice: repeat_guard.Notice,
                          limits: repeat_guard.Limits) -> repeat_guard.NoticeOutcome:
    """Discovery and delivery share one attempt budget in the supervised job group."""
    request = json.dumps({"state": asdict(state), "id": notice.id, "kind": notice.kind, "limits": asdict(limits)})
    code, out, err = run([sys.executable, str(Path(__file__).resolve()), "repeat-notice-attempt"],
                         request, limits.notice_timeout_seconds)
    if code == 0 and out.strip() in ("delivered", "queued", "failed", "unknown"):
        return cast(repeat_guard.NoticeOutcome, out.strip())
    repeat_guard.warn("notice attempt: " + one_line(err or out or "interrupted"))
    return "unknown"


def main(argv: list[str]) -> int:
    if marker := os.environ.get("TEST_SENDER_GUARD"):
        _ = Path(marker).write_text("real sender refused\n")
        print("REFUSED: real scripts/message/send.py is forbidden in tests", file=sys.stderr)
        return 1
    user_target = "--to=user" in argv or any(left == "--to" and right == USER for left, right in zip(argv, argv[1:]))
    has_template = "--text-id" in argv or any(value.startswith("--text-id=") and value != "--text-id=" for value in argv)
    if user_target and not has_template and argv[:1] != ["retire"]:
        print("REFUSED: " + push_texts.TEMPLATE_REQUIRED)
        return EXIT["refused"]
    match argv:
        case ["repeat-notices"]:
            repeat_guard.drain(Path(os.environ.get("SEND_REPEAT_STATE", str(STATE / "repeats"))), attempt_repeat_notice, now().timestamp(),
                               cleanup_seconds=2 * kill_grace())
        case ["repeat-notice-attempt"]:
            request = as_dict(loads(sys.stdin.read()))
            state = repeat_guard.decode(json.dumps(request["state"]))
            notice = next(item for item in state.notices if (item.id, item.kind) == (request["id"], request["kind"]))
            limits = repeat_guard.Limits(**cast(dict[str, int], request["limits"]))
            print(deliver_repeat_notice(state, notice, limits))
        case ["template-fix", failure_json]:
            template_fix_notice(as_dict(loads(failure_json)))
        case ["template-fix-deliver", text_id, recipient, text]:
            deliver_template_fix(text_id, recipient, text)
        case ["retire", *arguments]:
            request = parse_retire(arguments)
            if request.to == USER:
                code, lines = retire_user(request.sender, request.key)
                print("\n".join(lines))
                return code
            removed = retire(request.to, request.sender, request.key)
            print(f"retired {removed} queued message{'s' if removed != 1 else ''}")
        case ["clear-check"]:
            code, lines = clear_check()
            if lines:
                print("\n".join(lines))
            return code
        case ["drain-sockets"]:
            try:
                code, lines = drain_socket_queues()
            except (OSError, UnicodeError) as error:
                print(f"send.py: socket drain failed: {error}", file=sys.stderr)
                return EXIT["failed"]
            if lines:
                print("\n".join(lines))
            return code
        case ["ack", key]:
            print(acknowledge(key))
        case ["reopen", key]:
            print(reopen(key))
        case ["pending", *named] if len(named) <= 1:
            try:
                _ = pending(named[0] if named else None, emit=True)
            except ValueError as error:
                usage_error(str(error))
        case ["ack" | "reopen" | "pending" | "clear-check" | "drain-sockets", *_]:
            usage_error(f"usage: send.py {argv[0]}" + {"pending": " [NAME]", "clear-check": "",
                         "drain-sockets": ""}.get(argv[0], " KEY"))
        case _:
            options = parse(argv)
            if options.to == USER and os.environ.get(PROBE_ENV) == "1":
                print("not sent: probe sessions never push to the phone")
                return 0
            result = send(options)
            print(f"{result.outcome.upper()}: {result.detail}")
            if result.outcome == "failed" and options.to.startswith("uds:"):
                print(f"send.py: {result.detail}", file=sys.stderr)
            if isinstance(result.failure, FailedPush):
                failed = result.failure
                record: FailedPushTransportRecord = {"title": failed.title,
                    "action": failed.action.text if isinstance(failed.action, ActionRequired) else None,
                    "body": failed.body, "reviewed": failed.reviewed, "need": failed.need, "held": failed.held,
                    "text_id": failed.text_id, "sender": failed.sender}
                if isinstance(failed.hold, HeldRevision):
                    record["held_source"] = failed.hold.source
                    record["held_identity"] = failed.hold.identity
                print("send.py: failed push JSON: " + json.dumps(record), file=sys.stderr)
            return EXIT[result.outcome]
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
