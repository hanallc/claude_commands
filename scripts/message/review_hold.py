"""Keep local Claude messages while a review is open."""
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
import sys
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from time import time
from typing import cast

from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone


URGENT_PREFIX = "URGENT:"
RELEASED_AT = "Released at "


@dataclass(frozen=True)
class ReviewMessage:
    sender: str
    sent: float
    text: str
    native_id: str = "unavailable"
    key: str = ""
    key_recipient: str = ""
    once_per_key: bool = False


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


class NoNewMessage(Enum):
    OUTSIDE_THE_BATCH = "the new message is not in this batch"


@dataclass(frozen=True)
class HeldFileAppend:
    """The held messages file and its sizes before and after this release appended to it."""

    path: Path
    size_before: int
    size_after: int


class NothingFiled(Enum):
    NOT_FILED = "nothing written to the held messages file"


@dataclass
class ReleasedMessages:
    to_file: tuple[ReviewMessage, ...]
    held: tuple[ReviewMessage, ...] = ()
    new_message: ReviewMessage | NoNewMessage = NoNewMessage.OUTSIDE_THE_BATCH
    accepted_at: float | UnconfirmedRelease = UnconfirmedRelease.NO_RECEIPT
    filed: HeldFileAppend | NothingFiled = NothingFiled.NOT_FILED
    record_keys: RecordReleasedKeys | None = None

    @property
    def messages(self) -> tuple[ReviewMessage, ...]:
        """Every message this batch delivers."""
        if isinstance(self.new_message, NoNewMessage):
            return self.to_file
        return (*self.to_file, self.new_message)


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


def _held_file(count: Path) -> Path:
    return count.parent / ".holds" / (count.name + ".held.md")


def held_file(session: str) -> Path:
    """The file that receives this session's released review messages."""
    return _held_file(_count_file(session))


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
    messages = cast(list[dict[str, str | float | bool]], value["messages"])
    reserved = cast(list[dict[str, str | float | bool]], value.get("reserved", []))
    return ReviewState(activity, _read_messages(messages), _read_messages(reserved),
                       release_pending=value.get("release_pending") is True)


def _read_messages(messages: list[dict[str, str | float | bool]]) -> tuple[ReviewMessage, ...]:
    return tuple(
        ReviewMessage(str(item["sender"]), float(item["sent"]), str(item["text"]),
                      str(item.get("native_id", "unavailable")), str(item.get("key", "")),
                      str(item.get("key_recipient", "")),
                      item.get("once_per_key", str(item.get("key", "")).startswith("agents-resume:")) is True)
        for item in messages)


def _write_state(path: Path, state: ReviewState) -> None:
    at = state.last_activity
    record = {"last_activity": None if isinstance(at, EndedReview) else at,
              "messages": [asdict(item) for item in state.messages],
              "reserved": [asdict(item) for item in state.reserved],
              "release_pending": state.release_pending}
    write_text_atomic(path, json.dumps(record), new_file_mode=0o600)


def kept_once(key: str) -> bool:
    """Recover acceptance if a sender stopped after saving the review message."""
    directory = _state_file(_count_file("lookup")).parent
    for path in directory.glob("*.json"):
        try:
            state = _read_state(directory.parent / path.stem, path)
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if any(item.key == key and item.once_per_key for item in (*state.reserved, *state.messages)):
            return True
    return False


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
    if _carries_held_notice(message.text, _held_file(count)):
        return False
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


def _zone_time(at: datetime, form: str, *, unreadable: str) -> str:
    """Give the time in the user zone; on an unreadable zone, print `unreadable` and the error."""
    try:
        zone = user_zone()
    except (OSError, ValueError) as error:
        print(f"{unreadable}: " + " ".join(str(error).split())[:300], file=sys.stderr)
        return ""
    local = at.astimezone(zone)
    return f"{local:{form}} {local.tzname() or zone.key}"


def sent_line(at: datetime) -> str:
    time = _zone_time(at, "%H:%M", unreadable="send.py: Sent line omitted")
    return f"\nSent: {time}" if time else ""


MESSAGE_LINE_BREAK = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")


def quote_sender_lines(text: str) -> str:
    """Separate caller text from generated sender metadata."""
    normalized = MESSAGE_LINE_BREAK.sub("\n", text)
    return re.sub(r"(?m)^([^\S\n]*Sender native id:)", r"> \1", normalized)


