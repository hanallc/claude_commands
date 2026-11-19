"""Keep local Claude messages while a review is open."""
from __future__ import annotations

import fcntl
import json
import re
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
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


class EndedReview(Enum):
    HOLD_ENDED = "the hold ended"


@dataclass(frozen=True)
class ReviewState:
    last_activity: float | EndedReview
    messages: tuple[ReviewMessage, ...] = ()


@dataclass
class ReleasedMessages:
    messages: tuple[ReviewMessage, ...]
    held: tuple[ReviewMessage, ...] = ()
    accepted: bool = False


@dataclass(frozen=True)
class OpenReview:
    """The caller can add a message while it holds the review lock."""

    path: Path
    state: ReviewState

    def keep(self, message: ReviewMessage) -> None:
        _write_state(self.path, ReviewState(self.state.last_activity, (*self.state.messages, message)))


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
    return ReviewState(activity, tuple(
        ReviewMessage(str(item["sender"]), float(item["sent"]), str(item["text"]),
                      str(item.get("native_id", "unavailable")), str(item.get("key", "")),
                      str(item.get("key_recipient", "")))
        for item in messages))


def _write_state(path: Path, state: ReviewState) -> None:
    at = state.last_activity
    record = {"last_activity": None if isinstance(at, EndedReview) else at,
              "messages": [asdict(item) for item in state.messages]}
    write_text_atomic(path, json.dumps(record), new_file_mode=0o600)


def _idle(active_at: float, at: float) -> bool:
    return at - active_at >= conf.read("send").number("review_hold_idle_minutes") * 60


def _hold_open(count: Path, state: ReviewState, at: float) -> bool:
    activity = state.last_activity
    return not isinstance(activity, EndedReview) and _read_count(count) > 0 and not _idle(activity, at)


def keep_native_message(session: str, message: ReviewMessage) -> bool:
    if message.text.startswith(URGENT_PREFIX):
        return False
    count = _count_file(session)
    path = _state_file(count)
    # The sender can hold the lock while it delivers an ended review's list.
    if not _hold_open(count, _read_state(count, path), message.sent):
        return False
    with _locked(path):
        state = _read_state(count, path)
        if not _hold_open(count, state, message.sent):
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


def set_count(session: str, remaining: int, at: float, emit: Callable[[str], None]) -> None:
    count = _count_file(session)
    path = _state_file(count)
    with _locked(path):
        state = _read_state(count, path)
        if remaining > 0:
            if item_count(session) == 0:
                _write_state(path, ReviewState(_last_prompt(path, at), state.messages))
            elif not path.exists():
                _write_state(path, state)
            write_text_atomic(count, str(remaining))
            return
        count.unlink(missing_ok=True)
        if state.messages:
            emit(render(state.messages))
        path.unlink(missing_ok=True)


def _emit_saved(path: Path, state: ReviewState, emit: Callable[[str], None]) -> None:
    if state.messages:
        ended = EndedReview.HOLD_ENDED
        if state.last_activity is not ended:
            _write_state(path, ReviewState(ended, state.messages))
        emit(render(state.messages))
        _write_state(path, ReviewState(ended))


def record_user_prompt(session: str, at: float, emit: Callable[[str], None]) -> None:
    count = _count_file(session)
    path = _state_file(count)
    with _locked(path):
        state = _read_state(count, path)
        previous = state.last_activity
        if not isinstance(previous, EndedReview):
            activity = max(previous, at) if _hold_open(count, state, at) else EndedReview.HOLD_ENDED
            state = ReviewState(activity, state.messages)
            _write_state(path, state)
        write_text_atomic(path.with_suffix(".prompt"), repr(at), new_file_mode=0o600)
        if isinstance(state.last_activity, EndedReview):
            _emit_saved(path, state, emit)


def deliver_at_session_start(session: str, emit: Callable[[str], None]) -> None:
    count = _count_file(session)
    path = _state_file(count)
    if not path.exists():
        return
    with _locked(path):
        _emit_saved(path, _read_state(count, path), emit)


@contextmanager
def route(session: str, message: ReviewMessage, *, urgent: bool) -> Generator[OpenReview | AlreadyKeptForReview | ReleasedMessages]:
    count = _count_file(session)
    path = _state_file(count)
    if count.exists() or path.exists():
        with _locked(path):
            state = _read_state(count, path)
            activity = state.last_activity
            if _hold_open(count, state, message.sent):
                if not urgent:
                    if message.key and any(item.key == message.key for item in state.messages):
                        yield AlreadyKeptForReview()
                    else:
                        yield OpenReview(path, state)
                    return
            elif state.messages or not isinstance(activity, EndedReview):
                _write_state(path, ReviewState(EndedReview.HOLD_ENDED, state.messages))
                if state.messages:
                    repeated = message.key and any(item.key == message.key for item in state.messages)
                    messages = state.messages if repeated and not urgent else (*state.messages, message)
                    batch = ReleasedMessages(messages, held=state.messages)
                    yield batch
                    if batch.accepted:
                        try:
                            _write_state(path, ReviewState(EndedReview.HOLD_ENDED))
                        except OSError as error:
                            print(f"review hold: cannot clear the delivered list: {error}", file=sys.stderr)
                    return
    yield ReleasedMessages(())
