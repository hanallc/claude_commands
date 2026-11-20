"""Keep one Later time for each Review item across all five stores."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from scripts.lib import atomic_file, file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.production.record_store import state_root


def path() -> Path:
    return state_root() / 'review-later.json'


def decode_times(content: str) -> dict[str, object]:
    stored = cast(object, json.loads(content))
    if not isinstance(stored, dict):
        raise ValueError('Expected an object of Later times.')
    return cast(dict[str, object], stored)


def stored_times(target: Path) -> dict[str, object]:
    try:
        return decode_times(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError('A Later time must be text.')
    return datetime.fromisoformat(value)


def times(sources: list[Path] | None = None, *,
          skipped: Callable[[Path, str, Exception], None] | None = None) -> dict[str, datetime]:
    target = path()
    if sources is not None:
        sources.append(target)
    valid: dict[str, datetime] = {}
    zone = user_zone()
    for key, value in stored_times(target).items():
        try:
            until = _parse_time(value)
            valid[key] = until if until.tzinfo is not None else until.replace(tzinfo=zone)
        except (TypeError, ValueError) as error:
            if skipped is not None:
                skipped(target, key, error)
            else:
                logging.getLogger(__name__).warning('Review source %s (%s): %s', target, key, error)
    return valid


@dataclass(frozen=True)
class KnownLaterTimes:
    values: dict[str, datetime]


@dataclass(frozen=True)
class UnreadableLaterTimes:
    path: Path
    error: OSError | ValueError | RecursionError


LaterTimesRead = KnownLaterTimes | UnreadableLaterTimes


def read_times(sources: list[Path] | None = None, *,
               skipped: Callable[[Path, str, Exception], None] | None = None) -> LaterTimesRead:
    """Read Later times for display."""
    try:
        return KnownLaterTimes(times(sources, skipped=skipped))
    except (OSError, ValueError, RecursionError) as error:
        return UnreadableLaterTimes(path(), error)


@dataclass(frozen=True)
class MissingLaterFile:
    """No deadline file existed before the save."""


class LaterSaveRefused(ValueError):
    """The save cannot read the previous Later file."""

    def __init__(self) -> None:
        super().__init__('The Later file cannot be read. Nothing was saved.')


def _read_for_save(target: Path) -> tuple[bytes | MissingLaterFile, dict[str, object]]:
    try:
        previous = target.read_bytes()
        return previous, decode_times(previous.decode('utf-8'))
    except FileNotFoundError:
        return MissingLaterFile(), {}
    except (OSError, ValueError, RecursionError) as error:
        raise LaterSaveRefused() from error


_page_write_checked = ContextVar('page_write_checked', default=False)


@contextmanager
def page_write() -> Generator[None]:
    """Check the Later file before the first write in this page action."""
    if _page_write_checked.get():
        yield
        return
    _, stored = _read_for_save(path())
    try:
        for value in stored.values():
            _ = _parse_time(value)
    except (TypeError, ValueError) as error:
        raise LaterSaveRefused() from error
    token = _page_write_checked.set(True)
    try:
        yield
    finally:
        _page_write_checked.reset(token)


def save(key: str, until: datetime, *, record_log: Callable[[Callable[[], None]], None] | None = None) -> None:
    """Let the log writer publish the deadline after its revision check."""
    target = path()
    with ExitStack() as lock:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = lock.enter_context(file_lock.exclusive(target.with_suffix('.lock')))
        except OSError:
            _ = _read_for_save(target)
            raise
        previous, stored = _read_for_save(target)
        stored[key] = until.isoformat()
        published = False
        confirmed = False

        def publish() -> None:
            nonlocal published, confirmed
            try:
                write_text_atomic(target, json.dumps(stored)+'\n')
            except atomic_file.AtomicDurabilityUnconfirmed:
                published = True
                raise
            published = True
            confirmed = True

        try:
            try:
                if record_log is None:
                    publish()
                else:
                    record_log(publish)
            except atomic_file.AtomicDurabilityUnconfirmed as error:
                if not confirmed:
                    raise
                # The log writer only reports this error if its replacement stays visible.
                logging.getLogger(__name__).warning('Review Later log durability %s: %s', key, error)
        except BaseException:
            if published:
                if isinstance(previous, MissingLaterFile):
                    target.unlink()
                    atomic_file.sync_folder(target.parent)
                else:
                    atomic_file.write_bytes_atomic(target, previous)
            raise


def deadline() -> str:
    now = datetime.now().astimezone(user_zone())
    future = [value for value in times().values() if value > now]
    return str(int(min(future).timestamp())) if future else ''