def numbered_entries(messages: tuple[ReviewMessage, ...]) -> str:
    """Number the messages in sent order with sender, Sent line, reply address and text."""
    entries: list[str] = []
    for item in sorted(messages, key=lambda item: item.sent):
        stamp = sent_line(datetime.fromtimestamp(item.sent, timezone.utc)).replace("\n", "\n   ")
        sender = " ".join(item.sender.splitlines())
        body = "\n".join("   " + line for line in quote_sender_lines(item.text).splitlines())
        native_id = " ".join(item.native_id.splitlines())
        entries.append(f"{len(entries) + 1}. {sender}{stamp}\n   Sender native id: {native_id}\n{body}")
    return "\n\n".join(entries)


def _notice_source(path: Path) -> str:
    """The notice text between its message count and its release header."""
    return f' from the review in {path}, under "'


def held_notice(count: int, path: Path, header: str) -> str:
    if count == 1:
        return f'1 held message{_notice_source(path)}{header}". Read it at your own pace.'
    return f'{count} held messages{_notice_source(path)}{header}". Read them at your own pace.'


def _carries_held_notice(text: str, path: Path) -> bool:
    """A release batch names the held messages file on a line of its own, so it passes an open hold."""
    pattern = r"(?m)^\d+ held messages?" + re.escape(_notice_source(path) + RELEASED_AT)
    return re.search(pattern, text) is not None


def _file_once(target: Path, messages: tuple[ReviewMessage, ...], header: str) -> list[str]:
    """Publish each keyed entry once and reuse its notice after an interrupted release."""
    try:
        existing = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    new: dict[str, ReviewMessage] = {}
    released: dict[str, int] = {}
    seen: set[str] = set()
    for message in messages:
        if message.key in seen:
            continue
        seen.add(message.key)
        digest = hashlib.sha256(message.key.encode()).hexdigest()
        marker = f"\n<!-- review key {digest} -->\n"
        location = existing.find(marker)
        if location == -1:
            new[digest] = message
        else:
            saved_header = existing[:location].rsplit("\n" + RELEASED_AT, 1)[-1].splitlines()[0]
            saved_header = saved_header.removeprefix(RELEASED_AT)
            section = RELEASED_AT + saved_header
            released[section] = released.get(section, 0) + 1
    if new:
        section_id = next(iter(new))[:12]
        section = f"{header} ({section_id})"
        entries = numbered_entries(tuple(new.values()))
        markers = "".join(f"\n<!-- review key {digest} -->\n" for digest in new)
        contents = existing + ("\n" if existing else "") + f"{section}\n{markers}\n{entries}\n"
        write_text_atomic(target, contents, new_file_mode=0o600)
        released[section] = len(new)
    return [held_notice(count, target, section) for section, count in released.items()]


def _file_release(count: Path, batch: ReleasedMessages, at: float, *, batch_only: bool) -> str:
    """Append the batch's held part to the held messages file and give the notice that names it."""
    if not isinstance(batch.filed, NothingFiled):
        raise RuntimeError(f"review hold: this batch is already in {batch.filed.path}")
    messages = batch.held if batch_only else batch.to_file
    if not messages:
        return ""
    target = _held_file(count)
    released = datetime.fromtimestamp(at, timezone.utc)
    header = RELEASED_AT + (_zone_time(released, "%H:%M:%S", unreadable="review hold: release time in UTC")
                            or f"{released:%H:%M:%S} UTC")
    once = tuple(item for item in messages if item.key and item.once_per_key)
    ordinary = tuple(item for item in messages if item not in once)
    notices: list[str] = []
    if once:
        with _locked(_state_file(count)):
            notices = _file_once(target, once, header)
        if batch.record_keys is not None:
            batch.record_keys(once, f"session:{count.name}", at)
    if not ordinary:
        return "\n".join(notices)
    with _locked(_state_file(count)):
        descriptor = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            size_before = os.fstat(descriptor).st_size
            text = ("\n" if size_before else "") + f"{header}\n\n{numbered_entries(ordinary)}\n"
            remaining = memoryview(text.encode("utf-8", errors="replace"))
            try:
                while remaining:
                    remaining = remaining[os.write(descriptor, remaining):]
            except BaseException:
                # A partial write leaves no part of this release in the file.
                os.ftruncate(descriptor, size_before)
                raise
            size_after = os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)
    batch.filed = HeldFileAppend(target, size_before, size_after)
    notices.append(held_notice(len(ordinary), target, header))
    return "\n".join(notices)


