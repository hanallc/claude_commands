"""Keep local Claude messages while a review is open."""
from __future__ import annotations

import fcntl
import json
import re
import sys
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import cast

from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone


URGENT_PREFIX = "URGENT:"


@dataclass(frozen=True)
class ReviewMessage:
    sender: str
    sent: float
    text: str
    native_id: str = "unavailable"
    key: str = ""
    key_recipient: str = ""


type RecordReleasedKeys = Callable[[tuple[ReviewMessage, ...], str, float], None]


class EndedReview(Enum):
    HOLD_ENDED = "the hold ended"


@dataclass(frozen=True)
class ReviewState:
    last_activity: float | EndedReview
    messages: tuple[ReviewMessage, ...] = ()
    reserved: tuple[ReviewMessage, ...] = ()
    release_pending: bool = False


class UnconfirmedRelease(Enum):
    NO_RECEIPT = "no delivery receipt"


@dataclass
class ReleasedMessages:
    messages: tuple[ReviewMessage, ...]
    held: tuple[ReviewMessage, ...] = ()
    accepted_at: float | UnconfirmedRelease = UnconfirmedRelease.NO_RECEIPT


@dataclass(frozen=True)
class OpenReview:
    """The caller can add a message while it holds the review lock."""

    path: Path
    state: ReviewState

    def keep(self, message: ReviewMessage) -> None:
        _write_state(self.path, replace(self.state, messages=(*self.state.messages, message)))


class AlreadyKeptForReview:
    """A held message already owns this key."""


def _count_file(session: str) -> Path:
    if not session or Path(session).name != session or session.startswith("."):
        raise ValueError("invalid review session ID")
    return Path.home() / ".local/state/adhoc_review" / session


def item_count(session: str) -> int:
    try:
        return _read_count(_count_file(session))
    except ValueError:
        return 0


def _read_count(count: Path) -> int:
    try:
        value = count.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0
    return int(value) if value.isdecimal() else 0


def _state_file(count: Path) -> Path:
    return count.parent / ".holds" / (count.name + ".json")


