"""Keep local Claude messages while a review is open."""
from __future__ import annotations

import fcntl
import json
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


@dataclass(frozen=True)
class ReviewMessage:
    sender: str
    sent: float
    text: str


class EndedReview(Enum):
    HOLD_ENDED = "the hold ended"


@dataclass(frozen=True)
class ReviewState:
    last_activity: float | EndedReview
    messages: tuple[ReviewMessage, ...] = ()


@dataclass
class ReleasedMessages:
    messages: tuple[ReviewMessage, ...]
    accepted: bool = False


class HeldForReview:
    """The review accepted the message for later delivery."""


def _count_file(session: str) -> Path:
    if not session or Path(session).name != session or session.startswith("."):
        raise ValueError("invalid review session ID")
    return Path.home() / ".local/state/adhoc_review" / session


def item_count(session: str) -> int:
    try:
        value = _count_file(session).read_text(encoding="utf-8").strip()
        return int(value) if value.isdecimal() else 0
    except (FileNotFoundError, UnicodeError, ValueError):
        return 0


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
        return ReviewState(_last_prompt(path, count.stat().st_mtime) if item_count(count.name)
                           else EndedReview.HOLD_ENDED)
    at = value["last_activity"]
    activity = EndedReview.HOLD_ENDED if at is None else float(cast(float, at))
    messages = cast(list[dict[str, str | float]], value["messages"])
    return ReviewState(activity, tuple(
        ReviewMessage(str(item["sender"]), float(item["sent"]), str(item["text"])) for item in messages))


def _write_state(path: Path, state: ReviewState) -> None:
    at = state.last_activity
    record = {"last_activity": None if isinstance(at, EndedReview) else at,
              "messages": [asdict(item) for item in state.messages]}
    write_text_atomic(path, json.dumps(record), new_file_mode=0o600)


def _idle(active_at: float, at: float) -> bool:
    return at - active_at >= conf.read("send").number("review_hold_idle_minutes") * 60


def sent_line(at: datetime) -> str:
    try:
        zone = user_zone()
    except (OSError, ValueError) as error:
        print("send.py: Sent line omitted: " + " ".join(str(error).split())[:300], file=sys.stderr)
        return ""
    local = at.astimezone(zone)
    return f"\nSent: {local:%H:%M} {local.tzname() or zone.key}"


def render(messages: tuple[ReviewMessage, ...]) -> str:
    entries: list[str] = []
    for item in sorted(messages, key=lambda item: item.sent):
        stamp = sent_line(datetime.fromtimestamp(item.sent, timezone.utc)).replace("\n", "\n   ")
        sender = " ".join(item.sender.splitlines())
        body = "\n".join("   " + line for line in item.text.splitlines())
        entries.append(f"{len(entries) + 1}. {sender}{stamp}\n{body}")
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


def record_user_prompt(session: str, at: float) -> None:
    count = _count_file(session)
    path = _state_file(count)
    with _locked(path):
        if item_count(session):
            state = _read_state(count, path)
            previous = state.last_activity
            if not isinstance(previous, EndedReview):
                activity = EndedReview.HOLD_ENDED if _idle(previous, at) else max(previous, at)
                _write_state(path, ReviewState(activity, state.messages))
        write_text_atomic(path.with_suffix(".prompt"), repr(at), new_file_mode=0o600)


def deliver_at_session_start(session: str, emit: Callable[[str], None]) -> None:
    count = _count_file(session)
    path = _state_file(count)
    if not path.exists():
        return
    with _locked(path):
        state = _read_state(count, path)
        if state.messages:
            emit(render(state.messages))
            _write_state(path, ReviewState(state.last_activity))


@contextmanager
def route(session: str, message: ReviewMessage, *, urgent: bool) -> Generator[HeldForReview | ReleasedMessages]:
    count = _count_file(session)
    path = _state_file(count)
    if count.exists() or path.exists():
        with _locked(path):
            state = _read_state(count, path)
            activity = state.last_activity
            if not isinstance(activity, EndedReview) and item_count(session) and not _idle(activity, message.sent):
                if not urgent:
                    _write_state(path, ReviewState(state.last_activity, (*state.messages, message)))
                    yield HeldForReview()
                    return
            elif state.messages or not isinstance(activity, EndedReview):
                _write_state(path, ReviewState(EndedReview.HOLD_ENDED, state.messages))
                if state.messages:
                    batch = ReleasedMessages((*state.messages, message))
                    yield batch
                    if batch.accepted:
                        try:
                            _write_state(path, ReviewState(EndedReview.HOLD_ENDED))
                        except OSError as error:
                            print(f"review hold: cannot clear the delivered list: {error}", file=sys.stderr)
                    return
    yield ReleasedMessages(())