def file_release(session: str, batch: ReleasedMessages, at: float, *, batch_only: bool) -> str:
    """Append the batch's held part to the session's held messages file before its notice goes out."""
    return _file_release(_count_file(session), batch, at, batch_only=batch_only)


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
    batch.record_keys = record_keys
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
                    filed = batch.filed
                    if isinstance(accepted_at, UnconfirmedRelease) and isinstance(filed, HeldFileAppend):
                        _remove_undelivered(filed)
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


def _remove_undelivered(filed: HeldFileAppend) -> None:
    """Cut an undelivered release from the file, unless the file changed after the append."""
    try:
        size = filed.path.stat().st_size
        if size != filed.size_after:
            print(f"review hold: {filed.path} changed after the append ({size} bytes, not {filed.size_after});"
                  + " the undelivered messages stay in it", file=sys.stderr)
            return
        os.truncate(filed.path, filed.size_before)
    except OSError as error:
        print(f"review hold: cannot remove the undelivered messages from {filed.path}: {error}", file=sys.stderr)


def _emit_saved(count: Path, path: Path, held: tuple[ReviewMessage, ...], at: float,
                emit: Callable[[str], None], record_keys: RecordReleasedKeys) -> None:
    if held:
        with _settle_release(count, path, ReleasedMessages(held, held), record_keys) as batch:
            emit(_file_release(count, batch, at, batch_only=True))
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
            if isinstance(state.last_activity, EndedReview):
                held = _reserve(path, state, delivery)
                if _read_count(count) and _enabled(conf.read("send")):
                    # Restart the hold and keep any release requested before this prompt.
                    _write_state(path, replace(_read_state(count, path), last_activity=at,
                                               release_pending=state.release_pending))
            else:
                held = ()
        _emit_saved(count, path, held, at, emit, record_keys)


def deliver_at_session_start(session: str, emit: Callable[[str], None], *, record_keys: RecordReleasedKeys) -> None:
    _deliver_saved(session, datetime.now(timezone.utc).timestamp(), emit, record_keys)


def deliver_if_disabled(session: str, at: float, emit: Callable[[str], None], *,
                        record_keys: RecordReleasedKeys) -> None:
    if not _enabled(conf.read("send")):
        _deliver_saved(session, at, emit, record_keys)


def _deliver_saved(session: str, at: float, emit: Callable[[str], None], record_keys: RecordReleasedKeys, *,
                   end_now: bool = False) -> None:
    count = _count_file(session)
    path = _state_file(count)
    notice = "No review hold is open."
    if not (path.exists() or (end_now and count.exists())):
        if end_now:
            emit(notice)
        return
    with ExitStack() as delivery:
        held: tuple[ReviewMessage, ...] = ()
        with _locked(path):
            state = _read_state(count, path)
            has_messages = bool(state.messages or state.reserved)
            if not end_now and not has_messages:
                return
            hold_open = _hold_open(count, state, at, conf.read("send"))
            if hold_open and not end_now:
                return
            if has_messages or hold_open:
                held = _reserve(path, _end_review(path, state), delivery)
                notice = "A release of held messages is in progress." if has_messages else "No messages were held."
        if held:
            _emit_saved(count, path, held, at, emit, record_keys)
        elif end_now:
            emit(notice)


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
            to_file: tuple[ReviewMessage, ...] = ()
            new_message: ReviewMessage | NoNewMessage = NoNewMessage.OUTSIDE_THE_BATCH
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
                                to_file = held
                            elif urgent:
                                to_file = tuple(item for item in held if not message.key or item.key != message.key)
                                new_message = message
                            else:
                                to_file = held
                                if not (message.key and any(item.key == message.key for item in held)):
                                    new_message = message
            if held:
                with _settle_release(count, path, ReleasedMessages(to_file, held, new_message), record_keys) as batch:
                    yield batch
                    return
    yield ReleasedMessages(())


def main(argv: list[str] | None = None) -> int:
    from scripts.hooks import review_hold_delivery

    parser = argparse.ArgumentParser(description="End the current review hold and release its messages.")
    _ = parser.add_argument("command", choices=("end-now",))
    _ = parser.parse_args(argv)
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session:
        parser.error("CLAUDE_CODE_SESSION_ID is required")
    try:
        _deliver_saved(session, time(), lambda notice: print(notice, flush=True),
                       review_hold_delivery.record_keys, end_now=True)
    except (OSError, ValueError) as error:
        print(f"review hold: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