@contextmanager
def _locked(path: Path) -> Generator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def _delivery_turn(path: Path) -> Generator[bool]:
    """Give one sender the reservation until it finishes or exits."""
    with path.with_suffix(".delivery.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def _last_prompt(path: Path, fallback: float) -> float:
    try:
        return float(path.with_suffix(".prompt").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


def _read_state(count: Path, path: Path) -> ReviewState:
    try:
        value = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return ReviewState(_last_prompt(path, count.stat().st_mtime) if _read_count(count)
                           else EndedReview.HOLD_ENDED)
    at = value["last_activity"]
    activity = EndedReview.HOLD_ENDED if at is None else float(cast(float, at))
    messages = cast(list[dict[str, str | float]], value["messages"])
    reserved = cast(list[dict[str, str | float]], value.get("reserved", []))
    return ReviewState(activity, _read_messages(messages), _read_messages(reserved),
                       release_pending=value.get("release_pending") is True)


def _read_messages(messages: list[dict[str, str | float]]) -> tuple[ReviewMessage, ...]:
    return tuple(
        ReviewMessage(str(item["sender"]), float(item["sent"]), str(item["text"]),
                      str(item.get("native_id", "unavailable")), str(item.get("key", "")),
                      str(item.get("key_recipient", "")))
        for item in messages)


def _write_state(path: Path, state: ReviewState) -> None:
    at = state.last_activity
    record = {"last_activity": None if isinstance(at, EndedReview) else at,
              "messages": [asdict(item) for item in state.messages],
              "reserved": [asdict(item) for item in state.reserved],
              "release_pending": state.release_pending}
    write_text_atomic(path, json.dumps(record), new_file_mode=0o600)


def _enabled(settings: conf.ConfigRead) -> bool:
    value = settings.origin("review_hold", str)
    return isinstance(value, conf.UsableLiveValue) and value.value == "on"


def _hold_open(count: Path, state: ReviewState, at: float, settings: conf.ConfigRead) -> bool:
    activity = state.last_activity
    if isinstance(activity, EndedReview) or not _read_count(count):
        return False
    return _enabled(settings) and at - activity < settings.number("review_hold_idle_minutes") * 60


def keep_native_message(session: str, message: ReviewMessage) -> bool:
    if message.text.startswith(URGENT_PREFIX):
        return False
    count = _count_file(session)
    path = _state_file(count)
    if not (count.exists() or path.exists()):
        return False
    with _locked(path):
        settings = conf.read("send")
        if not _enabled(settings):
            return False
        state = _read_state(count, path)
        if not _hold_open(count, state, message.sent, settings):
            return False
        OpenReview(path, state).keep(message)
        return True


def sent_line(at: datetime) -> str:
    try:
        zone = user_zone()
    except (OSError, ValueError) as error:
        print("send.py: Sent line omitted: " + " ".join(str(error).split())[:300], file=sys.stderr)
        return ""
    local = at.astimezone(zone)
    return f"\nSent: {local:%H:%M} {local.tzname() or zone.key}"


MESSAGE_LINE_BREAK = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")


def quote_sender_lines(text: str) -> str:
    """Separate caller text from generated sender metadata."""
    normalized = MESSAGE_LINE_BREAK.sub("\n", text)
    return re.sub(r"(?m)^([^\S\n]*Sender native id:)", r"> \1", normalized)


def render(messages: tuple[ReviewMessage, ...]) -> str:
    entries: list[str] = []
    for item in sorted(messages, key=lambda item: item.sent):
        stamp = sent_line(datetime.fromtimestamp(item.sent, timezone.utc)).replace("\n", "\n   ")
        sender = " ".join(item.sender.splitlines())
        body = "\n".join("   " + line for line in quote_sender_lines(item.text).splitlines())
        native_id = " ".join(item.native_id.splitlines())
        entries.append(f"{len(entries) + 1}. {sender}{stamp}\n   Sender native id: {native_id}\n{body}")
    return "Messages from the review\n\n" + "\n\n".join(entries)


def _end_review(path: Path, state: ReviewState) -> ReviewState:
    if state.last_activity is EndedReview.HOLD_ENDED:
        return state
    ended = replace(state, last_activity=EndedReview.HOLD_ENDED)
    _write_state(path, ended)
    return ended


def _reserve(path: Path, state: ReviewState, delivery: ExitStack, *, key: str = "") -> tuple[ReviewMessage, ...]:
    """Move saved messages to the reservation while the review lock is held."""
    saved = (*state.reserved, *state.messages)
    held = tuple(item for item in saved if not key or item.key == key)
    if not held:
        return ()
    if not delivery.enter_context(_delivery_turn(path)):
        if not key and not state.release_pending:
            _write_state(path, replace(state, release_pending=True))
        return ()
    remaining = tuple(item for item in saved if key and item.key != key)
    _write_state(path, replace(state, messages=remaining, reserved=held))
    return held


@contextmanager
def _settle_release(count: Path, path: Path, batch: ReleasedMessages,
                    record_keys: RecordReleasedKeys) -> Generator[ReleasedMessages]:
    try:
        yield batch
    finally:
        if batch.held:
            accepted_at = batch.accepted_at
            if not isinstance(accepted_at, UnconfirmedRelease) and any(item.key for item in batch.held):
                try:
                    record_keys(batch.held, f"session:{count.name}", accepted_at)
                except (OSError, ValueError) as error:
                    print(f"review hold: cannot record the delivered keys: {error}", file=sys.stderr)
            try:
                with _locked(path):
                    state = _read_state(count, path)
                    messages = state.messages
                    if isinstance(accepted_at, UnconfirmedRelease):
                        messages = tuple(sorted((*state.reserved, *messages), key=lambda item: item.sent))
                    if not messages and not _read_count(count):
                        path.unlink(missing_ok=True)
                    else:
                        _write_state(path, replace(state, messages=messages, reserved=(),
                                                   release_pending=state.release_pending and bool(messages)))
            except OSError as error:
                print(f"review hold: cannot settle the delivery: {error}", file=sys.stderr)


def _emit_saved(count: Path, path: Path, held: tuple[ReviewMessage, ...], at: float,
                emit: Callable[[str], None], record_keys: RecordReleasedKeys) -> None:
    if held:
        with _settle_release(count, path, ReleasedMessages(held, held), record_keys) as batch:
            emit(render(held))
            batch.accepted_at = at


def set_count(session: str, remaining: int, at: float, emit: Callable[[str], None], *,
              record_keys: RecordReleasedKeys) -> None:
    count = _count_file(session)
    path = _state_file(count)
    with ExitStack() as delivery:
        with _locked(path):
            state = _read_state(count, path)
            if remaining > 0:
                if not _enabled(conf.read("send")):
                    _write_state(path, replace(state, last_activity=EndedReview.HOLD_ENDED))
                elif item_count(session) == 0:
                    _write_state(path, replace(state, last_activity=_last_prompt(path, at), release_pending=False))
                elif not path.exists():
                    _write_state(path, state)
                write_text_atomic(count, str(remaining))
                return
            count.unlink(missing_ok=True)
            state = _end_review(path, state)
            held = _reserve(path, state, delivery)
            if not state.messages and not state.reserved:
                path.unlink(missing_ok=True)
        _emit_saved(count, path, held, at, emit, record_keys)


def record_user_prompt(session: str, at: float, emit: Callable[[str], None], *,
                       record_keys: RecordReleasedKeys) -> None:
    count = _count_file(session)
    path = _state_file(count)
    with ExitStack() as delivery:
        with _locked(path):
            state = _read_state(count, path)
            previous = state.last_activity
            if not isinstance(previous, EndedReview):
                if _hold_open(count, state, at, conf.read("send")):
                    state = replace(state, last_activity=max(previous, at))
                    _write_state(path, state)
                else:
                    state = _end_review(path, state)
            write_text_atomic(path.with_suffix(".prompt"), repr(at), new_file_mode=0o600)
            held = _reserve(path, state, delivery) if isinstance(state.last_activity, EndedReview) else ()
        _emit_saved(count, path, held, at, emit, record_keys)


def deliver_at_session_start(session: str, emit: Callable[[str], None], *, record_keys: RecordReleasedKeys) -> None:
    _deliver_saved(session, datetime.now(timezone.utc).timestamp(), emit, record_keys)


def deliver_if_disabled(session: str, at: float, emit: Callable[[str], None], *,
                        record_keys: RecordReleasedKeys) -> None:
    if not _enabled(conf.read("send")):
        _deliver_saved(session, at, emit, record_keys)


def _deliver_saved(session: str, at: float, emit: Callable[[str], None], record_keys: RecordReleasedKeys) -> None:
    count = _count_file(session)
    path = _state_file(count)
    if not path.exists():
        return
    with ExitStack() as delivery:
        with _locked(path):
            state = _read_state(count, path)
            if not (state.messages or state.reserved):
                return
            if _hold_open(count, state, at, conf.read("send")):
                return
            held = _reserve(path, _end_review(path, state), delivery)
        _emit_saved(count, path, held, at, emit, record_keys)


@contextmanager
def pending_release(session: str, *, record_keys: RecordReleasedKeys) -> Generator[ReleasedMessages]:
    """Take a release that had to wait for a sender to settle."""
    count = _count_file(session)
    path = _state_file(count)
    held: tuple[ReviewMessage, ...] = ()
    with ExitStack() as delivery:
        if path.exists():
            with _locked(path):
                state = _read_state(count, path)
                if state.release_pending:
                    held = _reserve(path, state, delivery)
        with _settle_release(count, path, ReleasedMessages(held, held), record_keys) as batch:
            yield batch


@contextmanager
def route(session: str, message: ReviewMessage, *, urgent: bool,
          record_keys: RecordReleasedKeys, include_new: bool = True) -> Generator[OpenReview | AlreadyKeptForReview | ReleasedMessages]:
    count = _count_file(session)
    path = _state_file(count)
    if count.exists() or path.exists():
        with ExitStack() as delivery:
            held: tuple[ReviewMessage, ...] = ()
            messages: tuple[ReviewMessage, ...] = ()
            with _locked(path):
                settings = conf.read("send")
                if _enabled(settings):
                    state = _read_state(count, path)
                    if _hold_open(count, state, message.sent, settings):
                        if include_new and not urgent:
                            saved = (*state.reserved, *state.messages)
                            yield (AlreadyKeptForReview() if message.key and any(item.key == message.key for item in saved)
                                   else OpenReview(path, state))
                            return
                        if include_new and urgent and message.key:
                            held = _reserve(path, state, delivery, key=message.key)
                    else:
                        held = _reserve(path, _end_review(path, state), delivery)
                        if held:
                            if not include_new:
                                messages = held
                            elif urgent:
                                messages = (*[item for item in held if not message.key or item.key != message.key], message)
                            else:
                                repeated = message.key and any(item.key == message.key for item in held)
                                messages = held if repeated else (*held, message)
            if held:
                with _settle_release(count, path, ReleasedMessages(messages, held), record_keys) as batch:
                    yield batch
                    return
    yield ReleasedMessages(())
